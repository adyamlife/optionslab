"""
Walk-Forward Optimization — rolling out-of-sample evaluation of a trained model.

Slices the labeled dataset into successive test windows and runs inference with
the already-trained model (no re-training per window — this tests temporal
stability of a fixed model, not an expanding-window retrain strategy).

Metrics per fold: AUC, Prec@10, Prec@25, base_rate.
Summary: mean/std/min/max AUC across folds — declining AUC over time signals
regime drift that warrants triggering a retrain.

Conditional evaluation splits folds by VIX quartile to show whether the model
holds up in different volatility regimes.

Run standalone:
  python -m scripts.walk_forward                          # direction, 3-month folds
  python -m scripts.walk_forward --model iv_direction     # IV model
  python -m scripts.walk_forward --model regime           # regime classifier
  python -m scripts.walk_forward --step 1                 # monthly folds
  python -m scripts.walk_forward --conditional            # VIX-conditional breakdown
"""
import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)

SUPPORTED_MODELS = ("direction", "iv_direction", "regime", "return_classifier")


def _precision_at_k(proba: np.ndarray, y_true: np.ndarray, k: int = 25) -> float | None:
    if len(y_true) < k or y_true.sum() == 0:
        return None
    order = np.argsort(proba)[::-1]
    top_k = y_true[order[:k]]
    return round(float(top_k.mean()), 4)


def _fold_stats(aucs: list[float]) -> dict:
    if not aucs:
        return {"mean": None, "std": None, "min": None, "max": None}
    a = np.array(aucs)
    return {
        "mean": round(float(a.mean()), 4),
        "std":  round(float(a.std()),  4),
        "min":  round(float(a.min()),  4),
        "max":  round(float(a.max()),  4),
    }


