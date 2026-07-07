"""
Meta-Ensemble Trade Scorer — stacks all 5 trained model outputs into one
composite ML confidence score (0–100) per ticker.

Why stacking works here:
  The 5 base models (regime classifier, return regressor, volatility regressor,
  direction classifier, IV direction classifier) each capture a different
  dimension of the market. Their errors are partially independent: the regime
  model gets regime right but misses short-term direction; the direction model
  gets direction right but doesn't know IV; the IV model gets IV right but is
  agnostic to price direction. A stacker trained on their COMBINED outputs can
  learn when they agree (higher confidence) vs. when they conflict (lower
  confidence), which no individual model can see.

Training methodology — hold-out stacking (no data leakage):
  Each base model was trained on data before its split cutoff (~Feb 12-20 2026).
  The meta-learner is trained ONLY on rows AFTER the latest cutoff (2026-02-20),
  which were never seen during any base model's training. Running base-model
  inference on those rows gives out-of-sample predictions — the meta-learner
  learns to reweight them without overfitting to the base models' training data.

Meta-features (7):
  p_uptrend, p_downtrend, p_rangebound   — regime classifier probabilities
  expected_return                         — return regressor
  expected_vol                            — volatility regressor
  p_up                                    — direction classifier
  iv_expanding_prob                       — IV direction classifier

Target:
  forward_return > 0  (1 = up, 0 = down/flat)
  — The unified trade score is P(up) from this stacker, expressed as 0–100.
  — Score >60 = bullish ML lean, <40 = bearish lean, 40–60 = neutral/uncertain.

Run standalone: python -m scripts.train_meta_ensemble
Output: data/models/meta_ensemble.joblib
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report, roc_auc_score
from xgboost import XGBClassifier

_ROOT = Path(__file__).resolve().parent.parent
_DATA_PATH   = _ROOT / "data" / "regime_training.csv"
_MODEL_PATH  = _ROOT / "data" / "models" / "meta_ensemble.joblib"
_MODELS_DIR  = _ROOT / "data" / "models"

# Latest cutoff across all base models — data after this is fully held-out.
_META_CUTOFF = pd.Timestamp("2026-02-20")

META_FEATURES = [
    "p_uptrend", "p_downtrend", "p_rangebound",
    "expected_return", "expected_vol",
    "p_up", "iv_expanding_prob",
]


_BASE_MODEL_PATHS = {
    "regime":       _MODELS_DIR / "regime_classifier.joblib",
    "return":       _MODELS_DIR / "return_regressor.joblib",
    "vol":          _MODELS_DIR / "volatility_regressor.joblib",
    "direction":    _MODELS_DIR / "direction_classifier.joblib",
    "iv_direction": _MODELS_DIR / "iv_direction_classifier.joblib",
}


def _load_base_models() -> dict:
    """Load all trained base models. Returns dict of {name: artifact}."""
    models = {}
    for name, path in _BASE_MODEL_PATHS.items():
        if not path.exists():
            raise FileNotFoundError(
                f"Base model '{name}' not trained yet ({path}). "
                f"Train all 5 base models before running the meta-ensemble."
            )
        models[name] = joblib.load(path)
    return models


def _build_X_batch(df: pd.DataFrame, artifact: dict) -> pd.DataFrame:
    """
    Build the full feature matrix for an entire DataFrame in one shot —
    same logic as regime_predictor._build_X() but vectorized over all rows.

    Column order must match training: numerics first, then categoricals in the
    order they appear in artifact["feature_cols"] — exactly what _build_X does
    for single rows. Replicating it here avoids 7,553 per-row DataFrame
    constructions and cuts build_meta_dataset from ~15 min to ~5 s.
    """
    from scripts.regime_predictor import _CATEGORICAL_COLS
    encoders    = artifact.get("feature_encoders") or {}
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
    Run batched inference through all 5 base models on the full df at once.
    Each model gets one predict_proba/predict call over all rows — no per-row loop.
    Returns (X_meta, y) with META_FEATURES columns.
    """
    results: dict[str, np.ndarray] = {}

    # Regime classifier — 3 probability columns
    art = models["regime"]
    X = _build_X_batch(df, art)
    proba = art["model"].predict_proba(X)
    classes = art["label_encoder"].classes_
    for i, cls in enumerate(classes):
        results[f"p_{cls.lower().replace('-','').replace(' ','')}"] = proba[:, i]
    # Normalise keys to fixed names regardless of class order
    results["p_uptrend"]    = proba[:, list(classes).index("Uptrend")]
    results["p_downtrend"]  = proba[:, list(classes).index("Downtrend")]
    results["p_rangebound"] = proba[:, list(classes).index("Range-bound")]

    # Return regressor
    art = models["return"]
    X = _build_X_batch(df, art)
    results["expected_return"] = art["model"].predict(X).astype(float)

    # Volatility regressor
    art = models["vol"]
    X = _build_X_batch(df, art)
    results["expected_vol"] = art["model"].predict(X).astype(float)

    # Direction classifier
    art = models["direction"]
    X = _build_X_batch(df, art)
    results["p_up"] = art["model"].predict_proba(X)[:, 1]

    # IV direction classifier
    art = models["iv_direction"]
    X = _build_X_batch(df, art)
    results["iv_expanding_prob"] = art["model"].predict_proba(X)[:, 1]

    X_meta = pd.DataFrame({f: results[f] for f in META_FEATURES}, index=df.index)
    y      = (df["forward_return"] > 0).astype(int).values
    return X_meta, pd.Series(y, index=df.index)


