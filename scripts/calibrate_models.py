"""
Probability Calibration — calibrates already-trained classifiers without
retraining them.

XGBoost's predict_proba is not well-calibrated by default. When the model
says 70% win probability, the actual win rate may be only 55%. Kelly sizing
built on uncalibrated probabilities is systematically wrong.

Calibration method: isotonic regression on a held-out VALIDATION fold.

  Three-way chronological split used throughout:

      ┌─────────────────┬──────────────────┬──────────────────┐
      │     Train       │    Validation    │      Test        │
      │ (model trained) │ (cal fitted here)│ (reported Brier) │
      └─────────────────┴──────────────────┴──────────────────┘

  The reported Brier improvement is therefore a true out-of-sample estimate.
  Calibrator is never evaluated on the same data it was fitted on.

  When a training artifact stores val_cutoff / test_cutoff (all new-format
  scripts), those exact cutoffs are reused here. For older artifacts without
  stored cutoffs the 2-way test fold is split in half: the first half is used
  as the calibration set and the second half is the held-out evaluation set.

Binary classification: single IsotonicRegression on P(class=1).
  P(class=0) = 1 - P(class=1). This avoids the artefact of fitting two
  independent monotone functions and having them fail to sum to 1.

Multi-class: one-vs-rest isotonic per class, outputs clipped to [1e-8, 1-1e-8]
  and renormalized. Probability ordering is not guaranteed to be preserved
  — that is expected and acceptable for one-vs-rest calibration.

Models calibrated:
  regime_classifier       → regime_classifier_calibrated.joblib
  direction_classifier    → direction_classifier_calibrated.joblib
  iv_direction_classifier → iv_direction_classifier_calibrated.joblib
  meta_ensemble           → meta_ensemble_calibrated.joblib
  pop_classifier          → pop_classifier_calibrated.joblib (when data available)

Models skipped:
  return_regressor      (regressor — Brier not applicable)
  volatility_regressor  (regressor)
  anomaly_detector      (IsolationForest — uses decision_function, not predict_proba)

Run standalone: python -m scripts.calibrate_models
Also callable via POST /api/ml/calibrate
"""
import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

_ROOT       = Path(__file__).resolve().parent.parent
_MODELS_DIR = _ROOT / "data" / "models"


# ── PlattCalibrator (temperature/sigmoid scaling) ────────────────────────────
# More stable than isotonic on small val sets (< ~500 rows). Uses a single
# scalar temperature T such that calibrated_p = σ(logit(raw_p) / T).

class PlattCalibrator:
    """
    Temperature scaling: single-parameter sigmoid recalibration.
    Fits T on val set by minimising NLL; more stable than isotonic for
    small calibration sets (500 rows or fewer).
    """

    def __init__(self, raw_model):
        self.raw_model = raw_model
        self._T        = 1.0

    def fit(self, X, y):
        from scipy.optimize import minimize_scalar
        raw_proba = self.raw_model.predict_proba(X)[:, 1]
        eps = 1e-8
        raw_proba = np.clip(raw_proba, eps, 1 - eps)
        logits = np.log(raw_proba / (1 - raw_proba))
        y_arr  = np.asarray(y, dtype=float)

        def nll(T):
            p = 1 / (1 + np.exp(-logits / max(T, 1e-3)))
            p = np.clip(p, eps, 1 - eps)
            return -float(np.mean(y_arr * np.log(p) + (1 - y_arr) * np.log(1 - p)))

        res = minimize_scalar(nll, bounds=(0.05, 5.0), method="bounded")
        self._T = float(res.x)
        return self

    def predict_proba(self, X) -> np.ndarray:
        raw = self.raw_model.predict_proba(X)[:, 1]
        raw = np.clip(raw, 1e-8, 1 - 1e-8)
        logits = np.log(raw / (1 - raw))
        p1 = np.clip(1 / (1 + np.exp(-logits / max(self._T, 1e-3))), 1e-8, 1 - 1e-8)
        return np.column_stack([1 - p1, p1])

    def predict(self, X) -> np.ndarray:
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)

    def predict_log_proba(self, X) -> np.ndarray:
        return np.log(self.predict_proba(X))

    def __getattr__(self, name):
        if name in ("raw_model", "_T"):
            raise AttributeError(name)
        return getattr(self.raw_model, name)


# ── IsotonicCalibrator wrapper ────────────────────────────────────────────────

