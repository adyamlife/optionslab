"""
Optuna hyperparameter tuning for all XGBoost models in the pipeline.

Runs TPE search over the XGBoost search space for each model, saves best
params + metadata to data/models/best_params.json, then optionally re-trains
so the saved joblib immediately reflects the tuned model.

Each training script calls load_best_params() at startup and merges the
result with its own defaults. load_best_params() always injects _XGB_DEFAULTS
(random_state, tree_method, n_jobs) so callers never have to repeat them.

Tuning methodology — TimeSeriesSplit CV with early stopping:
  For each trial, we apply sklearn.model_selection.TimeSeriesSplit with
  _N_CV_FOLDS folds to the full labeled dataset. Early stopping
  (_EARLY_STOPPING_ROUNDS) is used on every fold's eval_set so XGBoost
  determines the actual n_estimators automatically — n_estimators in the model
  is the max cap, not a tuned parameter. Optuna's objective is the mean
  CV score (log-loss for classifiers, RMSE for regressors) across folds.

  This produces parameters that generalise much better than a single train/test
  split, and early stopping makes each trial faster and less prone to overfitting.

Usage:
    python -m scripts.tune_hyperparams                    # all models, 50 trials each
    python -m scripts.tune_hyperparams --model regime     # one model
    python -m scripts.tune_hyperparams --trials 100       # more trials
    python -m scripts.tune_hyperparams --no-retrain       # tune only, skip final retrain
"""
import argparse
import json
import logging
from pathlib import Path

import numpy as np
import optuna
from sklearn.model_selection import TimeSeriesSplit

optuna.logging.set_verbosity(optuna.logging.WARNING)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_ROOT        = Path(__file__).resolve().parent.parent
_PARAMS_PATH = _ROOT / "data" / "models" / "best_params.json"

MODELS = ["regime", "direction", "return", "volatility"]

_N_CV_FOLDS          = 5
_N_ESTIMATORS_MAX    = 800
_EARLY_STOPPING_ROUNDS = 30

# Defaults baked into every XGBoost model regardless of tuning.
# load_best_params() merges these so callers never have to remember them.
_XGB_DEFAULTS = {
    "random_state": 42,
    "tree_method":  "hist",
    "n_jobs":       -1,
    "n_estimators": _N_ESTIMATORS_MAX,
}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _load_best_params() -> dict:
    if _PARAMS_PATH.exists():
        return json.loads(_PARAMS_PATH.read_text())
    return {}


def _save_best_params(all_params: dict) -> None:
    _PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PARAMS_PATH.write_text(json.dumps(all_params, indent=2))