def train(data_path=None, out_path=_MODEL_PATH) -> dict:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=["forward_return", "rsi", "adx", "hv20"])

    df["date"] = pd.to_datetime(df["date"])

    # Meta-learner is trained only on the held-out portion (after all base-model cutoffs)
    meta_df = df[df["date"] >= _META_CUTOFF].copy()
    if len(meta_df) < 500:
        return {
            "ok": False,
            "error": (
                f"Only {len(meta_df)} held-out rows available (need 500+). "
                f"This improves as more data accumulates past the base-model cutoffs."
            ),
        }

    print(f"Building meta-feature matrix from {len(meta_df)} held-out rows…")
    models = _load_base_models()
    X_meta, y_meta = build_meta_dataset(meta_df, models)

    if len(X_meta) < 200:
        return {"ok": False, "error": f"Too few valid meta-rows after inference ({len(X_meta)})"}

    # Time-based split within the meta dataset (80/20 within the held-out window)
    n = len(X_meta)
    split = int(n * 0.8)
    X_train, X_test = X_meta.iloc[:split], X_meta.iloc[split:]
    y_train, y_test = y_meta.iloc[:split], y_meta.iloc[split:]

    # Shallow XGBoost — small depth to avoid overfitting on the meta-features
    model = XGBClassifier(
        n_estimators=100,
        max_depth=2,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="binary:logistic",
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    acc  = float(accuracy_score(y_test, y_pred))
    auc  = float(roc_auc_score(y_test, y_prob))
    report = classification_report(y_test, y_pred,
                                   target_names=["Down", "Up"], output_dict=True)

    # Naive baselines: does the stacker beat each individual model?
    baseline_p_up_acc  = float(accuracy_score(y_test, (X_test["p_up"] >= 0.5).astype(int)))
    baseline_regime_acc = float(accuracy_score(y_test, (X_test["p_uptrend"] >= 0.5).astype(int)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model":          model,
        "meta_features":  META_FEATURES,
        "meta_cutoff":    str(_META_CUTOFF),
        "train_rows":     len(X_train),
        "test_rows":      len(X_test),
    }, out_path)

    return {
        "ok":                True,
        "accuracy":          round(acc, 4),
        "auc":               round(auc, 4),
        "vs_direction_only": round(baseline_p_up_acc, 4),
        "vs_regime_only":    round(baseline_regime_acc, 4),
        "beats_direction":   acc > baseline_p_up_acc,
        "beats_regime":      acc > baseline_regime_acc,
        "train_rows":        len(X_train),
        "test_rows":         len(X_test),
        "up_pct_in_test":    round(float(y_test.mean()), 4),
        "feature_importances": dict(zip(META_FEATURES, model.feature_importances_.tolist())),
        "classification_report": report,
        "model_path":        str(out_path),
    }


if __name__ == "__main__":
    print("Training meta-ensemble…")
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"Accuracy : {result['accuracy']}  |  AUC : {result['auc']}")
    print(f"vs. direction-only baseline : {result['vs_direction_only']}  (beats: {result['beats_direction']})")
    print(f"vs. regime-only baseline    : {result['vs_regime_only']}  (beats: {result['beats_regime']})")
    print(f"Train rows: {result['train_rows']}  |  Test rows: {result['test_rows']}")
    print(f"Test Up% : {result['up_pct_in_test']:.1%}")
    print("\nFeature importances (stacker weights):")
    for f, imp in sorted(result["feature_importances"].items(), key=lambda x: -x[1]):
        print(f"  {f}: {imp:.3f}")
    print(f"\nModel saved to {result['model_path']}")
