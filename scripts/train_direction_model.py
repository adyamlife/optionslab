"""
Direction Model — 3-class classifier predicting whether forward 10-day return
will be Up, Flat, or Down (ATR-adjusted thresholds).

Class boundaries use ±0.5 × atr_pct to separate signal from noise:
  Up   (2): forward_return > +0.5 × atr_pct/100
  Flat (1): within the band  (≤ |0.5 × atr_pct|)
  Down (0): forward_return < -0.5 × atr_pct/100

Same dataset, same features, same time-based split as the other models.

Run standalone: python -m scripts.train_direction_model
Output: data/models/direction_classifier.joblib
"""
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (accuracy_score, brier_score_loss, classification_report,
                             confusion_matrix, roc_auc_score)
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBClassifier


from scripts.blended_classifier import BlendedMultiClassifier as _BlendedMultiClassifier

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH = _ROOT / "data" / "regime_training.csv"   # kept for tune_hyperparams import compat
_MODEL_PATH = _ROOT / "data" / "models" / "direction_classifier.joblib"

log = logging.getLogger(__name__)

# Class encoding: 0=Down, 1=Flat, 2=Up
_CLASS_NAMES = ["Down", "Flat", "Up"]
# ATR multiplier for Flat band — moves within ±0.5×ATR are treated as noise
_ATR_FACTOR = 0.5

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

_REQUIRED_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy"]


def load_labeled_data(path=None) -> pd.DataFrame:
    """path is accepted for API compatibility with tune_hyperparams but ignored — data
    is always read from DuckDB (the authoritative source)."""
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=_REQUIRED_COLS + ["forward_return", "atr_pct"])
    # ATR-adjusted 3-way label: Up / Flat / Down
    atr_thresh = pd.to_numeric(df["atr_pct"], errors="coerce").fillna(1.0) / 100 * _ATR_FACTOR
    fwd_ret = pd.to_numeric(df["forward_return"], errors="coerce")
    df["direction"] = np.where(fwd_ret > atr_thresh, 2,
                      np.where(fwd_ret < -atr_thresh, 0, 1))
    return df


