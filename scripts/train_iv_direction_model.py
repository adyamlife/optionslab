"""
IV Direction Model — binary classifier predicting whether implied volatility
rank will EXPAND (go higher) or CONTRACT (go lower) over the next 10 trading days.

This is distinct from all other models in that it directly drives structure
selection — the single most important binary decision in options trading:

  IV Expanding  → buy premium (debit spreads, long options) — avoid selling vol
  IV Contracting → sell premium (credit spreads, iron condors) — edge is in theta

Target: iv_expanding column from regime_training table
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
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, brier_score_loss,
                             classification_report, confusion_matrix, roc_auc_score)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.utils.class_weight import compute_sample_weight
from xgboost import XGBClassifier

_ROOT = Path(__file__).resolve().parent.parent


class _BlendedBinaryClassifier:
    """0.5 XGBoost + 0.5 CatBoost probability blend. Drop-in predict_proba replacement."""
    def __init__(self, xgb_m, cb_m):
        self._xgb = xgb_m
        self._cb  = cb_m

    def predict_proba(self, X):
        return 0.5 * self._xgb.predict_proba(X) + 0.5 * self._cb.predict_proba(X)

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)

    @property
    def feature_importances_(self):
        return self._xgb.feature_importances_ if hasattr(self._xgb, "feature_importances_") \
               else self._cb.feature_importances_
_DATA_PATH = _ROOT / "data" / "regime_training.csv"  # kept for import compat
_MODEL_PATH = _ROOT / "data" / "models" / "iv_direction_classifier.joblib"

log = logging.getLogger(__name__)

N_CV_SPLITS = 5

# vix_rank added vs. the other models — critical for IV mean-reversion signal
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

TARGET_COL = "iv_expanding"


def load_labeled_data(path=None) -> pd.DataFrame:
    """path is accepted for API compatibility with tune_hyperparams but ignored — data
    is always read from DuckDB (the authoritative source)."""
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    if TARGET_COL not in df.columns:
        raise ValueError(
            "iv_expanding column missing from DB — "
            "re-run build_regime_dataset() to regenerate with IV direction labels."
        )
    df = df.dropna(subset=_REQUIRED_COLS + [TARGET_COL])
    df[TARGET_COL] = df[TARGET_COL].astype(int)
    return df


def time_based_split(df: pd.DataFrame, test_fraction=0.2):
    """Two-way chronological split. Kept for model_audit / calibrate_models compatibility."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    cutoff = unique_dates[int(len(unique_dates) * (1 - test_fraction))]
    return df[df["date"] < cutoff], df[df["date"] >= cutoff], cutoff


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


def build_feature_matrix(df: pd.DataFrame, encoders: dict = None, fit: bool = False):
    """Build feature matrix using get_dummies for categoricals.

    encoders dict stores {"__dummy_cols__": [...]} so callers can pass it back
    for the non-fit path to align columns consistently.
    """
    encoders = encoders or {}
    X = pd.DataFrame(index=df.index)
    for col in NUMERIC_FEATURES:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")

    # Categorical features — one-hot encoding (get_dummies, no ordinal assumption)
    cat_data = {
        col: (df[col].fillna("unknown").astype(str)
              if col in df.columns
              else pd.Series(["unknown"] * len(df), index=df.index))
        for col in _CATEGORICAL_COLS
    }
    cat_df  = pd.DataFrame(cat_data, index=df.index)
    dummies = pd.get_dummies(cat_df, prefix_sep="__", drop_first=False)

    if fit:
        dummy_cols = dummies.columns.tolist()
        encoders["__dummy_cols__"] = dummy_cols
    else:
        dummy_cols = encoders.get("__dummy_cols__") or []
        for c in dummy_cols:
            if c not in dummies.columns:
                dummies[c] = 0
        extra = [c for c in dummies.columns if c not in dummy_cols]
        if extra:
            dummies = dummies.drop(columns=extra)
        if dummy_cols:
            dummies = dummies[dummy_cols]

    X = pd.concat([X, dummies.astype(float)], axis=1)
    return X, encoders


def _find_optimal_threshold(proba: np.ndarray, y_true: np.ndarray) -> float:
    """Grid search 0.10-0.90 on val set, maximize F1 for Expanding class."""
    from sklearn.metrics import f1_score
    best_thr, best_f1 = 0.30, -1.0
    for thr in np.arange(0.10, 0.91, 0.02):
        preds = (proba >= thr).astype(int)
        if preds.sum() == 0:
            continue
        f1 = float(f1_score(y_true, preds, pos_label=1, zero_division=0))
        if f1 > best_f1:
            best_f1, best_thr = f1, float(thr)
    return round(best_thr, 2)


def _precision_at_k(proba: np.ndarray, y_true: np.ndarray, ks=(10, 25, 50)) -> dict:
    base_rate = float(y_true.mean())
    if base_rate == 0:
        return {}
    order = np.argsort(proba)[::-1]
    n = len(y_true)
    results = {}
    for k in ks:
        if k > n:
            continue
        top_k  = y_true[order[:k]]
        prec   = float(top_k.mean())
        recall = float(top_k.sum() / max(y_true.sum(), 1))
        lift   = round(prec / base_rate, 2) if base_rate > 0 else None
        results[f"P@{k}"]    = round(prec, 4)
        results[f"R@{k}"]    = round(recall, 4)
        results[f"Lift@{k}"] = lift
    return results