class IsotonicCalibrator:
    """
    Wraps a pre-fitted classifier and applies isotonic regression to its
    predict_proba outputs. Drop-in replacement: exposes predict_proba,
    predict, predict_log_proba, and forwards unknown attributes to the
    raw model (feature_names_in_, classes_, etc.).

    Binary: single IsotonicRegression on P(class=1); P(class=0) = 1-P.
    Multi-class: one-vs-rest per class, clipped + renormalized.
    """

    def __init__(self, raw_model, n_classes: int = 2):
        self.raw_model = raw_model
        self.n_classes = n_classes
        self._isos     = None   # set by fit()

    def fit(self, X, y):
        from sklearn.isotonic import IsotonicRegression
        raw_proba  = self.raw_model.predict_proba(X)

        if self.n_classes == 2:
            # Single regressor on P(class=1); P(class=0) = 1 - P(class=1)
            iso = IsotonicRegression(out_of_bounds="clip")
            iso.fit(raw_proba[:, 1], (y == 1).astype(int))
            self._isos = [iso]
        else:
            self._isos = []
            for i in range(self.n_classes):
                iso = IsotonicRegression(out_of_bounds="clip")
                iso.fit(raw_proba[:, i], (y == i).astype(int))
                self._isos.append(iso)
        return self

    def predict_proba(self, X) -> np.ndarray:
        if self._isos is None:
            raise RuntimeError(
                "IsotonicCalibrator is not fitted. Call .fit(X_val, y_val) first."
            )
        raw_proba = self.raw_model.predict_proba(X)

        if self.n_classes == 2:
            p1 = np.clip(self._isos[0].predict(raw_proba[:, 1]), 1e-8, 1 - 1e-8)
            return np.column_stack([1.0 - p1, p1])

        calibrated = np.column_stack([
            self._isos[i].predict(raw_proba[:, i])
            for i in range(self.n_classes)
        ])
        calibrated  = np.clip(calibrated, 1e-8, 1 - 1e-8)
        row_sums    = calibrated.sum(axis=1, keepdims=True)
        row_sums    = np.where(row_sums == 0, 1.0, row_sums)
        return calibrated / row_sums

    def predict(self, X) -> np.ndarray:
        return np.argmax(self.predict_proba(X), axis=1)

    def predict_log_proba(self, X) -> np.ndarray:
        return np.log(self.predict_proba(X))   # already clipped to >= 1e-8

    def __getattr__(self, name):
        # Guard against recursion during unpickling before instance dict is ready
        if name in ("raw_model", "n_classes", "_isos"):
            raise AttributeError(name)
        return getattr(self.raw_model, name)

    def __reduce__(self):
        # Pin the canonical module so joblib can load from any entry point
        # (gunicorn, flask, pytest) — not just when run as __main__
        return (_load_isotonic_calibrator, (self.raw_model, self.n_classes, self._isos))


def _load_isotonic_calibrator(raw_model, n_classes, isos):
    obj           = IsotonicCalibrator.__new__(IsotonicCalibrator)
    obj.raw_model = raw_model
    obj.n_classes = n_classes
    obj._isos     = isos
    return obj


# ── Brier score helpers ───────────────────────────────────────────────────────

def _brier_binary(model, X, y) -> float:
    from sklearn.metrics import brier_score_loss
    return float(brier_score_loss(y, model.predict_proba(X)[:, 1]))


def _brier_multiclass(model, X, y, n_classes: int) -> float:
    from sklearn.metrics import brier_score_loss
    proba = model.predict_proba(X)
    return float(np.mean([
        brier_score_loss((y == i).astype(int), proba[:, i])
        for i in range(n_classes)
    ]))


# ── Split helpers ─────────────────────────────────────────────────────────────