def _xgb_search_space(trial: optuna.Trial, *, classifier: bool = False) -> dict:
    """XGBoost hyperparameter search space shared across all models.
    n_estimators is excluded — it is determined by early stopping per fold.
    """
    params = {
        "max_depth":        trial.suggest_int("max_depth", 3, 7),
        "learning_rate":    trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
        "subsample":        trial.suggest_float("subsample", 0.6, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
        "reg_alpha":        trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
        "reg_lambda":       trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
        "gamma":            trial.suggest_float("gamma", 0.0, 5.0),
        "grow_policy":      trial.suggest_categorical("grow_policy", ["depthwise", "lossguide"]),
    }
    if classifier:
        # max_delta_step helps with class imbalance in binary/multiclass classification
        params["max_delta_step"] = trial.suggest_int("max_delta_step", 0, 10)
    return params


def _cv_score_classifier(
    trial: optuna.Trial,
    X: np.ndarray,
    y: np.ndarray,
    objective: str,
    eval_metric: str,
    sample_weights: np.ndarray | None = None,
) -> float:
    """TimeSeriesSplit CV score (mean log-loss) for a classifier trial."""
    from xgboost import XGBClassifier
    from sklearn.metrics import log_loss

    params  = _xgb_search_space(trial, classifier=True)
    scores  = []
    for tr_idx, val_idx in TimeSeriesSplit(n_splits=_N_CV_FOLDS).split(X):
        X_tr, X_val = X[tr_idx], X[val_idx]
        y_tr, y_val = y[tr_idx], y[val_idx]
        sw = sample_weights[tr_idx] if sample_weights is not None else None

        model = XGBClassifier(
            **_XGB_DEFAULTS, **params,
            objective=objective,
            eval_metric=eval_metric,
            early_stopping_rounds=_EARLY_STOPPING_ROUNDS,
        )
        model.fit(X_tr, y_tr, sample_weight=sw, eval_set=[(X_val, y_val)], verbose=False)
        proba = model.predict_proba(X_val)
        # binary: keep only the positive-class column for log_loss
        if proba.shape[1] == 2:
            proba = proba[:, 1]
        scores.append(log_loss(y_val, proba))

    return float(np.mean(scores))


def _cv_score_regressor(trial: optuna.Trial, X: np.ndarray, y: np.ndarray) -> float:
    """TimeSeriesSplit CV score (mean RMSE) for a regressor trial."""
    from xgboost import XGBRegressor
    from sklearn.metrics import mean_squared_error

    params = _xgb_search_space(trial, classifier=False)
    scores = []
    for tr_idx, val_idx in TimeSeriesSplit(n_splits=_N_CV_FOLDS).split(X):
        X_tr, X_val = X[tr_idx], X[val_idx]
        y_tr, y_val = y[tr_idx], y[val_idx]

        model = XGBRegressor(
            **_XGB_DEFAULTS, **params,
            objective="reg:squarederror",
            eval_metric="rmse",
            early_stopping_rounds=_EARLY_STOPPING_ROUNDS,
        )
        model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
        scores.append(float(np.sqrt(mean_squared_error(y_val, model.predict(X_val)))))

    return float(np.mean(scores))


# ── Per-model objective functions ─────────────────────────────────────────────

def _objective_regime(trial: optuna.Trial) -> float:
    """Mean CV log-loss for the 3-class regime classifier."""
    from sklearn.preprocessing import LabelEncoder
    from sklearn.utils.class_weight import compute_sample_weight
    from scripts.train_regime_classifier import (
        load_labeled_data, build_feature_matrix, TARGET_COL,
    )

    df = load_labeled_data()
    if df.empty:
        raise RuntimeError("No training data for regime model")

    X, _ = build_feature_matrix(df, fit=True)
    le   = LabelEncoder()
    y    = le.fit_transform(df[TARGET_COL].values)
    sw   = compute_sample_weight("balanced", y)

    return _cv_score_classifier(trial, X.values, y,
                                 objective="multi:softprob",
                                 eval_metric="mlogloss",
                                 sample_weights=sw)


def _objective_direction(trial: optuna.Trial) -> float:
    """Mean CV log-loss for the binary direction classifier."""
    from scripts.train_direction_model import (
        load_labeled_data, build_feature_matrix,
    )

    df = load_labeled_data()
    if df.empty:
        raise RuntimeError("No training data for direction model")

    X, _ = build_feature_matrix(df, fit=True)
    y    = df["direction"].values

    return _cv_score_classifier(trial, X.values, y,
                                 objective="binary:logistic",
                                 eval_metric="logloss")


def _objective_return(trial: optuna.Trial) -> float:
    """Mean CV RMSE for the expected-return regressor."""
    from scripts.train_return_model import (
        load_labeled_data, build_feature_matrix, TARGET_COL,
    )

    df = load_labeled_data()
    if df.empty:
        raise RuntimeError("No training data for return model")

    X, _ = build_feature_matrix(df, fit=True)
    y    = df[TARGET_COL].values

    return _cv_score_regressor(trial, X.values, y)


def _objective_volatility(trial: optuna.Trial) -> float:
    """Mean CV RMSE for the volatility regressor."""
    from scripts.train_volatility_model import (
        load_labeled_data, build_feature_matrix, TARGET_COL,
    )

    df = load_labeled_data()
    if df.empty:
        raise RuntimeError("No training data for volatility model")

    X, _ = build_feature_matrix(df, fit=True)
    y    = df[TARGET_COL].values

    return _cv_score_regressor(trial, X.values, y)


_OBJECTIVES = {
    "regime":     _objective_regime,
    "direction":  _objective_direction,
    "return":     _objective_return,
    "volatility": _objective_volatility,
}


# ── Tune one model ────────────────────────────────────────────────────────────

def tune_model(name: str, n_trials: int = 50) -> dict:
    """Run Optuna TPE study for one model. Returns {score, params, n_trials}."""
    log.info("Tuning %s (%d trials)…", name, n_trials)

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
        # No pruner: objectives don't report intermediate values (early stopping
        # handles over-training within each XGBoost call instead).
    )
    study.optimize(_OBJECTIVES[name], n_trials=n_trials, show_progress_bar=False)

    best = study.best_trial
    log.info(
        "%s: best CV score=%.5f (trial %d/%d) params=%s",
        name, best.value, best.number, n_trials,
        json.dumps(best.params),
    )
    return {"score": best.value, "params": best.params, "n_trials": n_trials}


