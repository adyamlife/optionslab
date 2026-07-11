"""
Optuna hyperparameter tuning for all XGBoost models in the pipeline.

Runs TPE search over the XGBoost search space for each model, saves best
params to data/models/best_params.json, then re-runs training so the
saved joblib immediately reflects the tuned model.

Each training script checks for saved params at startup and uses them when
present (see _load_best_params() helper imported by each train_*.py).

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

optuna.logging.set_verbosity(optuna.logging.WARNING)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_ROOT = Path(__file__).resolve().parent.parent
_PARAMS_PATH = _ROOT / "data" / "models" / "best_params.json"

MODELS = ["regime", "direction", "return", "volatility"]


# ── Shared helpers ────────────────────────────────────────────────────────────

def _load_best_params() -> dict:
    """Load saved best params. Returns {} if file absent."""
    if _PARAMS_PATH.exists():
        return json.loads(_PARAMS_PATH.read_text())
    return {}


def _save_best_params(all_params: dict) -> None:
    _PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PARAMS_PATH.write_text(json.dumps(all_params, indent=2))


def _xgb_search_space(trial: optuna.Trial) -> dict:
    """Common XGBoost search space used across all models."""
    return {
        "n_estimators":      trial.suggest_int("n_estimators", 100, 800),
        "max_depth":         trial.suggest_int("max_depth", 3, 7),
        "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
        "subsample":         trial.suggest_float("subsample", 0.6, 1.0),
        "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "min_child_weight":  trial.suggest_int("min_child_weight", 1, 10),
        "reg_alpha":         trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
        "reg_lambda":        trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
    }


# ── Per-model objective functions ─────────────────────────────────────────────

def _objective_regime(trial: optuna.Trial) -> float:
    """Minimise validation log-loss for the 3-class regime classifier."""
    from xgboost import XGBClassifier
    from sklearn.metrics import log_loss
    from sklearn.utils.class_weight import compute_sample_weight
    from scripts.train_regime_classifier import (
        load_labeled_data, time_based_split, build_feature_matrix,
        _DATA_PATH, TARGET_COL,
    )
    from sklearn.preprocessing import LabelEncoder

    df = load_labeled_data(_DATA_PATH)
    if df.empty:
        raise optuna.exceptions.TrialPruned()

    train_df, test_df, _ = time_based_split(df)
    if train_df.empty or test_df.empty:
        raise optuna.exceptions.TrialPruned()

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_test, _ = build_feature_matrix(test_df, encoders=encoders, fit=False)

    le = LabelEncoder()
    y_train = le.fit_transform(train_df[TARGET_COL])
    y_test = le.transform(test_df[TARGET_COL])

    params = _xgb_search_space(trial)
    model = XGBClassifier(
        **params,
        objective="multi:softprob",
        eval_metric="mlogloss",
    )
    w = compute_sample_weight("balanced", y_train)
    model.fit(X_train, y_train, sample_weight=w, verbose=False)
    proba = model.predict_proba(X_test)
    return log_loss(y_test, proba)


def _objective_direction(trial: optuna.Trial) -> float:
    """Minimise validation log-loss for the binary direction classifier."""
    from xgboost import XGBClassifier
    from sklearn.metrics import log_loss
    from scripts.train_direction_model import (
        load_labeled_data, time_based_split, build_feature_matrix, _DATA_PATH,
    )

    df = load_labeled_data()
    if df.empty:
        raise optuna.exceptions.TrialPruned()

    train_df, test_df, _ = time_based_split(df)
    if train_df.empty or test_df.empty:
        raise optuna.exceptions.TrialPruned()

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_test, _ = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df["direction"].values
    y_test = test_df["direction"].values

    params = _xgb_search_space(trial)
    model = XGBClassifier(
        **params,
        objective="binary:logistic",
        eval_metric="logloss",
    )
    model.fit(X_train, y_train, verbose=False)
    proba = model.predict_proba(X_test)[:, 1]
    return log_loss(y_test, proba)


def _objective_return(trial: optuna.Trial) -> float:
    """Minimise validation RMSE for the expected-return regressor."""
    from xgboost import XGBRegressor
    from sklearn.metrics import mean_squared_error
    from scripts.train_return_model import (
        load_labeled_data, time_based_split, build_feature_matrix, TARGET_COL,
    )

    df = load_labeled_data()
    if df.empty:
        raise optuna.exceptions.TrialPruned()

    train_df, test_df, _ = time_based_split(df)
    if train_df.empty or test_df.empty:
        raise optuna.exceptions.TrialPruned()

    # return model uses dummy_cols (not encoders) as second return value
    X_train, dummy_cols = build_feature_matrix(train_df, fit=True)
    X_test, _ = build_feature_matrix(test_df, dummy_cols=dummy_cols, fit=False)
    y_train = train_df[TARGET_COL].values
    y_test = test_df[TARGET_COL].values

    params = _xgb_search_space(trial)
    model = XGBRegressor(**params, objective="reg:squarederror")
    model.fit(X_train, y_train, verbose=False)
    y_pred = model.predict(X_test)
    return float(np.sqrt(mean_squared_error(y_test, y_pred)))


def _objective_volatility(trial: optuna.Trial) -> float:
    """Minimise validation RMSE for the volatility regressor."""
    from xgboost import XGBRegressor
    from sklearn.metrics import mean_squared_error
    from scripts.train_volatility_model import (
        load_labeled_data, time_based_split, build_feature_matrix, TARGET_COL,
    )

    df = load_labeled_data()
    if df.empty:
        raise optuna.exceptions.TrialPruned()

    train_df, test_df, _ = time_based_split(df)
    if train_df.empty or test_df.empty:
        raise optuna.exceptions.TrialPruned()

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_test, _ = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df[TARGET_COL].values
    y_test = test_df[TARGET_COL].values

    params = _xgb_search_space(trial)
    model = XGBRegressor(**params, objective="reg:squarederror")
    model.fit(X_train, y_train, verbose=False)
    y_pred = model.predict(X_test)
    return float(np.sqrt(mean_squared_error(y_test, y_pred)))


_OBJECTIVES = {
    "regime":     _objective_regime,
    "direction":  _objective_direction,
    "return":     _objective_return,
    "volatility": _objective_volatility,
}


# ── Tune one model ────────────────────────────────────────────────────────────

def tune_model(name: str, n_trials: int = 50) -> dict:
    """Run Optuna study for one model. Returns best params dict."""
    log.info(f"Tuning {name} ({n_trials} trials)…")
    objective = _OBJECTIVES[name]

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=10, n_warmup_steps=0),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_trial
    log.info(
        f"{name}: best value={best.value:.5f} "
        f"(trial {best.number}/{n_trials}) — "
        f"n_estimators={best.params.get('n_estimators')} "
        f"max_depth={best.params.get('max_depth')} "
        f"lr={best.params.get('learning_rate', 0):.4f}"
    )
    return best.params


# ── Main entry point ──────────────────────────────────────────────────────────

def main(models: list[str], n_trials: int, retrain: bool) -> dict:
    all_params = _load_best_params()
    results = {}

    for name in models:
        try:
            params = tune_model(name, n_trials)
            all_params[name] = params
            results[name] = {"ok": True, "params": params}
        except Exception as e:
            log.error(f"{name} tuning failed: {e}")
            results[name] = {"ok": False, "error": str(e)}

    _save_best_params(all_params)
    log.info(f"Best params saved → {_PARAMS_PATH}")

    if retrain:
        log.info("Re-training models with tuned params…")
        train_map = {
            "regime":     ("scripts.train_regime_classifier",  "train"),
            "direction":  ("scripts.train_direction_model",    "train"),
            "return":     ("scripts.train_return_model",       "train"),
            "volatility": ("scripts.train_volatility_model",   "train"),
        }
        for name in models:
            if not results[name].get("ok"):
                continue
            mod_name, fn_name = train_map[name]
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                r = getattr(mod, fn_name)()
                results[name]["retrain"] = r
                log.info(f"{name} retrain: {r}")
            except Exception as e:
                log.error(f"{name} retrain failed: {e}")
                results[name]["retrain_error"] = str(e)

    return results


def load_best_params(model_name: str) -> dict | None:
    """
    Public helper imported by train_*.py scripts.
    Returns the saved Optuna best params for `model_name`, or None if absent.

    Usage in a training script:
        from scripts.tune_hyperparams import load_best_params
        tuned = load_best_params("regime") or {}
        model = XGBClassifier(n_estimators=200, max_depth=4, **tuned)
    """
    return _load_best_params().get(model_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=MODELS + ["all"], default="all")
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--no-retrain", dest="retrain", action="store_false", default=True)
    args = parser.parse_args()

    targets = MODELS if args.model == "all" else [args.model]
    results = main(targets, args.trials, args.retrain)

    print("\n=== Tuning Results ===")
    for name, r in results.items():
        status = "OK" if r.get("ok") else f"FAILED: {r.get('error')}"
        print(f"  {name:<12} {status}")
        if r.get("ok") and r.get("params"):
            p = r["params"]
            print(f"             n_est={p.get('n_estimators')}  depth={p.get('max_depth')}  "
                  f"lr={p.get('learning_rate', 0):.4f}  subsample={p.get('subsample', 0):.2f}")