def run_walk_forward(
    model_name:       str  = "direction",
    step_months:      int  = 3,
    min_train_months: int  = 6,
    conditional:      bool = False,
) -> dict:
    """
    Evaluate a trained model on successive test windows.

    Args:
        model_name:       Key in _BASE_MODEL_PATHS (e.g. "direction", "regime").
        step_months:      Size of each out-of-sample test window (months).
                          Use 1 for monthly folds.
        min_train_months: Minimum historical data required before first test window.
        conditional:      When True, also report per-VIX-quartile and per-regime AUC.

    Returns dict with fold-level results and aggregate AUC statistics.
    """
    from scripts.db import read_df, TABLE
    from scripts.train_meta_ensemble import _load_base_models, _build_X_batch

    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["forward_return"]).sort_values("date").reset_index(drop=True)

    if df.empty:
        return {"ok": False, "error": "No labeled rows in DB"}

    models = _load_base_models()
    art    = models.get(model_name)
    if art is None:
        return {"ok": False, "error": f"Model '{model_name}' not loaded; ensure it is trained"}

    dates    = pd.DatetimeIndex(sorted(df["date"].unique()))
    min_date = dates.min()
    max_date = dates.max()
    step     = pd.DateOffset(months=step_months)
    warmup   = pd.DateOffset(months=min_train_months)

    folds: list[dict] = []
    fold_idx   = 0
    test_start = min_date + warmup

    while test_start <= max_date:
        test_end = test_start + step
        test_df  = df[(df["date"] >= test_start) & (df["date"] < test_end)].copy()

        if len(test_df) < 20:
            test_start = test_end
            continue

        try:
            X_test = _build_X_batch(test_df, art)
            proba  = art["model"].predict_proba(X_test)

            n_cls = art.get("n_direction_classes", proba.shape[1])
            if n_cls == 3:
                p_bull = proba[:, 2].astype(float)
            else:
                p_bull = proba[:, 1].astype(float)

            y_test = (test_df["forward_return"].values > 0).astype(int)

            if y_test.sum() == 0 or y_test.sum() == len(y_test):
                test_start = test_end
                continue

            auc = round(float(roc_auc_score(y_test, p_bull)), 4)
            p10 = _precision_at_k(p_bull, y_test, k=10)
            p25 = _precision_at_k(p_bull, y_test, k=25)

            fold_entry: dict = {
                "fold":       fold_idx,
                "test_start": str(test_start.date()),
                "test_end":   str((test_end - pd.Timedelta(days=1)).date()),
                "n_test":     len(test_df),
                "n_tickers":  int(test_df["ticker"].nunique()) if "ticker" in test_df else None,
                "auc":        auc,
                "prec_at_10": p10,
                "prec_at_25": p25,
                "base_rate":  round(float(y_test.mean()), 4),
            }

            # Regime-conditional breakdown
            if conditional and "regime_label" in test_df.columns:
                cond: dict = {}
                for regime, grp in test_df.groupby("regime_label"):
                    g_mask = test_df.index.isin(grp.index)
                    g_prob = p_bull[g_mask.values]
                    g_y    = y_test[g_mask.values]
                    if len(g_y) >= 10 and g_y.sum() > 0 and g_y.sum() < len(g_y):
                        try:
                            cond[str(regime)] = {
                                "auc": round(float(roc_auc_score(g_y, g_prob)), 4),
                                "n":   int(len(g_y)),
                            }
                        except Exception:
                            pass
                fold_entry["regime_conditional"] = cond

            folds.append(fold_entry)
            fold_idx += 1

        except Exception as e:
            log.warning("[WalkForward] fold %d (%s→%s) failed: %s",
                        fold_idx, test_start.date(), test_end.date(), e)

        test_start = test_end

    if not folds:
        return {"ok": False, "error": "No valid test folds produced — check data volume"}

    aucs = [f["auc"] for f in folds]
    result: dict = {
        "ok":          True,
        "model":       model_name,
        "step_months": step_months,
        "n_folds":     len(folds),
        **_fold_stats(aucs),
        "folds": folds,
    }

    # VIX-quartile conditional: split all test rows by VIX level
    if conditional and "vix_close" in df.columns:
        vix_vals = pd.to_numeric(df["vix_close"], errors="coerce")
        q25, q50, q75 = vix_vals.quantile([0.25, 0.50, 0.75])
        vix_buckets = {
            "low_vix":    df[vix_vals <= q25].index,
            "med_vix":    df[(vix_vals > q25) & (vix_vals <= q50)].index,
            "high_vix":   df[(vix_vals > q50) & (vix_vals <= q75)].index,
            "spike_vix":  df[vix_vals > q75].index,
        }
        vix_cond: dict = {}
        for bucket, idx in vix_buckets.items():
            sub = df.loc[df.index.isin(idx)]
            if len(sub) < 30:
                continue
            try:
                X_sub = _build_X_batch(sub, art)
                p_sub = art["model"].predict_proba(X_sub)
                p_sub = p_sub[:, 2 if n_cls == 3 else 1].astype(float)
                y_sub = (sub["forward_return"].values > 0).astype(int)
                if y_sub.sum() > 0 and y_sub.sum() < len(y_sub):
                    vix_cond[bucket] = {
                        "auc":       round(float(roc_auc_score(y_sub, p_sub)), 4),
                        "n":         int(len(y_sub)),
                        "base_rate": round(float(y_sub.mean()), 4),
                        "vix_range": (round(float(vix_vals.loc[idx].min()), 1),
                                      round(float(vix_vals.loc[idx].max()), 1)),
                    }
            except Exception:
                pass
        result["vix_conditional"] = vix_cond

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Walk-forward model stability evaluation")
    parser.add_argument("--model",       default="direction", choices=SUPPORTED_MODELS)
    parser.add_argument("--step",        type=int, default=3,
                        help="Test window size in months (1 = monthly folds)")
    parser.add_argument("--warmup",      type=int, default=6,
                        help="Min training months before first fold")
    parser.add_argument("--conditional", action="store_true",
                        help="Show per-regime and per-VIX-quartile AUC breakdown")
    args = parser.parse_args()

    result = run_walk_forward(
        model_name       = args.model,
        step_months      = args.step,
        min_train_months = args.warmup,
        conditional      = args.conditional,
    )

    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)

    print(f"\n=== Walk-Forward ({result['model']}, {result['step_months']}-month steps) ===")
    print(f"Folds: {result['n_folds']}")
    print(f"AUC:   {result['mean']:.4f} ± {result['std']:.4f}  "
          f"[{result['min']:.4f} – {result['max']:.4f}]")
    print(f"\n{'Fold':<5} {'Test Period':<27} {'N':>5} {'AUC':>6} {'P@10':>6} {'P@25':>6} {'Base':>5}")
    for f in result["folds"]:
        p10 = f"{f['prec_at_10']:.4f}" if f["prec_at_10"] is not None else "  N/A"
        p25 = f"{f['prec_at_25']:.4f}" if f["prec_at_25"] is not None else "  N/A"
        print(f"{f['fold']:<5} {f['test_start']} → {f['test_end']}  "
              f"{f['n_test']:>5} {f['auc']:>6.4f} {p10:>6} {p25:>6} {f['base_rate']:>5.1%}")

    if result.get("vix_conditional"):
        print("\n=== VIX-Conditional AUC ===")
        for bucket, m in result["vix_conditional"].items():
            vr = m.get("vix_range", ("?", "?"))
            print(f"  {bucket:<12} AUC={m['auc']:.4f}  n={m['n']:>5}  "
                  f"base={m['base_rate']:.1%}  VIX=[{vr[0]:.1f}–{vr[1]:.1f}]")
