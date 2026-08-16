"""
P0: IV Direction A/B/C experiment.

Isolates which half of the IV direction signal is broken:
  Mode A — current production: HV20 rank as feature, HV20 forward rank as target
  Mode B — fix feature only:   real ATM IV rank from DB as feature, HV20 forward rank as target
  Mode C — fix both:           real ATM IV rank as feature, real forward ATM IV rank as target

Design rationale:
  The iv_expanding target currently labels rows as 1 when HV20 rank (t+10) > HV20 rank (t).
  This predicts realized-vol direction, not IV direction. IV can expand before earnings
  when HV is flat, and crush post-earnings even if HV spikes. Mode C corrects the target.

  Mode B isolates feature quality (real IV rank vs HV20 proxy).
  Mode A vs B shows whether the feature matters.
  Mode B vs C shows whether the target matters.
  A vs C is the full combined effect.

Data coverage:
  Mode A: all labeled regime_training rows (2-year backfill)
  Mode B: rows where training_snapshots has real atm_iv on the same date
  Mode C: rows where training_snapshots has real atm_iv at t AND t+10 trading days

Run:
  python -m scripts.iv_direction_experiment
  python -m scripts.iv_direction_experiment --save   # save Mode C model if it wins
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, brier_score_loss, roc_auc_score,
)

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
log = logging.getLogger(__name__)

FORWARD_DAYS = 10   # must match regime_backfill.FORWARD_DAYS
MIN_ROWS     = 100  # minimum labeled rows required for a mode to be evaluated


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_regime_training() -> pd.DataFrame:
    from scripts.db import read_df
    df = read_df("SELECT * FROM regime_training WHERE labeled = true AND iv_expanding IS NOT NULL")
    return df


def _load_real_iv_series() -> pd.DataFrame:
    """One row per (ticker, calendar_date): the latest atm_iv captured that day.
    Used to compute real IV rank (Mode B feature) and real forward IV rank (Mode C target).
    """
    from scripts.db import connect
    with connect(read_only=True) as con:
        df = con.execute("""
            SELECT ticker,
                   CAST(collected_at AS DATE) AS snap_date,
                   atm_iv
            FROM (
                SELECT ticker, collected_at, atm_iv,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker, CAST(collected_at AS DATE)
                           ORDER BY collected_at DESC
                       ) AS rn
                FROM training_snapshots
                WHERE atm_iv IS NOT NULL AND atm_iv > 0
            ) deduped
            WHERE rn = 1
            ORDER BY ticker, snap_date
        """).df()
    df["snap_date"] = pd.to_datetime(df["snap_date"]).dt.date
    return df


def _compute_real_iv_rank(iv_series: pd.DataFrame, min_window: int = 60,
                           max_window: int = 252) -> pd.DataFrame:
    """For each (ticker, date), compute the true IV percentile rank vs the prior
    max_window trading days of real ATM IV. Returns a df with columns
    [ticker, snap_date, real_iv_rank]."""
    results = []
    for ticker, grp in iv_series.groupby("ticker"):
        grp = grp.sort_values("snap_date").reset_index(drop=True)
        ivs = grp["atm_iv"].values
        dates = grp["snap_date"].values
        ranks = []
        for i, (dt, iv) in enumerate(zip(dates, ivs)):
            lookback = ivs[max(0, i - max_window):i]
            if len(lookback) < min_window:
                ranks.append(np.nan)
            else:
                ranks.append(float((lookback < iv).mean() * 100))
        results.append(pd.DataFrame({
            "ticker": ticker,
            "snap_date": dates,
            "real_iv_rank": ranks,
        }))
    return pd.concat(results, ignore_index=True)


def _compute_forward_real_iv_rank(iv_series: pd.DataFrame,
                                   iv_ranks: pd.DataFrame,
                                   forward: int = FORWARD_DAYS) -> pd.DataFrame:
    """For each (ticker, date), find the real IV rank `forward` trading days later.
    'Trading days later' means the forward-th subsequent date that has a snapshot.
    Returns df with [ticker, snap_date, fwd_real_iv_rank, iv_expanding_real].
    """
    results = []
    for ticker, grp in iv_ranks.groupby("ticker"):
        grp = grp.sort_values("snap_date").reset_index(drop=True)
        dates = list(grp["snap_date"])
        rank_map = dict(zip(grp["snap_date"], grp["real_iv_rank"]))
        fwd_ranks = []
        for i, dt in enumerate(dates):
            # forward-th next date that has a snapshot
            fwd_idx = i + forward
            if fwd_idx < len(dates):
                fwd_date = dates[fwd_idx]
                fwd_ranks.append(rank_map.get(fwd_date, np.nan))
            else:
                fwd_ranks.append(np.nan)
        cur_ranks = grp["real_iv_rank"].values
        fwd_arr   = np.array(fwd_ranks, dtype=float)
        expanding = np.where(
            ~np.isnan(cur_ranks) & ~np.isnan(fwd_arr),
            (fwd_arr > cur_ranks).astype(float),
            np.nan,
        )
        results.append(pd.DataFrame({
            "ticker":           ticker,
            "snap_date":        dates,
            "fwd_real_iv_rank": fwd_arr,
            "iv_expanding_real": expanding,
        }))
    return pd.concat(results, ignore_index=True)


# ── Feature / target assembly ─────────────────────────────────────────────────

def _prep_features(df: pd.DataFrame, encoders: dict = None, fit: bool = False):
    """Build feature matrix using the same build_feature_matrix() as training."""
    from scripts.train_iv_direction_model import build_feature_matrix
    encoders = encoders if encoders is not None else {}
    X, enc = build_feature_matrix(df, encoders=encoders, fit=fit)
    return X, list(X.columns), enc


def _eval_live_model(label: str) -> dict:
    """Evaluate the live iv_direction_classifier artifact on the same test set
    that train_iv_direction_model.train() uses — so Mode A is the real baseline,
    not a simplified fresh model."""
    import joblib
    from scripts.train_iv_direction_model import (
        load_labeled_data, build_feature_matrix, _three_way_time_split,
        _MODEL_PATH, TARGET_COL,
    )
    art = joblib.load(_MODEL_PATH)
    df  = load_labeled_data()
    _, _, test_df, _, _ = _three_way_time_split(df)
    X_test, _ = build_feature_matrix(test_df, encoders=art["feature_encoders"], fit=False)
    y_test    = test_df[TARGET_COL].values
    proba     = art["model"].predict_proba(X_test.fillna(0))[:, 1]
    pred      = (proba >= 0.5).astype(int)
    base      = float(y_test.mean())
    auc       = roc_auc_score(y_test, proba) if len(np.unique(y_test)) > 1 else None
    return {
        "label":    label,
        "n_train":  "live",
        "n_test":   int(len(y_test)),
        "base_rate": round(base, 3),
        "auc":      round(auc, 4) if auc else None,
        "brier":    round(float(brier_score_loss(y_test, proba)), 4),
        "acc":      round(float(accuracy_score(y_test, pred)), 4),
        "bacc":     round(float(balanced_accuracy_score(y_test, pred)), 4),
        "model":    art["model"],
    }


def _full_train_eval(df_override: pd.DataFrame, target_col: str, label: str,
                     save_path=None) -> dict:
    """Train Mode B or C using the real training pipeline (BlendedBinaryClassifier
    + walk-forward CV) on df_override, which has modified features or target.
    Optionally saves the artifact to save_path."""
    from scripts.train_iv_direction_model import (
        build_feature_matrix, _three_way_time_split,
        _find_optimal_threshold, _BlendedBinaryClassifier, N_CV_SPLITS,
        NUMERIC_FEATURES, _CATEGORICAL_COLS,
    )
    from xgboost import XGBClassifier
    from sklearn.utils.class_weight import compute_sample_weight
    from sklearn.model_selection import TimeSeriesSplit

    df = df_override.dropna(subset=[target_col]).sort_values("date").reset_index(drop=True)
    train_df, val_df, test_df, _, _ = _three_way_time_split(df)

    encoders = {}
    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_val,   _        = build_feature_matrix(val_df,   encoders=encoders, fit=False)
    X_test,  _        = build_feature_matrix(test_df,  encoders=encoders, fit=False)
    y_train = train_df[target_col].values
    y_val   = val_df[target_col].values
    y_test  = test_df[target_col].values

    # Walk-forward CV for best n_estimators
    params = {"max_depth": 4, "learning_rate": 0.05, "subsample": 0.8,
              "colsample_bytree": 0.8, "objective": "binary:logistic",
              "eval_metric": "logloss", "random_state": 42, "n_jobs": -1}
    tscv = TimeSeriesSplit(n_splits=min(N_CV_SPLITS, len(np.sort(train_df["date"].unique())) - 1))
    best_iters = []
    for tr_idx, va_idx in tscv.split(X_train.values):
        sw = compute_sample_weight("balanced", y_train[tr_idx])
        fm = XGBClassifier(n_estimators=800, early_stopping_rounds=30, **params)
        fm.fit(X_train.values[tr_idx], y_train[tr_idx], sample_weight=sw,
               eval_set=[(X_train.values[va_idx], y_train[va_idx])], verbose=False)
        best_iters.append(fm.best_iteration)

    best_n = max(10, int(np.median(best_iters))) if best_iters else 200
    sw = compute_sample_weight("balanced", y_train)
    xgb = XGBClassifier(n_estimators=best_n, **params)
    xgb.fit(X_train, y_train, sample_weight=sw)

    cb = None
    try:
        from catboost import CatBoostClassifier
        cb = CatBoostClassifier(iterations=best_n, depth=4, learning_rate=0.05,
                                loss_function="Logloss", random_seed=42,
                                verbose=0, auto_class_weights="Balanced")
        cb.fit(X_train.values, y_train, verbose=False)
    except Exception:
        pass

    model = _BlendedBinaryClassifier(xgb, cb) if cb else xgb
    proba = model.predict_proba(X_test.fillna(0))[:, 1]
    pred  = (proba >= 0.5).astype(int)
    base  = float(y_test.mean())
    auc   = roc_auc_score(y_test, proba) if len(np.unique(y_test)) > 1 else None

    if save_path is not None and auc is not None:
        import joblib
        joblib.dump({
            "model": model, "feature_encoders": encoders,
            "target": target_col, "auc": round(auc, 4),
            "base_rate": round(base, 3), "calibrated": False,
        }, save_path)
        print(f"  Saved → {save_path}")

    return {
        "label":    label,
        "n_train":  int(len(y_train)),
        "n_test":   int(len(y_test)),
        "base_rate": round(base, 3),
        "auc":      round(auc, 4) if auc else None,
        "brier":    round(float(brier_score_loss(y_test, proba)), 4),
        "acc":      round(float(accuracy_score(y_test, pred)), 4),
        "bacc":     round(float(balanced_accuracy_score(y_test, pred)), 4),
        "model":    model,
    }


# ── Main experiment ───────────────────────────────────────────────────────────

def run(save_mode_c: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("\nLoading regime_training (labeled rows)...")
    rt = _load_regime_training()
    print(f"  {len(rt)} labeled rows across {rt['ticker'].nunique()} tickers")
    if "date" not in rt.columns:
        rt["date"] = pd.to_datetime(rt.get("collected_at", rt.index)).dt.date

    print("Loading real IV series from training_snapshots...")
    iv_series = _load_real_iv_series()
    n_tickers = iv_series["ticker"].nunique()
    n_days    = iv_series["snap_date"].nunique()
    print(f"  {len(iv_series)} rows  |  {n_tickers} tickers  |  "
          f"{n_days} calendar days  ({iv_series['snap_date'].min()} – {iv_series['snap_date'].max()})")

    print("Computing real IV rank per ticker-date...")
    iv_ranks = _compute_real_iv_rank(iv_series)
    have_rank = iv_ranks["real_iv_rank"].notna().sum()
    print(f"  {have_rank}/{len(iv_ranks)} rows have real_iv_rank")

    print(f"Computing real forward IV rank (t+{FORWARD_DAYS} trading days)...")
    iv_fwd = _compute_forward_real_iv_rank(iv_series, iv_ranks)
    have_fwd = iv_fwd["iv_expanding_real"].notna().sum()
    print(f"  {have_fwd}/{len(iv_fwd)} rows have iv_expanding_real")

    # ── Merge real IV features/targets into regime_training ──
    rt["snap_date"] = pd.to_datetime(rt["date"]).dt.date
    rt = rt.merge(
        iv_ranks[["ticker", "snap_date", "real_iv_rank"]],
        on=["ticker", "snap_date"], how="left",
    )
    rt = rt.merge(
        iv_fwd[["ticker", "snap_date", "iv_expanding_real"]],
        on=["ticker", "snap_date"], how="left",
    )

    results = []

    # ── Mode A: live model on its own test set (true baseline) ───────────────
    print("\n── Mode A: live model (HV proxy feature + HV proxy target) ──")
    try:
        r = _eval_live_model("A: live model — HV proxy feature + HV proxy target")
        results.append(r)
        print(f"  n_test={r['n_test']}  base={r['base_rate']:.1%}  "
              f"AUC={r['auc']}  Brier={r['brier']}  BAcc={r['bacc']:.3f}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # ── Mode B: real IV rank feature + HV proxy target ───────────────────────
    print("\n── Mode B: real IV rank feature + HV proxy target ──")
    df_b = rt[rt["iv_expanding"].notna() & rt["real_iv_rank"].notna()].copy()
    if len(df_b) >= MIN_ROWS:
        # Replace iv_rank_52w with real ATM IV rank in the training data
        df_b["iv_rank_52w"] = df_b["real_iv_rank"]
        r = _full_train_eval(df_b, "iv_expanding", "B: real IV feature + HV proxy target")
        results.append(r)
        print(f"  n_train={r['n_train']}  n_test={r['n_test']}  base={r['base_rate']:.1%}  "
              f"AUC={r['auc']}  Brier={r['brier']}  BAcc={r['bacc']:.3f}")
    else:
        print(f"  SKIP — only {len(df_b)} rows with real_iv_rank (need {MIN_ROWS})")
        print(f"  Accumulating: {len(df_b)} rows. Need real ATM IV on ~{MIN_ROWS} more ticker-days.")

    # ── Mode C: real IV rank feature + real forward IV target ────────────────
    print("\n── Mode C: real IV rank feature + real forward IV target ──")
    df_c = rt[rt["iv_expanding_real"].notna() & rt["real_iv_rank"].notna()].copy()
    if len(df_c) >= MIN_ROWS:
        df_c["iv_rank_52w"] = df_c["real_iv_rank"]
        from scripts.train_iv_direction_model import _MODEL_PATH
        save_path = Path(_MODEL_PATH) if save_mode_c else None
        r = _full_train_eval(df_c, "iv_expanding_real",
                             "C: real IV feature + real IV target", save_path=save_path)
        results.append(r)
        print(f"  n_train={r['n_train']}  n_test={r['n_test']}  base={r['base_rate']:.1%}  "
              f"AUC={r['auc']}  Brier={r['brier']}  BAcc={r['bacc']:.3f}")
    else:
        days_available = rt["real_iv_rank"].notna().sum()
        days_fwd       = rt["iv_expanding_real"].notna().sum()
        print(f"  SKIP — only {len(df_c)} rows with both real IV rank and real forward IV")
        print(f"  real_iv_rank available: {days_available} ticker-days")
        print(f"  iv_expanding_real available: {days_fwd} ticker-days")
        print(f"  Need min_window(60) + forward(10) = 70 trading days of real ATM IV collection")

    # ── Summary table ────────────────────────────────────────────────────────
    if results:
        print("\n" + "=" * 78)
        print(f"{'Mode':<45} {'N_te':>6} {'Base':>6} {'AUC':>7} {'Brier':>7} {'BAcc':>7}")
        print("-" * 78)
        for r in results:
            auc_s = f"{r['auc']:.4f}" if r["auc"] else "  n/a "
            print(f"  {r['label']:<43} {r['n_test']:>6} {r['base_rate']:>6.1%} "
                  f"{auc_s:>7} {r['brier']:>7.4f} {r['bacc']:>7.3f}")
        print("=" * 78)

        print("\nInterpretation:")
        print("  A→B delta: effect of fixing the feature (real IV rank vs HV20 proxy)")
        print("  B→C delta: effect of fixing the target (real forward IV vs HV20 forward)")
        print("  A→C delta: total improvement from both fixes")
        print("  Use AUC as primary metric — Brier rewards calibration too.")
        if len(results) >= 2:
            aucs = [(r["label"], r["auc"]) for r in results if r["auc"] is not None]
            if len(aucs) >= 2:
                best = max(aucs, key=lambda x: x[1])
                print(f"\n  Best mode: {best[0]}  (AUC={best[1]:.4f})")
                if "C" in best[0] and not save_mode_c:
                    print("  → Re-run with --save to persist the Mode C model.")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true",
                        help="Save Mode C model if it has the highest AUC")
    args = parser.parse_args()
    run(save_mode_c=args.save)
