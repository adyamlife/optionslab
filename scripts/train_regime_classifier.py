"""
Regime Classifier — trains a model on the regime_training DuckDB table to predict
the forward 10-day regime (Uptrend/Downtrend/Range-bound) from today's
technical features (rsi, adx, hv20, macd_trend, trend).

This is a model-TRAINING script, not yet wired into live decision-making.
Run it standalone: python -m scripts.train_regime_classifier
Output: data/models/regime_classifier.joblib (model + label encoders bundled
together so scripts/regime_predictor.py can load one file).

Split strategy: time-based, not random. A random split would let the model
train on some tickers' Tuesday and test on the same week's Wednesday for a
DIFFERENT ticker, leaking market-wide regime information across the split
(most tickers move together on macro days). Instead we hold out the most
recent calendar dates across all tickers — every ticker's most recent stretch
is unseen, which is the realistic "train on the past, predict the future"
setup this model will actually face in production.
"""
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, classification_report, confusion_matrix
from sklearn.preprocessing import LabelEncoder
from sklearn.utils.class_weight import compute_sample_weight
from xgboost import XGBClassifier

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH = _ROOT / "data" / "regime_training.csv"   # kept for tune_hyperparams import compat
_MODEL_PATH = _ROOT / "data" / "models" / "regime_classifier.joblib"
_CATBOOST_PATH = _ROOT / "data" / "models" / "regime_catboost.joblib"

log = logging.getLogger(__name__)

TARGET_COL = "regime_label"
TEST_FRACTION = 0.2

CAT_COLS = ["macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend", "sector_etf", "sector_trend"]

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


def load_labeled_data(path=None) -> pd.DataFrame:
    """path is accepted for API compatibility with tune_hyperparams but ignored — data
    is always read from DuckDB (the authoritative source)."""
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
    """Two-way chronological split. Kept for tune_hyperparams / model_audit / calibrate_models
    compatibility. train() uses _three_way_time_split internally."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    cutoff = unique_dates[int(len(unique_dates) * (1 - test_fraction))]
    train = df[df["date"] < cutoff]
    test = df[df["date"] >= cutoff]
    return train, test, cutoff


def _three_way_time_split(df: pd.DataFrame, val_fraction=0.15, test_fraction=0.15):
    """Three-way chronological split: train / val / test.
    val is used to fit the probability calibrator; test is the uncontaminated holdout.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    n = len(unique_dates)
    val_cutoff  = unique_dates[int(n * (1 - val_fraction - test_fraction))]
    test_cutoff = unique_dates[int(n * (1 - test_fraction))]
    train = df[df["date"] <  val_cutoff]
    val   = df[(df["date"] >= val_cutoff) & (df["date"] < test_cutoff)]
    test  = df[df["date"] >= test_cutoff]
    return train, val, test, val_cutoff, test_cutoff


def build_catboost_matrix(df: pd.DataFrame):
    """Feature matrix for CatBoost: numerics as float, categoricals as raw strings.
    CatBoost uses ordered target statistics on the string values directly — no encoding needed."""
    X = pd.DataFrame(index=df.index)
    for col in NUMERIC_FEATURES:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")
    for col in CAT_COLS:
        col_vals = df[col].astype(str) if col in df.columns else pd.Series(["unknown"] * len(df), index=df.index)
        X[col] = col_vals.fillna("unknown")
    return X


def build_feature_matrix(df: pd.DataFrame, encoders: dict = None, fit: bool = False):
    """One-hot-free categorical encoding via LabelEncoder for cat cols
    (fine for tree-based models, which split on ordinal-encoded categories without
    implying false ordering in practice). Includes 'unknown' sentinel in every
    encoder so unseen values at inference map cleanly without bias."""
    encoders = encoders or {}
    X = pd.DataFrame(index=df.index)
    for col in NUMERIC_FEATURES:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")

    for col in CAT_COLS:
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


def FEATURE_COLS_ENCODED_ORDER():
    return NUMERIC_FEATURES + CAT_COLS


