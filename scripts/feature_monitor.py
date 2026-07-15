"""
Feature Stability Monitor — PSI-based drift detection.

Compares feature distributions between a historical reference window
(all labeled data before a cutoff) and a recent window (last N months).
Reports Population Stability Index (PSI) per feature.

PSI interpretation:
  < 0.10  — stable; no action needed
  0.10–0.25 — moderate shift; watch
  > 0.25  — significant drift; consider retraining

Run standalone:
  python -m scripts.feature_monitor
  python -m scripts.feature_monitor --lookback 6
"""
import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)

MONITORED_FEATURES = [
    "rsi", "adx", "hv20", "vix_close", "iv_rank_52w", "atr_pct",
    "rel_strength_spy", "macd_trend", "earnings_inside_expiry",
    "vol_oi_ratio", "iv_skew", "iv_term_slope",
    "otm_pcr", "oi_concentration",
]

_PSI_WARN  = 0.10
_PSI_ALERT = 0.25
_BUCKETS   = 10


def _psi(expected: np.ndarray, actual: np.ndarray, buckets: int = _BUCKETS) -> float:
    """Population Stability Index between two 1-D arrays."""
    expected = expected[~np.isnan(expected)]
    actual   = actual[~np.isnan(actual)]
    if len(expected) < 5 or len(actual) < 5:
        return float("nan")

    lo = min(expected.min(), actual.min())
    hi = max(expected.max(), actual.max())
    if hi <= lo:
        return 0.0

    bins     = np.linspace(lo, hi, buckets + 1)
    e_cnt, _ = np.histogram(expected, bins=bins)
    a_cnt, _ = np.histogram(actual,   bins=bins)

    e_pct = (e_cnt + 1e-6) / (e_cnt.sum() + 1e-6 * len(e_cnt))
    a_pct = (a_cnt + 1e-6) / (a_cnt.sum() + 1e-6 * len(a_cnt))

    return round(float(np.sum((a_pct - e_pct) * np.log(a_pct / e_pct))), 4)


def _flag(psi_val: float) -> str:
    if np.isnan(psi_val):
        return "insufficient_data"
    if psi_val < _PSI_WARN:
        return "stable"
    if psi_val < _PSI_ALERT:
        return "moderate_shift"
    return "ALERT_drift"


def run_feature_report(lookback_months: int = 3) -> dict:
    """
    Load labeled rows, split by date cutoff, compute PSI per feature.

    Args:
        lookback_months: How many recent months to treat as the "actual" window.

    Returns dict with per-feature PSI, flags, mean/std comparisons, and summary counts.
    """
    from scripts.db import read_df, TABLE

    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    if df.empty:
        return {"ok": False, "error": "No labeled rows"}

    cutoff    = df["date"].max() - pd.DateOffset(months=lookback_months)
    train_df  = df[df["date"] <  cutoff]
    recent_df = df[df["date"] >= cutoff]

    if len(train_df) < 50:
        return {"ok": False, "error": f"Too few historical rows ({len(train_df)}) for PSI baseline"}
    if len(recent_df) < 10:
        return {"ok": False, "error": f"Too few recent rows ({len(recent_df)}) to compute drift"}

    rows: list[dict] = []
    for feat in MONITORED_FEATURES:
        if feat not in df.columns:
            continue
        e = pd.to_numeric(train_df[feat],  errors="coerce").values
        a = pd.to_numeric(recent_df[feat], errors="coerce").values

        psi  = _psi(e, a)
        flag = _flag(psi)

        e_valid = e[~np.isnan(e)]
        a_valid = a[~np.isnan(a)]

        rows.append({
            "feature":     feat,
            "psi":         psi if not np.isnan(psi) else None,
            "flag":        flag,
            "train_mean":  round(float(e_valid.mean()), 4) if len(e_valid) else None,
            "recent_mean": round(float(a_valid.mean()), 4) if len(a_valid) else None,
            "train_std":   round(float(e_valid.std()),  4) if len(e_valid) > 1 else None,
            "recent_std":  round(float(a_valid.std()),  4) if len(a_valid) > 1 else None,
            "train_n":     int((~np.isnan(e)).sum()),
            "recent_n":    int((~np.isnan(a)).sum()),
        })

    rows.sort(key=lambda r: (r["psi"] or -1), reverse=True)

    n_alert    = sum(1 for r in rows if r["flag"] == "ALERT_drift")
    n_moderate = sum(1 for r in rows if r["flag"] == "moderate_shift")
    n_stable   = sum(1 for r in rows if r["flag"] == "stable")

    return {
        "ok":            True,
        "lookback_months": lookback_months,
        "cutoff":        str(cutoff.date()),
        "train_period":  f"up to {cutoff.date()}",
        "recent_period": f"{cutoff.date()} to {df['date'].max().date()}",
        "train_rows":    len(train_df),
        "recent_rows":   len(recent_df),
        "n_features":    len(rows),
        "n_alert":       n_alert,
        "n_moderate":    n_moderate,
        "n_stable":      n_stable,
        "features":      rows,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Feature distribution drift monitor")
    parser.add_argument("--lookback", type=int, default=3,
                        help="Recent window size in months (default: 3)")
    args = parser.parse_args()

    result = run_feature_report(lookback_months=args.lookback)
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)

    print(f"\n=== Feature Stability Monitor (last {result['lookback_months']} months) ===")
    print(f"Reference : {result['train_period']}  ({result['train_rows']} rows)")
    print(f"Recent    : {result['recent_period']} ({result['recent_rows']} rows)")
    print(f"\nSummary: {result['n_alert']} alert | {result['n_moderate']} moderate | {result['n_stable']} stable")

    if result["n_alert"] > 0:
        print("\n[ALERTS — consider retraining]")
        for r in result["features"]:
            if r["flag"] == "ALERT_drift":
                delta = ""
                if r["train_mean"] is not None and r["recent_mean"] is not None:
                    delta = f"  Δmean={r['recent_mean'] - r['train_mean']:+.3f}"
                print(f"  {r['feature']:<30} PSI={r['psi']:.4f}{delta}")

    print(f"\n{'Feature':<30} {'PSI':>7} {'Flag':<22} {'Train μ':>8} {'Recent μ':>9} {'Δμ':>7}")
    for r in result["features"]:
        psi_s  = f"{r['psi']:.4f}" if r["psi"] is not None else "   N/A"
        tm     = f"{r['train_mean']:.3f}"  if r["train_mean"]  is not None else "    N/A"
        rm     = f"{r['recent_mean']:.3f}" if r["recent_mean"] is not None else "    N/A"
        delta  = ""
        if r["train_mean"] is not None and r["recent_mean"] is not None:
            delta = f"{r['recent_mean'] - r['train_mean']:+.3f}"
        print(f"{r['feature']:<30} {psi_s:>7}  {r['flag']:<22} {tm:>8} {rm:>9} {delta:>7}")
