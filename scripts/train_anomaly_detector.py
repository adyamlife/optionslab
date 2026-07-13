"""
Anomaly Detector — Isolation Forest trained on market feature distributions.

Detects tickers in genuinely unusual market conditions: volatility regimes,
macro stress combinations, or options-chain patterns that don't resemble any
"normal" historical day in the training data. Useful for flagging when the
rulebook and ML models may be operating outside their training distribution.

Why Isolation Forest:
  Unsupervised — no outcome labels needed. Learns what the joint distribution
  of market features looks like on normal days, then scores how much each new
  observation deviates from that distribution. Unlike per-feature thresholds
  (e.g. "RSI > 80 = unusual"), IF catches multi-feature anomalies: a stock
  can have normal RSI and normal VIX but an unusual COMBINATION of both plus
  IV rank and macro stress that has rarely appeared together historically.

Auto-scaling feature set:
  The training script dynamically selects features by population rate. Phase 1
  (now) uses price/vol/macro features at 80-100% population. When chain-snapshot
  features (vol_oi_ratio, iv_skew, iv_skew_20d, gex_proxy, etc.) accumulate
  past MIN_POPULATION_PCT, they are automatically included on the next retrain
  without any code change. This means the detector quietly improves over time
  as more live data is collected.

Output per ticker (from score_row):
  anomaly_score    — 0-100 percentile rank in the training distribution;
                     100 = perfectly normal, 0 = most anomalous ever seen.
                     A score of 4 means: "more anomalous than 96% of history."
  is_anomaly       — True if IF predicts this point as anomalous
                     (i.e. in the most anomalous contamination_pct of history)
  anomaly_flags    — list of the 3 features most deviant from their norm
                     (z-score based — gives human-readable context for WHY)

Run standalone: python -m scripts.train_anomaly_detector
Output: data/models/anomaly_detector.joblib
"""
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

_ROOT       = Path(__file__).resolve().parent.parent
_MODEL_PATH = _ROOT / "data" / "models" / "anomaly_detector.joblib"

# Features eligible for anomaly detection — ordered by signal value.
# Dynamic selection: only those in df.columns AND above MIN_POPULATION_PCT make it in.
_CANDIDATE_FEATURES = [
    # Core price/vol signals — always available
    "rsi", "adx", "hv20", "atr_pct", "beta_60d", "rel_strength_spy",
    # VIX / volatility regime — always available
    "vix_close", "vix_rank", "vvix", "vix_3m", "vix_term_slope",
    # IV rank — HV20-based proxy, always available
    "iv_rank_52w",
    # Index context
    "spy_rsi", "qqq_rsi", "iwm_rsi", "sector_rsi",
    # Macro
    "yield_10y", "yield_3m", "yield_curve", "dollar_index",
    # Event proximity
    "cpi_within_dte", "fed_within_dte", "earnings_inside_expiry",
    # Phase 2 — chain-derived; included automatically once populated
    "vol_oi_ratio", "iv_skew", "iv_term_slope", "otm_pcr",
    "sector_iv_ratio", "iv_skew_20d", "gex_proxy",
    "oi_concentration", "wings_iv_ratio",
    # Phase 2 — sentiment (live-collected)
    "news_sentiment_score", "short_interest_pct",
]

MIN_POPULATION_PCT = 0.20   # include feature if >20% of rows are non-null
CONTAMINATION      = 0.05   # top 5% most anomalous rows labelled anomalies
N_ESTIMATORS       = 100
TOP_FLAGS          = 3      # number of deviant features to name in anomaly_flags
_TRAIN_FRACTION    = 0.80   # chronological split — scaler/medians fitted on this portion

log = logging.getLogger(__name__)


def select_features(df: pd.DataFrame) -> list[str]:
    """Return candidate features that exist in df AND meet the population threshold.
    Filters to df.columns first so missing columns never raise KeyError."""
    existing = [c for c in _CANDIDATE_FEATURES if c in df.columns]
    pct      = df[existing].notna().mean()
    return [f for f in existing if pct.get(f, 0) >= MIN_POPULATION_PCT]


