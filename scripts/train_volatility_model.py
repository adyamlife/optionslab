"""
Volatility Forecast Model — predicts forward 10-day REALIZED volatility
(annualized), not implied volatility.

This distinction matters: a real IV forecast would need a historical IV
surface, which we don't have (yfinance/Yahoo has no historical options
data — the same constraint documented in training_data_collector.py and
candidate_provider.py). Forward realized vol is a reasonable, buildable
proxy — it's what `regime_backfill.py`'s `forward_hv` column already
captures from pure price history — but it is not the same signal as "will
ATM IV rise or fall," which would need real historical option premiums.

Same dataset, same features, same time-based split rationale as
train_regime_classifier.py / train_return_model.py.

Run standalone: python -m scripts.train_volatility_model
Output: data/models/volatility_regressor.joblib
"""
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBRegressor

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH = _ROOT / "data" / "regime_training.csv"   # kept for import compat
_MODEL_PATH = _ROOT / "data" / "models" / "volatility_regressor.joblib"

log = logging.getLogger(__name__)

NUMERIC_FEATURES = ["rsi", "adx", "hv20", "vix_close", "rel_strength_spy",
                    "beta_60d", "atr_pct", "iv_rank_52w",
                    "vol_oi_ratio", "iv_skew", "iv_term_slope", "otm_pcr",
                    "spy_rsi", "qqq_rsi", "iwm_rsi",
                    "sector_rsi", "sector_iv_ratio",
                    "vvix", "vix_3m", "vix_term_slope",
                    "earnings_inside_expiry", "news_sentiment_score",
                    "analyst_rec_change", "short_interest_pct",
                    "iv_skew_20d", "gex_proxy", "max_pain_strike",
                    "oi_concentration", "wings_iv_ratio",
                    "yield_10y", "yield_3m", "yield_curve", "dollar_index",
                    "fed_within_dte", "cpi_within_dte"]

_CATEGORICAL_COLS = ("macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend",
                     "sector_etf", "sector_trend")

TARGET_COL = "forward_hv"
TEST_FRACTION = 0.2

_REQUIRED_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy"]


def load_labeled_data(path=None) -> pd.DataFrame:
    """path is accepted for API compatibility with tune_hyperparams but ignored — data
    is always read from DuckDB (the authoritative source)."""
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=_REQUIRED_COLS + [TARGET_COL])
    return df


def time_based_split(df: pd.DataFrame, test_fraction=TEST_FRACTION):
    """Two-way chronological split. Kept for tune_hyperparams compatibility."""
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

    for col in _CATEGORICAL_COLS:
        vals = (df[col].fillna("unknown").astype(str)
                if col in df.columns
                else pd.Series(["unknown"] * len(df), index=df.index))
        if fit:
            enc = LabelEncoder()
            all_classes = sorted(set(vals.tolist()) | {"unknown"})
            enc.fit(all_classes)
            X[col] = enc.transform(vals)
            encoders[col] = enc
        else:
            enc = encoders.get(col)
            if enc is None:
                X[col] = 0
            else:
                known = set(enc.classes_)
                safe_vals = vals.map(lambda v: v if v in known else "unknown")
                X[col] = enc.transform(safe_vals)

    return X, encoders


def train(out_path=_MODEL_PATH) -> dict:
    df = load_labeled_data()
    if df.empty:
        return {"ok": False, "error": "No labeled rows in regime_training table"}

    train_df, test_df, cutoff = time_based_split(df)
    if train_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty train/test (cutoff={cutoff})"}

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_test, _ = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df[TARGET_COL].values
    y_test = test_df[TARGET_COL].values

    try:
        from scripts.tune_hyperparams import load_best_params as _lbp
        _tuned = _lbp("volatility") or {}
    except Exception:
        _tuned = {}
    _xgb_params = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05,
                   "subsample": 0.8, "colsample_bytree": 0.8,
                   "random_state": 42, "n_jobs": -1}
    _xgb_params.update(_tuned)
    model = XGBRegressor(**_xgb_params, objective="reg:squarederror", eval_metric="rmse")
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
    mae  = float(mean_absolute_error(y_test, y_pred))
    r2   = float(r2_score(y_test, y_pred))
    # Naive baseline: "tomorrow's forward vol = today's trailing hv20" — if
    # the model can't beat this, it isn't adding value over the feature
    # that's already displayed live on every page.
    naive_rmse = float(np.sqrt(mean_squared_error(y_test, test_df["hv20"].values)))

    # Correlation metrics — useful for comparing against literature
    try:
        pearson,  _ = stats.pearsonr(y_test, y_pred)
        pearson = round(float(pearson), 4) if np.isfinite(pearson) else None
    except Exception:
        pearson = None
    try:
        spearman, _ = stats.spearmanr(y_test, y_pred)
        spearman = round(float(spearman), 4) if np.isfinite(spearman) else None
    except Exception:
        spearman = None

    # Feature importances keyed by actual column name — immune to ordering drift
    feature_importances = dict(zip(X_train.columns.tolist(), model.feature_importances_.tolist()))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model":               model,
        "feature_encoders":    encoders,
        "feature_cols":        X_train.columns.tolist(),
        "trained_on_rows":     len(train_df),
        "test_rows":           len(test_df),
        "split_cutoff":        str(cutoff),
        "rmse":                round(rmse, 5),
        "mae":                 round(mae, 5),
        "r2":                  round(r2, 5),
        "naive_baseline_rmse": round(naive_rmse, 5),
    }, out_path)

    return {
        "ok":                  True,
        "rmse":                round(rmse, 5),
        "mae":                 round(mae, 5),
        "r2":                  round(r2, 5),
        "pearson":             pearson,
        "spearman":            spearman,
        "naive_baseline_rmse": round(naive_rmse, 5),
        "beats_naive_baseline": rmse < naive_rmse,
        "train_rows":          len(train_df),
        "test_rows":           len(test_df),
        "split_cutoff":        str(cutoff),
        "feature_importances": feature_importances,
        "model_path":          str(out_path),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"RMSE: {result['rmse']} | MAE: {result['mae']} | R2: {result['r2']}")
    if result.get("pearson") is not None:
        print(f"Pearson r: {result['pearson']} | Spearman rho: {result['spearman']}")
    print(f"Naive baseline RMSE (today's hv20): {result['naive_baseline_rmse']} | Model beats it: {result['beats_naive_baseline']}")
    print(f"Train rows: {result['train_rows']} | Test rows: {result['test_rows']}")
    print(f"Split cutoff date: {result['split_cutoff']}")
    print(f"\nTop 10 features:")
    top10 = sorted(result["feature_importances"].items(), key=lambda x: -x[1])[:10]
    for f, imp in top10:
        print(f"  {f}: {imp:.3f}")
    print(f"\nModel saved to {result['model_path']}")