def time_based_split(df: pd.DataFrame, test_fraction=0.2):
    """Two-way chronological split. Kept for tune_hyperparams / model_audit compatibility."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    cutoff = unique_dates[int(len(unique_dates) * (1 - test_fraction))]
    return df[df["date"] < cutoff], df[df["date"] >= cutoff], cutoff


def _three_way_time_split(df: pd.DataFrame, val_fraction=0.15, test_fraction=0.15):
    """Three-way chronological split: train / val / test."""
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

    num_cols = [c for c in NUMERIC_FEATURES if c in X.columns]
    cat_cols_present = [c for c in _CATEGORICAL_COLS if c in X.columns]
    return X[num_cols + cat_cols_present], encoders


def _precision_at_k(proba_up: np.ndarray, y_up: np.ndarray, ks=(10, 25, 50)) -> dict:
    """Rank by P(Up); evaluate what fraction of top-K were actually Up."""
    base_rate = float(y_up.mean())
    if base_rate == 0:
        return {}
    order = np.argsort(proba_up)[::-1]
    results = {}
    for k in ks:
        if k > len(y_up):
            continue
        top_k  = y_up[order[:k]]
        prec   = float(top_k.mean())
        recall = float(top_k.sum() / max(y_up.sum(), 1))
        lift   = round(prec / base_rate, 2) if base_rate > 0 else None
        results[f"P@{k}"]    = round(prec, 4)
        results[f"R@{k}"]    = round(recall, 4)
        results[f"Lift@{k}"] = lift
    return results


def train(out_path=_MODEL_PATH) -> dict:
    df = load_labeled_data()
    if df.empty:
        return {"ok": False, "error": "No labeled rows in regime_training table"}

    train_df, val_df, test_df, val_cutoff, test_cutoff = _three_way_time_split(df)
    if train_df.empty or val_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty fold"}

    class_counts = {c: int((train_df["direction"] == c).sum()) for c in range(3)}

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_val,   _        = build_feature_matrix(val_df,  encoders=encoders, fit=False)
    X_test,  _        = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df["direction"].values.astype(int)
    y_val   = val_df["direction"].values.astype(int)
    y_test  = test_df["direction"].values.astype(int)

    try:
        from scripts.tune_hyperparams import load_best_params as _lbp
        _tuned = _lbp("direction") or {}
    except Exception:
        _tuned = {}
    _xgb_params = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05,
                   "subsample": 0.8, "colsample_bytree": 0.8,
                   "random_state": 42, "n_jobs": -1}
    _xgb_params.update(_tuned)
    model = XGBClassifier(
        **_xgb_params,
        objective="multi:softprob",
        num_class=3,
        eval_metric="mlogloss",
    )
    model.fit(X_train, y_train)

    # CatBoost ensemble (0.5 XGB + 0.5 CatBoost blend)
    _cb_dir = None
    try:
        from catboost import CatBoostClassifier
        _cb_dir = CatBoostClassifier(
            iterations=300, depth=4, learning_rate=0.05,
            loss_function="MultiClass", eval_metric="Accuracy",
            random_seed=42, verbose=0, thread_count=-1,
        )
        _cb_dir.fit(X_train.values, y_train, verbose=False)
    except Exception:
        _cb_dir = None

    if _cb_dir is not None:
        proba = 0.5 * model.predict_proba(X_test) + 0.5 * _cb_dir.predict_proba(X_test.values)
        _final_model = _BlendedMultiClassifier(model, _cb_dir)
    else:
        proba = model.predict_proba(X_test)
        _final_model = model

    y_pred   = np.argmax(proba, axis=1)
    proba_up = proba[:, 2]                 # P(Up) for ranking

    acc  = float(accuracy_score(y_test, y_pred))
    try:
        auc = float(roc_auc_score(y_test, proba, multi_class="ovr", average="macro"))
    except Exception:
        auc = None
    report = classification_report(y_test, y_pred, target_names=_CLASS_NAMES, output_dict=True)
    cm     = confusion_matrix(y_test, y_pred, labels=[0, 1, 2]).tolist()

    feature_importances = dict(zip(X_train.columns.tolist(),
                                   model.feature_importances_.tolist()))

    # Rank by P(Up) and measure top-K precision against actual Up labels
    y_up = (y_test == 2).astype(int)
    precision_at_k = _precision_at_k(proba_up, y_up)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model":               _final_model,
        "feature_encoders":    encoders,
        "feature_cols":        X_train.columns.tolist(),
        "n_direction_classes": 3,
        "class_names":         _CLASS_NAMES,
        "atr_factor":          _ATR_FACTOR,
        "trained_on_rows":     len(train_df),
        "val_rows":            len(val_df),
        "test_rows":           len(test_df),
        "val_cutoff":          str(val_cutoff),
        "test_cutoff":         str(test_cutoff),
        "train_class_counts":  class_counts,
    }
    joblib.dump(artifact, out_path)

    # ── Conditional calibration on val fold ──────────────────────────────────
    brier_before = brier_after = None
    try:
        brier_before = float(np.mean([
            brier_score_loss((y_test == i).astype(int), proba[:, i])
            for i in range(3)
        ]))
        from scripts.calibrate_models import IsotonicCalibrator
        cal_model = IsotonicCalibrator(_final_model, n_classes=3)
        cal_model.fit(X_val, y_val)
        proba_cal = cal_model.predict_proba(X_test)
        brier_after = float(np.mean([
            brier_score_loss((y_test == i).astype(int), proba_cal[:, i])
            for i in range(3)
        ]))
        if brier_after < brier_before:
            joblib.dump({**artifact, "model": cal_model, "calibrated": True,
                         "brier_before": round(brier_before, 4),
                         "brier_after":  round(brier_after, 4)},
                        out_path.with_name(out_path.stem + "_calibrated.joblib"))
        else:
            log.info("Calibration did not improve Brier (%.4f→%.4f); raw model preferred",
                     brier_before, brier_after)
    except Exception as e:
        log.warning("Calibration failed: %s", e)

    return {
        "ok":               True,
        "accuracy":         round(acc, 4),
        "auc":              round(auc, 4) if auc is not None else None,
        "train_class_counts": class_counts,
        "train_rows":       len(train_df),
        "val_rows":         len(val_df),
        "test_rows":        len(test_df),
        "val_cutoff":       str(val_cutoff),
        "test_cutoff":      str(test_cutoff),
        "confusion_matrix": cm,
        "classification_report": report,
        "feature_importances":   feature_importances,
        "precision_at_k":   precision_at_k,
        "model_path":       str(out_path),
        "brier_before":     round(brier_before, 4) if brier_before is not None else None,
        "brier_after":      round(brier_after, 4)  if brier_after  is not None else None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Accuracy : {result['accuracy']} | AUC (OvR macro): {result['auc']}")
    cc = result["train_class_counts"]
    total = sum(cc.values())
    print(f"Train class distribution:  Down={cc[0]}/{total} ({cc[0]/total:.1%})  "
          f"Flat={cc[1]}/{total} ({cc[1]/total:.1%})  Up={cc[2]}/{total} ({cc[2]/total:.1%})")
    print(f"Train rows: {result['train_rows']} | Val rows: {result['val_rows']} | Test rows: {result['test_rows']}")
    print(f"Val cutoff: {result['val_cutoff']} | Test cutoff: {result['test_cutoff']}")
    print(f"\nConfusion matrix (rows=actual [Down,Flat,Up], cols=predicted):")
    for i, row_data in enumerate(result["confusion_matrix"]):
        print(f"  {_CLASS_NAMES[i]:5s}: {row_data}")
    cr = result["classification_report"]
    print(f"\nPer-class report:")
    for cls in _CLASS_NAMES:
        m = cr.get(cls, {})
        print(f"  {cls:5s}  precision={m.get('precision',0):.3f}  recall={m.get('recall',0):.3f}  "
              f"f1={m.get('f1-score',0):.3f}")
    print(f"\nTop 10 features:")
    top10 = sorted(result["feature_importances"].items(), key=lambda x: -x[1])[:10]
    for f, imp in top10:
        print(f"  {f}: {imp:.3f}")
    if result.get("brier_before") is not None:
        print(f"\nBrier score  before calibration: {result['brier_before']}")
        print(f"Brier score  after  calibration: {result['brier_after']}")
    pak = result.get("precision_at_k") or {}
    if pak:
        base_up_pct = cc[2] / total if total else 0
        print(f"\nPrecision@K (P(Up) ranking, Up base rate {base_up_pct:.1%}):")
        print(f"  {'K':>6}  {'Precision':>10}  {'Recall':>8}  {'Lift':>6}")
        print("  " + "-" * 36)
        for k in [10, 25, 50]:
            p = pak.get(f"P@{k}"); r = pak.get(f"R@{k}"); l = pak.get(f"Lift@{k}")
            if p is not None:
                print(f"  {k:>6}  {p:>10.4f}  {r:>8.4f}  {l:>6.2f}x")
    print(f"\nModel saved to {result['model_path']}")
