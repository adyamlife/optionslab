"""
Temporal Validation — rolling out-of-sample evaluation across ALL trained models.

Extends scripts/walk_forward.py (classifier AUC) to also cover regression models
(return_regressor, volatility_regressor) using R²/RMSE/MAE metrics.

For classifiers:   AUC ± std, Precision@K per fold  (delegated to walk_forward.py)
For regressors:    R², RMSE, MAE, directional-accuracy per fold

Two evaluation modes (--mode)
-------------------------------
rolling_holdout (default)
  The pre-trained artifact is used unchanged across every test window. Useful for
  detecting temporal performance drift of one fixed model, but is NOT genuine
  walk-forward validation: the model has already seen all training data, including
  data that temporally follows some test windows, which produces optimistic metrics.

walk_forward (--mode walk_forward)
  For each fold:
    train window = all data before test_start (expanding)
    test  window = [test_start, test_start + step_months)
  A fresh XGBRegressor is trained from scratch on each expanding training window
  using the same hyperparameters stored in the artifact. This is a genuine
  out-of-sample estimate because no test-window data leaks into training.

NOTE — fold boundaries use calendar months (DateOffset), not trading days.
  A 3-month fold does not guarantee 63 trading days; months of varying length
  and holidays mean fold sizes differ. This is standard for financial data but
  may be surprising if you expected fixed-N folds.

NOTE — pipeline limitation
  Both modes reconstruct features from stored feature_cols. If the training
  pipeline includes additional preprocessing steps (scaling, imputation, polynomial
  features) not stored in the artifact, those are NOT reproduced here. For the
  current XGBRegressor artifacts (raw numeric features, no sklearn Pipeline
  wrapper), the feature_cols reconstruction is exact.

Run standalone:
  python -m scripts.validate_walk_forward                           # all, holdout
  python -m scripts.validate_walk_forward --mode walk_forward       # true walk-forward
  python -m scripts.validate_walk_forward --model direction         # one model
  python -m scripts.validate_walk_forward --step 1                 # monthly folds
  python -m scripts.validate_walk_forward --warmup 4               # 4-month warmup
  python -m scripts.validate_walk_forward --min-rows 30            # tight data guard
  python -m scripts.validate_walk_forward --dead-zone 0.005        # filter near-zero
"""
import argparse
import logging
import math
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

try:
    from xgboost import XGBRegressor as _XGBRegressor
    _XGBOOST_AVAILABLE = True
except ImportError:
    _XGBOOST_AVAILABLE = False

_ROOT = Path(__file__).resolve().parent.parent
log   = logging.getLogger(__name__)

# ── Model registry ────────────────────────────────────────────────────────────
_MODELS: dict[str, tuple[Path, str]] = {
    "direction":          (_ROOT / "data/models/direction_model.joblib",       "classifier"),
    "iv_direction":       (_ROOT / "data/models/iv_direction_model.joblib",    "classifier"),
    "regime":             (_ROOT / "data/models/regime_classifier.joblib",     "classifier"),
    "return_classifier":  (_ROOT / "data/models/return_classifier.joblib",     "classifier"),
    "return_regressor":   (_ROOT / "data/models/return_regressor.joblib",      "regressor"),
    "volatility":         (_ROOT / "data/models/volatility_regressor.joblib",  "regressor"),
}

SUPPORTED_MODELS = list(_MODELS.keys())

# Expected top-level keys in walk_forward.py's run_walk_forward output
_WF_SCHEMA      = {"mean", "std", "min", "max", "folds"}
# Expected per-fold keys in classifier output consumed by the CLI print block
_WF_FOLD_SCHEMA = {"auc", "prec_at_10", "prec_at_25", "test_start", "test_end", "n_test"}


# ── Metric helpers ────────────────────────────────────────────────────────────

def _fold_stats(values: list[float], label: str) -> dict:
    if not values:
        return {f"{label}_mean": None, f"{label}_std": None,
                f"{label}_min": None,  f"{label}_max": None}
    a = np.array(values, dtype=float)
    return {
        f"{label}_mean": round(float(a.mean()), 4),
        f"{label}_std":  round(float(a.std()),  4),
        f"{label}_min":  round(float(a.min()),  4),
        f"{label}_max":  round(float(a.max()),  4),
    }


