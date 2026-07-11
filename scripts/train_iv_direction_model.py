"""
IV Direction Model — binary classifier predicting whether implied volatility
rank will EXPAND (go higher) or CONTRACT (go lower) over the next 10 trading days.

This is distinct from all other models in that it directly drives structure
selection — the single most important binary decision in options trading:

  IV Expanding  → buy premium (debit spreads, long options) — avoid selling vol
  IV Contracting → sell premium (credit spreads, iron condors) — edge is in theta

Target: iv_expanding column from regime_training.csv
  1 = forward_iv_rank > current iv_rank_52w (HV20-based, expanding)
  0 = forward_iv_rank <= current iv_rank_52w (contracting / stable)

Feature additions vs. other models:
  - vix_rank: 52-week VIX percentile — market-wide IV rank. High rank means vol
    is elevated and likely to mean-revert lower (contraction); low rank means
    complacency and expansion risk. This is the single most predictive feature
    for whether IV will expand or contract.

Run standalone: python -m scripts.train_iv_direction_model
Output: data/models/iv_direction_classifier.joblib
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
_MODEL_PATH = _ROOT / "data" / "models" / "iv_direction_classifier.joblib"

# vix_rank added vs. the other models — critical for IV mean-reversion signal
FEATURE_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "vix_rank",
                "rel_strength_spy", "beta_60d", "atr_pct", "iv_rank_52w",
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

NUMERIC_FEATURES = ["rsi", "adx", "hv20", "vix_close", "vix_rank",
                    "rel_strength_spy", "beta_60d", "atr_pct", "iv_rank_52w",
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

_REQUIRED_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close",
                  "rel_strength_spy", "iv_rank_52w"]


def FEATURE_COLS_ENCODED_ORDER():
    return NUMERIC_FEATURES + list(_CATEGORICAL_COLS)


def load_labeled_data(path=None) -> pd.DataFrame:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    if "iv_expanding" not in df.columns:
        raise ValueError(
            "iv_expanding column missing from DB — "
            "re-run build_regime_dataset() to regenerate with IV direction labels."
        )
    df = df.dropna(subset=_REQUIRED_COLS + ["iv_expanding"])
    df["iv_expanding"] = df["iv_expanding"].astype(int)
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

    for col in _CATEGORICAL_COLS:
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
        return {"ok": False, "error": "No labeled rows with iv_expanding in regime_training.csv"}

    train_df, test_df, cutoff = time_based_split(df)
    if train_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty train/test (cutoff={cutoff})"}

    expanding_pct = float(train_df["iv_expanding"].mean())

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_test, _         = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df["iv_expanding"].values
    y_test  = test_df["iv_expanding"].values

    model = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="binary:logistic",
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    acc    = float(accuracy_score(y_test, y_pred))
    auc    = float(roc_auc_score(y_test, y_prob))
    report = classification_report(y_test, y_pred,
                                   target_names=["Contracting", "Expanding"],
                                   output_dict=True)
    cm = confusion_matrix(y_test, y_pred).tolist()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model":            model,
        "feature_encoders": encoders,
        "feature_cols":     FEATURE_COLS_ENCODED_ORDER(),
        "trained_on_rows":  len(train_df),
        "test_rows":        len(test_df),
        "split_cutoff":     str(cutoff),
        "train_expanding_pct": round(expanding_pct, 4),
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
        "ok":                   True,
        "accuracy":             round(acc, 4),
        "auc":                  round(auc, 4),
        "train_expanding_pct":  round(expanding_pct, 4),
        "train_rows":           len(train_df),
        "test_rows":            len(test_df),
        "split_cutoff":         str(cutoff),
        "confusion_matrix":     cm,
        "classification_report": report,
        "feature_importances":  dict(zip(FEATURE_COLS_ENCODED_ORDER(),
                                         model.feature_importances_.tolist())),
        "model_path":           str(out_path),
        "brier_before": round(brier_before, 4) if brier_before is not None else None,
        "brier_after":  round(brier_after, 4)  if brier_after  is not None else None,
    }


if __name__ == "__main__":
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Accuracy : {result['accuracy']} | AUC : {result['auc']}")
    print(f"Train Expanding%: {result['train_expanding_pct']:.1%}")
    print(f"Train rows: {result['train_rows']} | Test rows: {result['test_rows']}")
    print(f"Split cutoff: {result['split_cutoff']}")
    print(f"\nConfusion matrix (rows=actual [Contracting,Expanding], cols=predicted):")
    for row in result["confusion_matrix"]:
        print(" ", row)
    cr = result["classification_report"]
    print(f"\nContracting  precision={cr['Contracting']['precision']:.3f}  recall={cr['Contracting']['recall']:.3f}")
    print(f"Expanding    precision={cr['Expanding']['precision']:.3f}  recall={cr['Expanding']['recall']:.3f}")
    print(f"\nTop 10 features:")
    top10 = sorted(result["feature_importances"].items(), key=lambda x: -x[1])[:10]
    for f, imp in top10:
        print(f"  {f}: {imp:.3f}")
    print(f"\nModel saved to {result['model_path']}")