def _chronological_train_split(df: pd.DataFrame, train_fraction: float = _TRAIN_FRACTION) -> pd.DataFrame:
    """Return the earliest train_fraction of rows by date."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    cutoff = unique_dates[int(len(unique_dates) * train_fraction)]
    return df[df["date"] < cutoff]


def train(data_path=None, out_path=_MODEL_PATH,
          contamination: float = CONTAMINATION) -> dict:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")

    features = select_features(df)
    if len(features) < 5:
        return {
            "ok":    False,
            "error": f"Too few features meet {MIN_POPULATION_PCT:.0%} population threshold "
                     f"({len(features)} found)",
        }

    # ── Fit statistics on the training portion only (no leakage) ─────────────
    # Medians and scaler are computed on historical data only; then applied to all.
    train_df = _chronological_train_split(df)
    if len(train_df) < 50:
        # Fall back to full dataset when there isn't enough history to split
        log.warning("[AnomalyDetector] train split too small (%d rows); using full dataset", len(train_df))
        train_df = df

    X_train_raw = train_df[features].copy()
    medians     = X_train_raw.median()               # computed on train only

    # Fill NaN with training medians, then standardise
    X_train_filled = X_train_raw.fillna(medians)
    scaler         = StandardScaler()
    scaler.fit(X_train_filled)                        # fit on train only

    # Transform the full dataset for IF training (we want IF to see all history)
    X_full_filled = df[features].fillna(medians)
    X_full_scaled = scaler.transform(X_full_filled)

    model = IsolationForest(
        n_estimators=N_ESTIMATORS,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_full_scaled)

    # Score training set — store the full distribution for percentile inference.
    # more negative = more anomalous in IF's scoring convention.
    train_scores  = model.score_samples(X_full_scaled)
    anomaly_rate  = float((model.predict(X_full_scaled) == -1).mean())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model":         model,
        "scaler":        scaler,
        "scaler_mean":   scaler.mean_.tolist(),    # for SHAP / explainability
        "scaler_scale":  scaler.scale_.tolist(),   # for SHAP / explainability
        "features":      features,
        "medians":       medians.to_dict(),
        "train_scores":  train_scores,             # full score distribution for percentile scoring
        "contamination": contamination,
        "trained_rows":  len(df),
        "train_rows":    len(train_df),
    }, out_path)

    pop_summary = {f: round(float(df[f].notna().mean()), 3) for f in features}
    return {
        "ok":                 True,
        "features_used":      features,
        "feature_count":      len(features),
        "trained_rows":       len(df),
        "train_split_rows":   len(train_df),
        "anomaly_rate_train": round(anomaly_rate, 4),
        "population_pct":     pop_summary,
        "model_path":         str(out_path),
    }


def score_row(row: dict, artifact: dict) -> dict:
    """
    Score one feature dict against the trained detector.
    Returns {anomaly_score, is_anomaly, anomaly_flags}.
    Called from regime_predictor.predict_ticker().

    anomaly_score: 0-100 percentile rank in the training distribution.
      100 = perfectly normal (higher raw score than all training days).
        0 = most anomalous ever (lower raw score than all training days).
      A score of 4 means "more anomalous than 96% of training history."
    """
    features     = artifact["features"]
    medians      = artifact["medians"]
    scaler       = artifact["scaler"]
    model        = artifact["model"]
    train_scores = artifact.get("train_scores")

    # Build and impute feature vector using training medians
    vals  = {f: (row.get(f) if row.get(f) is not None else medians.get(f, 0.0))
             for f in features}
    x_raw = np.array([[vals[f] for f in features]], dtype=float)
    for i, f in enumerate(features):
        if np.isnan(x_raw[0, i]):
            x_raw[0, i] = medians.get(f, 0.0)

    x_scaled  = scaler.transform(x_raw)
    raw_score = float(model.score_samples(x_scaled)[0])
    is_anomaly = model.predict(x_scaled)[0] == -1

    # Percentile scoring: fraction of training days with a LOWER (more anomalous) score.
    # Stable across retraining — unlike min/max which compresses when outliers are added.
    if train_scores is not None and len(train_scores) > 0:
        anomaly_score = round(
            float(scipy_stats.percentileofscore(train_scores, raw_score, kind="rank")), 1
        )
    else:
        # Fallback for old artifacts that pre-date train_scores storage
        score_min = artifact.get("score_min", raw_score)
        score_max = artifact.get("score_max", raw_score)
        score_range = score_max - score_min
        anomaly_score = (
            round(float(np.clip((raw_score - score_min) / score_range, 0, 1)) * 100, 1)
            if score_range > 0 else 50.0
        )

    # Identify the most deviant features by z-score for human-readable flags
    x_std   = x_scaled[0]   # already standardised — each element is a z-score
    top_idx = np.argsort(np.abs(x_std))[::-1][:TOP_FLAGS]
    flags   = []
    for i in top_idx:
        fname     = features[i]
        z         = float(x_std[i])
        direction = "high" if z > 0 else "low"
        flags.append(f"{fname} unusually {direction} (z={z:+.1f})")

    return {
        "anomaly_score": anomaly_score,
        "is_anomaly":    bool(is_anomaly),
        "anomaly_flags": flags,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Training anomaly detector…")
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Trained on {result['trained_rows']} rows  "
          f"(scaler/medians fit on {result['train_split_rows']} training rows)  "
          f"using {result['feature_count']} features")
    print(f"Anomaly rate on full dataset: {result['anomaly_rate_train']:.1%}  "
          f"(target: {CONTAMINATION:.0%})")
    print(f"\nFeatures used ({result['feature_count']}):")
    for f, p in result["population_pct"].items():
        print(f"  {f}: {p:.0%} populated")
    print(f"\nModel saved to {result['model_path']}")