def _directional_accuracy(
    y_true:    np.ndarray,
    y_pred:    np.ndarray,
    dead_zone: float = 0.0,
) -> float:
    """
    Fraction of predictions where sign(pred) == sign(actual).

    dead_zone applies symmetrically to both y_true and y_pred.
    Rows where |y_true| <= dead_zone OR |y_pred| <= dead_zone are excluded.
    This filters out near-zero actual returns that are hard to call and near-zero
    predictions that happen to land on the correct side by chance
    (e.g. actual=5% pred=0.0001% — economically meaningless directional match).
    Set dead_zone=0.0 to disable filtering.
    """
    if dead_zone > 0:
        mask = (np.abs(y_true) > dead_zone) & (np.abs(y_pred) > dead_zone)
    else:
        mask = y_true != 0
    if mask.sum() == 0:
        return float("nan")
    return round(float((np.sign(y_pred[mask]) == np.sign(y_true[mask])).mean()), 4)


def _validate_wf_schema(result: dict, model_name: str) -> None:
    """Warn if walk_forward.py output is missing expected top-level or per-fold keys."""
    if not isinstance(result, dict):
        log.warning("[WalkForward] %s: run_walk_forward returned %s, expected dict",
                    model_name, type(result).__name__)
        return
    missing_top = _WF_SCHEMA - result.keys()
    if missing_top and result.get("ok") is not False:
        log.warning("[WalkForward] %s: result missing top-level keys %s — "
                    "walk_forward.py schema may have changed", model_name, missing_top)
    folds = result.get("folds") or []
    if folds:
        missing_fold = _WF_FOLD_SCHEMA - folds[0].keys()
        if missing_fold:
            log.warning("[WalkForward] %s: fold[0] missing keys %s — "
                        "walk_forward.py fold schema may have changed", model_name, missing_fold)


# ── Feature builder ───────────────────────────────────────────────────────────

def _build_X(df: pd.DataFrame, art: dict) -> pd.DataFrame | None:
    """
    Build a clean feature DataFrame from the stored feature_cols list.

    Returns a named DataFrame (not .values) so column ordering is always
    explicit and models receive correctly-labelled columns. Rows containing
    any NaN after coercion are dropped; callers must align their target
    arrays to the returned index using index intersection.
    """
    feature_cols = art.get("feature_cols") or art.get("features") or []
    if not feature_cols:
        return None
    X = pd.DataFrame(index=df.index)
    for col in feature_cols:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")
    return X.dropna()


def _aligned_Xy(
    df:         pd.DataFrame,
    art:        dict,
    target_col: str,
) -> tuple[pd.DataFrame, np.ndarray] | tuple[None, None]:
    """
    Build X and y with guaranteed row alignment using index intersection.

    Computes the intersection of:
      - rows where X has no NaN (from _build_X)
      - rows where target_col is not NaN
    Then selects both X and y from that shared index in the same order.
    This eliminates any risk of X/y misalignment from independent filtering.
    """
    X_raw = _build_X(df, art)
    if X_raw is None:
        return None, None
    valid_target = df[df[target_col].notna()].index
    valid_idx    = X_raw.index.intersection(valid_target)
    if len(valid_idx) == 0:
        return None, None
    X = X_raw.loc[valid_idx]
    y = df.loc[valid_idx, target_col].to_numpy(dtype=float)
    return X, y


# ── Regressor evaluation ──────────────────────────────────────────────────────

def _score_fold(
    y_true:    np.ndarray,
    y_pred:    np.ndarray,
    dead_zone: float,
) -> dict:
    """
    Per-fold regression metrics with a naive zero-prediction baseline.

    Baseline R² is omitted. R² already uses the sample mean as its reference;
    a constant-zero predictor can produce arbitrarily negative R² values
    (e.g. y=[2,2,2] → R²=-inf) which are difficult to interpret meaningfully.
    Baseline RMSE and MAE are included instead, where zero-prediction is a
    natural reference point for returns expected to be near zero.
    """
    y_zero = np.zeros_like(y_true)
    return {
        "r2":                   round(float(r2_score(y_true, y_pred)), 4),
        "rmse":                 round(float(math.sqrt(mean_squared_error(y_true, y_pred))), 6),
        "mae":                  round(float(mean_absolute_error(y_true, y_pred)), 6),
        "directional_accuracy": _directional_accuracy(y_true, y_pred, dead_zone),
        "baseline_rmse":        round(float(math.sqrt(mean_squared_error(y_true, y_zero))), 6),
        "baseline_mae":         round(float(mean_absolute_error(y_true, y_zero)), 6),
        "y_mean":               round(float(y_true.mean()), 6),
    }


