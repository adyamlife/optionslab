"""
Meta-Ensemble Trade Scorer — combines all base model outputs into one
composite ML confidence score (0–100) per ticker.

Training methodology — chronologically held-out meta-learner:
  Each base model was trained on its own chronological slice of data.
  The meta-learner is trained ONLY on rows after the LATEST of those cutoffs
  (derived dynamically from the artifacts — no hardcoded date), which were
  never seen during any base model's training. Running base-model inference
  on those rows gives truly out-of-sample predictions.

  This is NOT textbook k-fold stacking (which produces OOF predictions for
  every training row by re-training base models on each fold). It is instead
  a chronologically held-out meta-learner — appropriate for financial time
  series where the base models themselves would leak if re-trained on
  rolled-forward folds. The "no leakage" claim holds within the period after
  the cutoff; rows before it are simply unavailable to the meta-learner.

Meta-features (9):
  p_uptrend, p_downtrend, p_rangebound   — regime classifier probabilities
  expected_return                         — return regressor
  expected_vol                            — volatility regressor
  p_up                                    — direction classifier
  iv_expanding_prob                       — IV direction classifier
  regime_entropy                          — entropy of regime probs (how certain is regime model)
  pred_std                                — std of directional probs (how much models DISAGREE)

  regime_entropy near 0 → regime model very confident; near 1.1 → maximum uncertainty.
  pred_std near 0 → all classifiers agree; near 0.5 → strong disagreement.
  These two features let the stacker learn to shade its output by model agreement.

Target:
  forward_return > 0  (1 = up, 0 = down/flat)
  — The unified trade score is P(up) × 100.
  — Score >60 = bullish lean, <40 = bearish lean, 40–60 = neutral/uncertain.

Run standalone: python -m scripts.train_meta_ensemble
Output: data/models/meta_ensemble.joblib
"""
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, brier_score_loss, classification_report, roc_auc_score
from xgboost import XGBClassifier

log = logging.getLogger(__name__)

_ROOT       = Path(__file__).resolve().parent.parent
_MODEL_PATH = _ROOT / "data" / "models" / "meta_ensemble.joblib"
_MODELS_DIR = _ROOT / "data" / "models"

# Fallback cutoff used only when base-model artifacts don't expose their cutoffs.
_META_CUTOFF_FALLBACK = pd.Timestamp("2026-02-20")

# Base meta-features — output of each base model
_BASE_META_FEATURES = [
    "p_uptrend", "p_downtrend", "p_rangebound",
    "expected_return", "expected_vol",
    "p_up", "iv_expanding_prob",
]

# Derived agreement/entropy features — computed from base outputs
_DERIVED_META_FEATURES = [
    "regime_entropy",  # entropy of regime probs — how uncertain is the regime model
    "pred_std",        # std of directional probs — how much models disagree on direction
]

META_FEATURES = _BASE_META_FEATURES + _DERIVED_META_FEATURES

# Neutral fill values when a base model is unavailable or gated
_META_FILLNA = {
    "p_uptrend":        1 / 3,   # uniform regime distribution
    "p_downtrend":      1 / 3,
    "p_rangebound":     1 / 3,
    "expected_return":  0.0,     # neutral return expectation
    "expected_vol":     0.0,     # 0 signals "not available" to downstream
    "p_up":             0.5,     # maximum uncertainty on direction
    "iv_expanding_prob": 0.5,
    "regime_entropy":   1.0,     # max entropy when regime data missing
    "pred_std":         0.0,
}

_BASE_MODEL_PATHS = {
    "regime":          _MODELS_DIR / "regime_classifier.joblib",
    "regime_catboost": _MODELS_DIR / "regime_catboost.joblib",
    "return":          _MODELS_DIR / "return_regressor.joblib",
    "vol":             _MODELS_DIR / "volatility_regressor.joblib",
    "direction":       _MODELS_DIR / "direction_classifier.joblib",
    "iv_direction":    _MODELS_DIR / "iv_direction_classifier.joblib",
}

_OPTIONAL_BASE_MODELS = {"regime_catboost"}


def _load_base_models() -> dict:
    models = {}
    for name, path in _BASE_MODEL_PATHS.items():
        if not path.exists():
            if name in _OPTIONAL_BASE_MODELS:
                continue
            raise FileNotFoundError(
                f"Base model '{name}' not trained yet ({path}). "
                "Train all 5 base models before running the meta-ensemble."
            )
        models[name] = joblib.load(path)
    return models


