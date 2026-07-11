"""
Direction Model — binary classifier predicting whether forward 10-day return
will be positive (Up) or negative/flat (Down).

Distinct from the regime classifier, which predicts the TREND STATE (Uptrend/
Downtrend/Range-bound based on SMA thresholds). This model answers a simpler
question: will the stock close higher 10 trading days from now than today?
That's directly useful for options: a bullish structure on a Down-signal stock
is a headwind; a bearish structure on an Up-signal is headwind.

Same dataset, same features, same time-based split as the other models.
Target: (forward_return > 0) → 1=Up, 0=Down

Run standalone: python -m scripts.train_direction_model
Output: data/models/direction_classifier.joblib
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (accuracy_score, brier_score_loss, classification_report,
                             confusion_matrix, roc_auc_score)
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBClassifier

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH = _ROOT / "data" / "regime_training.csv"
_MODEL_PATH = _ROOT / "data" / "models" / "direction_classifier.joblib"

FEATURE_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy",
                "beta_60d", "atr_pct", "iv_rank_52w",
                "vol_oi_ratio", "iv_skew", "iv_term_slope", "otm_pcr",
                "spy_rsi", "spy_trend", "qqq_rsi", "qqq_trend", "iwm_rsi", "iwm_trend",
                "sector_etf", "sector_trend", "sector_rsi", "sector_iv_ratio",
                "vvix", "vix_3m", "vix_term_slope",
                "earnings_inside_expiry", "news_sentiment_score",
                "analyst_rec_change", "short_interest_pct",
                "iv_skew_20d", "gex_proxy", "max_pain_strike",
                "oi_concentration", "wings_iv_ratio",
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
                    "iv_skew_20d", "gex_proxy", "max_pain_strike",
                    "oi_concentration", "wings_iv_ratio",
                    "yield_10y", "yield_3m", "yield_curve", "dollar_index",
                    "fed_within_dte", "cpi_within_dte"]

_REQUIRED_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy"]


def FEATURE_COLS_ENCODED_ORDER():
    return NUMERIC_FEATURES + ["macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend",
                                "sector_etf", "sector_trend"]


def load_labeled_data(path=None) -> pd.DataFrame:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=_REQUIRED_COLS + ["forward_return"])
    df["direction"] = (df["forward_return"] > 0).astype(int)
    return df


def time_based_split(df: pd.DataFrame, test_fraction=0.2):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    cutoff = unique_dates[int(len(unique_dates) * (1 - test_fraction))]
    return df[df["date"] < cutoff], df[df["date"] >= cutoff], cutoff


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

    # Class balance info (54/46 historically — no forced weighting needed,
    # but reported so the caller can see distribution)
    up_pct = float(train_df["direction"].mean())

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_test, _         = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df["direction"].values
    y_test  = test_df["direction"].values

    try:
        from scripts.tune_hyperparams import load_best_params as _lbp
        _tuned = _lbp("direction") or {}
    except Exception:
        _tuned = {}
    _xgb_params = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05,
                   "subsample": 0.8, "colsample_bytree": 0.8}
    _xgb_params.update(_tuned)
    model = XGBClassifier(
        **_xgb_params,
        objective="binary:logistic",
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]   # P(Up)
    acc    = float(accuracy_score(y_test, y_pred))
    auc    = float(roc_auc_score(y_test, y_prob))
    report = classification_report(y_test, y_pred, target_names=["Down", "Up"], output_dict=True)
    cm     = confusion_matrix(y_test, y_pred).tolist()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model":            model,
        "feature_encoders": encoders,
        "feature_cols":     FEATURE_COLS_ENCODED_ORDER(),
        "trained_on_rows":  len(train_df),
        "test_rows":        len(test_df),
        "split_cutoff":     str(cutoff),
        "train_up_pct":     round(up_pct, 4),
    }
    joblib.dump(artifact, out_path)

    # ── Calibrate probabilities on the test fold ─────────────────────────────
    brier_before = brier_after = None
    try:
        brier_before = float(brier_score_loss(y_test, model.predict_proba(X_test)[:, 1]))
        from scripts.calibrate_models import IsotonicCalibrator
        cal_model = IsotonicCalibrator(model).fit(X_test, y_test)
        cal_model.fit(X_test, y_test)
        brier_after = float(brier_score_loss(y_test, cal_model.predict_proba(X_test)[:, 1]))
        joblib.dump({**artifact, "model": cal_model, "calibrated": True,
                     "brier_before": round(brier_before, 4),
                     "brier_after":  round(brier_after, 4)},
                    out_path.with_name(out_path.stem + "_calibrated.joblib"))
    except Exception:
        pass

    return {
        "ok":              True,
        "accuracy":        round(acc, 4),
        "auc":             round(auc, 4),
        "train_up_pct":    round(up_pct, 4),
        "train_rows":      len(train_df),
        "test_rows":       len(test_df),
        "split_cutoff":    str(cutoff),
        "confusion_matrix": cm,
        "classification_report": report,
        "feature_importances": dict(zip(FEATURE_COLS_ENCODED_ORDER(),
                                        model.feature_importances_.tolist())),
        "model_path":      str(out_path),
        "brier_before": round(brier_before, 4) if brier_before is not None else None,
        "brier_after":  round(brier_after, 4)  if brier_after  is not None else None,
    }


if __name__ == "__main__":
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Accuracy : {result['accuracy']} | AUC : {result['auc']}")
    print(f"Train Up%: {result['train_up_pct']:.1%}")
    print(f"Train rows: {result['train_rows']} | Test rows: {result['test_rows']}")
    print(f"Split cutoff: {result['split_cutoff']}")
    print(f"\nConfusion matrix (rows=actual [Down,Up], cols=predicted):")
    for row in result["confusion_matrix"]:
        print(" ", row)
    cr = result["classification_report"]
    print(f"\nDown  precision={cr['Down']['precision']:.3f}  recall={cr['Down']['recall']:.3f}")
    print(f"Up    precision={cr['Up']['precision']:.3f}  recall={cr['Up']['recall']:.3f}")
    print(f"\nTop 10 features:")
    top10 = sorted(result["feature_importances"].items(), key=lambda x: -x[1])[:10]
    for f, imp in top10:
        print(f"  {f}: {imp:.3f}")
    print(f"\nModel saved to {result['model_path']}")
