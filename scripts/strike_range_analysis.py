"""
Strike Range Analysis — principled post-trade prediction calibration.

Every structure encodes an implicit price-at-expiry prediction.
This module makes those predictions explicit, tests them against realized outcomes,
and computes calibration error (predicted probability vs realized frequency).

Prediction types by structure:
  IC          → containment:     price stays between short_put and short_call
  CDS / PDS   → breakeven_hit:  price crosses the breakeven (any profit)
                 target_hit:     price reaches the short strike (max profit)
  Strangle    → big_move:        price exceeds either breakeven
  CCS / PPS   → credit_otm:     short strike expires OTM

Delta ≠ probability. Delta is used as a rough heuristic benchmark only.
The primary calibration source is the BS-derived delta-implied POP.
The gold standard is ML pop_score (stored for new trades, QW5 onwards).

Output: a first-class prediction dataset ready for structure, DTE, IV, and
delta-bucket calibration analysis.
"""

from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Optional

_TRADES_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_trades.json"

# ── Black-Scholes helpers ─────────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_delta(S: float, K: float, T: float, sigma: float, call: bool = True) -> Optional[float]:
    """Black-Scholes delta (r=0 approximation; good for short DTE options)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
        return _ncdf(d1) if call else _ncdf(d1) - 1.0
    except (ValueError, ZeroDivisionError):
        return None


def _leg_iv(leg: dict) -> Optional[float]:
    """Extract annualised IV from a stored leg dict (iv_raw preferred, else iv/100)."""
    if leg.get("iv_raw") is not None:
        return float(leg["iv_raw"])
    if leg.get("iv") is not None:
        v = float(leg["iv"])
        return v / 100.0 if v > 1.0 else v
    return None


def _expected_move_pct(spot: float, iv: float, dte: int) -> Optional[float]:
    """Standard 1-sigma expected move as % of spot: IV × √(DTE/365)."""
    if spot <= 0 or iv <= 0 or dte <= 0:
        return None
    return round(iv * math.sqrt(dte / 365.0) * 100.0, 2)


# ── Per-leg delta computation ─────────────────────────────────────────────────

def _delta_from_leg(leg: dict, spot: float, dte: int, call: bool) -> Optional[float]:
    iv     = _leg_iv(leg)
    strike = leg.get("strike")
    if iv is None or strike is None or spot is None or dte is None:
        return None
    return _bs_delta(spot, strike, dte / 365.0, iv, call=call)


# ── Prediction record builders ────────────────────────────────────────────────

def _common(t: dict) -> dict:
    """Fields shared across all prediction types."""
    spot = t.get("spot_at_entry")
    dte  = t.get("dte_at_entry") or 0
    legs = t.get("entry_legs") or {}

    # ATM IV proxy: average of all stored leg IVs
    ivs = [_leg_iv(leg) for leg in legs.values() if isinstance(leg, dict)]
    ivs = [v for v in ivs if v is not None]
    atm_iv = sum(ivs) / len(ivs) if ivs else None

    em = _expected_move_pct(spot, atm_iv, dte) if spot and atm_iv else None

    ms = t.get("ml_scores_at_entry") or {}
    return {
        "ticker":          t["ticker"],
        "structure":       t["structure"],
        "entry_date":      (t.get("entered_at") or "")[:10],
        "expiry_date":     t.get("expiry"),
        "dte_at_entry":    dte,
        "spot_at_entry":   spot,
        "actual_expiry_price": round(t["exit"]["ul_price"], 4),
        "entry_credit":    t.get("entry_credit"),
        "max_profit":      t.get("max_profit"),
        "max_loss":        t.get("max_loss"),
        "realized_pnl":    t["exit"].get("pnl_total"),
        "pnl_pct_of_max":  t["exit"].get("pnl_pct_of_max"),
        "win":             t["exit"].get("win"),
        "days_held":       t["exit"].get("days_held"),
        "exit_reason":     t["exit"].get("reason"),
        "atm_iv":          round(atm_iv * 100, 2) if atm_iv else None,
        "expected_move_pct": em,
        "signal_rating":   t.get("signal_rating"),
        "iv_edge_label":   t.get("iv_edge_label"),
        # ML POP — stored only for newer trades (QW5 onwards)
        "ml_pop":          ms.get("pop_score"),
    }


def _build_ic_record(t: dict) -> Optional[dict]:
    sk   = t["strikes"]
    legs = t.get("entry_legs") or {}
    ps_k = sk.get("put_short")
    cs_k = sk.get("call_short")
    if ps_k is None or cs_k is None:
        return None

    spot = t.get("spot_at_entry")
    dte  = t.get("dte_at_entry") or 0
    ul   = t["exit"]["ul_price"]

    # BS delta for each short leg
    ps_leg = legs.get("put_short") or {}
    cs_leg = legs.get("call_short") or {}
    d_put  = _delta_from_leg(ps_leg, spot, dte, call=False)   # negative
    d_call = _delta_from_leg(cs_leg, spot, dte, call=True)    # positive

    # Delta-implied POP = (1 - |put_delta|) × (1 - |call_delta|)
    pop_delta = None
    if d_put is not None and d_call is not None:
        pop_delta = round((1.0 - abs(d_put)) * (1.0 - abs(d_call)) * 100.0, 2)

    contained = ps_k <= ul <= cs_k
    breach    = None if contained else ("put_side" if ul < ps_k else "call_side")
    dist      = 0.0 if contained else round(ps_k - ul if ul < ps_k else ul - cs_k, 4)
    range_w   = round(cs_k - ps_k, 4)

    rec = _common(t)
    rec.update({
        "prediction_type":     "ic_containment",
        "lower_bound":         ps_k,
        "upper_bound":         cs_k,
        "range_width":         range_w,
        "short_put_strike":    ps_k,
        "short_call_strike":   cs_k,
        "short_put_delta":     round(d_put, 4) if d_put is not None else None,
        "short_call_delta":    round(d_call, 4) if d_call is not None else None,
        "delta_implied_pop":   pop_delta,
        # Outcomes
        "outcome_contained":   contained,
        "outcome_binary":      1 if contained else 0,
        "breach_side":         breach,
        "distance_outside":    dist,
        "zone":                "contained" if contained else f"breached_{breach}",
        # Calibration helpers
        "predicted_pop":       pop_delta,          # primary prediction (delta-derived)
    })
    return rec


def _build_cds_record(t: dict) -> Optional[dict]:
    sk   = t["strikes"]
    legs = t.get("entry_legs") or {}
    lo_k = sk.get("long")    # long call (lower strike)
    hi_k = sk.get("short")   # short call (higher strike)
    if lo_k is None or hi_k is None:
        return None

    spot = t.get("spot_at_entry")
    dte  = t.get("dte_at_entry") or 0
    ec   = t["entry_credit"]   # debit paid
    ul   = t["exit"]["ul_price"]

    breakeven = round(lo_k + ec, 4)

    # BS delta
    lo_leg = legs.get("long") or {}
    hi_leg = legs.get("short") or {}
    d_long  = _delta_from_leg(lo_leg, spot, dte, call=True)
    d_short = _delta_from_leg(hi_leg, spot, dte, call=True)

    # Delta-implied probabilities (heuristic, not risk-neutral probability)
    # P(price > long_strike)  ≈ long_delta
    # P(price > short_strike) ≈ short_delta
    # P(price > breakeven)    ≈ interpolated from the two deltas
    pop_breakeven_delta = None
    if d_long is not None and d_short is not None and hi_k != lo_k:
        frac = (breakeven - lo_k) / (hi_k - lo_k)
        pop_breakeven_delta = round((d_long - frac * (d_long - d_short)) * 100.0, 2)
        pop_breakeven_delta = max(0.0, min(100.0, pop_breakeven_delta))

    breakeven_hit = ul >= breakeven
    target_hit    = ul >= hi_k

    zone = ("full_win" if target_hit
            else "partial_win" if breakeven_hit
            else "loss")

    rec = _common(t)
    rec.update({
        "prediction_type":         "debit_breakeven",
        "long_strike":             lo_k,
        "short_strike":            hi_k,
        "breakeven":               breakeven,
        "debit_paid":              ec,
        "long_delta":              round(d_long, 4) if d_long is not None else None,
        "short_delta":             round(d_short, 4) if d_short is not None else None,
        "delta_implied_pop":       pop_breakeven_delta,
        # Outcomes
        "outcome_breakeven_hit":   breakeven_hit,
        "outcome_target_hit":      target_hit,
        "outcome_binary":          1 if breakeven_hit else 0,
        "zone":                    zone,
        # How far did price travel toward the breakeven?
        "pct_move_to_breakeven":   round((ul - spot) / (breakeven - spot) * 100.0, 1)
                                   if spot and breakeven != spot else None,
        "predicted_pop":           pop_breakeven_delta,
    })
    return rec


def _build_pds_record(t: dict) -> Optional[dict]:
    sk   = t["strikes"]
    legs = t.get("entry_legs") or {}
    hi_k = sk.get("long")    # long put (higher strike)
    lo_k = sk.get("short")   # short put (lower strike)
    if lo_k is None or hi_k is None:
        return None

    spot = t.get("spot_at_entry")
    dte  = t.get("dte_at_entry") or 0
    ec   = t["entry_credit"]   # debit paid
    ul   = t["exit"]["ul_price"]

    breakeven = round(hi_k - ec, 4)

    lo_leg = legs.get("short") or {}   # short put (lower)
    hi_leg = legs.get("long")  or {}   # long put (higher)
    d_long  = _delta_from_leg(hi_leg, spot, dte, call=False)   # more negative
    d_short = _delta_from_leg(lo_leg, spot, dte, call=False)   # less negative

    # P(price < breakeven) ≈ interpolated between |d_long| and |d_short|
    pop_breakeven_delta = None
    if d_long is not None and d_short is not None and hi_k != lo_k:
        frac = (hi_k - breakeven) / (hi_k - lo_k)
        interp = abs(d_long) + frac * (abs(d_short) - abs(d_long))
        pop_breakeven_delta = round(interp * 100.0, 2)
        pop_breakeven_delta = max(0.0, min(100.0, pop_breakeven_delta))

    breakeven_hit = ul <= breakeven
    target_hit    = ul <= lo_k

    zone = ("full_win" if target_hit
            else "partial_win" if breakeven_hit
            else "loss")

    rec = _common(t)
    rec.update({
        "prediction_type":         "debit_breakeven",
        "long_strike":             hi_k,
        "short_strike":            lo_k,
        "breakeven":               breakeven,
        "debit_paid":              ec,
        "long_delta":              round(d_long, 4) if d_long is not None else None,
        "short_delta":             round(d_short, 4) if d_short is not None else None,
        "delta_implied_pop":       pop_breakeven_delta,
        "outcome_breakeven_hit":   breakeven_hit,
        "outcome_target_hit":      target_hit,
        "outcome_binary":          1 if breakeven_hit else 0,
        "zone":                    zone,
        "pct_move_to_breakeven":   round((spot - ul) / (spot - breakeven) * 100.0, 1)
                                   if spot and breakeven != spot else None,
        "predicted_pop":           pop_breakeven_delta,
    })
    return rec


def _build_strangle_record(t: dict) -> Optional[dict]:
    sk   = t["strikes"]
    legs = t.get("entry_legs") or {}
    put_k  = sk.get("short")   # put leg (lower strike)
    call_k = sk.get("long")    # call leg (higher strike)
    if put_k is None or call_k is None:
        return None

    spot = t.get("spot_at_entry")
    dte  = t.get("dte_at_entry") or 0
    ec   = t["entry_credit"]   # total debit
    ul   = t["exit"]["ul_price"]

    be_lo = round(put_k  - ec, 4)
    be_hi = round(call_k + ec, 4)

    # Strangle stores legs as "put"/"call", not "short"/"long"
    put_leg  = legs.get("put")  or legs.get("short") or {}
    call_leg = legs.get("call") or legs.get("long")  or {}
    d_put  = _delta_from_leg(put_leg,  spot, dte, call=False)
    d_call = _delta_from_leg(call_leg, spot, dte, call=True)

    # P(big move) ≈ P(price < put_strike) + P(price > call_strike) ≈ |d_put| + d_call
    # This is delta-implied tail probability, not the breakeven-adjusted probability
    pop_bigmove_delta = None
    if d_put is not None and d_call is not None:
        pop_bigmove_delta = round((abs(d_put) + d_call) * 100.0, 2)

    win_put  = ul <= be_lo
    win_call = ul >= be_hi
    zone     = ("win_put_side" if win_put
                else "win_call_side" if win_call
                else "loss_range")

    rec = _common(t)
    rec.update({
        "prediction_type":     "strangle_bigmove",
        "put_strike":          put_k,
        "call_strike":         call_k,
        "breakeven_lo":        be_lo,
        "breakeven_hi":        be_hi,
        "debit_paid":          ec,
        "put_delta":           round(d_put, 4) if d_put is not None else None,
        "call_delta":          round(d_call, 4) if d_call is not None else None,
        "delta_implied_pop":   pop_bigmove_delta,
        "outcome_win_put":     win_put,
        "outcome_win_call":    win_call,
        "outcome_binary":      1 if (win_put or win_call) else 0,
        "zone":                zone,
        "predicted_pop":       pop_bigmove_delta,
    })
    return rec


def _build_credit_spread_record(t: dict) -> Optional[dict]:
    sk   = t["strikes"]
    legs = t.get("entry_legs") or {}
    sh_k = sk.get("short")
    lo_k = sk.get("long")
    if sh_k is None or lo_k is None:
        return None

    is_call = t["structure"] == "Call Credit Spread"
    spot    = t.get("spot_at_entry")
    dte     = t.get("dte_at_entry") or 0
    ec      = t["entry_credit"]
    ul      = t["exit"]["ul_price"]

    sh_leg = legs.get("short") or {}
    d_short = _delta_from_leg(sh_leg, spot, dte, call=is_call)

    # P(short expires OTM) ≈ 1 - |d_short|
    pop_delta = None
    if d_short is not None:
        pop_delta = round((1.0 - abs(d_short)) * 100.0, 2)

    short_otm = (ul < sh_k) if is_call else (ul > sh_k)
    zone      = "full_win" if short_otm else "loss"

    rec = _common(t)
    rec.update({
        "prediction_type":  "credit_otm",
        "short_strike":     sh_k,
        "long_strike":      lo_k,
        "short_delta":      round(d_short, 4) if d_short is not None else None,
        "delta_implied_pop":pop_delta,
        "outcome_short_otm":short_otm,
        "outcome_binary":   1 if short_otm else 0,
        "zone":             zone,
        "predicted_pop":    pop_delta,
    })
    return rec


def _build_cc_csp_record(t: dict) -> Optional[dict]:
    sk   = t["strikes"]
    legs = t.get("entry_legs") or {}
    sh_k = sk.get("short")
    if sh_k is None:
        return None

    is_cc = t["structure"] == "Covered Call"
    spot  = t.get("spot_at_entry")
    dte   = t.get("dte_at_entry") or 0
    ec    = t["entry_credit"]
    ul    = t["exit"]["ul_price"]

    sh_leg  = legs.get("short") or {}
    d_short = _delta_from_leg(sh_leg, spot, dte, call=is_cc)

    pop_delta = None
    if d_short is not None:
        pop_delta = round((1.0 - abs(d_short)) * 100.0, 2)

    short_otm = (ul < sh_k) if is_cc else (ul > sh_k)

    rec = _common(t)
    rec.update({
        "prediction_type":   "credit_otm",
        "short_strike":      sh_k,
        "short_delta":       round(d_short, 4) if d_short is not None else None,
        "delta_implied_pop": pop_delta,
        "outcome_short_otm": short_otm,
        "outcome_binary":    1 if short_otm else 0,
        "zone":              "expired_otm" if short_otm else "assigned",
        "predicted_pop":     pop_delta,
    })
    return rec


_BUILDERS = {
    "Iron Condor":        _build_ic_record,
    "Call Debit Spread":  _build_cds_record,
    "Put Debit Spread":   _build_pds_record,
    "Long Strangle":      _build_strangle_record,
    "Call Credit Spread": _build_credit_spread_record,
    "Put Credit Spread":  _build_credit_spread_record,
    "Covered Call":       _build_cc_csp_record,
    "Cash Secured Put":   _build_cc_csp_record,
    # Calendar/Diagonal excluded — path-dependent, different validation framework
}


# ── Calibration analysis ──────────────────────────────────────────────────────

_POP_BUCKETS = [
    (0,  50,  "0–50%"),
    (50, 60,  "50–60%"),
    (60, 70,  "60–70%"),
    (70, 80,  "70–80%"),
    (80, 85,  "80–85%"),
    (85, 90,  "85–90%"),
    (90, 95,  "90–95%"),
    (95, 101, "95%+"),
]

_DTE_BUCKETS = [
    (0,  7,   "0–7 DTE"),
    (7,  15,  "7–15 DTE"),
    (15, 30,  "15–30 DTE"),
    (30, 999, "30+ DTE"),
]


def _bucket_label(value: float, buckets: list) -> str:
    for lo, hi, label in buckets:
        if lo <= value < hi:
            return label
    return buckets[-1][2]


def _calibration_table(records: list[dict], group_by: str = "pop_bucket") -> list[dict]:
    """
    Group records by predicted_pop bucket and compute:
      n, mean_predicted_pop, realized_rate, calibration_error
    """
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)

    for r in records:
        pop = r.get("predicted_pop")
        if pop is None:
            continue
        if group_by == "pop_bucket":
            key = _bucket_label(pop, _POP_BUCKETS)
        elif group_by == "dte_bucket":
            key = _bucket_label(r.get("dte_at_entry") or 0, _DTE_BUCKETS)
        else:
            key = str(r.get(group_by, "unknown"))
        groups[key].append(r)

    rows = []
    for label, recs in groups.items():
        n          = len(recs)
        pops       = [r["predicted_pop"] for r in recs if r.get("predicted_pop") is not None]
        outcomes   = [r["outcome_binary"] for r in recs]
        mean_pred  = round(sum(pops) / len(pops), 1) if pops else None
        realized   = round(sum(outcomes) / n * 100.0, 1) if n else None
        cal_error  = round(realized - mean_pred, 1) if (mean_pred is not None and realized is not None) else None
        rows.append({
            "bucket":          label,
            "n":               n,
            "mean_predicted":  mean_pred,
            "realized_pct":    realized,
            "calibration_error": cal_error,
        })

    # Sort by mean_predicted ascending
    rows.sort(key=lambda r: r.get("mean_predicted") or 0)
    return rows


def _zone_summary(records: list[dict]) -> dict:
    """Aggregate zone counts for a structure's records."""
    n     = len(records)
    zones: dict[str, int] = {}
    for r in records:
        z = r.get("zone", "unknown")
        zones[z] = zones.get(z, 0) + 1

    return {
        "n": n,
        "zones": {k: {"count": v, "pct": round(v / n * 100.0, 1)} for k, v in zones.items()},
        "overall_win_pct":    round(sum(r["outcome_binary"] for r in records) / n * 100.0, 1),
        "avg_pnl_pct_of_max": round(
            sum(r["pnl_pct_of_max"] for r in records if r.get("pnl_pct_of_max") is not None)
            / max(1, sum(1 for r in records if r.get("pnl_pct_of_max") is not None)), 1
        ),
        "avg_delta_implied_pop": round(
            sum(r["delta_implied_pop"] for r in records if r.get("delta_implied_pop") is not None)
            / max(1, sum(1 for r in records if r.get("delta_implied_pop") is not None)), 1
        ),
    }


