"""
Feature Drift Monitor — ML Pipeline Improvement #13

Compares the distribution of ML input features across two time windows:
  - recent:   last 30 days of snapshots
  - baseline: 30–90 days ago

Computes per-feature mean, std, missing_pct, and PSI (Population Stability
Index) to detect distribution shift before it degrades model performance.

PSI interpretation:
  < 0.10  no significant shift
  0.10–0.25  moderate shift; investigate
  > 0.25  major shift; retrain likely needed

Output: data/feature_drift_report.json
Wired into the daily label job via app.py _daily_label().
"""
import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT        = Path(__file__).resolve().parent.parent
_REPORT_PATH = _ROOT / "data" / "feature_drift_report.json"

# ML input features to monitor — mirrors the feature set used by the ranker/regressor
MONITORED_FEATURES = [
    "atm_iv", "iv_rank_proxy", "hv20", "iv_rank_52w", "iv_term_slope",
    "pcr", "otm_pcr", "vol_oi_ratio", "beta_60d", "atr_pct",
    "rsi", "adx",
    "gex_proxy", "oi_concentration", "wings_iv_ratio", "iv_skew_20d",
    "vix", "vvix", "vix_3m", "vix_term_slope",
    "spy_rsi", "qqq_rsi", "iwm_rsi",
    "yield_curve", "dollar_index",
    "fed_within_dte", "cpi_within_dte", "ppi_within_dte",
    "jobs_within_dte", "opex_within_dte", "is_opex_week",
    "signal_score",
    "iv_pct_rank", "gamma_pct_rank", "volume_pct_rank",
    "momentum_pct_rank", "oi_pct_rank",
]

# Market-wide features: one true value per trading day, broadcast identically
# across every ticker's row. Computing PSI on the raw (duplicated) row-level
# series massively inflates apparent drift — a single day's value moving bins
# gets counted once per ticker (~100x), and PSI's log-ratio term explodes when
# a bin flips from near-empty to holding a whole day's duplicated mass. These
# must be deduplicated to one observation per date before PSI.
MARKET_WIDE_FEATURES = {
    "vix", "vvix", "vix_3m", "vix_term_slope",
    "spy_rsi", "qqq_rsi", "iwm_rsi",
    "yield_curve", "dollar_index",
}

# Event/calendar indicators: also one true value per date (same "is FOMC within
# N days" flag applies to every ticker on a given day) — same dedup treatment.
EVENT_FEATURES = {
    "fed_within_dte", "cpi_within_dte", "ppi_within_dte",
    "jobs_within_dte", "opex_within_dte", "is_opex_week",
}

# Everything else in MONITORED_FEATURES is ticker-level (genuinely one
# independent observation per ticker per day) and keeps the row-level PSI.
_DATE_LEVEL_FEATURES = MARKET_WIDE_FEATURES | EVENT_FEATURES

_N_BINS = 10   # PSI bins


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return None


def _feature_stats(values: list[float | None]) -> dict:
    clean = [v for v in values if v is not None]
    n_total = len(values)
    n_clean = len(clean)
    if not clean:
        return {"mean": None, "std": None, "missing_pct": 100.0, "min": None, "max": None}
    mean = sum(clean) / n_clean
    variance = sum((x - mean) ** 2 for x in clean) / max(n_clean - 1, 1)
    std = math.sqrt(variance)
    missing_pct = round((n_total - n_clean) / max(n_total, 1) * 100, 1)
    return {
        "mean":        round(mean, 6),
        "std":         round(std, 6),
        "missing_pct": missing_pct,
        "min":         round(min(clean), 6),
        "max":         round(max(clean), 6),
        "n":           n_clean,
    }


def _psi(base_vals: list[float], recent_vals: list[float], n_bins: int = _N_BINS,
         min_n: int = 10) -> float | None:
    """Population Stability Index between baseline and recent distributions.

    min_n is the minimum sample size per side. The default (10) matches
    n_bins=10 only nominally — 10 points across 10 bins is ~1/bin and produces
    wildly unstable PSI. Callers with a smaller effective population (e.g.
    date-deduplicated market-wide series) should lower n_bins and raise min_n
    together so there are enough points per bin for the statistic to mean
    anything, per standard PSI guidance (~5+ per bin at a minimum).
    """
    if len(base_vals) < min_n or len(recent_vals) < min_n:
        return None
    all_vals = base_vals + recent_vals
    vmin, vmax = min(all_vals), max(all_vals)
    if vmin == vmax:
        return 0.0
    step = (vmax - vmin) / n_bins
    edges = [vmin + i * step for i in range(n_bins + 1)]
    edges[-1] = vmax + 1e-9  # include right edge

    def _hist(vals):
        counts = [0] * n_bins
        for v in vals:
            for b in range(n_bins):
                if edges[b] <= v < edges[b + 1]:
                    counts[b] += 1
                    break
        n = len(vals)
        return [max(c / n, 1e-6) for c in counts]  # floor to avoid log(0)

    base_pct  = _hist(base_vals)
    recent_pct = _hist(recent_vals)
    psi = sum(
        (r - b) * math.log(r / b)
        for b, r in zip(base_pct, recent_pct)
    )
    return round(psi, 4)