def train(out_path=_MODEL_PATH) -> dict:
    df = load_labeled_data()
    if df.empty:
        return {"ok": False, "error": "No labeled rows with iv_expanding in regime table"}

    train_df, val_df, test_df, val_cutoff, test_cutoff = _three_way_time_split(df)
    if train_df.empty or val_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty fold (val_cutoff={val_cutoff}, test_cutoff={test_cutoff})"}

    expanding_pct = float(train_df[TARGET_COL].mean())

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_val,   _        = build_feature_matrix(val_df,  encoders=encoders, fit=False)
    X_test,  _        = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df[TARGET_COL].values
    y_val   = val_df[TARGET_COL].values
    y_test  = test_df[TARGET_COL].values

    # ── Walk-forward CV on train_df to find best n_estimators ────────────────
    try:
        from scripts.tune_hyperparams import load_best_params as _lbp
        _tuned = _lbp("iv_direction") or {}
    except Exception:
        _tuned = {}
    _base_params = {
        "max_depth": 4, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "objective": "binary:logistic", "eval_metric": "logloss",
        "random_state": 42, "n_jobs": -1,
    }
    _base_params.update({k: v for k, v in _tuned.items() if k != "n_estimators"})

    train_dates = np.sort(train_df["date"].unique())
    n_splits = min(N_CV_SPLITS, len(train_dates) - 1)
    best_iters = []
    tscv = TimeSeriesSplit(n_splits=n_splits)

    # CV uses integer positional index into X_train
    X_tr_arr = X_train.values
    y_tr_arr = y_train

    for fold_tr_idx, fold_val_idx in tscv.split(X_tr_arr):
        Xf_tr, Xf_val = X_tr_arr[fold_tr_idx], X_tr_arr[fold_val_idx]
        yf_tr, yf_val = y_tr_arr[fold_tr_idx], y_tr_arr[fold_val_idx]
        sw = compute_sample_weight(class_weight="balanced", y=yf_tr)
        fold_model = XGBClassifier(n_estimators=800, early_stopping_rounds=30,
                                   **_base_params)
        fold_model.fit(Xf_tr, yf_tr, sample_weight=sw,
                       eval_set=[(Xf_val, yf_val)], verbose=False)
        best_iters.append(fold_model.best_iteration)

    best_n_estimators = max(10, int(np.median(best_iters))) if best_iters else 200
    log.info("[IV Direction] CV best_iteration: %s → final n_estimators=%d",
             best_iters, best_n_estimators)

    # ── Final model on full train_df ──────────────────────────────────────────
    sample_weight = compute_sample_weight(class_weight="balanced", y=y_train)
    model = XGBClassifier(n_estimators=best_n_estimators, **_base_params)
    model.fit(X_train, y_train, sample_weight=sample_weight)

    # CatBoost ensemble (0.5 XGB + 0.5 CatBoost blend)
    _cb_iv = None
    try:
        from catboost import CatBoostClassifier
        _cb_iv = CatBoostClassifier(
            iterations=best_n_estimators, depth=4, learning_rate=0.05,
            loss_function="Logloss", eval_metric="AUC",
            random_seed=42, verbose=0, thread_count=-1,
            auto_class_weights="Balanced",
        )
        _cb_iv.fit(X_train.values, y_train, verbose=False)
    except Exception:
        _cb_iv = None

    _final_model = _BlendedBinaryClassifier(model, _cb_iv) if _cb_iv is not None else model

    # ── Optimal threshold on val set (not test — no contamination) ───────────
    # Must be computed BEFORE test evaluation so the reported confusion matrix
    # reflects the same threshold used in live inference.
    y_val_prob = _final_model.predict_proba(X_val)[:, 1]
    optimal_threshold = _find_optimal_threshold(y_val_prob, y_val)
    log.info("[IV Direction] optimal_threshold=%.2f (F1-maximizing on val set)", optimal_threshold)

    # ── Evaluate on uncontaminated test set using production threshold ────────
    y_prob = _final_model.predict_proba(X_test)[:, 1]
    y_pred = (y_prob >= optimal_threshold).astype(int)

    acc      = float(accuracy_score(y_test, y_pred))
    bal_acc  = float(balanced_accuracy_score(y_test, y_pred))
    auc      = float(roc_auc_score(y_test, y_prob)) if len(np.unique(y_test)) > 1 else None
    report   = classification_report(y_test, y_pred,
                                     target_names=["Contracting", "Expanding"],
                                     output_dict=True)
    cm       = confusion_matrix(y_test, y_pred)

    # Naive baseline: always predict the majority class in train
    majority = int(round(expanding_pct))
    naive_pred = np.full(len(y_test), majority)
    naive_acc  = float(accuracy_score(y_test, naive_pred))

    # Feature importances keyed by actual column name — immune to ordering drift
    feature_importances = dict(zip(X_train.columns.tolist(),
                                   _final_model.feature_importances_.tolist()))

    precision_at_k = _precision_at_k(y_prob, y_test)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model":               _final_model,
        "feature_encoders":    encoders,     # kept for _build_X compat; contains __dummy_cols__
        "dummy_cols":          X_train.columns.tolist(),
        "numeric_features":    list(NUMERIC_FEATURES),
        "cat_cols":            list(_CATEGORICAL_COLS),
        "trained_on_rows":     len(train_df),
        "val_rows":            len(val_df),
        "test_rows":           len(test_df),
        "val_cutoff":          str(val_cutoff),
        "test_cutoff":         str(test_cutoff),
        "train_expanding_pct": round(expanding_pct, 4),
        "best_n_estimators":   best_n_estimators,
        "accuracy":            round(acc, 4),
        "balanced_accuracy":   round(bal_acc, 4),
        "auc":                 round(auc, 4) if auc is not None else None,
        "optimal_threshold":   optimal_threshold,
    }
    joblib.dump(artifact, out_path)

    # ── Conditional calibration on val fold — keeps test uncontaminated ───────
    brier_before = brier_after = None
    try:
        brier_before = float(brier_score_loss(y_test, y_prob))
        from scripts.calibrate_models import IsotonicCalibrator
        cal_model = IsotonicCalibrator(_final_model)
        cal_model.fit(X_val, y_val)                            # val, not test
        brier_after = float(brier_score_loss(
            y_test, cal_model.predict_proba(X_test)[:, 1]))
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
        "ok":                   True,
        "accuracy":             round(acc, 4),
        "balanced_accuracy":    round(bal_acc, 4),
        "auc":                  round(auc, 4) if auc is not None else None,
        "naive_accuracy":       round(naive_acc, 4),
        "train_expanding_pct":  round(expanding_pct, 4),
        "best_n_estimators":    best_n_estimators,
        "train_rows":           len(train_df),
        "val_rows":             len(val_df),
        "test_rows":            len(test_df),
        "val_cutoff":           str(val_cutoff),
        "test_cutoff":          str(test_cutoff),
        "confusion_matrix":     cm.tolist(),
        "classification_report": report,
        "feature_importances":  feature_importances,
        "precision_at_k":       precision_at_k,
        "optimal_threshold":    optimal_threshold,
        "model_path":           str(out_path),
        "brier_before": round(brier_before, 4) if brier_before is not None else None,
        "brier_after":  round(brier_after, 4)  if brier_after  is not None else None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Accuracy : {result['accuracy']} | Balanced : {result['balanced_accuracy']} | AUC : {result['auc']}")
    print(f"Naive majority-class accuracy: {result['naive_accuracy']}")
    print(f"Train Expanding%: {result['train_expanding_pct']:.1%}")
    print(f"Best n_estimators (CV median): {result['best_n_estimators']}")
    print(f"Train rows: {result['train_rows']} | Val rows: {result['val_rows']} | Test rows: {result['test_rows']}")
    print(f"Val cutoff: {result['val_cutoff']} | Test cutoff: {result['test_cutoff']}")

    cm = result["confusion_matrix"]
    total = sum(sum(r) for r in cm)
    print(f"\nConfusion matrix (rows=actual [Contracting,Expanding], cols=predicted):")
    for i, row in enumerate(cm):
        row_total = sum(row)
        pcts = [f"{v/row_total:.0%}" if row_total else "n/a" for v in row]
        label = ["Contracting", "Expanding"][i]
        print(f"  {label:14}: {row}  {pcts}")

    cr = result["classification_report"]
    print(f"\nContracting  precision={cr['Contracting']['precision']:.3f}  recall={cr['Contracting']['recall']:.3f}")
    print(f"Expanding    precision={cr['Expanding']['precision']:.3f}  recall={cr['Expanding']['recall']:.3f}")

    print(f"\nTop 10 features:")
    top10 = sorted(result["feature_importances"].items(), key=lambda x: -x[1])[:10]
    for f, imp in top10:
        print(f"  {f}: {imp:.3f}")

    if result.get("brier_before") is not None:
        print(f"\nBrier score  before calibration: {result['brier_before']}")
        print(f"Brier score  after  calibration: {result['brier_after']}")
    pak = result.get("precision_at_k") or {}
    if pak:
        exp_pct = result["train_expanding_pct"]
        print(f"\nPrecision@K (P(Expanding) ranking, base rate {exp_pct:.1%}):")
        print(f"  {'K':>6}  {'Precision':>10}  {'Recall':>8}  {'Lift':>6}")
        print("  " + "-" * 36)
        for k in [10, 25, 50]:
            p = pak.get(f"P@{k}"); r = pak.get(f"R@{k}"); l = pak.get(f"Lift@{k}")
            if p is not None:
                print(f"  {k:>6}  {p:>10.4f}  {r:>8.4f}  {l:>6.2f}x")
    print(f"\nModel saved to {result['model_path']}")
