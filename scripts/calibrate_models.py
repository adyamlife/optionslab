"""
Probability Calibration — calibrates already-trained classifiers without
retraining them.

XGBoost's predict_proba is not well-calibrated by default. When the model
says 70% win probability, the actual win rate may be only 55%. Kelly sizing
built on uncalibrated probabilities is systematically wrong.

Calibration method: isotonic regression on the held-out test fold.
  - Fits one IsotonicRegression per class (one-vs-rest for multi-class)
  - Predictions are raw XGBoost proba → isotonic map → renormalized
  - Uses the same test fold as model evaluation (never seen during training)
  - Self-contained wrapper class — no sklearn version dependency

Models calibrated:
  regime_classifier       → regime_classifier_calibrated.joblib
  direction_classifier    → direction_classifier_calibrated.joblib
  iv_direction_classifier → iv_direction_classifier_calibrated.joblib
  meta_ensemble           → meta_ensemble_calibrated.joblib
  pop_classifier          → pop_classifier_calibrated.joblib (when data available)

Models skipped:
  return_regressor      (regressor — Brier score not applicable)
  volatility_regressor  (regressor)
  anomaly_detector      (IsolationForest — uses decision_function not predict_proba)

Run standalone: python -m scripts.calibrate_models
Also callable via POST /api/ml/calibrate
"""
from pathlib import Path

import joblib
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_MODELS_DIR = _ROOT / "data" / "models"


# ── Isotonic calibration wrapper ──────────────────────────────────────────────

class IsotonicCalibrator:
    """
    Wraps a pre-fitted classifier and applies per-class isotonic regression
    to its predict_proba outputs. Drop-in replacement: exposes predict_proba
    and predict with the same interface as the raw model.

    Multi-class: one isotonic regressor per class (one-vs-rest), outputs
    are renormalized to sum to 1. Binary: single regressor on class-1 proba.
    """

    def __init__(self, raw_model, n_classes: int = 2):
        self.raw_model = raw_model
        self.n_classes = n_classes
        self._isos = None   # list of IsotonicRegression, one per class

    def fit(self, X, y):
        from sklearn.isotonic import IsotonicRegression
        raw_proba = self.raw_model.predict_proba(X)
        self._isos = []
        for i in range(self.n_classes):
            iso = IsotonicRegression(out_of_bounds="clip")
            iso.fit(raw_proba[:, i], (y == i).astype(int))
            self._isos.append(iso)
        return self

    def predict_proba(self, X):
        raw_proba = self.raw_model.predict_proba(X)
        calibrated = np.column_stack([
            self._isos[i].predict(raw_proba[:, i])
            for i in range(self.n_classes)
        ])
        # Renormalize rows to sum to 1 (isotonic doesn't guarantee this)
        row_sums = calibrated.sum(axis=1, keepdims=True)
        row_sums = np.where(row_sums == 0, 1, row_sums)
        return calibrated / row_sums

    def predict(self, X):
        return np.argmax(self.predict_proba(X), axis=1)

    # Forward attribute access to raw model (feature_names_in_, classes_, etc.)
    def __getattr__(self, name):
        # Guard against recursion during unpickling: instance dict not yet populated
        if name in ('raw_model', 'n_classes', '_isos'):
            raise AttributeError(name)
        return getattr(self.raw_model, name)

    def __reduce__(self):
        # Always pickle with the canonical module so joblib can load from any
        # entry point (gunicorn, flask, pytest) — not just when run as __main__
        return (_load_isotonic_calibrator, (self.raw_model, self.n_classes, self._isos))


def _load_isotonic_calibrator(raw_model, n_classes, isos):
    obj = IsotonicCalibrator.__new__(IsotonicCalibrator)
    obj.raw_model = raw_model
    obj.n_classes = n_classes
    obj._isos = isos
    return obj


# ── Brier score helpers ───────────────────────────────────────────────────────

def _brier_binary(model, X, y):
    from sklearn.metrics import brier_score_loss
    return float(brier_score_loss(y, model.predict_proba(X)[:, 1]))


def _brier_multiclass(model, X, y, n_classes):
    from sklearn.metrics import brier_score_loss
    proba = model.predict_proba(X)
    return float(np.mean([
        brier_score_loss((y == i).astype(int), proba[:, i])
        for i in range(n_classes)
    ]))