def _dedup_by_date(records: list[dict], feat: str) -> list[dict]:
    """One record per collected_at date, keeping the first occurrence.
    Used for market-wide/event features so PSI sees one independent
    observation per trading day instead of one per (day x ticker)."""
    seen_dates: set[str] = set()
    out = []
    for r in records:
        d = (r.get("collected_at") or "")[:10]
        if d and d not in seen_dates:
            seen_dates.add(d)
            out.append(r)
    return out


def compute_drift_report() -> dict:
    """
    Load recent snapshots, compute per-feature drift stats, write
    data/feature_drift_report.json, and return the report dict.
    """
    from scripts.db import load_all_snapshots

    today    = date.today()
    recent_cutoff  = today - timedelta(days=30)
    baseline_start = today - timedelta(days=90)
    baseline_end   = today - timedelta(days=30)

    records = load_all_snapshots()
    recent_recs  = [
        r for r in records
        if recent_cutoff.isoformat() <= (r.get("collected_at") or "")[:10] <= today.isoformat()
    ]
    baseline_recs = [
        r for r in records
        if baseline_start.isoformat() <= (r.get("collected_at") or "")[:10] < baseline_end.isoformat()
    ]

    feature_reports = {}
    flagged = []

    for feat in MONITORED_FEATURES:
        is_date_level = feat in _DATE_LEVEL_FEATURES
        granularity = "date-level" if is_date_level else "ticker-level"

        base_raw   = [_safe_float(r.get(feat)) for r in baseline_recs]
        recent_raw = [_safe_float(r.get(feat)) for r in recent_recs]
        base_clean   = [v for v in base_raw   if v is not None]
        recent_clean = [v for v in recent_raw if v is not None]

        base_stats   = _feature_stats(base_raw)
        recent_stats = _feature_stats(recent_raw)

        # Row-level PSI: always computed, kept as an audit trail even when a
        # date-level PSI supersedes it for flagging — lets a reader see that a
        # "major_shift" alert was a duplicated-row artifact, not silently hide it.
        psi_row_level = _psi(base_clean, recent_clean)

        if is_date_level:
            base_dated   = _dedup_by_date(baseline_recs, feat)
            recent_dated = _dedup_by_date(recent_recs, feat)
            base_dated_clean   = [v for v in (_safe_float(r.get(feat)) for r in base_dated)   if v is not None]
            recent_dated_clean = [v for v in (_safe_float(r.get(feat)) for r in recent_dated) if v is not None]
            # Date-deduplicated populations are small (a few dozen trading days
            # at most, currently as few as 11-14) — 10 bins would leave ~1 point
            # per bin and produce noise-driven PSI swings of 5-15+. Use 4 bins
            # (~5+ points/bin at the current sample size) and require 20+ dates
            # per side before trusting the statistic at all.
            psi = _psi(base_dated_clean, recent_dated_clean, n_bins=4, min_n=20)
            n_base_dates, n_recent_dates = len(base_dated_clean), len(recent_dated_clean)
        else:
            psi = psi_row_level
            n_base_dates = n_recent_dates = None

        flag = "ok"
        if psi is None:
            flag = "insufficient_data"
        elif psi > 0.25:
            flag = "major_shift"
            flagged.append(feat)
        elif psi > 0.10:
            flag = "moderate_shift"

        feature_reports[feat] = {
            "psi":            psi,
            "psi_row_level":  psi_row_level,
            "granularity":    granularity,
            "n_base_dates":   n_base_dates,
            "n_recent_dates": n_recent_dates,
            "flag":           flag,
            "baseline":       base_stats,
            "recent":         recent_stats,
        }

    report = {
        "generated_at":        today.isoformat(),
        "n_recent_snapshots":  len(recent_recs),
        "n_baseline_snapshots": len(baseline_recs),
        "recent_window":       f"{recent_cutoff} → {today}",
        "baseline_window":     f"{baseline_start} → {baseline_end}",
        "flagged_features":    flagged,
        "features":            feature_reports,
    }

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(json.dumps(report, indent=2))
    log.info(
        f"[feature_drift] report written: {len(recent_recs)} recent / "
        f"{len(baseline_recs)} baseline snapshots; {len(flagged)} flagged"
    )
    return report


def load_drift_report() -> dict | None:
    """Load the latest drift report from disk, or None if not yet generated."""
    if not _REPORT_PATH.exists():
        return None
    try:
        return json.loads(_REPORT_PATH.read_text())
    except Exception:
        return None