def _pnl_breakdown() -> dict:
    """
    Realized P&L grouped by ticker, ticker+structure, and ticker+signal+structure —
    across ALL closed trades (unlike build_prediction_dataset(), not limited to
    structures with a calibration builder, since P&L doesn't depend on having a
    prediction model). Recomputed fresh every call from paper_trades.json, same as
    the rest of this module — no separate storage, no staleness.
    """
    trades = json.loads(_TRADES_PATH.read_text(encoding="utf-8"))
    closed = [t for t in trades if t.get("exit") and t["exit"].get("pnl_total") is not None]

    def _group(records: list[dict], key_fn) -> list[dict]:
        groups: dict[tuple, dict] = {}
        for t in records:
            key = key_fn(t)
            g = groups.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
            g["n"]   += 1
            g["pnl"] += t["exit"]["pnl_total"]
            if t["exit"]["pnl_total"] > 0:
                g["wins"] += 1
        rows = []
        for key, g in groups.items():
            rows.append({
                "key":        key if isinstance(key, str) else list(key),
                "n":          g["n"],
                "total_pnl":  round(g["pnl"], 2),
                "avg_pnl":    round(g["pnl"] / g["n"], 2),
                "win_pct":    round(g["wins"] / g["n"] * 100.0, 1),
            })
        rows.sort(key=lambda r: r["total_pnl"])
        return rows

    return {
        "total_trades": len(closed),
        "total_pnl":    round(sum(t["exit"]["pnl_total"] for t in closed), 2),
        "by_ticker":              _group(closed, lambda t: t.get("ticker") or "?"),
        "by_ticker_structure":    _group(closed, lambda t: (t.get("ticker") or "?", t.get("structure") or "?")),
        "by_ticker_signal_structure": _group(closed, lambda t: (
            t.get("ticker") or "?", t.get("signal_rating") or "(missing)", t.get("structure") or "?",
        )),
        "by_signal_rating":       _group(closed, lambda t: t.get("signal_rating") or "(missing)"),
    }


