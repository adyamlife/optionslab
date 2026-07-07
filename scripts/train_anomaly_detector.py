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

Output per ticker:
  anomaly_score    — 0-100; 0 = most anomalous, 100 = perfectly normal
  is_anomaly       — True if in the most anomalous contamination_pct of history
  anomaly_flags    — list of the 3 features most deviant from their norm
                     (z-score based — gives human-readable context for WHY)

Run standalone: python -m scripts.train_anomaly_detector
Output: data/models/anomaly_detector.joblib
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH  = _ROOT / "data" / "regime_training.csv"
_MODEL_PATH = _ROOT / "data" / "models" / "anomaly_detector.joblib"

# Features eligible for anomaly detection — ordered by signal value.
# Dynamic selection: only those above MIN_POPULATION_PCT make it into training.
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


def select_features(df: pd.DataFrame) -> list[str]:
    """Return candidate features that meet the population threshold."""
    pct = df[_CANDIDATE_FEATURES].notna().mean()
    selected = [f for f in _CANDIDATE_FEATURES if pct.get(f, 0) >= MIN_POPULATION_PCT]
    return selected


def train(data_path=None, out_path=_MODEL_PATH) -> dict:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")

    features = select_features(df)
    if len(features) < 5:
        return {"ok": False, "error": f"Too few features meet {MIN_POPULATION_PCT:.0%} population threshold ({len(features)} found)"}

    X_raw = df[features].copy()

    # Impute NaN with column medians — IF doesn't handle missing values.
    medians = X_raw.median()
    X_filled = X_raw.fillna(medians)

    # Standardise so no feature dominates by scale.
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_filled)

    model = IsolationForest(
        n_estimators=N_ESTIMATORS,
        contamination=CONTAMINATION,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_scaled)

    # Score the training set to compute score distribution for percentile mapping.
    raw_scores = model.score_samples(X_scaled)   # more negative = more anomalous
    score_min, score_max = float(raw_scores.min()), float(raw_scores.max())
    anomaly_rate = float((model.predict(X_scaled) == -1).mean())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model":         model,
        "scaler":        scaler,
        "features":      features,
        "medians":       medians.to_dict(),
        "score_min":     score_min,
        "score_max":     score_max,
        "contamination": CONTAMINATION,
        "trained_rows":  len(df),
    }, out_path)

    pop_summary = {f: round(float(df[f].notna().mean()), 3) for f in features}
    return {
        "ok":            True,
        "features_used": features,
        "feature_count": len(features),
        "trained_rows":  len(df),
        "anomaly_rate_train": round(anomaly_rate, 4),
        "population_pct": pop_summary,
        "model_path":    str(out_path),
    }


def score_row(row: dict, artifact: dict) -> dict:
    """
    Score one feature dict against the trained detector.
    Returns {anomaly_score, is_anomaly, anomaly_flags}.
    Called from regime_predictor.predict_ticker().
    """
    features  = artifact["features"]
    medians   = artifact["medians"]
    scaler    = artifact["scaler"]
    model     = artifact["model"]
    score_min = artifact["score_min"]
    score_max = artifact["score_max"]

    # Build and impute feature vector
    vals = {f: (row.get(f) if row.get(f) is not None else medians.get(f, 0.0))
            for f in features}
    x_raw = np.array([[vals[f] for f in features]], dtype=float)
    # Replace any remaining NaN (feature absent from today_row) with median
    for i, f in enumerate(features):
        if np.isnan(x_raw[0, i]):
            x_raw[0, i] = medians.get(f, 0.0)

    x_scaled = scaler.transform(x_raw)
    raw_score = float(model.score_samples(x_scaled)[0])
    is_anomaly = model.predict(x_scaled)[0] == -1

    # Normalise to 0-100: 100 = perfectly normal, 0 = most anomalous ever seen.
    score_range = score_max - score_min
    anomaly_score = round(
        float(np.clip((raw_score - score_min) / score_range, 0, 1)) * 100, 1
    ) if score_range > 0 else 50.0

    # Identify the most deviant features by z-score for human-readable flags.
    x_std = x_scaled[0]   # already standardised — each element is a z-score
    top_idx = np.argsort(np.abs(x_std))[::-1][:TOP_FLAGS]
    flags = []
    for i in top_idx:
        fname = features[i]
        z     = float(x_std[i])
        direction = "high" if z > 0 else "low"
        flags.append(f"{fname} unusually {direction} (z={z:+.1f})")

    return {
        "anomaly_score": anomaly_score,
        "is_anomaly":    bool(is_anomaly),
        "anomaly_flags": flags,
    }


if __name__ == "__main__":
    print("Training anomaly detector…")
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Trained on {result['trained_rows']} rows using {result['feature_count']} features")
    print(f"Anomaly rate on training set: {result['anomaly_rate_train']:.1%} (target: {CONTAMINATION:.0%})")
    print(f"\nFeatures used ({result['feature_count']}):")
    for f, p in result["population_pct"].items():
        print(f"  {f}: {p:.0%} populated")
    print(f"\nModel saved to {result['model_path']}")
