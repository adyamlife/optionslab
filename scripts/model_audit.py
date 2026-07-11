"""
Model Audit — reliability diagrams and Brier scores for all trained classifiers.

A reliability diagram (calibration curve) plots:
  X axis: mean predicted probability in each bin (what the model said)
  Y axis: actual fraction of positives in each bin (what actually happened)

A perfectly calibrated model traces the diagonal y = x.
  Above diagonal → underconfident (model says 40%, reality is 60%)
  Below diagonal → overconfident (model says 80%, reality is 60%)

This module:
  - Loads raw and calibrated model artifacts
  - Rebuilds the same held-out test fold used during training
  - Computes calibration curves + Brier scores for each classifier
  - Returns JSON-serializable data for /api/ml/audit

Run standalone: python -m scripts.model_audit
"""
from __future__ import annotations

import datetime
from pathlib import Path

import joblib
import numpy as np

# Shim: calibrated .joblib files saved while calibrate_models ran as __main__
# store the class as __main__.IsotonicCalibrator. Patch sys.modules so gunicorn
# (and any other non-__main__ entry point) can deserialize them.
import sys as _sys
from scripts.calibrate_models import IsotonicCalibrator, _load_isotonic_calibrator  # noqa: F401
_main = _sys.modules.get('__main__')
if _main is not None and not hasattr(_main, 'IsotonicCalibrator'):
    _main.IsotonicCalibrator = IsotonicCalibrator
    _main._load_isotonic_calibrator = _load_isotonic_calibrator

_ROOT = Path(__file__).resolve().parent.parent
_MODELS_DIR = _ROOT / "data" / "models"

N_BINS = 10   # calibration curve bins


def _cal_curve(y_true, y_prob, n_bins=N_BINS):
    """Returns (fraction_of_positives, mean_predicted_value) as plain lists."""
    from sklearn.calibration import calibration_curve
    frac, mean_pred = calibration_curve(
        y_true, y_prob, n_bins=n_bins, strategy="uniform"
    )
    return {"x": [round(float(v), 4) for v in mean_pred],
            "y": [round(float(v), 4) for v in frac]}


def _brier(y_true, y_prob):
    from sklearn.metrics import brier_score_loss
    return round(float(brier_score_loss(y_true, y_prob)), 4)


def _brier_multi(model, X, y, n_classes):
    from sklearn.metrics import brier_score_loss
    proba = model.predict_proba(X)
    return round(float(np.mean([
        brier_score_loss((y == i).astype(int), proba[:, i])
        for i in range(n_classes)
    ])), 4)


# ── Per-model audit functions ─────────────────────────────────────────────────