def _derive_meta_cutoff(models: dict) -> pd.Timestamp:
    """
    Derive the meta-learner cutoff dynamically from base-model artifacts.
    Uses the LATEST cutoff across all required base models, so the meta-learner
    only sees rows that were genuinely out-of-sample for every base model.
    Falls back to _META_CUTOFF_FALLBACK only when artifacts don't expose cutoffs.
    """
    cutoffs = []
    for name, art in models.items():
        if name in _OPTIONAL_BASE_MODELS:
            continue
        for key in ("test_cutoff", "split_cutoff", "val_cutoff"):
            val = art.get(key)
            if val:
                try:
                    cutoffs.append(pd.Timestamp(str(val)))
                    break
                except Exception:
                    pass
    if not cutoffs:
        log.warning(
            "[MetaEnsemble] Could not derive cutoff from model artifacts; "
            "falling back to %s", _META_CUTOFF_FALLBACK
        )
        return _META_CUTOFF_FALLBACK
    cutoff = max(cutoffs)
    log.info("[MetaEnsemble] Derived meta-cutoff: %s (from %d base-model artifacts)", cutoff.date(), len(cutoffs))
    return cutoff


def _build_X_batch(df: pd.DataFrame, artifact: dict) -> pd.DataFrame:
    """
    Build the full feature matrix for an entire DataFrame in one shot —
    same logic as regime_predictor._build_X() but vectorized over all rows.
    Avoids per-row DataFrame construction (10-100× faster for large datasets).
    """
    from scripts.regime_predictor import _CATEGORICAL_COLS
    encoders     = artifact.get("feature_encoders") or {}
    feature_cols = artifact.get("feature_cols") or []
    numeric_cols = [c for c in feature_cols if c not in _CATEGORICAL_COLS]

    X = pd.DataFrame(index=df.index)
    for c in numeric_cols:
        X[c] = pd.to_numeric(df.get(c, np.nan), errors="coerce")

    for col in _CATEGORICAL_COLS:
        if col not in feature_cols:
            continue
        vals = df[col].astype(str) if col in df.columns else pd.Series(
            ["unknown"] * len(df), index=df.index
        )
        enc = encoders.get(col)
        if enc is None:
            X[col] = 0
        else:
            known = set(enc.classes_)
            safe  = vals.map(lambda v: v if v in known else enc.classes_[0])
            X[col] = enc.transform(safe)

    cat_cols_in_artifact = [c for c in feature_cols if c in _CATEGORICAL_COLS]
    return X[numeric_cols + cat_cols_in_artifact]


