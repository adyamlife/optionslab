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


def _psi(base_vals: list[float], recent_vals: list[float], n_bins: int = _N_BINS) -> float | None:
    """Population Stability Index between baseline and recent distributions."""
    if len(base_vals) < 10 or len(recent_vals) < 10:
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
        base_raw   = [_safe_float(r.get(feat)) for r in baseline_recs]
        recent_raw = [_safe_float(r.get(feat)) for r in recent_recs]

        base_clean   = [v for v in base_raw   if v is not None]
        recent_clean = [v for v in recent_raw if v is not None]

        base_stats   = _feature_stats(base_raw)
        recent_stats = _feature_stats(recent_raw)
        psi          = _psi(base_clean, recent_clean)

        flag = "ok"
        if psi is None:
            flag = "insufficient_data"
        elif psi > 0.25:
            flag = "major_shift"
            flagged.append(feat)
        elif psi > 0.10:
            flag = "moderate_shift"

        feature_reports[feat] = {
            "psi":          psi,
            "flag":         flag,
            "baseline":     base_stats,
            "recent":       recent_stats,
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