# ── Main entry point ──────────────────────────────────────────────────────────

def main(models: list[str], n_trials: int, retrain: bool) -> dict:
    all_params = _load_best_params()
    results    = {}

    for name in models:
        try:
            result        = tune_model(name, n_trials)
            all_params[name] = result
            results[name] = {"ok": True, **result}
        except Exception:
            log.exception("%s tuning failed", name)
            results[name] = {"ok": False}

    _save_best_params(all_params)
    log.info("Best params saved → %s", _PARAMS_PATH)

    if retrain:
        log.info("Re-training models with tuned params…")
        _train_map = {
            "regime":     ("scripts.train_regime_classifier", "train"),
            "direction":  ("scripts.train_direction_model",   "train"),
            "return":     ("scripts.train_return_model",      "train"),
            "volatility": ("scripts.train_volatility_model",  "train"),
        }
        for name in models:
            if not results[name].get("ok"):
                continue
            mod_name, fn_name = _train_map[name]
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                r   = getattr(mod, fn_name)()
                results[name]["retrain"] = r
                log.info("%s retrain: %s", name, r)
            except Exception:
                log.exception("%s retrain failed", name)
                results[name]["retrain_error"] = "see log"

    return results


def load_best_params(model_name: str) -> dict | None:
    """
    Public helper imported by train_*.py scripts.
    Returns {**_XGB_DEFAULTS, **tuned_params} so callers get random_state,
    tree_method, and n_jobs for free, or None if this model hasn't been tuned.

    Usage in a training script:
        tuned = load_best_params("regime") or {}
        model = XGBClassifier(objective=..., **tuned)
    """
    saved = _load_best_params().get(model_name)
    if saved is None:
        return None
    # Support both new format {score, params, n_trials} and legacy flat dict
    tuned = saved.get("params") if isinstance(saved, dict) and "params" in saved else saved
    if not isinstance(tuned, dict):
        return None
    return {**_XGB_DEFAULTS, **tuned}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model",   choices=MODELS + ["all"], default="all")
    parser.add_argument("--trials",  type=int, default=50)
    parser.add_argument("--no-retrain", dest="retrain", action="store_false", default=True)
    args = parser.parse_args()

    targets = MODELS if args.model == "all" else [args.model]
    results = main(targets, args.trials, args.retrain)

    print("\n=== Tuning Results ===")
    for name, r in results.items():
        if not r.get("ok"):
            print(f"  {name:<12} FAILED — see log")
            continue
        p = r.get("params", {})
        print(
            f"  {name:<12} CV score={r['score']:.5f}  "
            f"depth={p.get('max_depth')}  lr={p.get('learning_rate', 0):.4f}  "
            f"gamma={p.get('gamma', 0):.2f}  subsample={p.get('subsample', 0):.2f}"
        )