def train(path=_DATA_PATH, out_path=_MODEL_PATH) -> dict:
    df = load_labeled_data(path)
    if df.empty:
        return {"ok": False, "error": "No labeled rows in regime_training table"}

    train_df, val_df, test_df, val_cutoff, test_cutoff = _three_way_time_split(df)
    if train_df.empty or val_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty fold (val_cutoff={val_cutoff}, test_cutoff={test_cutoff})"}

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_val,   _        = build_feature_matrix(val_df,  encoders=encoders, fit=False)
    X_test,  _        = build_feature_matrix(test_df, encoders=encoders, fit=False)

    label_enc = LabelEncoder()
    y_train = label_enc.fit_transform(train_df[TARGET_COL])
    y_val   = label_enc.transform(val_df[TARGET_COL])
    y_test  = label_enc.transform(test_df[TARGET_COL])

    try:
        from scripts.tune_hyperparams import load_best_params as _lbp
        _tuned = _lbp("regime") or {}
    except Exception:
        _tuned = {}
    _xgb_params = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05,
                   "subsample": 0.8, "colsample_bytree": 0.8,
                   "random_state": 42, "n_jobs": -1}
    _xgb_params.update(_tuned)
    model = XGBClassifier(
        **_xgb_params,
        objective="multi:softprob",
        eval_metric="mlogloss",
    )
    sample_weight = compute_sample_weight(class_weight="balanced", y=y_train)
    # Boost minority regime classes that tend to be underrepresented.
    # New 4-class system: Low-vol-squeeze and High-vol-breakout are rarer;
    # give them extra weight so the model doesn't collapse to Trending/Mean-reverting.
    for _boosted_class, _boost_mul in [
        ("Low-vol-squeeze",   2.0),
        ("High-vol-breakout", 1.5),
        # Legacy label — kept in case data predates the 2026-07 label redesign
        ("Range-bound",       2.0),
    ]:
        _idx = list(label_enc.classes_).index(_boosted_class) if _boosted_class in label_enc.classes_ else -1
        if _idx >= 0:
            sample_weight[y_train == _idx] *= _boost_mul
    model.fit(X_train, y_train, sample_weight=sample_weight)

    y_pred = model.predict(X_test)
    acc = accuracy_score(y_test, y_pred)
    report = classification_report(y_test, y_pred, target_names=label_enc.classes_, output_dict=True)
    cm = confusion_matrix(y_test, y_pred).tolist()

    # Feature importances keyed by actual column name — immune to ordering drift
    feature_importances = dict(zip(X_train.columns.tolist(), model.feature_importances_.tolist()))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model":            model,
        "label_encoder":    label_enc,
        "feature_encoders": encoders,
        "feature_cols":     X_train.columns.tolist(),
        "trained_on_rows":  len(train_df),
        "val_rows":         len(val_df),
        "test_rows":        len(test_df),
        "val_cutoff":       str(val_cutoff),
        "test_cutoff":      str(test_cutoff),
    }
    joblib.dump(artifact, out_path)

    # ── Calibrate on val fold (not test fold) to keep test uncontaminated ───────
    brier_before = brier_after = None
    try:
        proba_raw = model.predict_proba(X_test)
        brier_before = float(np.mean([
            brier_score_loss((y_test == i).astype(int), proba_raw[:, i])
            for i in range(len(label_enc.classes_))
        ]))
        from scripts.calibrate_models import IsotonicCalibrator
        cal_model = IsotonicCalibrator(model, n_classes=len(label_enc.classes_))
        cal_model.fit(X_val, y_val)                              # fit on val, not test
        proba_cal = cal_model.predict_proba(X_test)
        brier_after = float(np.mean([
            brier_score_loss((y_test == i).astype(int), proba_cal[:, i])
            for i in range(len(label_enc.classes_))
        ]))
        if brier_after < brier_before:
            joblib.dump({**artifact, "model": cal_model, "calibrated": True,
                         "brier_before": round(brier_before, 4),
                         "brier_after":  round(brier_after, 4)},
                        out_path.with_name(out_path.stem + "_calibrated.joblib"))
        else:
            log.info("XGBoost calibration did not improve Brier (%.4f→%.4f); raw model preferred",
                     brier_before, brier_after)
    except Exception as e:
        log.warning("XGBoost calibration failed: %s", e)

    # ── Random Forest baseline (sanity check — not saved) ────────────────────
    rf_baseline = None
    try:
        rf = RandomForestClassifier(
            n_estimators=200, max_depth=None, min_samples_leaf=5,
            class_weight="balanced", n_jobs=-1, random_state=42,
        )
        rf.fit(X_train, y_train, sample_weight=sample_weight)
        rf_pred  = rf.predict(X_test)
        rf_proba = rf.predict_proba(X_test)
        rf_acc   = float(accuracy_score(y_test, rf_pred))
        n = len(label_enc.classes_)
        rf_brier = float(np.mean([
            brier_score_loss((y_test == i).astype(int), rf_proba[:, i])
            for i in range(n)
        ]))
        rf_baseline = {
            "accuracy": round(rf_acc, 4),
            "brier": round(rf_brier, 4),
        }
    except Exception as e:
        rf_baseline = {"error": str(e)}

    # ── CatBoost (native categoricals — no label encoding) ───────────────────
    cb_result = None
    try:
        from catboost import CatBoostClassifier

        X_cb_train = build_catboost_matrix(train_df)
        X_cb_val   = build_catboost_matrix(val_df)
        X_cb_test  = build_catboost_matrix(test_df)

        cb = CatBoostClassifier(
            iterations=500,
            depth=6,
            learning_rate=0.05,
            loss_function="MultiClass",
            eval_metric="Accuracy",
            cat_features=CAT_COLS,
            auto_class_weights="Balanced",
            random_seed=42,
            verbose=False,
        )
        cb.fit(X_cb_train, y_train)

        cb_pred  = cb.predict(X_cb_test).flatten()
        cb_proba = cb.predict_proba(X_cb_test)
        cb_acc   = float(accuracy_score(y_test, cb_pred))
        n = len(label_enc.classes_)
        cb_brier = float(np.mean([
            brier_score_loss((y_test == i).astype(int), cb_proba[:, i])
            for i in range(n)
        ]))

        cb_art = {
            "model":         cb,
            "label_encoder": label_enc,
            "cat_cols":      CAT_COLS,
            "numeric_cols":  NUMERIC_FEATURES,
            "trained_on_rows": len(train_df),
            "val_rows":      len(val_df),
            "test_rows":     len(test_df),
            "val_cutoff":    str(val_cutoff),
            "test_cutoff":   str(test_cutoff),
            "accuracy":      round(cb_acc, 4),
            "brier":         round(cb_brier, 4),
        }

        # Calibrate CatBoost on val fold (not test)
        cb_brier_after = None
        try:
            from scripts.calibrate_models import IsotonicCalibrator
            cb_cal = IsotonicCalibrator(cb, n_classes=n)
            cb_cal.fit(X_cb_val, y_val)                          # val, not test
            cb_proba_cal = cb_cal.predict_proba(X_cb_test)
            cb_brier_after = float(np.mean([
                brier_score_loss((y_test == i).astype(int), cb_proba_cal[:, i])
                for i in range(n)
            ]))
            if cb_brier_after < cb_brier:
                joblib.dump({**cb_art, "model": cb_cal, "calibrated": True,
                             "brier_before": round(cb_brier, 4),
                             "brier_after":  round(cb_brier_after, 4)},
                            _CATBOOST_PATH.with_name("regime_catboost_calibrated.joblib"))
                cb_art["brier_after"] = round(cb_brier_after, 4)
            else:
                log.info("CatBoost calibration did not improve Brier (%.4f→%.4f); raw model preferred",
                         cb_brier, cb_brier_after)
        except Exception as e:
            log.warning("CatBoost calibration failed: %s", e)

        joblib.dump(cb_art, _CATBOOST_PATH)
        cb_result = {
            "accuracy":   round(cb_acc, 4),
            "brier":      round(cb_brier, 4),
            "brier_after": round(cb_brier_after, 4) if cb_brier_after is not None else None,
            "model_path": str(_CATBOOST_PATH),
        }
    except ImportError:
        cb_result = {"error": "catboost not installed — pip install catboost"}
    except Exception as e:
        cb_result = {"error": str(e)}

    return {
        "ok":           True,
        "accuracy":     round(float(acc), 4),
        "train_rows":   len(train_df),
        "val_rows":     len(val_df),
        "test_rows":    len(test_df),
        "val_cutoff":   str(val_cutoff),
        "test_cutoff":  str(test_cutoff),
        "classes":      list(label_enc.classes_),
        "confusion_matrix": cm,
        "classification_report": report,
        "feature_importances": feature_importances,
        "model_path":   str(out_path),
        "brier_before": round(brier_before, 4) if brier_before is not None else None,
        "brier_after":  round(brier_after, 4)  if brier_after  is not None else None,
        "rf_baseline":  rf_baseline,
        "catboost":     cb_result,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Train rows: {result['train_rows']} | Val rows: {result['val_rows']} | Test rows: {result['test_rows']}")
    print(f"Val cutoff: {result['val_cutoff']} | Test cutoff: {result['test_cutoff']}")
    print(f"Classes: {result['classes']}")
    rf = result.get("rf_baseline") or {}
    cb = result.get("catboost") or {}
    fmt = lambda v: f"{v:.4f}" if isinstance(v, float) else (str(v) if v else "—")
    print(f"\n{'Metric':<26} {'XGBoost':>10} {'CatBoost':>10} {'RandomForest':>13}")
    print("-" * 63)
    print(f"  {'Accuracy':<24} {fmt(result['accuracy']):>10} {fmt(cb.get('accuracy')):>10} {fmt(rf.get('accuracy')):>13}")
    print(f"  {'Brier (raw)':<24} {fmt(result.get('brier_before')):>10} {fmt(cb.get('brier')):>10} {fmt(rf.get('brier')):>13}")
    print(f"  {'Brier (calibrated)':<24} {fmt(result.get('brier_after')):>10} {fmt(cb.get('brier_after')):>10} {'—':>13}")
    if cb.get("error"):
        print(f"  CatBoost error: {cb['error']}")
    print("\nConfusion matrix (rows=actual, cols=predicted):")
    for row in result["confusion_matrix"]:
        print(row)
    print(f"\nModel saved to {result['model_path']}")
