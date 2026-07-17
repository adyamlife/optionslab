"""
E*TRADE Transaction History Importer
=====================================
Parses DownloadTxnHistory.csv (2-year account history), groups individual
option legs into complete strategies, reconstructs market-context features at
each entry date using the existing regime_backfill pipeline, and writes a
labeled dataset to data/etrade_labeled_trades.jsonl.

The output format mirrors the training_data_collector snapshot schema so it can
be fed directly into train_pop_model.py when ready (not wired up yet — run
standalone and review first).

Run:
    python -m scripts.etrade_history_importer
    python -m scripts.etrade_history_importer --csv data/DownloadTxnHistory.csv
    python -m scripts.etrade_history_importer --summary        # stats only, no file write
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT       = Path(__file__).resolve().parent.parent
_DEFAULT_CSV = _ROOT / "data" / "DownloadTxnHistory.csv"
_OUT_PATH    = _ROOT / "data" / "etrade_labeled_trades.jsonl"

# ── Activity type buckets ─────────────────────────────────────────────────────

_OPEN_TYPES  = {"Bought To Open", "Sold Short"}
_CLOSE_TYPES = {"Bought To Cover", "Sold To Close",
                "Option Expired", "Option Assigned", "Option Exercised"}
_OPTION_TYPES = _OPEN_TYPES | _CLOSE_TYPES

# ── Option description parser: "CALL INTC   07/17/26   115.000" ──────────────

_DESC_RE = re.compile(
    r"^(CALL|PUT)\s+(\S+)\s+(\d{2}/\d{2}/\d{2})\s+([\d.]+)"
)

def _parse_desc(desc: str) -> dict | None:
    m = _DESC_RE.match(desc.strip())
    if not m:
        return None
    mm, dd, yy = m.group(3).split("/")
    expiry_iso = f"20{yy}-{mm}-{dd}"
    return {
        "opt_type": m.group(1),   # CALL / PUT
        "ticker":   m.group(2),
        "expiry":   expiry_iso,
        "strike":   float(m.group(4)),
    }


def _parse_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# ── CSV loader ────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list[dict]:
    """
    Read the E*TRADE CSV, skip the header boilerplate, return parsed option legs.
    Non-option rows (dividends, transfers, stock buys/sells) are filtered out.
    """
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    # Find the real header row
    header_idx = next(
        (i for i, l in enumerate(lines) if "Activity/Trade Date" in l), None
    )
    if header_idx is None:
        raise ValueError("Could not find header row in CSV")

    reader = csv.DictReader(lines[header_idx:])
    legs: list[dict] = []

    for raw in reader:
        if not raw or not (raw.get("Activity/Trade Date") or "").strip():
            continue

        act = (raw.get("Activity Type") or "").strip()
        if act not in _OPTION_TYPES:
            continue

        parsed = _parse_desc(raw.get("Description") or "")
        if not parsed:
            continue

        trade_date = _parse_date(raw.get("Activity/Trade Date") or "")
        if trade_date is None:
            continue

        qty = raw.get("Quantity #", "0").strip()
        try:
            qty = float(qty)
        except ValueError:
            qty = 0.0

        try:
            amount = float((raw.get("Amount $") or "0").strip().replace(",", "") or 0)
        except ValueError:
            amount = 0.0

        try:
            commission = abs(float((raw.get("Commission") or "0").strip() or 0))
        except ValueError:
            commission = 0.0

        legs.append({
            "trade_date":  trade_date,
            "act_type":    act,
            "ticker":      parsed["ticker"],
            "expiry":      parsed["expiry"],
            "strike":      parsed["strike"],
            "opt_type":    parsed["opt_type"],
            "qty":         qty,
            "amount":      amount,        # positive = credit received; negative = debit paid
            "commission":  commission,
            "is_open":     act in _OPEN_TYPES,
            "is_close":    act in _CLOSE_TYPES,
        })

    return legs


# ── Strategy grouper ──────────────────────────────────────────────────────────

def _identify_structure(open_legs: list[dict]) -> str:
    """
    Infer the strategy name from the opening leg set.
    Uses qty sign (negative = short, positive = long) and opt_type.
    """
    if not open_legs:
        return "Unknown"

    calls = [l for l in open_legs if l["opt_type"] == "CALL"]
    puts  = [l for l in open_legs if l["opt_type"] == "PUT"]
    n     = len(open_legs)

    # Determine direction per leg: qty > 0 → long, qty < 0 → short
    long_calls  = [l for l in calls if l["qty"] > 0]
    short_calls = [l for l in calls if l["qty"] < 0]
    long_puts   = [l for l in puts  if l["qty"] > 0]
    short_puts  = [l for l in puts  if l["qty"] < 0]

    if n == 1:
        leg = open_legs[0]
        if leg["opt_type"] == "CALL":
            return "Short Call" if leg["qty"] < 0 else "Long Call"
        else:
            return "Short Put" if leg["qty"] < 0 else "Long Put"

    if n == 2:
        if calls and not puts:
            if long_calls and short_calls:
                lo = min(l["strike"] for l in calls)
                hi = max(l["strike"] for l in calls)
                long_is_lo = any(l["strike"] == lo and l["qty"] > 0 for l in calls)
                if long_is_lo:
                    return "Call Debit Spread"   # buy low, sell high call
                else:
                    return "Call Credit Spread"  # sell low, buy high call
        if puts and not calls:
            if long_puts and short_puts:
                lo = min(l["strike"] for l in puts)
                hi = max(l["strike"] for l in puts)
                short_is_hi = any(l["strike"] == hi and l["qty"] < 0 for l in puts)
                if short_is_hi:
                    return "Put Credit Spread"   # sell high, buy low put
                else:
                    return "Put Debit Spread"    # buy high, sell low put
        if calls and puts:
            if long_calls and long_puts:
                return "Long Strangle"
            if short_calls and short_puts:
                return "Short Strangle"

    if n == 4:
        if long_calls and short_calls and long_puts and short_puts:
            return "Iron Condor"
        if calls and puts and len(calls) == 2 and len(puts) == 2:
            # Could be Iron Butterfly if call strikes == put strikes
            call_strikes = sorted(l["strike"] for l in calls)
            put_strikes  = sorted(l["strike"] for l in puts)
            if call_strikes[0] == put_strikes[1]:  # ATM overlap
                return "Iron Butterfly"
            return "Iron Condor"

    return f"Complex-{n}leg"


def _net_strikes(open_legs: list[dict]) -> dict:
    """Extract strike fields for the snapshot candidate dict."""
    calls = sorted(open_legs, key=lambda l: l["strike"])
    if not calls:
        return {}

    structure = _identify_structure(open_legs)
    call_legs = [l for l in open_legs if l["opt_type"] == "CALL"]
    put_legs  = [l for l in open_legs if l["opt_type"] == "PUT"]

    out: dict = {}

    if structure in ("Put Credit Spread", "Put Debit Spread"):
        short = next((l for l in put_legs if l["qty"] < 0), None)
        long  = next((l for l in put_legs if l["qty"] > 0), None)
        if short: out["short_strike"] = short["strike"]
        if long:  out["long_strike"]  = long["strike"]

    elif structure in ("Call Credit Spread", "Call Debit Spread"):
        short = next((l for l in call_legs if l["qty"] < 0), None)
        long  = next((l for l in call_legs if l["qty"] > 0), None)
        if short: out["short_strike"] = short["strike"]
        if long:  out["long_strike"]  = long["strike"]

    elif structure == "Iron Condor":
        put_short  = next((l for l in put_legs  if l["qty"] < 0), None)
        put_long   = next((l for l in put_legs  if l["qty"] > 0), None)
        call_short = next((l for l in call_legs if l["qty"] < 0), None)
        call_long  = next((l for l in call_legs if l["qty"] > 0), None)
        if put_long:   out["put_long_strike"]   = put_long["strike"]
        if put_short:  out["put_short_strike"]  = put_short["strike"]
        if call_short: out["call_short_strike"] = call_short["strike"]
        if call_long:  out["call_long_strike"]  = call_long["strike"]

    elif structure in ("Short Strangle", "Long Strangle"):
        call_l = call_legs[0] if call_legs else None
        put_l  = put_legs[0]  if put_legs  else None
        if call_l: out["call_strike"] = call_l["strike"]
        if put_l:  out["put_strike"]  = put_l["strike"]

    elif structure in ("Short Call", "Long Call"):
        out["short_strike" if "Short" in structure else "long_strike"] = call_legs[0]["strike"] if call_legs else None

    elif structure in ("Short Put", "Long Put"):
        out["short_strike" if "Short" in structure else "long_strike"] = put_legs[0]["strike"] if put_legs else None

    return out


def group_positions(legs: list[dict]) -> list[dict]:
    """
    Group legs into complete (entry+exit) positions keyed by (ticker, expiry).

    Each position has:
        ticker, expiry, entry_date, exit_date, structure,
        entry_legs, exit_legs, net_pnl, commission_total,
        is_win, max_profit, max_loss, entry_credit
    """
    by_pos: dict[tuple, list] = defaultdict(list)
    for leg in legs:
        by_pos[(leg["ticker"], leg["expiry"])].append(leg)

    positions = []
    for (ticker, expiry), pos_legs in by_pos.items():
        open_legs  = sorted([l for l in pos_legs if l["is_open"]],
                            key=lambda l: l["trade_date"])
        close_legs = sorted([l for l in pos_legs if l["is_close"]],
                            key=lambda l: l["trade_date"])

        if not open_legs:
            continue

        # Skip if no close/expiry recorded — position may still be open
        has_close = bool(close_legs)
        if not has_close:
            continue

        entry_date = open_legs[0]["trade_date"]
        exit_date  = close_legs[-1]["trade_date"]

        # Detect rolls: if both open AND close legs land on the same date as
        # another position's open, the same-day close is a roll-off, not an exit.
        # Simple heuristic: if open_legs span multiple dates, treat each date
        # cluster as a separate position.  For now split by first open date.
        # (Multi-roll positions appear as Complex-Nleg — labeled and kept for review.)

        net_pnl     = sum(l["amount"] for l in pos_legs)
        comm_total  = sum(l["commission"] for l in pos_legs)
        net_after   = round(net_pnl - comm_total, 2)

        structure   = _identify_structure(open_legs)
        strikes     = _net_strikes(open_legs)

        # Entry credit = net amount from opening legs (positive = credit received)
        entry_credit = round(sum(l["amount"] for l in open_legs), 2)
        # Max profit proxy = entry credit for credit structures (abs value of open receipt)
        # Max loss proxy   = abs(net debit paid) for debit structures
        is_credit = entry_credit > 0
        max_profit = abs(entry_credit) if is_credit else None
        max_loss   = abs(entry_credit) if not is_credit else None

        # Width of spread if strikes known
        short = strikes.get("short_strike")
        long_ = strikes.get("long_strike")
        if short and long_:
            width = abs(short - long_)
            if is_credit:
                max_loss   = round(width - abs(entry_credit) / 100, 2)  # per share
            else:
                max_profit = round(width - abs(entry_credit) / 100, 2)

        dte_at_entry = (
            datetime.strptime(expiry, "%Y-%m-%d").date() - entry_date
        ).days

        positions.append({
            "ticker":          ticker,
            "expiry":          expiry,
            "entry_date":      entry_date.isoformat(),
            "exit_date":       exit_date.isoformat(),
            "structure":       structure,
            "dte_at_entry":    dte_at_entry,
            "entry_credit":    entry_credit,
            "is_credit":       is_credit,
            "max_profit":      max_profit,
            "max_loss":        max_loss,
            "net_pnl":         round(net_pnl, 2),
            "commission_total": round(comm_total, 2),
            "net_pnl_after_comm": net_after,
            "is_win":          net_after > 0,
            "strikes":         strikes,
            "n_open_legs":     len(open_legs),
            "n_close_legs":    len(close_legs),
            "has_assignment":  any(l["act_type"] == "Option Assigned"  for l in close_legs),
            "has_exercise":    any(l["act_type"] == "Option Exercised"  for l in close_legs),
            "expired_worthless": all(l["act_type"] == "Option Expired" for l in close_legs),
        })

    return sorted(positions, key=lambda p: p["entry_date"])


# ── Market-context feature reconstruction ────────────────────────────────────

def _reconstruct_features(ticker: str, entry_date_str: str) -> dict:
    """
    Reconstruct market-context features for a ticker on the entry date.
    Builds features via regime_backfill indicators but without the
    forward-label dropna filter (we only need market context, not labels).
    """
    try:
        import numpy as np
        import pandas as pd
        import yfinance as yf
        from scripts import regime_backfill as _rb

        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()

        hist = yf.Ticker(ticker).history(period="2y")
        if hist.empty or len(hist) < 30:
            return {}

        close, high, low = hist["Close"], hist["High"], hist["Low"]
        dates = [d.date() for d in hist.index]

        # Find the index position at or before entry_date
        target_idx = None
        for i, d in enumerate(dates):
            if d <= entry_date:
                target_idx = i
        if target_idx is None or target_idx < 26:
            return {}

        # Compute indicators up to target_idx (use full series, index at position)
        rsi_s      = _rb._rsi_series(close)
        adx_s      = _rb._adx_series(high, low, close)
        hv20_s     = _rb._realized_vol_series(close, 20)
        trend_s    = _rb._trend_series(close)
        macd_s     = _rb._macd_trend_series(close)
        vix_close, spy_close, qqq_close, iwm_close = _rb._fetch_market_context("2y")
        atr_s      = _rb._atr_pct_series(high, low, close, period=14)
        iv_rank_s  = _rb._iv_rank_52w_series(hv20_s)

        vix_today      = vix_close.get(dates[target_idx])
        vix_rank_arr   = _rb._vix_rank_series(vix_close)
        beta_s         = _rb._beta_series(close, spy_close, window=60)
        spy_rsi_arr    = _rb._index_rsi_aligned(spy_close, dates)
        spy_trend_arr  = _rb._index_trend_aligned(spy_close, dates)
        qqq_rsi_arr    = _rb._index_rsi_aligned(qqq_close, dates) if qqq_close is not None else [None] * len(dates)
        qqq_trend_arr  = _rb._index_trend_aligned(qqq_close, dates) if qqq_close is not None else [None] * len(dates)
        iwm_rsi_arr    = _rb._index_rsi_aligned(iwm_close, dates) if iwm_close is not None else [None] * len(dates)
        rel_strength   = _rb._rel_strength_series(close, spy_close)

        def _val(series_or_arr, idx):
            try:
                v = series_or_arr.iloc[idx] if hasattr(series_or_arr, "iloc") else series_or_arr[idx]
                if v is None:
                    return None
                if isinstance(v, float) and np.isnan(v):
                    return None
                return float(v) if isinstance(v, (int, float, np.floating)) else v
            except Exception:
                return None

        macro  = _rb._fetch_macro_series("2y")
        date_s = pd.Series(dates)
        i      = target_idx

        result = {
            "close":           _val(close, i),
            "rsi":             _val(rsi_s, i),
            "adx":             _val(adx_s, i),
            "hv20":            _val(hv20_s, i),
            "trend":           trend_s[i] if i < len(trend_s) else None,
            "macd_trend":      macd_s[i]  if i < len(macd_s)  else None,
            "vix_close":       float(vix_today) if vix_today is not None else None,
            "vix_rank":        float(vix_rank_arr[i]) if i < len(vix_rank_arr) and not np.isnan(vix_rank_arr[i]) else None,
            "rel_strength_spy": _val(rel_strength, i),
            "beta_60d":        _val(beta_s, i),
            "atr_pct":         _val(atr_s, i),
            "iv_rank_52w":     _val(iv_rank_s, i),
            "spy_rsi":         spy_rsi_arr[i]   if i < len(spy_rsi_arr)   else None,
            "spy_trend":       spy_trend_arr[i] if i < len(spy_trend_arr) else None,
            "qqq_rsi":         qqq_rsi_arr[i]   if i < len(qqq_rsi_arr)   else None,
            "qqq_trend":       qqq_trend_arr[i] if i < len(qqq_trend_arr) else None,
            "iwm_rsi":         iwm_rsi_arr[i]   if i < len(iwm_rsi_arr)   else None,
            "yield_10y":       float(macro["yield_10y"].get(dates[i])) if macro and macro.get("yield_10y") is not None and macro["yield_10y"].get(dates[i]) is not None else None,
            "yield_3m":        float(macro["yield_3m"].get(dates[i]))  if macro and macro.get("yield_3m")  is not None and macro["yield_3m"].get(dates[i])  is not None else None,
            "yield_curve":     float(macro["yield_curve"].get(dates[i])) if macro and macro.get("yield_curve") is not None and macro["yield_curve"].get(dates[i]) is not None else None,
        }

        # Sector context
        sector_etf_sym = _rb._sector_etf_for(ticker)
        result["sector_etf"] = sector_etf_sym
        if sector_etf_sym:
            try:
                sec_hist  = yf.Ticker(sector_etf_sym).history(period="2y")
                sec_close = sec_hist["Close"]
                sec_close.index = sec_close.index.date
                result["sector_trend"] = (_rb._index_trend_aligned(sec_close, dates) or [None] * len(dates))[i]
                result["sector_rsi"]   = (_rb._index_rsi_aligned(sec_close,   dates) or [None] * len(dates))[i]
            except Exception:
                pass

        return result

    except Exception as e:
        log.debug("Feature reconstruction failed for %s %s: %s", ticker, entry_date_str, e)
        return {}


# ── Snapshot builder ──────────────────────────────────────────────────────────

def build_snapshot(pos: dict) -> dict:
    """
    Convert a grouped position into a dict that mirrors the training_data_collector
    snapshot schema.  Fields not derivable from the CSV are set to None —
    the POP model handles missing values via XGBoost's native NaN support.
    """
    features = _reconstruct_features(pos["ticker"], pos["entry_date"])

    candidate = {
        "structure":    pos["structure"],
        "is_credit":    pos["is_credit"],
        "max_profit":   pos["max_profit"],
        "max_loss":     pos["max_loss"],
        "dte":          pos["dte_at_entry"],
        "pop":          None,   # not available from transaction history
        "ev":           None,
        "net_delta":    None,
        "net_theta":    None,
        "net_gamma":    None,
        "net_vega":     None,
        "capital_required": None,
        **pos["strikes"],
    }

    snapshot = {
        # Identity
        "source":       "etrade_history",
        "ticker":       pos["ticker"],
        "expiry":       pos["expiry"],
        "collected_at": pos["entry_date"] + "T09:30:00",   # approximate market open

        # Market features (from regime_backfill reconstruction)
        "spot":                 features.get("close"),
        "rsi":                  features.get("rsi"),
        "adx":                  features.get("adx"),
        "atm_iv":               features.get("atm_iv"),
        "iv_rank_proxy":        features.get("iv_rank_52w"),   # iv_rank_52w = HV20-based percentile
        "hv20":                 features.get("hv20"),
        "pcr":                  None,   # not reconstructable historically
        "vix":                  features.get("vix_close"),
        "earnings_days_away":   None,
        "signal_score":         features.get("signal_score"),
        "iv_env":               features.get("iv_env"),
        "trend":                features.get("trend"),
        "weekly_trend":         features.get("weekly_trend"),
        "regime":               features.get("regime_label"),
        "macd_trend":           features.get("macd_trend"),

        # Price-derived fields — reconstructable from build_ticker_features()
        "vol_oi_ratio":         None,   # options chain only
        "iv_skew":              None,   # options chain only
        "iv_term_slope":        None,   # options chain only
        "otm_pcr":              None,   # options chain only
        "beta_60d":             features.get("beta_60d"),
        "atr_pct":              features.get("atr_pct"),
        "iv_rank_52w":          features.get("iv_rank_52w"),
        "sector_etf":           features.get("sector_etf"),
        "sector_trend":         features.get("sector_trend"),
        "sector_rsi":           features.get("sector_rsi"),
        "sector_iv_ratio":      None,   # options chain only
        "spy_rsi":              features.get("spy_rsi"),
        "qqq_rsi":              features.get("qqq_rsi"),
        "iwm_rsi":              features.get("iwm_rsi"),
        "vvix":                 features.get("vvix"),
        "vix_3m":               features.get("vix_3m"),
        "vix_term_slope":       features.get("vix_term_slope"),
        "yield_10y":            features.get("yield_10y"),
        "yield_3m":             features.get("yield_3m"),
        "yield_curve":          features.get("yield_curve"),
        "dollar_index":         None,
        "fed_within_dte":       None,
        "cpi_within_dte":       None,
        "earnings_inside_expiry": features.get("earnings_inside_expiry"),
        "news_sentiment_score": None,
        "analyst_rec_change":   None,
        "short_interest_pct":   features.get("short_interest_pct"),
        "spy_trend":            features.get("spy_trend"),
        "qqq_trend":            features.get("qqq_trend"),
        "iwm_trend":            None,

        # Candidate
        "candidate":    candidate,

        # Label
        "labeled":      True,
        "labeled_at":   datetime.now().isoformat(),
        "outcome": {
            "win":              pos["is_win"],
            "net_pnl":          pos["net_pnl_after_comm"],
            "exit_date":        pos["exit_date"],
            "expired_worthless":pos["expired_worthless"],
            "assigned":         pos["has_assignment"],
            "exercised":        pos["has_exercise"],
        },

        # Extra provenance fields for review
        "_etrade": {
            "entry_credit":     pos["entry_credit"],
            "net_pnl_raw":      pos["net_pnl"],
            "commission_total": pos["commission_total"],
            "n_open_legs":      pos["n_open_legs"],
            "n_close_legs":     pos["n_close_legs"],
        },
    }
    return snapshot


# ── Summary printer ───────────────────────────────────────────────────────────

def _print_summary(positions: list[dict]) -> None:
    from collections import Counter

    print(f"\n{'='*60}")
    print(f"  E*TRADE History Import Summary")
    print(f"{'='*60}")
    print(f"  Total closed positions:  {len(positions)}")

    by_struct = Counter(p["structure"] for p in positions)
    wins      = sum(1 for p in positions if p["is_win"])
    losses    = len(positions) - wins
    total_pnl = sum(p["net_pnl_after_comm"] for p in positions)

    print(f"  Win / Loss:              {wins} W / {losses} L  ({wins/len(positions)*100:.1f}% win rate)")
    print(f"  Total net P&L:           ${total_pnl:+,.2f}")
    print(f"\n  By structure:")
    for struct, count in by_struct.most_common():
        w = sum(1 for p in positions if p["structure"] == struct and p["is_win"])
        print(f"    {struct:<30} {count:>3} trades   {w}/{count} wins")

    assigned  = sum(1 for p in positions if p["has_assignment"])
    exercised = sum(1 for p in positions if p["has_exercise"])
    expired   = sum(1 for p in positions if p["expired_worthless"])
    print(f"\n  Exit types:")
    print(f"    Expired worthless:     {expired}")
    print(f"    Option assigned:       {assigned}")
    print(f"    Option exercised:      {exercised}")
    print(f"    Closed early:          {len(positions) - expired - assigned - exercised}")

    date_range = (
        min(p["entry_date"] for p in positions),
        max(p["entry_date"] for p in positions),
    )
    print(f"\n  Date range:              {date_range[0]} to {date_range[1]}")

    unique_tickers = sorted(set(p["ticker"] for p in positions))
    print(f"  Unique tickers:          {len(unique_tickers)}  {unique_tickers}")
    print(f"{'='*60}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(csv_path: Path = _DEFAULT_CSV, out_path: Path = _OUT_PATH,
        summary_only: bool = False) -> list[dict]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    log.info("Loading CSV: %s", csv_path)
    legs = load_csv(csv_path)
    log.info("Parsed %d option legs", len(legs))

    positions = group_positions(legs)
    log.info("Grouped into %d closed positions", len(positions))

    _print_summary(positions)

    if summary_only:
        log.info("--summary mode: no file written")
        return positions

    # Build snapshots with reconstructed market features
    log.info("Reconstructing market features per position (yfinance calls — takes a few minutes)...")
    snapshots = []
    errors    = 0
    for i, pos in enumerate(positions, 1):
        try:
            snap = build_snapshot(pos)
            snapshots.append(snap)
            if i % 20 == 0:
                log.info("  %d / %d processed...", i, len(positions))
        except Exception as e:
            log.warning("Skipping %s %s: %s", pos["ticker"], pos["expiry"], e)
            errors += 1

    log.info("Built %d snapshots (%d errors)", len(snapshots), errors)

    # Feature reconstruction stats
    with_features = sum(1 for s in snapshots if s.get("rsi") is not None)
    log.info("Snapshots with reconstructed features: %d / %d", with_features, len(snapshots))

    # Write output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for snap in snapshots:
            f.write(json.dumps(snap, default=str) + "\n")

    log.info("Written to %s", out_path)
    log.info("")
    log.info("Next step: review the file, then run:")
    log.info("  python -m scripts.train_pop_model --extra-data data/etrade_labeled_trades.jsonl")

    return snapshots


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import E*TRADE transaction history for ML training")
    parser.add_argument("--csv",     default=str(_DEFAULT_CSV), help="Path to DownloadTxnHistory.csv")
    parser.add_argument("--out",     default=str(_OUT_PATH),    help="Output JSONL path")
    parser.add_argument("--summary", action="store_true",       help="Print stats only, do not write output")
    args = parser.parse_args()

    run(
        csv_path=Path(args.csv),
        out_path=Path(args.out),
        summary_only=args.summary,
    )