def build_meta_dataset(df: pd.DataFrame, models: dict) -> tuple[pd.DataFrame, pd.Series]:
    """
    Run batched inference through all base models on df at once.
    Returns (X_meta, y) with META_FEATURES columns.
    NaN cells are filled with _META_FILLNA before returning so downstream
    code always gets a fully-populated matrix.
    """
    results: dict[str, np.ndarray] = {}

    # ── Regime classifier ─────────────────────────────────────────────────────
    art = models["regime"]
    X   = _build_X_batch(df, art)
    proba   = art["model"].predict_proba(X).astype(float)
    classes = art["label_encoder"].classes_

    cb_regime = models.get("regime_catboost")
    if cb_regime is not None:
        try:
            from scripts.train_regime_classifier import build_catboost_matrix as _rcb_feat
            X_rcb    = _rcb_feat(df)
            cb_proba = cb_regime["model"].predict_proba(X_rcb).astype(float)
            proba    = (proba + cb_proba) / 2.0
        except Exception as e:
            log.warning("[MetaEnsemble] CatBoost regime averaging failed (%s) — using XGB only", e)

    results["p_uptrend"]    = proba[:, list(classes).index("Uptrend")]
    results["p_downtrend"]  = proba[:, list(classes).index("Downtrend")]
    results["p_rangebound"] = proba[:, list(classes).index("Range-bound")]

    # ── Regime entropy: -Σ p·log(p) — how certain is the regime model ────────
    eps            = 1e-10
    regime_proba   = np.stack([results["p_uptrend"], results["p_downtrend"],
                                results["p_rangebound"]], axis=1)
    results["regime_entropy"] = -np.sum(regime_proba * np.log(regime_proba + eps), axis=1)

    # ── Return regressor (config-gated) ──────────────────────────────────────
    art        = models.get("return")
    _use_return = False
    if art is not None:
        try:
            from scripts.regime_predictor import _load_ml_config as _ml_cfg_fn
            _ml_cfg = _ml_cfg_fn()
            stored_r2 = art.get("r2", -999.0)
            _use_return = (
                _ml_cfg["return_regressor_enabled"]
                or stored_r2 >= _ml_cfg["return_regressor_r2_threshold"]
            )
        except Exception:
            pass
    if _use_return:
        if "dummy_cols" in art:
            from scripts.train_return_model import build_feature_matrix as _ret_feat, compute_lag_features
            df_lag    = compute_lag_features(df.copy())
            X_ret, _  = _ret_feat(df_lag, dummy_cols=art["dummy_cols"], fit=False)
            results["expected_return"] = art["model"].predict(X_ret).astype(float)
        else:
            X = _build_X_batch(df, art)
            results["expected_return"] = art["model"].predict(X).astype(float)
    else:
        results["expected_return"] = np.zeros(len(df))

    # ── Volatility regressor ──────────────────────────────────────────────────
    art = models["vol"]
    X   = _build_X_batch(df, art)
    results["expected_vol"] = art["model"].predict(X).astype(float)

    # ── Direction classifier ──────────────────────────────────────────────────
    art = models["direction"]
    X   = _build_X_batch(df, art)
    results["p_up"] = art["model"].predict_proba(X)[:, 1].astype(float)

    # ── IV direction classifier (v2: dummy_cols; v1: feature_encoders) ────────
    art = models["iv_direction"]
    if "dummy_cols" in art:
        from scripts.train_iv_direction_model import build_feature_matrix as _iv_feat
        X_iv, _ = _iv_feat(df, encoders=art.get("feature_encoders") or {}, fit=False)
    else:
        X_iv = _build_X_batch(df, art)
    results["iv_expanding_prob"] = art["model"].predict_proba(X_iv)[:, 1].astype(float)

    # ── Directional disagreement: std of bullish-indicator probs ─────────────
    # p_uptrend, p_up, iv_expanding_prob are all "probability of bullish outcome"
    # Low std → models agree; high std → models conflict
    direction_probs      = np.stack([results["p_uptrend"], results["p_up"],
                                      results["iv_expanding_prob"]], axis=1)
    results["pred_std"]  = np.std(direction_probs, axis=1)

    # Build X_meta and fill NaN so XGBoost always sees a complete matrix
    X_meta = pd.DataFrame({f: results[f] for f in META_FEATURES}, index=df.index)
    X_meta = X_meta.fillna(_META_FILLNA)

    y = (df["forward_return"] > 0).astype(int).values
    return X_meta, pd.Series(y, index=df.index)