def _walk_regressor_holdout(
    art:           dict,
    df:            pd.DataFrame,
    target_col:    str,
    step_months:   int,
    warmup_months: int,
    min_rows:      int,
    dead_zone:     float,
) -> list[dict]:
    """
    Rolling holdout: score the pre-trained artifact across time windows.
    The model is NOT retrained per fold — see module docstring for implications.

    Builds the full feature matrix once over all rows and slices it per fold,
    avoiding redundant reconstruction on every iteration.
    """
    model = art.get("model") or art.get("regressor")
    if model is None:
        return []

    df = df.sort_values("date").reset_index(drop=True)

    # Build X and y once over the entire dataset; slice per fold by index
    X_all, y_all = _aligned_Xy(df, art, target_col)
    if X_all is None:
        return []

    dates      = pd.DatetimeIndex(sorted(df["date"].unique()))
    step       = pd.DateOffset(months=step_months)
    warmup     = pd.DateOffset(months=warmup_months)
    test_start = dates.min() + warmup

    folds: list[dict] = []
    fold_idx = 0

    while test_start <= dates.max():
        test_end  = test_start + step
        fold_mask = (df["date"] >= test_start) & (df["date"] < test_end)
        fold_idx_in_df = df.index[fold_mask]
        shared    = X_all.index.intersection(fold_idx_in_df)

        if len(shared) < min_rows:
            test_start = test_end
            continue

        X_fold = X_all.loc[shared]
        y_true = y_all[X_all.index.get_indexer(shared)]

        try:
            y_pred = model.predict(X_fold).astype(float)
        except Exception as e:
            log.warning("[WalkForward] holdout fold %d failed: %s", fold_idx, e)
            test_start = test_end
            continue

        fin_mask = np.isfinite(y_true) & np.isfinite(y_pred)
        if fin_mask.sum() < min_rows:
            test_start = test_end
            continue
        y_true, y_pred = y_true[fin_mask], y_pred[fin_mask]

        folds.append({
            "fold":       fold_idx,
            "test_start": str(test_start.date()),
            "test_end":   str((test_end - pd.Timedelta(days=1)).date()),
            "n_test":     int(fin_mask.sum()),
            **_score_fold(y_true, y_pred, dead_zone),
        })
        fold_idx  += 1
        test_start = test_end

    return folds


def _walk_regressor_retrain(
    art:           dict,
    df:            pd.DataFrame,
    target_col:    str,
    step_months:   int,
    warmup_months: int,
    min_rows:      int,
    dead_zone:     float,
) -> list[dict]:
    """
    True walk-forward: retrain from scratch on each expanding training window.

    Hyperparameters are inherited from the stored artifact via get_params().
    Requires the artifact's model to support get_params() (XGBRegressor does;
    a Pipeline wrapper would require unwrapping the final step).
    """
    if not _XGBOOST_AVAILABLE:
        log.error("[WalkForward] --mode walk_forward requires xgboost; install it first")
        return []

    base_model = art.get("model") or art.get("regressor")
    if base_model is None:
        return []

    if not hasattr(base_model, "get_params"):
        log.error(
            "[WalkForward] artifact model type %s does not support get_params(); "
            "true walk-forward requires an XGBRegressor or compatible estimator",
            type(base_model).__name__,
        )
        return []

    params = {k: v for k, v in base_model.get_params().items()
              if k not in ("callbacks", "early_stopping_rounds")}

    df = df.sort_values("date").reset_index(drop=True)

    dates      = pd.DatetimeIndex(sorted(df["date"].unique()))
    step       = pd.DateOffset(months=step_months)
    warmup     = pd.DateOffset(months=warmup_months)
    test_start = dates.min() + warmup

    folds: list[dict] = []
    fold_idx = 0

    while test_start <= dates.max():
        test_end = test_start + step

        train_df = df[df["date"] < test_start].copy()
        test_df  = df[(df["date"] >= test_start) & (df["date"] < test_end)].copy()

        X_train, y_train = _aligned_Xy(train_df, art, target_col)
        X_test,  y_test  = _aligned_Xy(test_df,  art, target_col)

        if (X_train is None or X_test is None or
                len(X_train) < min_rows or len(X_test) < min_rows):
            test_start = test_end
            continue

        try:
            m = _XGBRegressor(**params)
            m.fit(X_train, y_train, verbose=False)
            y_pred = m.predict(X_test).astype(float)
        except Exception as e:
            log.warning("[WalkForward] retrain fold %d failed: %s", fold_idx, e)
            test_start = test_end
            continue

        fin_mask = np.isfinite(y_test) & np.isfinite(y_pred)
        if fin_mask.sum() < min_rows:
            test_start = test_end
            continue
        y_test, y_pred = y_test[fin_mask], y_pred[fin_mask]

        folds.append({
            "fold":       fold_idx,
            "test_start": str(test_start.date()),
            "test_end":   str((test_end - pd.Timedelta(days=1)).date()),
            "n_train":    len(X_train),
            "n_test":     int(fin_mask.sum()),
            **_score_fold(y_test, y_pred, dead_zone),
        })
        fold_idx  += 1
        test_start = test_end

    return folds