def _audit_regime_classifier() -> dict:
    raw_path = _MODELS_DIR / "regime_classifier.joblib"
    cal_path = _MODELS_DIR / "regime_classifier_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained"}
    try:
        from scripts.train_regime_classifier import (
            load_labeled_data, build_feature_matrix, time_based_split, TARGET_COL
        )
        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None

        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}

        _, test_df, cutoff = time_based_split(df)
        X_test, _ = build_feature_matrix(test_df, encoders=art_raw["feature_encoders"], fit=False)
        label_enc = art_raw["label_encoder"]
        y_test = label_enc.transform(test_df[TARGET_COL])
        classes = list(label_enc.classes_)
        n = len(classes)

        proba_raw = art_raw["model"].predict_proba(X_test)
        proba_cal = art_cal["model"].predict_proba(X_test) if art_cal else None

        curves = {}
        for i, cls in enumerate(classes):
            y_bin = (y_test == i).astype(int)
            entry = {"raw": _cal_curve(y_bin, proba_raw[:, i])}
            if proba_cal is not None:
                entry["calibrated"] = _cal_curve(y_bin, proba_cal[:, i])
            curves[cls] = entry

        return {
            "ok": True,
            "type": "multiclass",
            "classes": classes,
            "test_rows": len(test_df),
            "split_cutoff": str(cutoff)[:10],
            "brier_raw":        _brier_multi(art_raw["model"], X_test, y_test, n),
            "brier_calibrated": _brier_multi(art_cal["model"], X_test, y_test, n) if art_cal else None,
            "calibrated_exists": art_cal is not None,
            "curves": curves,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _audit_binary(model_stem: str, target_fn) -> dict:
    """Generic binary classifier auditor. target_fn(test_df) → y_test array."""
    raw_path = _MODELS_DIR / f"{model_stem}.joblib"
    cal_path = _MODELS_DIR / f"{model_stem}_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained"}
    try:
        # Import training module dynamically
        if model_stem == "direction_classifier":
            from scripts.train_direction_model import (
                load_labeled_data, build_feature_matrix, time_based_split
            )
        elif model_stem == "iv_direction_classifier":
            from scripts.train_iv_direction_model import (
                load_labeled_data, build_feature_matrix, time_based_split
            )
        elif model_stem == "meta_ensemble":
            return _audit_meta_ensemble()
        else:
            return {"ok": False, "error": f"Unknown model stem: {model_stem}"}

        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None

        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}

        _, test_df, cutoff = time_based_split(df)
        X_test, _ = build_feature_matrix(test_df, encoders=art_raw["feature_encoders"], fit=False)
        y_test = target_fn(test_df)

        p_raw = art_raw["model"].predict_proba(X_test)[:, 1]
        p_cal = art_cal["model"].predict_proba(X_test)[:, 1] if art_cal else None

        curve = {"raw": _cal_curve(y_test, p_raw)}
        if p_cal is not None:
            curve["calibrated"] = _cal_curve(y_test, p_cal)

        return {
            "ok": True,
            "type": "binary",
            "test_rows": len(test_df),
            "split_cutoff": str(cutoff)[:10],
            "brier_raw":        _brier(y_test, p_raw),
            "brier_calibrated": _brier(y_test, p_cal) if p_cal is not None else None,
            "calibrated_exists": art_cal is not None,
            "curve": curve,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _audit_meta_ensemble() -> dict:
    raw_path = _MODELS_DIR / "meta_ensemble.joblib"
    cal_path = _MODELS_DIR / "meta_ensemble_calibrated.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained"}
    try:
        from scripts.train_meta_ensemble import (
            _load_base_models, build_meta_dataset, _META_CUTOFF
        )
        from scripts.db import read_df, TABLE
        import pandas as pd

        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None

        df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
        df["date"] = pd.to_datetime(df["date"])
        meta_df = df[df["date"] >= _META_CUTOFF].copy()
        if len(meta_df) < 100:
            return {"ok": False, "error": f"Only {len(meta_df)} meta rows (need 100+)"}

        models = _load_base_models()
        X_meta, y_meta = build_meta_dataset(meta_df, models)
        n = len(X_meta)
        split = int(n * 0.8)
        X_test = X_meta.iloc[split:]
        y_test = y_meta.iloc[split:].values.astype(int)

        p_raw = art_raw["model"].predict_proba(X_test)[:, 1]
        p_cal = art_cal["model"].predict_proba(X_test)[:, 1] if art_cal else None

        curve = {"raw": _cal_curve(y_test, p_raw)}
        if p_cal is not None:
            curve["calibrated"] = _cal_curve(y_test, p_cal)

        return {
            "ok": True,
            "type": "binary",
            "test_rows": len(X_test),
            "split_cutoff": str(_META_CUTOFF)[:10],
            "brier_raw":        _brier(y_test, p_raw),
            "brier_calibrated": _brier(y_test, p_cal) if p_cal is not None else None,
            "calibrated_exists": art_cal is not None,
            "curve": curve,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _audit_pop_classifier() -> dict:
    raw_path = _MODELS_DIR / "pop_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet — waiting for labeled paper trade outcomes"}
    try:
        from scripts.train_pop_model import load_dataset, build_feature_matrix, time_based_split
        cal_path = _MODELS_DIR / "pop_classifier_calibrated.joblib"

        art_raw = joblib.load(raw_path)
        art_cal = joblib.load(cal_path) if cal_path.exists() else None

        df = load_dataset()
        if df is None or df.empty:
            return {"ok": False, "error": "No labeled paper trade outcomes yet"}

        _, test_df, cutoff = time_based_split(df)
        X_test, _ = build_feature_matrix(test_df, encoders=art_raw["feature_encoders"], fit=False)
        y_test = test_df["win"].astype(int).values

        p_raw = art_raw["model"].predict_proba(X_test)[:, 1]
        p_cal = art_cal["model"].predict_proba(X_test)[:, 1] if art_cal else None

        curve = {"raw": _cal_curve(y_test, p_raw)}
        if p_cal is not None:
            curve["calibrated"] = _cal_curve(y_test, p_cal)

        return {
            "ok": True,
            "type": "binary",
            "test_rows": len(test_df),
            "split_cutoff": str(cutoff)[:10],
            "brier_raw":        _brier(y_test, p_raw),
            "brier_calibrated": _brier(y_test, p_cal) if p_cal is not None else None,
            "calibrated_exists": art_cal is not None,
            "curve": curve,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Public entry point ────────────────────────────────────────────────────────

def run_audit() -> dict:
    """Run the full audit. Returns JSON-serializable dict."""
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "n_bins": N_BINS,
        "models": {
            "regime_classifier":      _audit_regime_classifier(),
            "direction_classifier":   _audit_binary(
                "direction_classifier",
                lambda df: (df["forward_return"] > 0).astype(int).values
            ),
            "iv_direction_classifier": _audit_binary(
                "iv_direction_classifier",
                lambda df: df["iv_expanding"].astype(int).values
            ),
            "meta_ensemble":          _audit_meta_ensemble(),
            "pop_classifier":         _audit_pop_classifier(),
        },
    }


if __name__ == "__main__":
    import json
    print("Running model audit...\n")
    audit = run_audit()
    for name, r in audit["models"].items():
        if not r.get("ok"):
            print(f"  {name:35s}  SKIP  {r.get('error', '')}")
            continue
        br = r.get("brier_raw")
        bc = r.get("brier_calibrated")
        rows = r.get("test_rows", "?")
        cal_str = f" -> {bc:.4f} (calibrated)" if bc is not None else ""
        print(f"  {name:35s}  Brier {br:.4f}{cal_str}  [{rows} test rows]")
        if r["type"] == "multiclass":
            for cls, curves in r["curves"].items():
                pts = len(curves["raw"]["x"])
                print(f"    {cls}: {pts} curve points")
        else:
            pts = len(r["curve"]["raw"]["x"])
            print(f"    curve: {pts} points")
    print(f"\nGenerated at: {audit['generated_at']}")
