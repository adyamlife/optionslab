"""
Phase 2B — Distribution calibration analysis (Tasks #20, #21, #22).

Three analyses, each independent:

  #20  zone_calibration()
       Predicted zone probability (mc_zone_*) vs actual zone_at_expiry frequency.
       Measures whether MC zone probabilities are well-calibrated by structure.

  #21  tail_calibration()
       Checks whether spot_at_expiry falls inside predicted price intervals
       (p10–p90 should contain ~80 pct of outcomes; p25–p75 ~50 pct).

  #22  market_implied_vs_model()
       Derives a log-normal distribution from atm_iv + spot + DTE (the
       market-implied GBM baseline) and compares its percentiles to the MC
       percentiles stored at entry. No labeling required — pure model comparison.

Run all:
  python -m scripts.distribution_calibration

Run one:
  python -m scripts.distribution_calibration zone
  python -m scripts.distribution_calibration tail
  python -m scripts.distribution_calibration implied

Requires: training_snapshots with distribution columns populated (post Phase 2A
deploy). Tasks #20/#21 also need zone_at_expiry/spot_at_expiry from labeling.
"""

import json
import math
import sys
from collections import defaultdict

import numpy as np

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MIN_N = 10   # minimum sample size before reporting a metric


def _dbload():
    """Return list of snapshot dicts with relevant columns selected."""
    from scripts.db import connect, SNAPSHOTS_TABLE, ensure_snapshot_tables
    ensure_snapshot_tables()
    cols = (
        "snapshot_id, ticker, trade_structure, dte, spot, labeled, "
        "zone_at_expiry, spot_at_expiry, "
        "mc_expiry_p10, mc_expiry_p25, mc_expiry_p50, mc_expiry_p75, mc_expiry_p90, "
        "mc_expiry_mean, distribution_model_version, "
        "mc_zone_below_long, mc_zone_between, mc_zone_above_short, mc_zone_in_profit, "
        "mc_zone_below_put_long, mc_zone_in_loss_put, mc_zone_in_loss_call, "
        "mc_zone_above_call_long, mc_zone_below_short, "
        "candidate"
    )
    with connect(read_only=True) as con:
        cur     = con.execute(f"SELECT {cols} FROM {SNAPSHOTS_TABLE}")
        headers = [d[0] for d in cur.description]
        rows    = cur.fetchall()

    result = []
    for row in rows:
        r = dict(zip(headers, row))
        if isinstance(r.get("candidate"), str):
            try:
                r["candidate"] = json.loads(r["candidate"])
            except Exception:
                r["candidate"] = {}
        result.append(r)
    return result


def _fmt(val, width=8, decimals=1):
    if val is None:
        return " " * (width - 2) + "--"
    return f"{val:>{width}.{decimals}f}"


def _pct(val, width=7):
    if val is None:
        return " " * (width - 2) + "--"
    return f"{val * 100:>{width}.1f}%"


# ---------------------------------------------------------------------------
# Task #20 — Zone calibration
# ---------------------------------------------------------------------------

# Map structure to the zone probability fields that exist for it and which
# zone label each corresponds to.
# Tuple: (zone_label, mc_field_name)
_ZONE_MAP = {
    "Iron Condor": [
        ("full_win",     "mc_zone_in_profit"),
        ("partial_win",  None),   # partial = in_loss_put + in_loss_call (derived)
        ("loss",         None),   # loss    = below_put_long + above_call_long
    ],
    "Call Debit Spread": [
        ("full_win",     "mc_zone_above_short"),
        ("partial_win",  "mc_zone_between"),
        ("loss",         "mc_zone_below_long"),
    ],
    "Put Debit Spread": [
        ("full_win",     "mc_zone_below_long"),
        ("partial_win",  "mc_zone_between"),
        ("loss",         "mc_zone_above_short"),
    ],
    "Call Credit Spread": [
        ("full_win",     "mc_zone_below_long"),
        ("partial_win",  "mc_zone_between"),
        ("loss",         "mc_zone_above_short"),
    ],
    "Put Credit Spread": [
        ("full_win",     "mc_zone_above_short"),
        ("partial_win",  "mc_zone_between"),
        ("loss",         "mc_zone_below_long"),
    ],
    "Cash Secured Put": [
        ("full_win",     "mc_zone_above_short"),
        ("loss",         "mc_zone_below_short"),
    ],
    "Covered Call": [
        ("full_win",     "mc_zone_below_short"),
        ("partial_win",  "mc_zone_above_short"),
    ],
}

