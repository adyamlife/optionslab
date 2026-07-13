"""
Model Audit — calibration curves, Brier scores, ECE/MCE, log loss, and
sharpness histograms for all trained classifiers.

Two audit modes are produced for every model:

  training_audit  — static, uses the same held-out test fold from training.
                    Useful for model comparison and post-training QA.

  production_audit — dynamic, uses the last PROD_WINDOW_DAYS of labeled rows
                     that post-date the model's own training cutoff (truly OOS).
                     This is the audit traders care about — it answers whether
                     calibration has drifted since January.

Reliability diagram interpretation:
  X axis: mean predicted probability per bin (what the model said)
  Y axis: actual fraction of positives per bin (what happened)
  Perfect calibration = diagonal y = x.
  Above diagonal → underconfident (said 40%, happened 60%)
  Below diagonal → overconfident (said 80%, happened 60%)

Calibration metrics:
  Brier  — mean squared error of probabilities; penalises all errors moderately
  ECE    — Expected Calibration Error; weighted avg |acc - conf| per bin
  MCE    — Maximum Calibration Error; worst-case |acc - conf| across bins
  Log loss — heavily penalises overconfidence; complements Brier
  Sharpness — confidence histogram: tells you whether the model makes decisive
              predictions, not just whether its probabilities are accurate.
              A perfectly calibrated model that's always near 50% is useless.

Multi-class Brier: mean of per-class binary Brier scores (one-vs-rest).
  This is NOT the same as Σ(p-y)² / N per row — it is the average of K
  binary Brier scores. Consistent with sklearn's convention.

Calibration curves return both "uniform" (equal-width bins) and "quantile"
  (equal-sample bins). For financial models where probabilities cluster near
  0.5, quantile bins usually give smoother curves with fewer empty bins.
  Both are returned so the UI can offer a toggle.

Direction target: mirrors train_direction_model.py exactly — rows where
  |forward_return| < DIRECTION_BAND are excluded (ambiguous neutral zone).
  This was previously inconsistent (the old code used forward_return > 0).

Run standalone: python -m scripts.model_audit
"""
from __future__ import annotations

import datetime
import traceback
from datetime import date, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

# Backward-compat shim: old calibrated artifacts saved before __reduce__ was
# added to IsotonicCalibrator stored the class under __main__. The fix (adding
# __reduce__) is already in calibrate_models.py so new artifacts don't need
# this. The shim only matters when deserialising pre-fix artifacts.
import sys as _sys
from scripts.calibrate_models import IsotonicCalibrator, _load_isotonic_calibrator  # noqa: F401
_main = _sys.modules.get("__main__")
if _main is not None and not hasattr(_main, "IsotonicCalibrator"):
    _main.IsotonicCalibrator      = IsotonicCalibrator
    _main._load_isotonic_calibrator = _load_isotonic_calibrator

_ROOT       = Path(__file__).resolve().parent.parent
_MODELS_DIR = _ROOT / "data" / "models"

N_BINS           = 10    # calibration curve bins
PROD_WINDOW_DAYS = 30    # rolling window for production audit

# Must match train_direction_model.py BAND constant exactly (point 6)
_DIRECTION_BAND = 0.005


# ── Statistical helpers ───────────────────────────────────────────────────────

