"""
Expected Return Model — predicts the MAGNITUDE of the forward 10-day return
(not just the Up/Down/Range-bound bucket scripts/train_regime_classifier.py
predicts), from the same technical features.

Same dataset, same time-based split rationale, same features as the regime
classifier — this is a regression sibling, not a separate data pipeline.
See train_regime_classifier.py's docstring for the split-leakage reasoning;
it applies identically here.

Run standalone: python -m scripts.train_return_model
Output: data/models/return_regressor.joblib
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBRegressor

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH = _ROOT / "data" / "regime_training.csv"
_MODEL_PATH = _ROOT / "data" / "models" / "return_regressor.joblib"

FEATURE_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy",
                "beta_60d", "atr_pct", "iv_rank_52w",
                "vol_oi_ratio", "iv_skew", "iv_term_slope", "otm_pcr",
                "spy_rsi", "spy_trend", "qqq_rsi", "qqq_trend", "iwm_rsi", "iwm_trend",
                "sector_etf", "sector_trend", "sector_rsi", "sector_iv_ratio",
                "vvix", "vix_3m", "vix_term_slope",
                "earnings_inside_expiry", "news_sentiment_score",
                "analyst_rec_change", "short_interest_pct",
                # Tier 4 — chain-snapshot-derived (NaN on historical rows)
                "iv_skew_20d", "gex_proxy", "max_pain_strike",
                "oi_concentration", "wings_iv_ratio",
                # Tier 5 — macro context
                "yield_10y", "yield_3m", "yield_curve", "dollar_index",
                "fed_within_dte", "cpi_within_dte"]
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
TARGET_COL = "forward_return"
TEST_FRACTION = 0.2


_REQUIRED_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy"]


def FEATURE_COLS_ENCODED_ORDER():
    return NUMERIC_FEATURES + ["macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend",
                                "sector_etf", "sector_trend"]


def load_labeled_data(path=None) -> pd.DataFrame:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=_REQUIRED_COLS + [TARGET_COL])
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
    y_train = train_df[TARGET_COL].values
    y_test = test_df[TARGET_COL].values

    model = XGBRegressor(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, objective="reg:squarederror",
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
    mae = float(mean_absolute_error(y_test, y_pred))
    r2 = float(r2_score(y_test, y_pred))
    # Directional accuracy: sign(predicted) == sign(actual) — comparable to
    # the regime classifier's per-class accuracy as a sanity cross-check.
    directional_acc = float(np.mean(np.sign(y_pred) == np.sign(y_test)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": model,
        "feature_encoders": encoders,
        "feature_cols": FEATURE_COLS,
        "trained_on_rows": len(train_df),
        "test_rows": len(test_df),
        "split_cutoff": str(cutoff),
    }, out_path)

    return {
        "ok": True,
        "rmse": round(rmse, 5),
        "mae": round(mae, 5),
        "r2": round(r2, 5),
        "directional_accuracy": round(directional_acc, 4),
        "train_rows": len(train_df),
        "test_rows": len(test_df),
        "split_cutoff": str(cutoff),
        "feature_importances": dict(zip(FEATURE_COLS_ENCODED_ORDER(), model.feature_importances_.tolist())),
        "model_path": str(out_path),
    }


if __name__ == "__main__":
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"RMSE: {result['rmse']} | MAE: {result['mae']} | R2: {result['r2']}")
    print(f"Directional accuracy: {result['directional_accuracy']}")
    print(f"Train rows: {result['train_rows']} | Test rows: {result['test_rows']}")
    print(f"Split cutoff date: {result['split_cutoff']}")
    print(f"Feature importances: {result['feature_importances']}")
    print(f"\nModel saved to {result['model_path']}")