# For IC: predicted partial_win = sum of wing probs; predicted loss = below_put_long + above_call_long
def _ic_zone_probs(r):
    """Return (p_full_win, p_partial_win, p_loss) for Iron Condor from stored fields."""
    fw  = r.get("mc_zone_in_profit")
    ipl = r.get("mc_zone_in_loss_put")
    icl = r.get("mc_zone_in_loss_call")
    bpl = r.get("mc_zone_below_put_long")
    acl = r.get("mc_zone_above_call_long")
    if None in (fw, ipl, icl, bpl, acl):
        return None, None, None
    return fw, ipl + icl, bpl + acl


def _zone_predicted_prob(r, zone_label):
    """Return the MC-predicted probability of zone_label for row r."""
    structure = (r.get("candidate") or {}).get("structure") or r.get("trade_structure") or ""

    if structure == "Iron Condor":
        fw, pw, lw = _ic_zone_probs(r)
        return {"full_win": fw, "partial_win": pw, "loss": lw}.get(zone_label)

    mapping = _ZONE_MAP.get(structure)
    if not mapping:
        return None
    for label, field in mapping:
        if label == zone_label and field is not None:
            return r.get(field)
    return None


def zone_calibration(rows=None):
    """
    Calibration: predicted P(zone) vs actual zone frequency.

    For each structure, bins rows by predicted probability of the actual
    observed zone into decile buckets and computes average predicted vs
    actual frequency. Also reports raw zone frequency by structure.
    """
    if rows is None:
        rows = _dbload()

    # Filter: needs zone_at_expiry + at least one mc_zone field
    eligible = [
        r for r in rows
        if r.get("zone_at_expiry") and r.get("labeled")
        and any(r.get(f) is not None for f in (
            "mc_zone_in_profit", "mc_zone_below_long", "mc_zone_between",
            "mc_zone_above_short", "mc_zone_below_short",
        ))
    ]

    print(f"\n{'='*66}")
    print("TASK #20 — Zone Calibration: predicted P(zone) vs actual rate")
    print(f"{'='*66}")
    print(f"Total labeled snapshots:  {len([r for r in rows if r.get('labeled')])}")
    print(f"With zone probabilities:  {len(eligible)}")

    if len(eligible) < _MIN_N:
        print(f"\n  [waiting] Need >= {_MIN_N} eligible rows — accumulating post-deploy data.")
        return

    # ── Per-structure zone frequency table ──────────────────────────────────
    by_structure = defaultdict(list)
    for r in eligible:
        struct = (r.get("candidate") or {}).get("structure") or r.get("structure") or "unknown"
        by_structure[struct].append(r)

    print(f"\n{'Structure':<28} {'n':>5}  {'full_win%':>9} {'part_win%':>9} {'loss%':>9}")
    print("-" * 66)

    all_zones  = defaultdict(lambda: defaultdict(int))  # structure -> zone -> count
    pred_pairs = []   # (predicted_prob, actual_bool) for calibration curve

    for struct, rs in sorted(by_structure.items(), key=lambda x: -len(x[1])):
        n = len(rs)
        zone_counts = defaultdict(int)
        for r in rs:
            zone_counts[r["zone_at_expiry"]] += 1
            all_zones[struct][r["zone_at_expiry"]] += 1
            # Collect (predicted, actual) for the observed zone
            pred = _zone_predicted_prob(r, r["zone_at_expiry"])
            if pred is not None:
                pred_pairs.append((pred, 1.0))
                # Also collect for zones that did NOT occur (predicted vs 0)
                for other_zone in ("full_win", "partial_win", "loss"):
                    if other_zone != r["zone_at_expiry"]:
                        p = _zone_predicted_prob(r, other_zone)
                        if p is not None:
                            pred_pairs.append((p, 0.0))

        fw  = zone_counts.get("full_win", 0) / n
        pw  = zone_counts.get("partial_win", 0) / n
        lw  = zone_counts.get("loss", 0) / n
        print(f"  {struct:<26} {n:>5}  {_pct(fw):>9} {_pct(pw):>9} {_pct(lw):>9}")

    # ── Calibration curve: predicted P vs actual frequency in decile bins ───
    print(f"\n{'Predicted P(zone) bin':<24} {'n_pairs':>8} {'avg_predicted':>13} {'actual_rate':>11} {'error':>8}")
    print("-" * 66)

    if len(pred_pairs) >= _MIN_N * 3:
        pred_arr   = np.array([p for p, _ in pred_pairs])
        actual_arr = np.array([a for _, a in pred_pairs])
        n_bins = min(10, len(pred_pairs) // _MIN_N)
        edges  = np.percentile(pred_arr, np.linspace(0, 100, n_bins + 1))

        for i in range(n_bins):
            lo, hi = edges[i], edges[i + 1]
            mask = (pred_arr >= lo) & (pred_arr <= hi) if i == n_bins - 1 \
                   else (pred_arr >= lo) & (pred_arr < hi)
            bucket_pred   = pred_arr[mask]
            bucket_actual = actual_arr[mask]
            if len(bucket_pred) == 0:
                continue
            avg_pred   = bucket_pred.mean()
            avg_actual = bucket_actual.mean()
            error      = avg_actual - avg_pred
            label      = f"[{lo:.2f}, {hi:.2f}]"
            print(f"  {label:<22} {len(bucket_pred):>8} {_pct(avg_pred):>13} "
                  f"{_pct(avg_actual):>11} {error:>+8.3f}")
    else:
        print(f"  [waiting] Need >= {_MIN_N * 3} zone-probability pairs for calibration curve.")

    # ── Distribution-version breakdown ──────────────────────────────────────
    version_counts = defaultdict(int)
    for r in eligible:
        version_counts[r.get("distribution_model_version") or "unknown"] += 1
    if len(version_counts) > 1:
        print("\nModel version split:")
        for v, n in sorted(version_counts.items()):
            print(f"  {v:<40} {n:>5}")


# ---------------------------------------------------------------------------
# Task #21 — Tail calibration
# ---------------------------------------------------------------------------

def tail_calibration(rows=None):
    """
    Interval coverage: does spot_at_expiry fall within [p10, p90] and [p25, p75]?

    A well-calibrated model should show ~80 pct coverage for p10–p90 and
    ~50 pct for p25–p75. Systematic under/over-coverage reveals bias.
    """
    if rows is None:
        rows = _dbload()

    eligible = [
        r for r in rows
        if r.get("labeled")
        and r.get("spot_at_expiry") is not None
        and r.get("mc_expiry_p10") is not None
        and r.get("mc_expiry_p90") is not None
    ]

    print(f"\n{'='*66}")
    print("TASK #21 — Tail Calibration: interval coverage vs expected")
    print(f"{'='*66}")
    print(f"Total labeled snapshots:  {len([r for r in rows if r.get('labeled')])}")
    print(f"With expiry + MC prices:  {len(eligible)}")

    if len(eligible) < _MIN_N:
        print(f"\n  [waiting] Need >= {_MIN_N} eligible rows — accumulating post-deploy data.")
        return

    s_arr  = np.array([r["spot_at_expiry"] for r in eligible])
    p10    = np.array([r["mc_expiry_p10"]  for r in eligible])
    p25    = np.array([r.get("mc_expiry_p25") or np.nan for r in eligible])
    p75    = np.array([r.get("mc_expiry_p75") or np.nan for r in eligible])
    p90    = np.array([r["mc_expiry_p90"]  for r in eligible])
    p50    = np.array([r.get("mc_expiry_p50") or np.nan for r in eligible])

    in_8090  = ((s_arr >= p10) & (s_arr <= p90))
    in_5075  = (~np.isnan(p25)) & (~np.isnan(p75)) & (s_arr >= p25) & (s_arr <= p75)
    valid_50 = ~np.isnan(p25)

    cov_80 = in_8090.mean()
    cov_50 = in_5075.sum() / valid_50.sum() if valid_50.sum() > 0 else None

    print(f"\n{'Interval':<18} {'Expected':>9} {'Actual':>9} {'n':>6} {'Gap':>8}")
    print("-" * 52)
    print(f"  p10 - p90        {'80.0%':>9} {_pct(cov_80):>9} {in_8090.size:>6} "
          f"{(cov_80 - 0.80):>+8.3f}")
    if cov_50 is not None:
        print(f"  p25 - p75        {'50.0%':>9} {_pct(cov_50):>9} {valid_50.sum():>6} "
              f"{(cov_50 - 0.50):>+8.3f}")

    # Median bias: MC p50 vs actual spot_at_expiry / spot_entry
    p50_valid = ~np.isnan(p50)
    if p50_valid.sum() >= _MIN_N:
        spots_entry = np.array([
            float((r.get("candidate") or {}).get("spot") or r.get("spot") or np.nan)
            for r in eligible
        ])
        mc_med_rel  = np.where(spots_entry > 0, p50 / spots_entry - 1.0, np.nan)
        act_rel     = np.where(spots_entry > 0, s_arr / spots_entry - 1.0, np.nan)
        valid_rel   = p50_valid & ~np.isnan(mc_med_rel) & ~np.isnan(act_rel)
        if valid_rel.sum() >= _MIN_N:
            avg_mc_med_rel  = mc_med_rel[valid_rel].mean()
            avg_act_rel     = act_rel[valid_rel].mean()
            bias            = avg_mc_med_rel - avg_act_rel
            print(f"\n  Median forecast bias (MC p50 vs actual, relative to spot):")
            print(f"    MC p50 avg drift  : {_pct(avg_mc_med_rel):>9}  (predicted)")
            print(f"    Actual avg drift  : {_pct(avg_act_rel):>9}  (observed)")
            print(f"    Model bias        : {bias:>+9.4f}  (+ = model overestimates drift)")

    # Per-structure breakdown
    by_struct = defaultdict(list)
    for r, in8090 in zip(eligible, in_8090):
        struct = (r.get("candidate") or {}).get("structure") or r.get("structure") or "unknown"
        by_struct[struct].append(in8090)

    if len(by_struct) > 1:
        print(f"\n{'Structure':<28} {'n':>5} {'p10-p90 cov':>12} {'gap':>8}")
        print("-" * 56)
        for struct, hits in sorted(by_struct.items(), key=lambda x: -len(x[1])):
            n   = len(hits)
            cov = sum(hits) / n
            print(f"  {struct:<26} {n:>5} {_pct(cov):>12} {(cov - 0.80):>+8.3f}")

    # Left/right tail asymmetry
    below_p10 = (s_arr < p10).mean()
    above_p90 = (s_arr > p90).mean()
    print(f"\n  Tail asymmetry (should be ~10% each for p10/p90):")
    print(f"    Below p10 : {_pct(below_p10):>8}  (expected ~10%)")
    print(f"    Above p90 : {_pct(above_p90):>8}  (expected ~10%)")
    skew = above_p90 - below_p10
    if abs(skew) > 0.05:
        direction = "right (upside)" if skew > 0 else "left (downside)"
        print(f"    Skew      : {skew:>+8.3f}  -> model under-covers {direction}")

    # Model version split
    version_cov = defaultdict(list)
    for r, in8090 in zip(eligible, in_8090):
        version_cov[r.get("distribution_model_version") or "unknown"].append(in8090)
    if len(version_cov) > 1:
        print(f"\n  Coverage by model version:")
        for v, hits in sorted(version_cov.items()):
            print(f"    {v:<38} {_pct(sum(hits)/len(hits)):>8}  (n={len(hits)})")


# ---------------------------------------------------------------------------
# Task #22 — Market-implied log-normal vs MC distribution
# ---------------------------------------------------------------------------

# Standard normal quantiles for p10 / p25 / p50 / p75 / p90
_Z = {10: -1.28155, 25: -0.67449, 50: 0.0, 75: 0.67449, 90: 1.28155}


def _lognormal_pct(spot, atm_iv, dte_days, pct):
    """Log-normal percentile implied by ATM IV (annualised) over DTE calendar days."""
    if not spot or not atm_iv or not dte_days:
        return None
    t = dte_days / 365.0
    sigma_t = atm_iv * math.sqrt(t)
    z = _Z[pct]
    return spot * math.exp(z * sigma_t - 0.5 * sigma_t ** 2)


def market_implied_vs_model(rows=None):
    """
    Compare MC percentiles to log-normal percentiles derived from ATM IV.

    Log-normal (GBM) is the market-implied baseline.  Systematic differences
    reveal where the GARCH/MC model diverges from the market's own distribution
    — either because the MC model has edge (captures regime-specific vol) or
    because it is miscalibrated.

    Does NOT require any labeling — compares two entry-time distributions.
    """
    if rows is None:
        rows = _dbload()

    eligible = [
        r for r in rows
        if r.get("mc_expiry_p10") is not None
        and r.get("mc_expiry_p90") is not None
        and r.get("mc_expiry_p50") is not None
    ]

    print(f"\n{'='*66}")
    print("TASK #22 — Market-Implied (log-normal ATM IV) vs MC Distribution")
    print(f"{'='*66}")
    print(f"Total snapshots:          {len(rows)}")
    print(f"With MC percentiles:      {len(eligible)}")

    if len(eligible) < _MIN_N:
        print(f"\n  [waiting] Need >= {_MIN_N} rows with MC percentiles.")
        return

    # Build pairs (mc_pct, lognormal_pct) for each percentile level
    pct_levels = [10, 25, 50, 75, 90]
    mc_fields  = {10: "mc_expiry_p10", 25: "mc_expiry_p25",
                  50: "mc_expiry_p50",  75: "mc_expiry_p75", 90: "mc_expiry_p90"}

    ratios = defaultdict(list)       # pct_level -> list of (mc/ln - 1)
    spread_ratios = []               # (MC IQR) / (LN IQR) per row
    missing_iv = 0

    for r in eligible:
        cand = r.get("candidate") or {}
        spot    = float(cand.get("spot") or r.get("spot") or 0)
        atm_iv  = float(cand.get("atm_iv") or cand.get("hv20") or 0)
        dte     = float(cand.get("dte") or r.get("dte") or 0)

        if not spot or not atm_iv or not dte:
            missing_iv += 1
            continue

        for pct in pct_levels:
            mc_val = r.get(mc_fields[pct])
            if mc_val is None:
                continue
            ln_val = _lognormal_pct(spot, atm_iv, dte, pct)
            if ln_val and ln_val > 0:
                ratios[pct].append(mc_val / ln_val - 1.0)

        mc_p25 = r.get("mc_expiry_p25")
        mc_p75 = r.get("mc_expiry_p75")
        if mc_p25 and mc_p75:
            ln_p25 = _lognormal_pct(spot, atm_iv, dte, 25)
            ln_p75 = _lognormal_pct(spot, atm_iv, dte, 75)
            if ln_p25 and ln_p75 and (ln_p75 - ln_p25) > 0:
                spread_ratios.append((mc_p75 - mc_p25) / (ln_p75 - ln_p25))

    valid_n = len(eligible) - missing_iv
    print(f"  With ATM IV available:  {valid_n}  (missing IV: {missing_iv})")

    if valid_n < _MIN_N:
        print(f"\n  [waiting] Need >= {_MIN_N} rows with valid ATM IV in candidate JSON.")
        return

    # ── Percentile comparison table ──────────────────────────────────────────
    print(f"\n  MC vs log-normal percentile comparison (positive = MC wider/higher):")
    print(f"\n  {'Percentile':<12} {'n':>5}  {'mean MC/LN-1':>13}  {'p25':>8}  {'p75':>8}")
    print("  " + "-" * 50)
    for pct in pct_levels:
        rs = ratios[pct]
        if len(rs) < _MIN_N:
            print(f"  p{pct:<11} {len(rs):>5}  {'--':>13}")
            continue
        arr = np.array(rs)
        print(f"  p{pct:<11} {len(rs):>5}  {arr.mean():>+13.4f}  "
              f"{np.percentile(arr, 25):>+8.4f}  {np.percentile(arr, 75):>+8.4f}")

    # ── IQR / spread ratio ───────────────────────────────────────────────────
    if len(spread_ratios) >= _MIN_N:
        sr = np.array(spread_ratios)
        print(f"\n  IQR spread ratio (MC p25-p75 width / log-normal p25-p75 width):")
        print(f"    Mean  : {sr.mean():>8.4f}  (>1.0 = MC predicts wider spread)")
        print(f"    Median: {np.median(sr):>8.4f}")
        print(f"    p25   : {np.percentile(sr, 25):>8.4f}")
        print(f"    p75   : {np.percentile(sr, 75):>8.4f}")

    # ── Interpretation ───────────────────────────────────────────────────────
    if ratios[50] and len(ratios[50]) >= _MIN_N:
        med_bias = np.array(ratios[50]).mean()
        print(f"\n  Median (p50) bias: MC median is {abs(med_bias)*100:+.2f}% "
              + ("above" if med_bias > 0 else "below") + " the log-normal median")
        if abs(med_bias) > 0.02:
            print("    -> Non-trivial drift component in MC vs zero-drift log-normal baseline")

    if spread_ratios and len(spread_ratios) >= _MIN_N:
        sr_mean = np.array(spread_ratios).mean()
        if sr_mean > 1.05:
            print(f"  Spread: MC distributes {(sr_mean-1)*100:.1f}% wider than ATM IV implies")
            print("    -> GARCH may be amplifying vol-of-vol; watch for over-coverage")
        elif sr_mean < 0.95:
            print(f"  Spread: MC distributes {(1-sr_mean)*100:.1f}% narrower than ATM IV implies")
            print("    -> MC may be under-dispersed; check simulation path count")

    # ── Per-structure breakdown ──────────────────────────────────────────────
    by_struct = defaultdict(list)
    for r in eligible:
        cand   = r.get("candidate") or {}
        spot   = float(cand.get("spot") or r.get("spot") or 0)
        atm_iv = float(cand.get("atm_iv") or cand.get("hv20") or 0)
        dte    = float(cand.get("dte") or r.get("dte") or 0)
        mc50   = r.get("mc_expiry_p50")
        if not (spot and atm_iv and dte and mc50):
            continue
        ln50 = _lognormal_pct(spot, atm_iv, dte, 50)
        if not ln50:
            continue
        struct = cand.get("structure") or r.get("structure") or "unknown"
        by_struct[struct].append(mc50 / ln50 - 1.0)

    if any(len(v) >= _MIN_N for v in by_struct.values()):
        print(f"\n  Median bias by structure (MC p50 vs log-normal p50):")
        print(f"  {'Structure':<28} {'n':>5}  {'MC/LN-1 mean':>13}")
        print("  " + "-" * 48)
        for struct, vals in sorted(by_struct.items(), key=lambda x: -len(x[1])):
            arr = np.array(vals)
            if len(arr) < _MIN_N:
                print(f"  {struct:<28} {len(arr):>5}  {'<min_n':>13}")
            else:
                print(f"  {struct:<28} {len(arr):>5}  {arr.mean():>+13.4f}")

    # ── Model version ────────────────────────────────────────────────────────
    version_counts = defaultdict(int)
    for r in eligible:
        version_counts[r.get("distribution_model_version") or "unknown"] += 1
    if len(version_counts) > 1:
        print("\n  Model version in eligible rows:")
        for v, n in sorted(version_counts.items()):
            print(f"    {v:<40} {n:>5}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    target = argv[0].lower() if argv else "all"

    rows = _dbload()

    if target in ("all", "zone"):
        zone_calibration(rows)
    if target in ("all", "tail"):
        tail_calibration(rows)
    if target in ("all", "implied"):
        market_implied_vs_model(rows)


if __name__ == "__main__":
    main()
