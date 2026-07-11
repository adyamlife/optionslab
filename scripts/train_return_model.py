"""
Expected Return Model v2 — significant improvements over v1:

  Walk-forward validation  TimeSeriesSplit(n_splits=5) on calendar dates, not rows.
                           Gives honest out-of-sample metrics across 5 folds rather
                           than a single lucky/unlucky holdout.

  Early stopping           XGBoost trains up to 800 estimators; halts when validation
                           RMSE stops improving for 30 rounds. Best n_estimators is
                           used for the final model trained on all training data.

  One-hot encoding         pd.get_dummies replaces LabelEncoder for categorical cols.
                           No false ordinal assumption; dummy columns are saved in the
                           artifact for consistent inference alignment.

  Lagged / change features 5-day and 10-day lags + change scores for key momentum
                           indicators (rsi, adx, hv20, vix_close, rel_strength_spy).
                           Computed within each ticker's time series to avoid leakage.

  Correlation metrics      Pearson r, Spearman rho, and Information Coefficient (IC).
                           IC = mean per-date cross-sectional Spearman rank correlation.
                           ICIR = IC_mean / IC_std (analogous to a Sharpe ratio for
                           factor quality — above 0.5 is considered meaningful in
                           factor research).

  Naive benchmarks         Three baselines on the same test set:
                             predict-zero  : always predict 0% return
                             predict-mean  : always predict training-set mean return
                             predict-last  : use the ticker's most recent known return
                           If the model can't beat predict-zero on RMSE, it has no signal.

Run standalone: python -m scripts.train_return_model
Outputs:        data/models/return_regressor.joblib
                data/models/return_catboost.joblib
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBRegressor

_ROOT           = Path(__file__).resolve().parent.parent
_MODEL_PATH     = _ROOT / "data" / "models" / "return_regressor.joblib"
_CATBOOST_PATH  = _ROOT / "data" / "models" / "return_catboost.joblib"

TARGET_COL    = "forward_return"
TEST_FRACTION = 0.20
N_CV_SPLITS   = 5

_REQUIRED_COLS = ["rsi", "adx", "hv20", "macd_trend", "trend", "vix_close", "rel_strength_spy"]

NUMERIC_FEATURES = [
    "rsi", "adx", "hv20", "vix_close", "rel_strength_spy",
    "beta_60d", "atr_pct", "iv_rank_52w",
    "vol_oi_ratio", "iv_skew", "iv_term_slope", "otm_pcr",
    "spy_rsi", "qqq_rsi", "iwm_rsi",
    "sector_rsi", "sector_iv_ratio",
    "vvix", "vix_3m", "vix_term_slope",
    "earnings_inside_expiry", "news_sentiment_score",
    "analyst_rec_change", "short_interest_pct",
    "iv_skew_20d", "gex_proxy", "max_pain_strike", "oi_concentration", "wings_iv_ratio",
    "yield_10y", "yield_3m", "yield_curve", "dollar_index",
    "fed_within_dte", "cpi_within_dte",
]

CAT_COLS = ["macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend",
            "sector_etf", "sector_trend"]

# Features for which we compute 5d and 10d lags and change scores
LAG_SOURCES = ["rsi", "adx", "hv20", "vix_close", "rel_strength_spy"]
LAG_DAYS    = [5, 10]
LAG_COLS    = (
    [f"{f}_lag{d}"  for f in LAG_SOURCES for d in LAG_DAYS] +
    [f"{f}_chg{d}"  for f in LAG_SOURCES for d in LAG_DAYS]
)


# ── Data loading ──────────────────────────────────────────────────────────────

def load_labeled_data() -> pd.DataFrame:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=_REQUIRED_COLS + [TARGET_COL])
    return df


def compute_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute per-ticker lagged values and change scores for LAG_SOURCES.
    Rows where a lag is unavailable (first N rows per ticker) are dropped.
    Must be called after sorting is guaranteed — we sort inside here.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"])

    for feat in LAG_SOURCES:
        if feat not in df.columns:
            for d in LAG_DAYS:
                df[f"{feat}_lag{d}"] = np.nan
                df[f"{feat}_chg{d}"] = np.nan
            continue
        col = pd.to_numeric(df[feat], errors="coerce")
        for d in LAG_DAYS:
            lag_col           = df.groupby("ticker")[feat].shift(d)
            df[f"{feat}_lag{d}"] = lag_col
            df[f"{feat}_chg{d}"] = col - lag_col

    # Drop rows where any lag is missing (first N rows per ticker)
    df = df.dropna(subset=[c for c in LAG_COLS if c in df.columns])
    return df


def time_based_split(df: pd.DataFrame, test_fraction=TEST_FRACTION):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    unique_dates = np.sort(df["date"].unique())
    cutoff = unique_dates[int(len(unique_dates) * (1 - test_fraction))]
    return df[df["date"] < cutoff].copy(), df[df["date"] >= cutoff].copy(), cutoff


# ── Feature matrix ────────────────────────────────────────────────────────────

def build_feature_matrix(df: pd.DataFrame, dummy_cols: list = None, fit: bool = False):
    """
    Returns (X DataFrame, dummy_cols list).
    On fit=True: fits one-hot encoding and returns the column list.
    On fit=False: aligns to `dummy_cols`, filling missing dummies with 0.
    """
    X = pd.DataFrame(index=df.index)

    for col in NUMERIC_FEATURES:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")

    for col in LAG_COLS:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")

    cat_df = pd.DataFrame(index=df.index)
    for col in CAT_COLS:
        cat_df[col] = df[col].astype(str) if col in df.columns else "unknown"
    dummies = pd.get_dummies(cat_df, prefix_sep="__", drop_first=False)
    X = pd.concat([X, dummies], axis=1)

    if fit:
        dummy_cols = list(X.columns)
        return X, dummy_cols
    else:
        for col in dummy_cols:
            if col not in X.columns:
                X[col] = 0
        return X[dummy_cols], dummy_cols


def build_catboost_matrix(df: pd.DataFrame):
    """Feature matrix for CatBoost: numerics + lags as float, categoricals as raw strings.
    CatBoost uses ordered target statistics on string values — no encoding needed."""
    X = pd.DataFrame(index=df.index)
    for col in NUMERIC_FEATURES:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")
    for col in LAG_COLS:
        X[col] = pd.to_numeric(df.get(col), errors="coerce")
    for col in CAT_COLS:
        col_vals = df[col].astype(str) if col in df.columns else pd.Series(["unknown"] * len(df), index=df.index)
        X[col] = col_vals.fillna("unknown")
    return X


# ── Evaluation helpers ────────────────────────────────────────────────────────

def _regression_metrics(y_true, y_pred, label=""):
    """Returns dict of RMSE, MAE, R², Pearson r, Spearman rho, Dir.Acc."""
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    mae  = float(mean_absolute_error(y_true, y_pred))
    r2   = float(r2_score(y_true, y_pred))
    pearson,  _ = stats.pearsonr(y_true,  y_pred)
    spearman, _ = stats.spearmanr(y_true, y_pred)
    dir_acc = float(np.mean(np.sign(y_pred) == np.sign(y_true)))
    return {
        "rmse": round(rmse, 5), "mae": round(mae, 5), "r2": round(r2, 5),
        "pearson": round(float(pearson), 4), "spearman": round(float(spearman), 4),
        "directional_accuracy": round(dir_acc, 4),
    }


def _information_coefficient(y_true, y_pred, dates):
    """
    Per-date cross-sectional Spearman rank correlation (IC), averaged.
    Only dates with >= 5 tickers contribute.
    Returns (IC_mean, IC_std, ICIR) or (None, None, None) if insufficient data.
    """
    ics = []
    for date in np.unique(dates):
        mask = dates == date
        if mask.sum() < 5:
            continue
        rho, _ = stats.spearmanr(y_pred[mask], y_true[mask])
        if not np.isnan(rho):
            ics.append(float(rho))
    if len(ics) < 3:
        return None, None, None
    ic_mean = float(np.mean(ics))
    ic_std  = float(np.std(ics, ddof=1))
    icir    = round(ic_mean / ic_std, 3) if ic_std > 0 else None
    return round(ic_mean, 4), round(ic_std, 4), icir


def _naive_metrics(y_train, y_test):
    """
    Three naive baselines on the test set:
      zero: predict 0 for every row
      mean: predict training-set mean for every row
    Returns dict of RMSE and directional accuracy for each.
    """
    train_mean = float(np.mean(y_train))
    results = {}
    for name, pred in [
        ("zero", np.zeros(len(y_test))),
        ("mean", np.full(len(y_test), train_mean)),
    ]:
        results[name] = {
            "rmse": round(float(np.sqrt(mean_squared_error(y_test, pred))), 5),
            "directional_accuracy": round(float(np.mean(np.sign(pred) == np.sign(y_test))), 4),
        }
    return results


# ── Training ──────────────────────────────────────────────────────────────────

def train(out_path=_MODEL_PATH) -> dict:
    df = load_labeled_data()
    if df.empty:
        return {"ok": False, "error": "No labeled rows available"}

    df = compute_lag_features(df)
    if df.empty:
        return {"ok": False, "error": "No rows remain after computing lag features"}

    train_df, test_df, cutoff = time_based_split(df)
    if train_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty train/test (cutoff={cutoff})"}

    # ── Walk-forward cross-validation on training portion ─────────────────────
    train_df = train_df.sort_values("date")
    unique_train_dates = np.sort(train_df["date"].unique())
    tscv = TimeSeriesSplit(n_splits=N_CV_SPLITS)

    cv_metrics = []
    best_iterations = []

    for fold, (tr_idx, val_idx) in enumerate(tscv.split(unique_train_dates)):
        fold_train_dates = set(unique_train_dates[tr_idx])
        fold_val_dates   = set(unique_train_dates[val_idx])

        f_train = train_df[train_df["date"].isin(fold_train_dates)]
        f_val   = train_df[train_df["date"].isin(fold_val_dates)]
        if f_train.empty or f_val.empty:
            continue

        X_ft, dcols = build_feature_matrix(f_train, fit=True)
        X_fv, _     = build_feature_matrix(f_val, dummy_cols=dcols, fit=False)
        y_ft = f_train[TARGET_COL].values
        y_fv = f_val[TARGET_COL].values

        m = XGBRegressor(
            n_estimators=800, max_depth=4, learning_rate=0.03,
            subsample=0.8, colsample_bytree=0.7, min_child_weight=5,
            reg_alpha=0.1, reg_lambda=1.0,
            objective="reg:squarederror", tree_method="hist",
            early_stopping_rounds=30, eval_metric="rmse",
            random_state=42,
        )
        m.fit(X_ft, y_ft, eval_set=[(X_fv, y_fv)], verbose=False)
        best_iterations.append(m.best_iteration + 1)

        y_fv_pred = m.predict(X_fv)
        fold_m = _regression_metrics(y_fv, y_fv_pred)
        fold_m["fold"] = fold + 1
        fold_m["train_rows"] = len(f_train)
        fold_m["val_rows"]   = len(f_val)
        cv_metrics.append(fold_m)

    cv_summary = {}
    if cv_metrics:
        for key in ("rmse", "mae", "r2", "pearson", "spearman", "directional_accuracy"):
            vals = [m[key] for m in cv_metrics]
            cv_summary[key + "_mean"] = round(float(np.mean(vals)), 5)
            cv_summary[key + "_std"]  = round(float(np.std(vals, ddof=1)), 5)

    # ── Final model: best n_estimators from CV, trained on full training set ──
    best_n = int(np.median(best_iterations)) if best_iterations else 200

    X_train, dummy_cols = build_feature_matrix(train_df, fit=True)
    X_test,  _          = build_feature_matrix(test_df, dummy_cols=dummy_cols, fit=False)
    y_train = train_df[TARGET_COL].values
    y_test  = test_df[TARGET_COL].values

    try:
        from scripts.tune_hyperparams import load_best_params as _lbp
        _tuned = _lbp("return") or {}
    except Exception:
        _tuned = {}
    _xgb_params = {"n_estimators": best_n, "max_depth": 4, "learning_rate": 0.03,
                   "subsample": 0.8, "colsample_bytree": 0.7, "min_child_weight": 5,
                   "reg_alpha": 0.1, "reg_lambda": 1.0}
    _xgb_params.update(_tuned)
    model = XGBRegressor(
        **_xgb_params,
        objective="reg:squarederror", tree_method="hist", random_state=42,
    )
    model.fit(X_train, y_train)

    # ── Holdout evaluation ────────────────────────────────────────────────────
    y_pred   = model.predict(X_test)
    holdout  = _regression_metrics(y_test, y_pred)
    naive    = _naive_metrics(y_train, y_test)

    test_dates = test_df["date"].values if "date" in test_df.columns else None
    ic_mean = ic_std = icir = None
    if test_dates is not None and "ticker" in test_df.columns:
        ic_mean, ic_std, icir = _information_coefficient(y_test, y_pred, test_dates)

    pearson_p  = stats.pearsonr(y_test, y_pred).pvalue  if len(y_test) > 2 else None
    spearman_p = stats.spearmanr(y_test, y_pred).pvalue if len(y_test) > 2 else None

    # ── Random Forest baseline ────────────────────────────────────────────────
    rf_baseline = None
    try:
        rf = RandomForestRegressor(
            n_estimators=300, max_depth=None, min_samples_leaf=5,
            n_jobs=-1, random_state=42,
        )
        rf.fit(X_train, y_train)
        rf_pred    = rf.predict(X_test)
        rf_baseline = _regression_metrics(y_test, rf_pred)
    except Exception as e:
        rf_baseline = {"error": str(e)}

    # ── CatBoost (native categoricals) ───────────────────────────────────────
    cb_baseline = None
    try:
        from catboost import CatBoostRegressor

        X_cb_train = build_catboost_matrix(train_df)
        X_cb_test  = build_catboost_matrix(test_df)

        cb = CatBoostRegressor(
            iterations=500,
            depth=6,
            learning_rate=0.05,
            loss_function="RMSE",
            cat_features=CAT_COLS,
            random_seed=42,
            verbose=False,
        )
        cb.fit(X_cb_train, y_train)
        cb_pred  = cb.predict(X_cb_test)
        cb_metrics = _regression_metrics(y_test, cb_pred)

        cb_art = {
            "model":          cb,
            "cat_cols":       CAT_COLS,
            "numeric_cols":   NUMERIC_FEATURES,
            "lag_sources":    LAG_SOURCES,
            "lag_days":       LAG_DAYS,
            "trained_on_rows": len(train_df),
            "test_rows":      len(test_df),
            "split_cutoff":   str(cutoff),
            "r2":   round(cb_metrics["r2"],   5),
            "rmse": round(cb_metrics["rmse"], 5),
            "directional_accuracy": round(cb_metrics["directional_accuracy"], 4),
        }
        _CATBOOST_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(cb_art, _CATBOOST_PATH)
        cb_baseline = {**cb_metrics, "model_path": str(_CATBOOST_PATH)}
    except ImportError:
        cb_baseline = {"error": "catboost not installed — pip install catboost"}
    except Exception as e:
        cb_baseline = {"error": str(e)}

    # ── Save XGBoost artifact ─────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model":        model,
        "dummy_cols":   dummy_cols,       # for inference alignment (replaces feature_encoders)
        "lag_sources":  LAG_SOURCES,
        "lag_days":     LAG_DAYS,
        "trained_on_rows": len(train_df),
        "test_rows":    len(test_df),
        "split_cutoff": str(cutoff),
        "best_n_estimators": best_n,
        # Stored so regime_predictor can auto-enable once threshold is reached
        "r2":   round(holdout["r2"],   5),
        "rmse": round(holdout["rmse"], 5),
        "directional_accuracy": round(holdout["directional_accuracy"], 4),
        "ic_mean": ic_mean,
        "icir":    icir,
    }, out_path)

    return {
        "ok":           True,
        "holdout":      holdout,
        "naive":        naive,
        "ic_mean":      ic_mean,
        "ic_std":       ic_std,
        "icir":         icir,
        "pearson_p":    round(float(pearson_p), 4)  if pearson_p  is not None else None,
        "spearman_p":   round(float(spearman_p), 4) if spearman_p is not None else None,
        "cv_metrics":   cv_metrics,
        "cv_summary":   cv_summary,
        "best_n_estimators": best_n,
        "train_rows":   len(train_df),
        "test_rows":    len(test_df),
        "split_cutoff": str(cutoff),
        "rf_baseline":  rf_baseline,
        "catboost":     cb_baseline,
        "model_path":   str(out_path),
        # top-level r2/rmse for regime_predictor config gate compatibility
        "r2":   holdout["r2"],
        "rmse": holdout["rmse"],
        "directional_accuracy": holdout["directional_accuracy"],
    }


# ── CLI output ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)

    h  = result["holdout"]
    rf = result.get("rf_baseline") or {}
    cb = result.get("catboost") or {}
    n  = result.get("naive") or {}

    print(f"Train rows: {result['train_rows']} | Test rows: {result['test_rows']} | Cutoff: {result['split_cutoff']}")
    print(f"Best n_estimators (median of CV folds): {result['best_n_estimators']}")

    # Walk-forward CV summary
    cs = result.get("cv_summary") or {}
    if cs:
        print(f"\n── Walk-forward CV ({len(result['cv_metrics'])} folds) ──────────────────────────")
        print(f"  {'Metric':<26} {'Mean':>10} {'Std':>10}")
        print("  " + "-" * 46)
        for key, label in [("rmse","RMSE"),("r2","R2"),("spearman","Spearman"),("directional_accuracy","Dir.Acc")]:
            print(f"  {label:<26} {cs.get(key+'_mean','—'):>10} {cs.get(key+'_std','—'):>10}")

    # Holdout metrics
    print(f"\n── Holdout test set ─────────────────────────────────────────────────────")
    print(f"  {'Metric':<26} {'XGBoost':>10} {'CatBoost':>10} {'RF':>8} {'Zero':>8} {'Mean':>8}")
    print("  " + "-" * 74)
    rows = [
        ("rmse",                "RMSE"),
        ("mae",                 "MAE"),
        ("r2",                  "R2"),
        ("pearson",             "Pearson r"),
        ("spearman",            "Spearman rho"),
        ("directional_accuracy","Dir.Acc"),
    ]
    for key, label in rows:
        fmt = lambda v: f"{v:>8.4f}" if isinstance(v, float) else "       —"
        print(f"  {label:<26} {fmt(h.get(key))} {fmt(cb.get(key))} {fmt(rf.get(key))} {fmt(n.get('zero',{}).get(key))} {fmt(n.get('mean',{}).get(key))}")
    if cb.get("error"):
        print(f"  CatBoost error: {cb['error']}")

    # Correlation significance
    pp = result.get("pearson_p")
    sp = result.get("spearman_p")
    if pp is not None:
        print(f"\n  Pearson  p-value: {pp:.4f}  {'(significant)' if pp < 0.05 else '(not significant)'}")
    if sp is not None:
        print(f"  Spearman p-value: {sp:.4f}  {'(significant)' if sp < 0.05 else '(not significant)'}")

    # IC
    if result.get("ic_mean") is not None:
        print(f"\n  IC (cross-sectional): mean={result['ic_mean']:.4f}  std={result['ic_std']:.4f}  ICIR={result['icir']}")
        print(f"  (ICIR >= 0.5 is meaningful for factor research; >= 1.0 is strong)")

    print(f"\nModel saved to {result['model_path']}")
