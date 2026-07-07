"""
Regime Classifier — trains a model on data/regime_training.csv to predict
the forward 10-day regime (Uptrend/Downtrend/Range-bound) from today's
technical features (rsi, adx, hv20, macd_trend, trend).

This is a model-TRAINING script, not yet wired into live decision-making.
Run it standalone: python -m scripts.train_regime_classifier
Output: data/models/regime_classifier.joblib (model + label encoders bundled
together so scripts/regime_predictor.py — not yet built — can load one file).

Split strategy: time-based, not random. A random split would let the model
train on some tickers' Tuesday and test on the same week's Wednesday for a
DIFFERENT ticker, leaking market-wide regime information across the split
(most tickers move together on macro days). Instead we hold out the most
recent ~20% of CALENDAR DATES across all tickers — every ticker's most
recent stretch is unseen, which is the realistic "train on the past, predict
the future" setup this model will actually face in production.
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.preprocessing import LabelEncoder
from sklearn.utils.class_weight import compute_sample_weight
from xgboost import XGBClassifier

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH = _ROOT / "data" / "regime_training.csv"
_MODEL_PATH = _ROOT / "data" / "models" / "regime_classifier.joblib"

FEATURE_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy",
                # Tier 1 — price-derived (backfillable)
                "beta_60d", "atr_pct", "iv_rank_52w",
                # Tier 1 — options-derived (NaN on historical rows; live on today rows)
                "vol_oi_ratio", "iv_skew", "iv_term_slope", "otm_pcr",
                # Tier 2 — index trends / RSI (price-derived, backfillable)
                "spy_rsi", "spy_trend", "qqq_rsi", "qqq_trend", "iwm_rsi", "iwm_trend",
                # Tier 2 — sector context
                "sector_etf", "sector_trend", "sector_rsi", "sector_iv_ratio",
                # Tier 2 — VIX context (live-only; NaN on historical rows)
                "vvix", "vix_3m", "vix_term_slope",
                # Tier 3 — event / sentiment
                "earnings_inside_expiry", "news_sentiment_score",
                "analyst_rec_change", "short_interest_pct",
                # Tier 4 — chain-snapshot-derived (NaN on historical rows)
                "iv_skew_20d", "gex_proxy", "max_pain_strike",
                "oi_concentration", "wings_iv_ratio",
                # Tier 5 — macro context (yield/dollar backfillable; event flags not)
                "yield_10y", "yield_3m", "yield_curve", "dollar_index",
                "fed_within_dte", "cpi_within_dte"]
TARGET_COL = "regime_label"
TEST_FRACTION = 0.2


NUMERIC_FEATURES = ["rsi", "adx", "hv20", "vix_close", "rel_strength_spy",
                    "beta_60d", "atr_pct", "iv_rank_52w",
                    "vol_oi_ratio", "iv_skew", "iv_term_slope", "otm_pcr",
                    "spy_rsi", "qqq_rsi", "iwm_rsi",
                    "sector_rsi", "sector_iv_ratio",
                    "vvix", "vix_3m", "vix_term_slope",
                    "earnings_inside_expiry", "news_sentiment_score",
                    "analyst_rec_change", "short_interest_pct",
                    # Tier 4
                    "iv_skew_20d", "gex_proxy", "max_pain_strike",
                    "oi_concentration", "wings_iv_ratio",
                    # Tier 5
                    "yield_10y", "yield_3m", "yield_curve", "dollar_index",
                    "fed_within_dte", "cpi_within_dte"]

def load_labeled_data(path=None) -> pd.DataFrame:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    if df.empty:
        return df
    for col in NUMERIC_FEATURES:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    _required = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy"]
    df = df.dropna(subset=_required + [TARGET_COL])
    return df


def time_based_split(df: pd.DataFrame, test_fraction=TEST_FRACTION):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    cutoff = unique_dates[int(len(unique_dates) * (1 - test_fraction))]
    train = df[df["date"] < cutoff]
    test = df[df["date"] >= cutoff]
    return train, test, cutoff


def build_feature_matrix(df: pd.DataFrame, encoders: dict = None, fit: bool = False):
    """One-hot-free categorical encoding via LabelEncoder for macd_trend/trend
    (only 2-3 categories each — fine for tree-based models, which split on
    ordinal-encoded categories without implying false ordering in practice)."""
    encoders = encoders or {}
    X = pd.DataFrame(index=df.index)
    for col in NUMERIC_FEATURES:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")

    for col in ("macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend", "sector_etf", "sector_trend"):
        vals = df[col].astype(str) if col in df.columns else pd.Series(["unknown"] * len(df), index=df.index)
        if fit:
            enc = LabelEncoder()
            X[col] = enc.fit_transform(vals)
            encoders[col] = enc
        else:
            enc = encoders.get(col)
            if enc is None:
                X[col] = 0
            else:
                known = set(enc.classes_)
                safe_vals = vals.map(lambda v: v if v in known else enc.classes_[0])
                X[col] = enc.transform(safe_vals)

    return X, encoders


def train(path=_DATA_PATH, out_path=_MODEL_PATH) -> dict:
    df = load_labeled_data(path)
    if df.empty:
        return {"ok": False, "error": "No labeled rows in regime_training.csv"}

    train_df, test_df, cutoff = time_based_split(df)
    if train_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty train/test (cutoff={cutoff})"}

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_test, _ = build_feature_matrix(test_df, encoders=encoders, fit=False)

    label_enc = LabelEncoder()
    y_train = label_enc.fit_transform(train_df[TARGET_COL])
    y_test = label_enc.transform(test_df[TARGET_COL])

    model = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="multi:softprob",
        eval_metric="mlogloss",
    )
    sample_weight = compute_sample_weight(class_weight="balanced", y=y_train)
    model.fit(X_train, y_train, sample_weight=sample_weight)

    y_pred = model.predict(X_test)
    acc = accuracy_score(y_test, y_pred)
    report = classification_report(y_test, y_pred, target_names=label_enc.classes_, output_dict=True)
    cm = confusion_matrix(y_test, y_pred).tolist()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": model,
        "label_encoder": label_enc,
        "feature_encoders": encoders,
        "feature_cols": FEATURE_COLS,
        "trained_on_rows": len(train_df),
        "test_rows": len(test_df),
        "split_cutoff": str(cutoff),
    }, out_path)

    return {
        "ok": True,
        "accuracy": round(float(acc), 4),
        "train_rows": len(train_df),
        "test_rows": len(test_df),
        "split_cutoff": str(cutoff),
        "classes": list(label_enc.classes_),
        "confusion_matrix": cm,
        "classification_report": report,
        "feature_importances": dict(zip(FEATURE_COLS_ENCODED_ORDER(), model.feature_importances_.tolist())),
        "model_path": str(out_path),
    }


def FEATURE_COLS_ENCODED_ORDER():
    return NUMERIC_FEATURES + ["macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend",
                                "sector_etf", "sector_trend"]


if __name__ == "__main__":
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Accuracy: {result['accuracy']}")
    print(f"Train rows: {result['train_rows']} | Test rows: {result['test_rows']}")
    print(f"Split cutoff date: {result['split_cutoff']}")
    print(f"Classes: {result['classes']}")
    print(f"Feature importances: {result['feature_importances']}")
    print("\nConfusion matrix (rows=actual, cols=predicted):")
    for row in result["confusion_matrix"]:
        print(row)
    print(f"\nModel saved to {result['model_path']}")