def train(data_path=None, out_path=_MODEL_PATH) -> dict:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=["forward_return", "rsi", "adx", "hv20"])
    df["date"] = pd.to_datetime(df["date"])

    models      = _load_base_models()
    meta_cutoff = _derive_meta_cutoff(models)

    meta_df = df[df["date"] >= meta_cutoff].copy()
    if len(meta_df) < 500:
        return {
            "ok":   False,
            "error": (
                f"Only {len(meta_df)} held-out rows available after cutoff "
                f"{meta_cutoff.date()} (need 500+). "
                "This improves as more data accumulates past the base-model cutoffs."
            ),
            "meta_cutoff": str(meta_cutoff.date()),
        }

    log.info("[MetaEnsemble] Building meta-feature matrix from %d held-out rows…", len(meta_df))
    X_meta, y_meta = build_meta_dataset(meta_df, models)

    if len(X_meta) < 200:
        return {"ok": False, "error": f"Too few valid meta-rows after inference ({len(X_meta)})"}

    # ── Three-way split: 70% train / 15% val (calibration) / 15% test ────────
    n          = len(X_meta)
    val_split  = int(n * 0.70)
    test_split = int(n * 0.85)
    X_train, X_val, X_test = (X_meta.iloc[:val_split],
                               X_meta.iloc[val_split:test_split],
                               X_meta.iloc[test_split:])
    y_train, y_val, y_test = (y_meta.iloc[:val_split],
                               y_meta.iloc[val_split:test_split],
                               y_meta.iloc[test_split:])

    # ── Shallow stacker — low depth prevents overfitting on 7 meta-features ──
    model = XGBClassifier(
        n_estimators=100, max_depth=2, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        objective="binary:logistic", eval_metric="logloss",
        random_state=42, n_jobs=-1,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    acc    = float(accuracy_score(y_test, y_pred))
    auc    = float(roc_auc_score(y_test, y_prob)) if len(np.unique(y_test)) > 1 else None
    report = classification_report(y_test, y_pred,
                                   target_names=["Down", "Up"], output_dict=True)

    # ── Baselines ─────────────────────────────────────────────────────────────
    up_pct_test          = float(y_test.mean())
    naive_majority_acc   = max(up_pct_test, 1 - up_pct_test)     # always predict majority class
    baseline_p_up_acc    = float(accuracy_score(y_test, (X_test["p_up"] >= 0.5).astype(int)))
    baseline_regime_acc  = float(accuracy_score(y_test, (X_test["p_uptrend"] >= 0.5).astype(int)))

    # ── Meta-feature statistics for SHAP / explainability ────────────────────
    meta_feature_stats = {
        f: {"mean": round(float(X_meta[f].mean()), 4),
            "std":  round(float(X_meta[f].std()), 4)}
        for f in META_FEATURES
    }

    feature_importances = dict(zip(META_FEATURES, model.feature_importances_.tolist()))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model":               model,
        "meta_features":       META_FEATURES,
        "meta_cutoff":         str(meta_cutoff.date()),
        "meta_feature_stats":  meta_feature_stats,
        "train_rows":          len(X_train),
        "val_rows":            len(X_val),
        "test_rows":           len(X_test),
        "accuracy":            round(acc, 4),
        "auc":                 round(auc, 4) if auc is not None else None,
    }
    joblib.dump(artifact, out_path)

    # ── Calibrate on val (not test) ───────────────────────────────────────────
    brier_before = brier_after = None
    try:
        brier_before = float(brier_score_loss(y_test, y_prob))
        from scripts.calibrate_models import IsotonicCalibrator
        cal_model = IsotonicCalibrator(model)
        cal_model.fit(X_val, y_val)
        brier_after = float(brier_score_loss(y_test, cal_model.predict_proba(X_test)[:, 1]))
        joblib.dump({**artifact, "model": cal_model, "calibrated": True,
                     "brier_before": round(brier_before, 4),
                     "brier_after":  round(brier_after, 4)},
                    out_path.with_name(out_path.stem + "_calibrated.joblib"))
    except Exception as e:
        log.warning("Calibration failed: %s", e)

    return {
        "ok":                  True,
        "meta_cutoff":         str(meta_cutoff.date()),
        "accuracy":            round(acc, 4),
        "auc":                 round(auc, 4) if auc is not None else None,
        "naive_majority_acc":  round(naive_majority_acc, 4),
        "vs_direction_only":   round(baseline_p_up_acc, 4),
        "vs_regime_only":      round(baseline_regime_acc, 4),
        "beats_majority":      acc > naive_majority_acc,
        "beats_direction":     acc > baseline_p_up_acc,
        "beats_regime":        acc > baseline_regime_acc,
        "train_rows":          len(X_train),
        "val_rows":            len(X_val),
        "test_rows":           len(X_test),
        "up_pct_in_test":      round(up_pct_test, 4),
        "feature_importances": feature_importances,
        "meta_feature_stats":  meta_feature_stats,
        "classification_report": report,
        "model_path":          str(out_path),
        "brier_before": round(brier_before, 4) if brier_before is not None else None,
        "brier_after":  round(brier_after, 4)  if brier_after  is not None else None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)

    print(f"\nMeta-cutoff (derived from base models): {result['meta_cutoff']}")
    print(f"Accuracy : {result['accuracy']}  |  AUC : {result['auc']}")
    print(f"\nBaselines (test set, Up%={result['up_pct_in_test']:.1%}):")
    print(f"  Majority-class naive : {result['naive_majority_acc']:.4f}  beats: {result['beats_majority']}")
    print(f"  Direction model only : {result['vs_direction_only']:.4f}  beats: {result['beats_direction']}")
    print(f"  Regime model only    : {result['vs_regime_only']:.4f}  beats: {result['beats_regime']}")
    print(f"\nRows — train: {result['train_rows']}  val: {result['val_rows']}  test: {result['test_rows']}")

    if result.get("brier_before") is not None:
        print(f"\nBrier before calibration: {result['brier_before']} → after: {result['brier_after']}")

    print("\nMeta-feature importances:")
    for f, imp in sorted(result["feature_importances"].items(), key=lambda x: -x[1]):
        stats = result["meta_feature_stats"][f]
        print(f"  {f:<22} importance={imp:.3f}  mean={stats['mean']:.3f}  std={stats['std']:.3f}")

    print(f"\nModel saved to {result['model_path']}")