def _wilson_interval(n_correct: int, n_total: int,
                      confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score 95% CI for a proportion. Returns (lower, upper)."""
    if n_total <= 0:
        return 0.0, 1.0
    from scipy.stats import norm
    p      = n_correct / n_total
    z      = float(norm.ppf((1 + confidence) / 2))
    denom  = 1 + z ** 2 / n_total
    center = (p + z ** 2 / (2 * n_total)) / denom
    margin = z * np.sqrt(p * (1 - p) / n_total + z ** 2 / (4 * n_total ** 2)) / denom
    return float(np.clip(center - margin, 0.0, 1.0)), float(np.clip(center + margin, 0.0, 1.0))


def _cal_curve_full(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    n_bins: int = N_BINS,
    strategy: str = "uniform",
) -> dict:
    """
    Calibration curve with bin sample counts and Wilson 95% CI on each point.
    Empty bins are dropped; effective_bins tells you how many survived.

    Returns:
      x             — mean predicted prob per bin
      y             — actual fraction of positives per bin
      count         — number of samples in each bin (reveals low-support bins)
      ci_lower/upper— Wilson CI on y; wide intervals → low confidence in that point
      effective_bins — number of non-empty bins (may be < n_bins)
      strategy       — "uniform" or "quantile"
    """
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)

    if strategy == "quantile":
        bin_edges = np.unique(np.percentile(y_prob, np.linspace(0, 100, n_bins + 1)))
    else:
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)

    bin_edges[0]  = -1e-8
    bin_edges[-1] =  1 + 1e-8

    xs, ys, counts, ci_lo, ci_hi = [], [], [], [], []
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
        mask  = (y_prob > lo) & (y_prob <= hi)
        n     = int(mask.sum())
        if n == 0:
            continue
        n_pos = int(y_true[mask].sum())
        lo_ci, hi_ci = _wilson_interval(n_pos, n)
        xs.append(round(float(y_prob[mask].mean()), 4))
        ys.append(round(float(n_pos / n), 4))
        counts.append(n)
        ci_lo.append(round(lo_ci, 4))
        ci_hi.append(round(hi_ci, 4))

    return {
        "x":             xs,
        "y":             ys,
        "count":         counts,
        "ci_lower":      ci_lo,
        "ci_upper":      ci_hi,
        "effective_bins": len(xs),
        "strategy":      strategy,
    }


def _ece_mce(y_true: np.ndarray, y_prob: np.ndarray,
              n_bins: int = N_BINS) -> tuple[float, float]:
    """Expected Calibration Error and Maximum Calibration Error (uniform bins)."""
    y_true   = np.asarray(y_true, dtype=float)
    y_prob   = np.asarray(y_prob, dtype=float)
    n_total  = len(y_true)
    edges    = np.linspace(-1e-8, 1 + 1e-8, n_bins + 1)
    ece, mce = 0.0, 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob > lo) & (y_prob <= hi)
        n    = mask.sum()
        if n == 0:
            continue
        err  = abs(float(y_true[mask].mean()) - float(y_prob[mask].mean()))
        ece += (n / n_total) * err
        mce  = max(mce, err)
    return round(ece, 4), round(mce, 4)


def _log_loss_score(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    from sklearn.metrics import log_loss
    return round(float(log_loss(y_true, np.clip(y_prob, 1e-8, 1 - 1e-8))), 4)


def _sharpness(y_prob: np.ndarray, n_bins: int = 10) -> dict:
    """
    Confidence histogram — distribution of the model's predicted probabilities.
    Calibration tells you IF probabilities are accurate; sharpness tells you
    WHETHER the model is decisive. A well-calibrated but low-sharpness model
    (always near 50%) is not operationally useful.
    """
    y_prob     = np.asarray(y_prob, dtype=float)
    edges      = np.linspace(0.0, 1.0, n_bins + 1)
    counts, _  = np.histogram(y_prob, bins=edges)
    total      = len(y_prob)
    return {
        "bins":             [round(float(b), 2) for b in edges[:-1]],
        "bin_width":        round(1.0 / n_bins, 2),
        "counts":           counts.tolist(),
        "fractions":        [round(float(c / total), 4) for c in counts],
        "mean_confidence":  round(float(y_prob.mean()), 4),
        "std_confidence":   round(float(y_prob.std()), 4),
    }


# ── Brier helpers ─────────────────────────────────────────────────────────────

def _brier_binary(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    from sklearn.metrics import brier_score_loss
    return round(float(brier_score_loss(y_true, y_prob)), 4)


def _brier_multi_arr(proba: np.ndarray, y_true: np.ndarray, n_classes: int) -> float:
    """Mean of per-class binary Brier scores (one-vs-rest, see module docstring)."""
    from sklearn.metrics import brier_score_loss
    return round(float(np.mean([
        brier_score_loss((y_true == i).astype(int), proba[:, i])
        for i in range(n_classes)
    ])), 4)


# ── Metric bundles ────────────────────────────────────────────────────────────

def _binary_metrics(art_raw: dict, art_cal: dict | None,
                     X, y_true: np.ndarray) -> dict:
    """Full calibration metric suite for a binary classifier."""
    y_true = np.asarray(y_true, dtype=int)
    p_raw  = art_raw["model"].predict_proba(X)[:, 1]
    p_cal  = art_cal["model"].predict_proba(X)[:, 1] if art_cal else None

    ece_raw, mce_raw = _ece_mce(y_true, p_raw)
    out = {
        "n":                   len(y_true),
        "brier_raw":           _brier_binary(y_true, p_raw),
        "log_loss_raw":        _log_loss_score(y_true, p_raw),
        "ece_raw":             ece_raw,
        "mce_raw":             mce_raw,
        "curves_uniform":      {"raw": _cal_curve_full(y_true, p_raw, strategy="uniform")},
        "curves_quantile":     {"raw": _cal_curve_full(y_true, p_raw, strategy="quantile")},
        "sharpness":           {"raw": _sharpness(p_raw)},
    }
    if p_cal is not None:
        ece_cal, mce_cal = _ece_mce(y_true, p_cal)
        out["brier_calibrated"]   = _brier_binary(y_true, p_cal)
        out["log_loss_calibrated"] = _log_loss_score(y_true, p_cal)
        out["ece_calibrated"]     = ece_cal
        out["mce_calibrated"]     = mce_cal
        out["curves_uniform"]["calibrated"]  = _cal_curve_full(y_true, p_cal, strategy="uniform")
        out["curves_quantile"]["calibrated"] = _cal_curve_full(y_true, p_cal, strategy="quantile")
        out["sharpness"]["calibrated"]       = _sharpness(p_cal)
    return out


def _multiclass_metrics(art_raw: dict, art_cal: dict | None,
                         X, y_true: np.ndarray, classes: list) -> dict:
    """Full calibration metric suite for a multiclass classifier (one-vs-rest)."""
    n_classes = len(classes)
    proba_raw = art_raw["model"].predict_proba(X)
    proba_cal = art_cal["model"].predict_proba(X) if art_cal else None

    per_class = {}
    for i, cls in enumerate(classes):
        y_bin     = (y_true == i).astype(int)
        ece_r, mce_r = _ece_mce(y_bin, proba_raw[:, i])
        entry = {
            "ece_raw": ece_r, "mce_raw": mce_r,
            "curves_uniform":  {"raw": _cal_curve_full(y_bin, proba_raw[:, i], strategy="uniform")},
            "curves_quantile": {"raw": _cal_curve_full(y_bin, proba_raw[:, i], strategy="quantile")},
            "sharpness":       {"raw": _sharpness(proba_raw[:, i])},
        }
        if proba_cal is not None:
            ece_c, mce_c = _ece_mce(y_bin, proba_cal[:, i])
            entry["ece_calibrated"] = ece_c
            entry["mce_calibrated"] = mce_c
            entry["curves_uniform"]["calibrated"]  = _cal_curve_full(y_bin, proba_cal[:, i], strategy="uniform")
            entry["curves_quantile"]["calibrated"] = _cal_curve_full(y_bin, proba_cal[:, i], strategy="quantile")
            entry["sharpness"]["calibrated"]       = _sharpness(proba_cal[:, i])
        per_class[cls] = entry

    out = {
        "n":                len(y_true),
        "brier_raw":        _brier_multi_arr(proba_raw, y_true, n_classes),
        "per_class_curves": per_class,
    }
    if proba_cal is not None:
        out["brier_calibrated"] = _brier_multi_arr(proba_cal, y_true, n_classes)
    return out


# ── Model-info extractor ──────────────────────────────────────────────────────

def _model_info(art_raw: dict, art_cal: dict | None) -> dict:
    """Pull version metadata from artifact dicts for the audit report."""
    return {
        "training_rows":  art_raw.get("trained_on_rows") or art_raw.get("train_rows"),
        "test_rows":      art_raw.get("test_rows"),
        "test_cutoff":    art_raw.get("test_cutoff") or art_raw.get("meta_cutoff"),
        "val_cutoff":     art_raw.get("val_cutoff"),
        "accuracy":       art_raw.get("accuracy"),
        "auc":            art_raw.get("auc"),
        "calibrated_at":  art_cal.get("calibrated_at") if art_cal else None,
        "brier_at_training": art_cal.get("brier_before") if art_cal else None,
    }


# ── Production data helper ────────────────────────────────────────────────────

def _production_df(art_raw: dict, window_days: int = PROD_WINDOW_DAYS) -> pd.DataFrame:
    """
    Return labeled rows from the last window_days that post-date this model's
    training test_cutoff. These are genuinely OOS — the model never touched them.
    """
    from scripts.db import read_df, TABLE
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    df = read_df(
        f"SELECT * FROM {TABLE} WHERE labeled = true AND date >= '{cutoff}'"
    )
    df = df.dropna(subset=["forward_return", "rsi", "adx", "hv20"])
    df["date"] = pd.to_datetime(df["date"])
    for key in ("test_cutoff", "meta_cutoff", "split_cutoff"):
        val = art_raw.get(key)
        if val:
            df = df[df["date"] > pd.Timestamp(str(val))]
            break
    return df


def _direction_target(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build direction target exactly as train_direction_model.py does.
    Rows with |forward_return| < DIRECTION_BAND are ambiguous and dropped.
    """
    df = df.copy()
    df["_direction"] = np.where(
        df["forward_return"] >= _DIRECTION_BAND, 1,
        np.where(df["forward_return"] <= -_DIRECTION_BAND, 0, np.nan),
    )
    return df.dropna(subset=["_direction"]).copy()


# ── Per-model audit functions ─────────────────────────────────────────────────

def _audit_regime_classifier() -> dict:
    raw_path = _MODELS_DIR / "regime_classifier.joblib"
    cal_path = _MODELS_DIR / "regime_classifier_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained"}
    try:
        from scripts.train_regime_classifier import (
            load_labeled_data, build_feature_matrix, time_based_split, TARGET_COL,
        )
        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None
        le      = art_raw["label_encoder"]
        classes = list(le.classes_)
        enc     = art_raw["feature_encoders"]

        def _make_Xy(df):
            X, _ = build_feature_matrix(df, encoders=enc, fit=False)
            y    = le.transform(df[TARGET_COL].values)
            return X, y

        # Training audit
        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}
        _, test_df, cutoff = time_based_split(df)
        X_test, y_test     = _make_Xy(test_df)
        training = {
            "split_cutoff":    str(cutoff)[:10],
            **_multiclass_metrics(art_raw, art_cal, X_test, y_test, classes),
        }

        # Production audit
        prod_df = _production_df(art_raw)
        prod_df = prod_df.dropna(subset=[TARGET_COL])
        production = None
        if len(prod_df) >= 20:
            known  = prod_df[TARGET_COL].isin(le.classes_)
            prod_df = prod_df[known]
            if len(prod_df) >= 10:
                X_p, y_p = _make_Xy(prod_df)
                production = {
                    "window_days": PROD_WINDOW_DAYS,
                    **_multiclass_metrics(art_raw, art_cal, X_p, y_p, classes),
                }
        if production is None:
            production = {"window_days": PROD_WINDOW_DAYS, "n": len(prod_df),
                          "warning": "insufficient OOS data for production audit"}

        return {
            "ok": True, "type": "multiclass", "classes": classes,
            "calibrated_exists": art_cal is not None,
            "model_info":  _model_info(art_raw, art_cal),
            "training":    training,
            "production":  production,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


def _audit_direction_classifier() -> dict:
    raw_path = _MODELS_DIR / "direction_classifier.joblib"
    cal_path = _MODELS_DIR / "direction_classifier_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained"}
    try:
        from scripts.train_direction_model import (
            load_labeled_data, build_feature_matrix, time_based_split,
        )
        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None
        enc     = art_raw["feature_encoders"]

        def _make_Xy(df):
            sub    = _direction_target(df)
            X, _   = build_feature_matrix(sub, encoders=enc, fit=False)
            y      = sub["_direction"].astype(int).values
            return X, y

        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}
        _, test_df, cutoff = time_based_split(df)
        X_test, y_test     = _make_Xy(test_df)
        training = {"split_cutoff": str(cutoff)[:10],
                    **_binary_metrics(art_raw, art_cal, X_test, y_test)}

        prod_df = _production_df(art_raw)
        production = None
        if len(prod_df) >= 20:
            X_p, y_p = _make_Xy(prod_df)
            if len(y_p) >= 10:
                production = {"window_days": PROD_WINDOW_DAYS,
                              **_binary_metrics(art_raw, art_cal, X_p, y_p)}
        if production is None:
            production = {"window_days": PROD_WINDOW_DAYS, "n": 0,
                          "warning": "insufficient OOS data for production audit"}

        return {
            "ok": True, "type": "binary",
            "calibrated_exists": art_cal is not None,
            "model_info":  _model_info(art_raw, art_cal),
            "training":    training,
            "production":  production,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


def _audit_iv_direction_classifier() -> dict:
    raw_path = _MODELS_DIR / "iv_direction_classifier.joblib"
    cal_path = _MODELS_DIR / "iv_direction_classifier_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained"}
    try:
        from scripts.train_iv_direction_model import (
            load_labeled_data, build_feature_matrix, time_based_split,
        )
        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None
        enc     = art_raw.get("feature_encoders") or {}

        def _make_Xy(df):
            sub  = df.dropna(subset=["iv_expanding"])
            X, _ = build_feature_matrix(sub, encoders=enc, fit=False)
            y    = sub["iv_expanding"].astype(int).values
            return X, y

        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}
        _, test_df, cutoff = time_based_split(df)
        X_test, y_test     = _make_Xy(test_df)
        training = {"split_cutoff": str(cutoff)[:10],
                    **_binary_metrics(art_raw, art_cal, X_test, y_test)}

        prod_df = _production_df(art_raw).dropna(subset=["iv_expanding"])
        production = None
        if len(prod_df) >= 10:
            X_p, y_p = _make_Xy(prod_df)
            production = {"window_days": PROD_WINDOW_DAYS,
                          **_binary_metrics(art_raw, art_cal, X_p, y_p)}
        if production is None:
            production = {"window_days": PROD_WINDOW_DAYS, "n": len(prod_df),
                          "warning": "insufficient OOS data for production audit"}

        return {
            "ok": True, "type": "binary",
            "calibrated_exists": art_cal is not None,
            "model_info":  _model_info(art_raw, art_cal),
            "training":    training,
            "production":  production,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


def _audit_meta_ensemble() -> dict:
    raw_path = _MODELS_DIR / "meta_ensemble.joblib"
    cal_path = _MODELS_DIR / "meta_ensemble_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained"}
    try:
        from scripts.train_meta_ensemble import _load_base_models, build_meta_dataset
        from scripts.db import read_df, TABLE

        art_raw    = joblib.load(raw_path)
        art_cal    = joblib.load(cal_path) if cal_path.exists() else None
        meta_cutoff = pd.Timestamp(str(art_raw["meta_cutoff"]))

        df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
        df["date"] = pd.to_datetime(df["date"])
        meta_df    = df[df["date"] >= meta_cutoff].copy()
        if len(meta_df) < 100:
            return {"ok": False, "error": f"Only {len(meta_df)} meta rows (need 100+)"}

        models     = _load_base_models()
        X_meta, y_meta = build_meta_dataset(meta_df, models)

        # Sort chronologically before splitting — build_meta_dataset preserves
        # the order of meta_df which is date-sorted from the DB query.
        # Explicit sort guard in case the upstream order ever changes.
        if "date" in meta_df.columns:
            order  = meta_df.reset_index(drop=True)["date"].argsort()
            X_meta = X_meta.iloc[order]
            y_meta = y_meta.iloc[order]

        n          = len(X_meta)
        # Use 70/15/15 mirrors the training script's own split boundaries
        test_start = int(n * 0.85)
        X_test = X_meta.iloc[test_start:]
        y_test = y_meta.iloc[test_start:].values.astype(int)

        training = {
            "meta_cutoff": str(meta_cutoff.date()),
            "split_pct":   "last 15%",
            **_binary_metrics(art_raw, art_cal, X_test, y_test),
        }

        # Production: post-meta_cutoff rows from the last PROD_WINDOW_DAYS
        prod_raw = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
        prod_raw["date"] = pd.to_datetime(prod_raw["date"])
        prod_cutoff = pd.Timestamp((date.today() - timedelta(days=PROD_WINDOW_DAYS)).isoformat())
        prod_raw = prod_raw[(prod_raw["date"] >= prod_cutoff) &
                             (prod_raw["date"] > meta_cutoff)].copy()
        production = None
        if len(prod_raw) >= 20:
            X_p, y_p = build_meta_dataset(prod_raw, models)
            if len(X_p) >= 10:
                production = {"window_days": PROD_WINDOW_DAYS,
                              **_binary_metrics(art_raw, art_cal, X_p, y_p.values.astype(int))}
        if production is None:
            production = {"window_days": PROD_WINDOW_DAYS, "n": len(prod_raw),
                          "warning": "insufficient OOS data for production audit"}

        return {
            "ok": True, "type": "binary",
            "calibrated_exists": art_cal is not None,
            "model_info":  _model_info(art_raw, art_cal),
            "training":    training,
            "production":  production,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


def _audit_pop_classifier() -> dict:
    raw_path = _MODELS_DIR / "pop_classifier.joblib"
    cal_path = _MODELS_DIR / "pop_classifier_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet — waiting for labeled paper trade outcomes"}
    try:
        from scripts.train_pop_model import load_dataset, build_feature_matrix, time_based_split
        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None
        enc     = art_raw["feature_encoders"]

        df = load_dataset()
        if df is None or df.empty:
            return {"ok": False, "error": "No labeled paper trade outcomes yet"}

        _, test_df, cutoff = time_based_split(df)
        X_test, _  = build_feature_matrix(test_df, encoders=enc, fit=False)
        y_test     = test_df["win"].astype(int).values
        training   = {"split_cutoff": str(cutoff)[:10],
                      **_binary_metrics(art_raw, art_cal, X_test, y_test)}

        # Production: same dataset but filtered to post-test_cutoff rows only
        prod_df = df[df["date"] > str(cutoff)].copy() if "date" in df.columns else pd.DataFrame()
        production = None
        if len(prod_df) >= 10:
            X_p, _ = build_feature_matrix(prod_df, encoders=enc, fit=False)
            y_p    = prod_df["win"].astype(int).values
            production = {"window_days": PROD_WINDOW_DAYS,
                          **_binary_metrics(art_raw, art_cal, X_p, y_p)}
        if production is None:
            production = {"window_days": PROD_WINDOW_DAYS, "n": len(prod_df),
                          "warning": "insufficient OOS data for production audit"}

        return {
            "ok": True, "type": "binary",
            "calibrated_exists": art_cal is not None,
            "model_info":  _model_info(art_raw, art_cal),
            "training":    training,
            "production":  production,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


# ── Public entry point ────────────────────────────────────────────────────────

def run_audit() -> dict:
    """Run the full audit. Returns JSON-serializable dict."""
    return {
        "generated_at":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "n_bins":         N_BINS,
        "prod_window_days": PROD_WINDOW_DAYS,
        "models": {
            "regime_classifier":       _audit_regime_classifier(),
            "direction_classifier":    _audit_direction_classifier(),
            "iv_direction_classifier": _audit_iv_direction_classifier(),
            "meta_ensemble":           _audit_meta_ensemble(),
            "pop_classifier":          _audit_pop_classifier(),
        },
    }


if __name__ == "__main__":
    import json
    print("Running model audit…\n")
    audit = run_audit()
    for name, r in audit["models"].items():
        if not r.get("ok"):
            print(f"  {name:35s}  SKIP  {r.get('error', '')}")
            continue
        tr  = r.get("training", {})
        pr  = r.get("production", {})
        cal = "✓" if r["calibrated_exists"] else "✗"
        print(f"  {name:35s}  [cal={cal}]")
        print(f"    TRAINING   n={tr.get('n','?'):>4}  "
              f"Brier={tr.get('brier_raw','?'):>6}  "
              f"→{tr.get('brier_calibrated') or '—':>6}  "
              f"ECE={tr.get('ece_raw','?')}")
        if "n" in pr and pr.get("n", 0) >= 10:
            print(f"    PRODUCTION n={pr.get('n','?'):>4}  "
                  f"Brier={pr.get('brier_raw','?'):>6}  "
                  f"→{pr.get('brier_calibrated') or '—':>6}  "
                  f"ECE={pr.get('ece_raw','?')}")
        else:
            print(f"    PRODUCTION {pr.get('warning','no data')}")
    print(f"\nGenerated at: {audit['generated_at']}")