# ── Classifier delegation ─────────────────────────────────────────────────────

def _run_existing_walk_forward(model_name: str, step_months: int) -> dict:
    """Delegate to walk_forward.py's run_walk_forward for classifier models."""
    from scripts.walk_forward import run_walk_forward
    result = run_walk_forward(model_name=model_name, step_months=step_months)
    _validate_wf_schema(result, model_name)
    return result


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run_validate(
    model_name:    str | None = None,
    step_months:   int        = 3,
    warmup_months: int        = 6,
    min_rows:      int        = 20,
    mode:          str        = "rolling_holdout",
    dead_zone:     float      = 0.0,
) -> dict:
    """
    Run temporal validation for one or all models.

    Args:
        mode:      "rolling_holdout" (default) or "walk_forward".
                   See module docstring for the distinction.
        dead_zone: Exclude rows where |y_true| <= dead_zone AND |y_pred| <= dead_zone
                   from directional accuracy. Filters near-zero calls in both actual
                   returns and predictions.

    Returns dict with per-model results nested under 'models' key.
    """
    from scripts.db import read_df, TABLE

    targets = [model_name] if model_name else list(_MODELS.keys())
    results: dict = {"ok": True, "models": {}, "step_months": step_months,
                     "warmup_months": warmup_months, "mode": mode}

    df_full: pd.DataFrame | None = None

    for name in targets:
        art_path, kind = _MODELS[name]

        if not art_path.exists():
            results["models"][name] = {"ok": False, "error": f"Artifact not found: {art_path.name}"}
            continue

        try:
            art = joblib.load(art_path)
        except Exception as e:
            results["models"][name] = {"ok": False, "error": f"Load failed: {e}"}
            continue

        if kind == "classifier":
            wf = _run_existing_walk_forward(name, step_months)
            results["models"][name] = wf
            continue

        # Regressor — load labeled data once, reuse across models
        if df_full is None:
            try:
                df_full = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
                df_full["date"] = pd.to_datetime(df_full["date"])
            except Exception as e:
                results["models"][name] = {"ok": False, "error": f"DB read failed: {e}"}
                continue

        target_col = "forward_return" if name == "return_regressor" else "forward_hv"
        if target_col not in df_full.columns:
            results["models"][name] = {
                "ok":    False,
                "error": f"Target column '{target_col}' not in training table",
            }
            continue

        df_clean = df_full.dropna(subset=[target_col]).copy()

        walk_fn = (_walk_regressor_retrain if mode == "walk_forward"
                   else _walk_regressor_holdout)
        folds = walk_fn(
            art           = art,
            df            = df_clean,
            target_col    = target_col,
            step_months   = step_months,
            warmup_months = warmup_months,
            min_rows      = min_rows,
            dead_zone     = dead_zone,
        )

        if not folds:
            results["models"][name] = {
                "ok":    False,
                "error": "No valid folds — insufficient data or missing feature columns",
            }
            continue

        r2s   = [f["r2"]   for f in folds]
        rmses = [f["rmse"] for f in folds]
        maes  = [f["mae"]  for f in folds]
        dirs  = [f["directional_accuracy"] for f in folds
                 if not (isinstance(f["directional_accuracy"], float) and
                         math.isnan(f["directional_accuracy"]))]

        results["models"][name] = {
            "ok":      True,
            "kind":    "regressor",
            "mode":    mode,
            "target":  target_col,
            "n_folds": len(folds),
            **_fold_stats(r2s,   "r2"),
            **_fold_stats(rmses, "rmse"),
            **_fold_stats(maes,  "mae"),
            **_fold_stats(dirs,  "directional_accuracy"),
            "folds":   folds,
        }

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Temporal validation across all models")
    parser.add_argument("--model",     default=None, choices=SUPPORTED_MODELS,
                        help="Validate one model; omit for all")
    parser.add_argument("--mode",      default="rolling_holdout",
                        choices=["rolling_holdout", "walk_forward"],
                        help="rolling_holdout (default) or walk_forward (retrains per fold)")
    parser.add_argument("--step",      type=int, default=3,
                        help="Test window size in calendar months (default 3)")
    parser.add_argument("--warmup",    type=int, default=6,
                        help="Warm-up period in months before first test fold (default 6)")
    parser.add_argument("--min-rows",  type=int, default=20,
                        help="Minimum rows per fold to include it (default 20)")
    parser.add_argument("--dead-zone", type=float, default=0.0,
                        help="Filter rows where |actual| and |pred| <= this from "
                             "directional accuracy (default 0.0 = disabled)")
    args = parser.parse_args()

    result = run_validate(
        model_name    = args.model,
        step_months   = args.step,
        warmup_months = args.warmup,
        min_rows      = args.min_rows,
        mode          = args.mode,
        dead_zone     = args.dead_zone,
    )

    print(f"\n=== Temporal Validation  (step={args.step}mo  warmup={args.warmup}mo"
          f"  mode={args.mode}) ===\n")

    for name, res in result["models"].items():
        if not res.get("ok"):
            print(f"  {name:<22} SKIPPED -- {res.get('error')}")
            continue

        kind = res.get("kind", "classifier")
        n    = res.get("n_folds", 0)

        if kind == "regressor":
            print(f"\n-- {name}  target={res['target']}  mode={res.get('mode')} --")
            print(f"   Folds: {n}")
            print(f"   R2:    {res['r2_mean']:+.4f} +/- {res['r2_std']:.4f}"
                  f"  [{res['r2_min']:+.4f} -> {res['r2_max']:+.4f}]")
            print(f"   RMSE:  {res['rmse_mean']:.6f} +/- {res['rmse_std']:.6f}")
            print(f"   MAE:   {res['mae_mean']:.6f} +/- {res['mae_std']:.6f}")
            if res.get("directional_accuracy_mean") is not None:
                print(f"   DirAcc:{res['directional_accuracy_mean']:.1%} "
                      f"+/- {res['directional_accuracy_std']:.1%}")
            retrain_mode = args.mode == "walk_forward"
            hdr = (f"   {'Fold':<5} {'Period':<27} {'N':>5}"
                   f" {'R2':>7} {'RMSE':>9} {'MAE':>9} {'DirAcc':>7} {'BslRMSE':>9}")
            if retrain_mode:
                hdr += f" {'N_train':>7}"
            print(hdr)
            for f in res["folds"]:
                da = f["directional_accuracy"]
                dir_s = f"{da:.1%}" if not (isinstance(da, float) and math.isnan(da)) else "  N/A"
                row = (f"   {f['fold']:<5} {f['test_start']} -> {f['test_end']}  "
                       f"{f['n_test']:>5} {f['r2']:>+7.4f} {f['rmse']:>9.6f}"
                       f" {f['mae']:>9.6f} {dir_s:>7} {f['baseline_rmse']:>9.6f}")
                if retrain_mode:
                    row += f" {f.get('n_train', ''):>7}"
                print(row)

        else:
            mean_auc = res.get("mean")
            std_auc  = res.get("std")
            if mean_auc is None:
                print(f"  {name:<22} no folds")
                continue
            print(f"\n-- {name} (classifier) --")
            print(f"   Folds: {n}   AUC: {mean_auc:.4f} +/- {std_auc:.4f}"
                  f"  [{res.get('min', 0):.4f} -> {res.get('max', 0):.4f}]")
            print(f"\n   {'Fold':<5} {'Period':<27} {'N':>5} {'AUC':>6} {'P@10':>6} {'P@25':>6}")
            for f in res.get("folds", []):
                p10 = f"{f['prec_at_10']:.4f}" if f.get("prec_at_10") is not None else "  N/A"
                p25 = f"{f['prec_at_25']:.4f}" if f.get("prec_at_25") is not None else "  N/A"
                print(f"   {f['fold']:<5} {f['test_start']} -> {f['test_end']}  "
                      f"{f['n_test']:>5} {f['auc']:>6.4f} {p10:>6} {p25:>6}")

    print()