# ── Public entry point ────────────────────────────────────────────────────────

def build_prediction_dataset() -> list[dict]:
    """Return the full first-class prediction dataset, one record per closed trade."""
    trades = json.loads(_TRADES_PATH.read_text(encoding="utf-8"))
    closed = [
        t for t in trades
        if t.get("exit")
        and t["exit"].get("ul_price") is not None
        and t.get("strikes")
        and t.get("entry_credit") is not None
    ]
    records = []
    for t in closed:
        builder = _BUILDERS.get(t["structure"])
        if builder is None:
            continue
        rec = builder(t)
        if rec is not None:
            records.append(rec)
    return records


def compute_range_analysis() -> dict:
    """
    Main entry point for /api/paper-trades/range-analysis.
    Returns overall stats, per-structure summaries, calibration tables, and the
    full prediction dataset.
    """
    records = build_prediction_dataset()

    # ── Per-structure breakdown ───────────────────────────────────────────────
    by_structure: dict[str, list] = {}
    for r in records:
        by_structure.setdefault(r["structure"], []).append(r)

    structure_summaries = {}
    structure_calibration = {}
    for struct, recs in by_structure.items():
        structure_summaries[struct]    = _zone_summary(recs)
        structure_calibration[struct]  = _calibration_table(recs, group_by="pop_bucket")

    # ── Calibration across ALL structures combined ────────────────────────────
    overall_calibration  = _calibration_table(records, group_by="pop_bucket")
    dte_calibration      = _calibration_table(records, group_by="dte_bucket")
    signal_calibration   = _calibration_table(records, group_by="signal_rating")

    # ── Overall accuracy ─────────────────────────────────────────────────────
    n       = len(records)
    correct = sum(r["outcome_binary"] for r in records)
    pops    = [r["delta_implied_pop"] for r in records if r.get("delta_implied_pop") is not None]
    avg_predicted = round(sum(pops) / len(pops), 1) if pops else None
    avg_realized  = round(correct / n * 100.0, 1) if n else None
    overall_cal_error = round(avg_realized - avg_predicted, 1) if (avg_predicted and avg_realized) else None

    overall = {
        "total_trades":          n,
        "correct_pct":           avg_realized,
        "incorrect_pct":         round(100.0 - avg_realized, 1) if avg_realized else None,
        "avg_delta_implied_pop": avg_predicted,
        "overall_calibration_error": overall_cal_error,
        "structures_analysed":   list(by_structure.keys()),
        "excluded_structures":   ["Calendar Spread", "Diagonal Spread"],
    }

    return {
        "overall":               overall,
        "structure_summaries":   structure_summaries,
        "structure_calibration": structure_calibration,
        "overall_calibration":   overall_calibration,
        "dte_calibration":       dte_calibration,
        "signal_calibration":    signal_calibration,
        "records":               records,
        "pnl_breakdown":         _pnl_breakdown(),
    }


if __name__ == "__main__":
    import json as _j
    result = compute_range_analysis()
    print("=== OVERALL ===")
    print(_j.dumps(result["overall"], indent=2))
    print()
    print("=== STRUCTURE SUMMARIES ===")
    print(_j.dumps(result["structure_summaries"], indent=2))
    print()
    print("=== OVERALL CALIBRATION (POP buckets) ===")
    print(_j.dumps(result["overall_calibration"], indent=2))
    print()
    print("=== DTE CALIBRATION ===")
    print(_j.dumps(result["dte_calibration"], indent=2))
    print()
    print("=== CALIBRATION BY STRUCTURE ===")
    for struct, cal in result["structure_calibration"].items():
        print(f"  {struct}:")
        for row in cal:
            print(f"    {row}")
