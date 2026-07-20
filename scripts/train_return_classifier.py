"""
Return Classifier — replaces the return regressor.

Regression on raw option returns has poor signal (R²=-0.22) because the
distribution is fat-tailed and non-linear. Classification on binary outcomes
works much better. We train three independent binary targets, all derived from
the existing `forward_return` column — no new data collection needed.

Targets:
  p_return_positive  P(return > 0%)   — directional binary (baseline check)
  p_return_gt5       P(return > 5%)   — typical short-put premium capture
  p_return_gt10      P(return > 10%)  — strong winner threshold
  return_decile      top-decile rank label (cross-sectional ranking per date)

All four are stored in the artifact and exposed at inference. The main
inference output `return_score` is a composite: average of the three
threshold probabilities, scaled 0-100.

AUC is the primary metric (same rationale as IV direction model).
Accuracy is reported but secondary — class imbalance makes it misleading.

Run standalone: python -m scripts.train_return_classifier
Output: data/models/return_classifier.joblib
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import (
    accuracy_score, brier_score_loss, classification_report, roc_auc_score,
)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.utils.class_weight import compute_sample_weight
from xgboost import XGBClassifier

_ROOT       = Path(__file__).resolve().parent.parent
_MODEL_PATH = _ROOT / "data" / "models" / "return_classifier.joblib"


from scripts.blended_classifier import BlendedBinaryClassifier as _BlendedBinaryClassifier

RETURN_COL    = "forward_return"
TEST_FRACTION = 0.20
N_CV_SPLITS   = 5

# Thresholds for binary classification targets
_THRESH_GT5  = 0.05
_THRESH_GT10 = 0.10

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

LAG_SOURCES = ["rsi", "adx", "hv20", "vix_close", "rel_strength_spy"]
LAG_DAYS    = [5, 10]
LAG_COLS    = (
    [f"{f}_lag{d}" for f in LAG_SOURCES for d in LAG_DAYS] +
    [f"{f}_chg{d}" for f in LAG_SOURCES for d in LAG_DAYS]
)


# ── Data loading ──────────────────────────────────────────────────────────────

def load_labeled_data() -> pd.DataFrame:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=_REQUIRED_COLS + [RETURN_COL])
    return df


def _add_targets(df: pd.DataFrame) -> pd.DataFrame:
    """Derive binary classification targets from forward_return."""
    df = df.copy()
    ret = pd.to_numeric(df[RETURN_COL], errors="coerce")
    df["y_positive"] = (ret > 0.0).astype(int)
    df["y_gt5"]      = (ret > _THRESH_GT5).astype(int)
    df["y_gt10"]     = (ret > _THRESH_GT10).astype(int)

    # Cross-sectional top-decile rank per date
    df["date"] = pd.to_datetime(df["date"])
    df["y_top_decile"] = 0
    for _, grp in df.groupby("date"):
        if len(grp) < 5:
            continue
        threshold = grp[RETURN_COL].quantile(0.90)
        df.loc[grp.index[grp[RETURN_COL] >= threshold], "y_top_decile"] = 1

    return df


def compute_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = df.sort_values(["ticker", "date"])
    for feat in LAG_SOURCES:
        if feat not in df.columns:
            for d in LAG_DAYS:
                df[f"{feat}_lag{d}"] = np.nan
                df[f"{feat}_chg{d}"] = np.nan
            continue
        col = pd.to_numeric(df[feat], errors="coerce")
        for d in LAG_DAYS:
            lag_col = df.groupby("ticker")[feat].shift(d)
            df[f"{feat}_lag{d}"] = lag_col
            df[f"{feat}_chg{d}"] = col - lag_col
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


# ── Ranking metrics ───────────────────────────────────────────────────────────

def _precision_at_k(proba: np.ndarray, y_true: np.ndarray,
                    ks=(10, 25, 50, 100), returns: np.ndarray = None) -> dict:
    """
    Precision@K, Recall@K, Lift@K, and portfolio metrics for ranked predictions.
    Sorts test rows by descending predicted probability and evaluates the top-K.

    When `returns` (continuous forward_return) is supplied, also computes:
      avg_return@K   — mean realized return of top-K (vs. universe mean)
      win_rate@K     — fraction of top-K with positive return
      profit_factor@K — sum(positive returns) / abs(sum(negative returns))
      sharpe@K        — mean return / std return (raw, not annualised)
    """
    base_rate = float(y_true.mean())
    if base_rate == 0:
        return {}
    order = np.argsort(proba)[::-1]
    n = len(y_true)
    universe_avg_return = float(returns.mean()) if returns is not None else None
    results = {}
    for k in ks:
        if k > n:
            continue
        top_idx = order[:k]
        top_k   = y_true[top_idx]
        prec    = float(top_k.mean())
        recall  = float(top_k.sum() / max(y_true.sum(), 1))
        lift    = round(prec / base_rate, 2) if base_rate > 0 else None
        results[f"P@{k}"]    = round(prec, 4)
        results[f"R@{k}"]    = round(recall, 4)
        results[f"Lift@{k}"] = lift
        if returns is not None:
            top_ret = returns[top_idx]
            avg_ret = float(top_ret.mean())
            win_rate = float((top_ret > 0).mean())
            gains  = top_ret[top_ret > 0]
            losses = top_ret[top_ret < 0]
            pf = (gains.sum() / abs(losses.sum())) if len(losses) > 0 and losses.sum() != 0 else None
            std = float(top_ret.std())
            sharpe = round(avg_ret / std, 3) if std > 0 else None
            results[f"avg_ret@{k}"]      = round(avg_ret, 4)
            results[f"vs_universe@{k}"]  = round(avg_ret - universe_avg_return, 4)
            results[f"win_rate@{k}"]     = round(win_rate, 4)
            results[f"profit_factor@{k}"] = round(pf, 3) if pf is not None else None
            results[f"sharpe@{k}"]       = sharpe
    return results


# ── Extended evaluation functions ────────────────────────────────────────────

def _lift_curve(proba: np.ndarray, y_true: np.ndarray,
                returns: np.ndarray, step: int = 10) -> list[dict]:
    """
    Continuous Lift curve from K=step to min(500, n), every `step` rows.
    Returns a list of dicts so the caller can print or plot it.
    Each entry: {k, precision, lift, avg_ret, win_rate, profit_factor, sharpe}
    """
    base_rate = float(y_true.mean())
    universe_avg = float(returns.mean())
    order = np.argsort(proba)[::-1]
    n = len(y_true)
    curve = []
    for k in range(step, min(500, n) + 1, step):
        top_idx = order[:k]
        top_y   = y_true[top_idx]
        top_r   = returns[top_idx]
        prec    = float(top_y.mean())
        lift    = round(prec / base_rate, 3) if base_rate > 0 else None
        avg_ret = float(top_r.mean())
        win_r   = float((top_r > 0).mean())
        gains   = top_r[top_r > 0]; losses = top_r[top_r < 0]
        pf      = round(gains.sum() / abs(losses.sum()), 3) \
                  if len(losses) > 0 and losses.sum() != 0 else None
        std     = float(top_r.std())
        sharpe  = round(avg_ret / std, 3) if std > 0 else None
        curve.append({
            "k": k, "precision": round(prec, 4), "lift": lift,
            "avg_ret": round(avg_ret, 4),
            "vs_universe": round(avg_ret - universe_avg, 4),
            "win_rate": round(win_r, 4),
            "profit_factor": pf, "sharpe": sharpe,
        })
    return curve


def _calibration_by_decile(proba: np.ndarray, y_true: np.ndarray) -> list[dict]:
    """
    Bin predictions into 10 equal-frequency deciles.
    For each decile: mean predicted probability vs observed positive rate.
    A well-calibrated model has these track each other (predicted ≈ observed).
    """
    n = len(proba)
    order = np.argsort(proba)
    results = []
    decile_size = n // 10
    for d in range(10):
        lo = d * decile_size
        hi = (d + 1) * decile_size if d < 9 else n
        idx = order[lo:hi]
        mean_pred = float(proba[idx].mean())
        obs_rate  = float(y_true[idx].mean())
        results.append({
            "decile":    d + 1,
            "mean_pred": round(mean_pred, 4),
            "obs_rate":  round(obs_rate, 4),
            "gap":       round(obs_rate - mean_pred, 4),
            "n":         len(idx),
        })
    return results


def _walk_forward_metrics(proba: np.ndarray, y_true: np.ndarray,
                          returns: np.ndarray, dates: np.ndarray,
                          top_k: int = 25) -> list[dict]:
    """
    Split the test set by calendar quarter and compute Lift@K and portfolio
    metrics independently in each period. Reveals whether model performance
    is stable over time or concentrated in one lucky stretch.
    """
    dates_dt = pd.to_datetime(dates)
    quarters  = dates_dt.to_period("Q").unique()
    results   = []
    base_rate_global = float(y_true.mean())
    for q in sorted(quarters):
        mask = np.asarray(dates_dt.to_period("Q") == q)
        if mask.sum() < top_k * 2:
            continue
        p_q = proba[mask]; y_q = y_true[mask]; r_q = returns[mask]
        order = np.argsort(p_q)[::-1]
        top_idx = order[:top_k]
        top_y   = y_q[top_idx]; top_r = r_q[top_idx]
        prec    = float(top_y.mean())
        lift    = round(prec / base_rate_global, 3) if base_rate_global > 0 else None
        avg_ret = float(top_r.mean())
        win_r   = float((top_r > 0).mean())
        gains   = top_r[top_r > 0]; losses = top_r[top_r < 0]
        pf      = round(gains.sum() / abs(losses.sum()), 3) \
                  if len(losses) > 0 and losses.sum() != 0 else None
        results.append({
            "quarter": str(q), "n_rows": int(mask.sum()),
            "precision": round(prec, 4), "lift": lift,
            "avg_ret": round(avg_ret, 4),
            "win_rate": round(win_r, 4),
            "profit_factor": pf,
        })
    return results


# ── Per-target trainer ────────────────────────────────────────────────────────

def _train_one(X_train, y_train, X_test, y_test, label: str, tuned: dict) -> dict:
    """Train XGBClassifier for one binary target, return metrics + model."""
    if y_train.sum() < 20:
        return {"error": f"Too few positive examples ({int(y_train.sum())}) for target '{label}'"}

    params = {
        "n_estimators": 400, "max_depth": 4, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.7, "min_child_weight": 5,
        "reg_alpha": 0.1, "reg_lambda": 1.0, "tree_method": "hist",
        "objective": "binary:logistic", "eval_metric": "auc",
        "random_state": 42, "n_jobs": -1,
    }
    params.update(tuned)

    sw = compute_sample_weight(class_weight="balanced", y=y_train)
    model = XGBClassifier(**params)
    model.fit(X_train, y_train, sample_weight=sw, verbose=False)

    # CatBoost ensemble (0.5 XGB + 0.5 CatBoost blend)
    _cb_model = None
    try:
        from catboost import CatBoostClassifier
        _cb = CatBoostClassifier(
            iterations=400, depth=4, learning_rate=0.05,
            loss_function="Logloss", eval_metric="AUC",
            random_seed=42, verbose=0, thread_count=-1,
            auto_class_weights="Balanced",
        )
        _cb.fit(X_train.values, y_train, verbose=False)
        _cb_model = _cb
    except Exception:
        pass

    if _cb_model is not None:
        proba_xgb = model.predict_proba(X_test)[:, 1]
        proba_cb  = _cb_model.predict_proba(X_test.values)[:, 1]
        proba = 0.5 * proba_xgb + 0.5 * proba_cb
    else:
        proba = model.predict_proba(X_test)[:, 1]
    pred  = (proba >= 0.5).astype(int)

    try:
        auc = round(float(roc_auc_score(y_test, proba)), 4)
    except Exception:
        auc = None

    pos_rate_train = round(float(y_train.mean()), 3)
    pos_rate_test  = round(float(y_test.mean()),  3)

    brier_raw = round(float(brier_score_loss(y_test, proba)), 4)

    # Wrap into blended model before calibration (so calibration wraps the ensemble)
    final_model = _BlendedBinaryClassifier(model, _cb_model) if _cb_model is not None else model

    cal_model = final_model
    brier_cal = brier_raw
    try:
        cal = CalibratedClassifierCV(final_model, method="isotonic", cv="prefit")
        cal.fit(X_test, y_test)
        proba_cal = cal.predict_proba(X_test)[:, 1]
        brier_cal = round(float(brier_score_loss(y_test, proba_cal)), 4)
        cal_model = cal
    except Exception:
        pass

    report = classification_report(y_test, pred, output_dict=True, zero_division=0)

    fi = dict(zip(X_train.columns.tolist(), model.feature_importances_.tolist()))

    return {
        "model":          cal_model,
        "auc":            auc,
        "brier_raw":      brier_raw,
        "brier_cal":      brier_cal,
        "accuracy":       round(float(accuracy_score(y_test, pred)), 4),
        "pos_rate_train": pos_rate_train,
        "pos_rate_test":  pos_rate_test,
        "report":         report,
        "feature_importances": fi,
        # proba stored so train() can compute portfolio metrics with actual returns
        "_proba": proba,
    }


# ── Main train() ──────────────────────────────────────────────────────────────

def train(out_path=_MODEL_PATH) -> dict:
    df = load_labeled_data()
    if df.empty:
        return {"ok": False, "error": "No labeled rows available"}

    df = _add_targets(df)
    df = compute_lag_features(df)
    if df.empty:
        return {"ok": False, "error": "No rows remain after computing lag features"}

    train_df, test_df, cutoff = time_based_split(df)
    if train_df.empty or test_df.empty:
        return {"ok": False, "error": f"Split produced empty train/test (cutoff={cutoff})"}

    X_train, dummy_cols = build_feature_matrix(train_df, fit=True)
    X_test,  _          = build_feature_matrix(test_df, dummy_cols=dummy_cols, fit=False)

    try:
        from scripts.tune_hyperparams import load_best_params as _lbp
        tuned = _lbp("return_classifier") or {}
    except Exception:
        tuned = {}

    targets = {
        "positive": ("y_positive", train_df["y_positive"].values, test_df["y_positive"].values),
        "gt5":      ("y_gt5",      train_df["y_gt5"].values,      test_df["y_gt5"].values),
        "gt10":     ("y_gt10",     train_df["y_gt10"].values,     test_df["y_gt10"].values),
        "top_decile": ("y_top_decile", train_df["y_top_decile"].values, test_df["y_top_decile"].values),
    }

    results = {}
    models  = {}
    test_returns = test_df[RETURN_COL].values.astype(float)
    test_dates   = test_df["date"].values

    for name, (_, y_tr, y_te) in targets.items():
        r = _train_one(X_train, y_tr, X_test, y_te, label=name, tuned=tuned)
        if "model" in r:
            models[name] = r.pop("model")
        proba = r.pop("_proba", None)
        if proba is not None:
            r["precision_at_k"] = _precision_at_k(proba, y_te, returns=test_returns)
            if name == "gt10":
                r["lift_curve"]         = _lift_curve(proba, y_te, test_returns)
                r["calibration_deciles"] = _calibration_by_decile(proba, y_te)
                r["walk_forward"]        = _walk_forward_metrics(
                    proba, y_te, test_returns, test_dates, top_k=25)
        results[name] = r

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "models":          models,          # dict: name → trained model
        "dummy_cols":      dummy_cols,
        "numeric_features": NUMERIC_FEATURES,
        "cat_cols":        CAT_COLS,
        "lag_cols":        LAG_COLS,
        "lag_sources":     LAG_SOURCES,
        "lag_days":        LAG_DAYS,
        "thresholds":      {"gt5": _THRESH_GT5, "gt10": _THRESH_GT10},
        "trained_on_rows": len(train_df),
        "test_rows":       len(test_df),
        "split_cutoff":    str(cutoff),
        "target_metrics":  results,
    }, out_path)

    return {
        "ok":           True,
        "train_rows":   len(train_df),
        "test_rows":    len(test_df),
        "split_cutoff": str(cutoff),
        "targets":      results,
        "model_path":   str(out_path),
    }


if __name__ == "__main__":
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)

    print(f"Train rows: {result['train_rows']} | Test rows: {result['test_rows']} | Cutoff: {result['split_cutoff']}")
    print(f"\n{'Target':<18} {'AUC':>8} {'Accuracy':>10} {'Brier raw':>10} {'Brier cal':>10} {'Pos rate (test)':>16}")
    print("-" * 76)
    for name, m in result["targets"].items():
        if "error" in m:
            print(f"  {name:<16}  ERROR: {m['error']}")
            continue
        auc = f"{m['auc']:.4f}" if m.get("auc") is not None else "—"
        print(f"  {name:<16}  {auc:>8}  {m['accuracy']:>10.4f}  {m['brier_raw']:>10.4f}  {m['brier_cal']:>10.4f}  {m['pos_rate_test']:>16.3f}")

    gt10     = result["targets"].get("gt10") or {}
    gt10_pak = gt10.get("precision_at_k") or {}
    base     = gt10.get("pos_rate_test", 0)
    print(f"\nPortfolio metrics — gt10 target (base rate {base:.1%}):")
    print(f"  {'K':>5}  {'Prec':>6}  {'Lift':>6}  {'AvgRet':>8}  {'vsUni':>7}  {'WinRate':>8}  {'ProfitF':>8}  {'Sharpe':>7}")
    print("  " + "-" * 68)
    for k in [10, 25, 50, 100]:
        p  = gt10_pak.get(f"P@{k}")
        l  = gt10_pak.get(f"Lift@{k}")
        ar = gt10_pak.get(f"avg_ret@{k}")
        vu = gt10_pak.get(f"vs_universe@{k}")
        wr = gt10_pak.get(f"win_rate@{k}")
        pf = gt10_pak.get(f"profit_factor@{k}")
        sh = gt10_pak.get(f"sharpe@{k}")
        if p is not None:
            fmt = lambda v, fmt_s: (fmt_s % v) if v is not None else "     —"
            print(f"  {k:>5}  {p:>6.3f}  {l:>5.2f}x"
                  f"  {fmt(ar, '%+8.3f')}"
                  f"  {fmt(vu, '%+7.3f')}"
                  f"  {fmt(wr, '%8.3f')}"
                  f"  {fmt(pf, '%8.2f')}"
                  f"  {fmt(sh, '%7.3f')}")

    # ── Calibration by decile ──────────────────────────────────────────────────
    cal_dec = gt10.get("calibration_deciles") or []
    if cal_dec:
        print(f"\nCalibration by decile (gt10) — predicted prob vs observed rate:")
        print(f"  {'Decile':>7}  {'Predicted':>10}  {'Observed':>9}  {'Gap':>6}  {'N':>6}")
        print("  " + "-" * 44)
        for d in cal_dec:
            gap_sign = "+" if d["gap"] >= 0 else ""
            print(f"  {d['decile']:>7}  {d['mean_pred']:>10.3f}  {d['obs_rate']:>9.3f}"
                  f"  {gap_sign}{d['gap']:>5.3f}  {d['n']:>6}")

    # ── Walk-forward stability ──────────────────────────────────────────────────
    wf = gt10.get("walk_forward") or []
    if wf:
        print(f"\nWalk-forward by quarter (gt10, top-25 per period):")
        print(f"  {'Quarter':>9}  {'N':>6}  {'Prec':>6}  {'Lift':>6}  {'AvgRet':>8}  {'WinRate':>8}  {'ProfitF':>8}")
        print("  " + "-" * 62)
        for p in wf:
            pf_str = f"{p['profit_factor']:>8.2f}" if p["profit_factor"] is not None else "       —"
            print(f"  {p['quarter']:>9}  {p['n_rows']:>6}  {p['precision']:>6.3f}"
                  f"  {p['lift']:>5.2f}x  {p['avg_ret']:>+8.3f}  {p['win_rate']:>8.3f}"
                  f"  {pf_str}")

    # ── Lift curve sample (every 50 rows) ─────────────────────────────────────
    curve = gt10.get("lift_curve") or []
    if curve:
        sample = [c for c in curve if c["k"] % 50 == 0 or c["k"] == 10]
        print(f"\nLift curve — gt10 (sample of full curve stored in artifact):")
        print(f"  {'K':>5}  {'Lift':>6}  {'AvgRet':>8}  {'WinRate':>8}  {'Sharpe':>7}")
        print("  " + "-" * 42)
        for c in sample:
            sh = f"{c['sharpe']:>7.3f}" if c["sharpe"] is not None else "      —"
            print(f"  {c['k']:>5}  {c['lift']:>5.2f}x  {c['avg_ret']:>+8.3f}"
                  f"  {c['win_rate']:>8.3f}  {sh}")

    print(f"\nTop features (by gt5 target):")
    gt5 = result["targets"].get("gt5") or {}
    fi  = gt5.get("feature_importances") or {}
    for f, imp in sorted(fi.items(), key=lambda x: -x[1])[:10]:
        print(f"  {f}: {imp:.3f}")

    print(f"\nModel saved to {result['model_path']}")