def _calibrate_and_save(raw_model, X_test, y_test, artifact, raw_path,
                        n_classes=2):
    multiclass = n_classes > 2

    if multiclass:
        brier_before = _brier_multiclass(raw_model, X_test, y_test, n_classes)
    else:
        brier_before = _brier_binary(raw_model, X_test, y_test)

    cal = IsotonicCalibrator(raw_model, n_classes=n_classes)
    cal.fit(X_test, y_test)

    if multiclass:
        brier_after = _brier_multiclass(cal, X_test, y_test, n_classes)
    else:
        brier_after = _brier_binary(cal, X_test, y_test)

    calib_path = Path(raw_path).with_name(Path(raw_path).stem + "_calibrated.joblib")
    joblib.dump({**artifact, "model": cal, "calibrated": True,
                 "brier_before": round(brier_before, 4),
                 "brier_after":  round(brier_after, 4)}, calib_path)

    return {"brier_before": round(brier_before, 4),
            "brier_after":  round(brier_after, 4),
            "improvement":  round(brier_before - brier_after, 4),
            "calibrated_path": str(calib_path)}


# ── Per-model calibration functions ──────────────────────────────────────────

def calibrate_regime_classifier():
    raw_path = _MODELS_DIR / "regime_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_regime_classifier import (
            load_labeled_data, build_feature_matrix, time_based_split, TARGET_COL
        )
        art = joblib.load(raw_path)
        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}
        _, test_df, _ = time_based_split(df)
        X_test, _ = build_feature_matrix(test_df, encoders=art["feature_encoders"], fit=False)
        label_enc = art["label_encoder"]
        y_test = label_enc.transform(test_df[TARGET_COL])
        result = _calibrate_and_save(art["model"], X_test, y_test, art, raw_path,
                                     n_classes=len(label_enc.classes_))
        return {"ok": True, "model": "regime_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_direction_classifier():
    raw_path = _MODELS_DIR / "direction_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_direction_model import (
            load_labeled_data, build_feature_matrix, time_based_split
        )
        art = joblib.load(raw_path)
        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}
        _, test_df, _ = time_based_split(df)
        X_test, _ = build_feature_matrix(test_df, encoders=art["feature_encoders"], fit=False)
        y_test = (test_df["forward_return"] > 0).astype(int).values
        result = _calibrate_and_save(art["model"], X_test, y_test, art, raw_path)
        return {"ok": True, "model": "direction_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_iv_direction_classifier():
    raw_path = _MODELS_DIR / "iv_direction_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_iv_direction_model import (
            load_labeled_data, build_feature_matrix, time_based_split
        )
        art = joblib.load(raw_path)
        df = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}
        _, test_df, _ = time_based_split(df)
        X_test, _ = build_feature_matrix(test_df, encoders=art["feature_encoders"], fit=False)
        y_test = test_df["iv_expanding"].values.astype(int)
        result = _calibrate_and_save(art["model"], X_test, y_test, art, raw_path)
        return {"ok": True, "model": "iv_direction_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_meta_ensemble():
    raw_path = _MODELS_DIR / "meta_ensemble.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_meta_ensemble import (
            _load_base_models, build_meta_dataset, _META_CUTOFF
        )
        from scripts.db import read_df, TABLE
        import pandas as pd

        art = joblib.load(raw_path)
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
        y_test = y_meta.iloc[split:].values
        result = _calibrate_and_save(art["model"], X_test, y_test, art, raw_path)
        return {"ok": True, "model": "meta_ensemble", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_pop_classifier():
    raw_path = _MODELS_DIR / "pop_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet (waiting for labeled paper trade outcomes)"}
    try:
        from scripts.train_pop_model import (
            load_dataset, build_feature_matrix, time_based_split
        )
        art = joblib.load(raw_path)
        df = load_dataset()
        if df is None or df.empty:
            return {"ok": False, "error": "No labeled paper trade data"}
        _, test_df, _ = time_based_split(df)
        X_test, _ = build_feature_matrix(test_df, encoders=art["feature_encoders"], fit=False)
        y_test = test_df["win"].astype(int).values
        result = _calibrate_and_save(art["model"], X_test, y_test, art, raw_path)
        return {"ok": True, "model": "pop_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_all() -> dict:
    """Calibrate all classifiers. Returns a dict of results keyed by model name."""
    results = {}
    for name, fn in [
        ("regime_classifier",       calibrate_regime_classifier),
        ("direction_classifier",    calibrate_direction_classifier),
        ("iv_direction_classifier", calibrate_iv_direction_classifier),
        ("meta_ensemble",           calibrate_meta_ensemble),
        ("pop_classifier",          calibrate_pop_classifier),
    ]:
        results[name] = fn()
    return results


if __name__ == "__main__":
    print("Calibrating all classifiers...\n")
    results = calibrate_all()
    for name, r in results.items():
        if not r.get("ok"):
            print(f"  {name:35s}  SKIP  {r.get('error', '')}")
        else:
            before = r["brier_before"]
            after  = r["brier_after"]
            delta  = r["improvement"]
            sign   = "-" if delta > 0 else "+"
            print(f"  {name:35s}  Brier {before:.4f} -> {after:.4f}  ({sign}{abs(delta):.4f})")
    print("\nDone. Calibrated artifacts saved to data/models/")