def _val_test_from_artifact(
    df: pd.DataFrame,
    art: dict,
    time_based_split_fn=None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Return (val_df, test_df) for calibration.

    Priority:
      1. Use val_cutoff + test_cutoff stored in the artifact (all new-format
         training scripts). These are the EXACT same splits the training script
         used, so the calibrator sees rows the model genuinely never trained on.
      2. Fallback (old artifacts): apply time_based_split_fn to get the 2-way
         test fold, then split it in half chronologically — first half calibrates,
         second half evaluates.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    if "val_cutoff" in art and "test_cutoff" in art:
        val_cutoff  = pd.Timestamp(str(art["val_cutoff"]))
        test_cutoff = pd.Timestamp(str(art["test_cutoff"]))
        val_df  = df[(df["date"] >= val_cutoff) & (df["date"] < test_cutoff)]
        test_df = df[df["date"] >= test_cutoff]
        return val_df, test_df

    if time_based_split_fn is None:
        raise ValueError("Artifact has no cutoff metadata and no split function supplied")

    _, fold_df, _ = time_based_split_fn(df)
    mid  = len(fold_df) // 2
    return fold_df.iloc[:mid].copy(), fold_df.iloc[mid:].copy()


# ── Core calibrate-and-save ───────────────────────────────────────────────────

def _calibrate_and_save(
    raw_model,
    X_val, y_val,
    X_test, y_test,
    artifact: dict,
    raw_path: Path,
    n_classes: int = 2,
) -> dict:
    """
    Calibrate on (X_val, y_val); report Brier on (X_test, y_test).
    Strategy:
      1. Try IsotonicCalibrator (flexible, needs ~500+ samples).
      2. If isotonic doesn't improve Brier, fall back to PlattCalibrator
         (temperature scaling — single-parameter, stable on small val sets).
      3. Save whichever improves Brier the most.
    """
    multiclass = n_classes > 2
    brier_fn   = (_brier_multiclass if multiclass else _brier_binary)

    brier_before = (
        brier_fn(raw_model, X_test, y_test, n_classes)
        if multiclass else brier_fn(raw_model, X_test, y_test)
    )

    best_cal    = None
    best_brier  = brier_before
    best_method = "none"

    # Attempt 1: isotonic regression
    try:
        iso = IsotonicCalibrator(raw_model, n_classes=n_classes)
        iso.fit(X_val, y_val)
        iso_brier = (
            brier_fn(iso, X_test, y_test, n_classes)
            if multiclass else brier_fn(iso, X_test, y_test)
        )
        if iso_brier < best_brier:
            best_cal, best_brier, best_method = iso, iso_brier, "isotonic"
    except Exception:
        iso_brier = None

    # Attempt 2: Platt / temperature scaling (binary only — more stable on small data)
    if not multiclass:
        try:
            platt = PlattCalibrator(raw_model)
            platt.fit(X_val, y_val)
            platt_brier = brier_fn(platt, X_test, y_test)
            if platt_brier < best_brier:
                best_cal, best_brier, best_method = platt, platt_brier, "platt"
        except Exception:
            platt_brier = None

    calib_path = Path(raw_path).with_name(Path(raw_path).stem + "_calibrated.joblib")
    if best_cal is None:
        # Neither method improved — save raw model as calibrated artifact so
        # downstream loaders still get a consistent file.
        best_method = "none (raw preferred)"
        best_cal = raw_model

    calib_meta = {
        "calibration_method":    best_method,
        "n_calibration_samples": int(len(y_val)),
        "n_evaluation_samples":  int(len(y_test)),
        "calibrated_at":         datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "brier_before":          round(brier_before, 4),
        "brier_after":           round(best_brier, 4),
        "brier_improvement":     round(brier_before - best_brier, 4),
    }
    joblib.dump({**artifact, "model": best_cal, "calibrated": True, **calib_meta}, calib_path)

    return {
        "brier_before":     round(brier_before, 4),
        "brier_after":      round(best_brier, 4),
        "improvement":      round(brier_before - best_brier, 4),
        "calibration_method": best_method,
        "n_val_samples":    int(len(y_val)),
        "n_test_samples":   int(len(y_test)),
        "calibrated_path":  str(calib_path),
    }


# ── Per-model calibration functions ──────────────────────────────────────────

def calibrate_regime_classifier():
    raw_path = _MODELS_DIR / "regime_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_regime_classifier import (
            load_labeled_data, build_feature_matrix, time_based_split, TARGET_COL,
        )
        art = joblib.load(raw_path)
        df  = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}

        val_df, test_df = _val_test_from_artifact(df, art, time_based_split)
        enc = art["feature_encoders"]
        X_val,  _ = build_feature_matrix(val_df,  encoders=enc, fit=False)
        X_test, _ = build_feature_matrix(test_df, encoders=enc, fit=False)
        le        = art["label_encoder"]
        y_val     = le.transform(val_df[TARGET_COL])
        y_test    = le.transform(test_df[TARGET_COL])

        result = _calibrate_and_save(
            art["model"], X_val, y_val, X_test, y_test, art, raw_path,
            n_classes=len(le.classes_),
        )
        return {"ok": True, "model": "regime_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_direction_classifier():
    raw_path = _MODELS_DIR / "direction_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_direction_model import (
            load_labeled_data, build_feature_matrix, time_based_split,
        )
        art = joblib.load(raw_path)
        df  = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}

        val_df, test_df = _val_test_from_artifact(df, art, time_based_split)
        enc = art["feature_encoders"]
        X_val,  _ = build_feature_matrix(val_df,  encoders=enc, fit=False)
        X_test, _ = build_feature_matrix(test_df, encoders=enc, fit=False)
        y_val  = (val_df["forward_return"]  > 0).astype(int).values
        y_test = (test_df["forward_return"] > 0).astype(int).values

        result = _calibrate_and_save(
            art["model"], X_val, y_val, X_test, y_test, art, raw_path,
        )
        return {"ok": True, "model": "direction_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_iv_direction_classifier():
    raw_path = _MODELS_DIR / "iv_direction_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_iv_direction_model import (
            load_labeled_data, build_feature_matrix, time_based_split,
        )
        art = joblib.load(raw_path)
        df  = load_labeled_data()
        if df.empty:
            return {"ok": False, "error": "No labeled data"}

        val_df, test_df = _val_test_from_artifact(df, art, time_based_split)
        enc = art.get("feature_encoders") or {}
        X_val,  _ = build_feature_matrix(val_df,  encoders=enc, fit=False)
        X_test, _ = build_feature_matrix(test_df, encoders=enc, fit=False)
        y_val  = val_df["iv_expanding"].values.astype(int)
        y_test = test_df["iv_expanding"].values.astype(int)

        result = _calibrate_and_save(
            art["model"], X_val, y_val, X_test, y_test, art, raw_path,
        )
        return {"ok": True, "model": "iv_direction_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_meta_ensemble():
    raw_path = _MODELS_DIR / "meta_ensemble.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet"}
    try:
        from scripts.train_meta_ensemble import _load_base_models, build_meta_dataset
        from scripts.db import read_df, TABLE

        art      = joblib.load(raw_path)
        meta_cutoff = pd.Timestamp(str(art["meta_cutoff"]))

        df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
        df["date"] = pd.to_datetime(df["date"])
        meta_df = df[df["date"] >= meta_cutoff].copy()
        if len(meta_df) < 100:
            return {"ok": False, "error": f"Only {len(meta_df)} meta rows (need 100+)"}

        models = _load_base_models()
        X_meta, y_meta = build_meta_dataset(meta_df, models)
        n       = len(X_meta)
        val_end = int(n * 0.50)          # first 50% → calibration val
        # second 50% → held-out evaluation (meta already starts at post-cutoff)
        X_val   = X_meta.iloc[:val_end];   y_val  = y_meta.iloc[:val_end].values
        X_test  = X_meta.iloc[val_end:];   y_test = y_meta.iloc[val_end:].values

        result = _calibrate_and_save(
            art["model"], X_val, y_val, X_test, y_test, art, raw_path,
        )
        return {"ok": True, "model": "meta_ensemble", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_pop_classifier():
    raw_path = _MODELS_DIR / "pop_classifier.joblib"
    if not raw_path.exists():
        return {"ok": False, "error": "Not trained yet (waiting for labeled paper trade outcomes)"}
    try:
        from scripts.train_pop_model import (
            load_dataset, build_feature_matrix, time_based_split,
        )
        art = joblib.load(raw_path)
        df  = load_dataset()
        if df is None or df.empty:
            return {"ok": False, "error": "No labeled paper trade data"}

        val_df, test_df = _val_test_from_artifact(df, art, time_based_split)
        enc = art["feature_encoders"]
        X_val,  _ = build_feature_matrix(val_df,  encoders=enc, fit=False)
        X_test, _ = build_feature_matrix(test_df, encoders=enc, fit=False)
        y_val  = val_df["win"].astype(int).values
        y_test = test_df["win"].astype(int).values

        result = _calibrate_and_save(
            art["model"], X_val, y_val, X_test, y_test, art, raw_path,
        )
        return {"ok": True, "model": "pop_classifier", **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def calibrate_all() -> dict:
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
    print("Calibrating all classifiers…\n")
    results = calibrate_all()
    for name, r in results.items():
        if not r.get("ok"):
            print(f"  {name:35s}  SKIP  {r.get('error', '')}")
        else:
            before = r["brier_before"]
            after  = r["brier_after"]
            delta  = r["improvement"]
            sign   = "-" if delta > 0 else "+"
            print(
                f"  {name:35s}  "
                f"Brier {before:.4f} → {after:.4f}  ({sign}{abs(delta):.4f})  "
                f"[cal n={r['n_val_samples']}  test n={r['n_test_samples']}]"
            )
    print("\nDone. Calibrated artifacts saved to data/models/")
