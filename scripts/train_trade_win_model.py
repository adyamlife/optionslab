"""
Trade-Win Model v1 — four-variant experiment (system v2 data).

Trains four XGB classifiers on the same labeled dataset, each with a
different feature set. Target is always `win` from training_snapshots.

Data scope: post-2026-07-13 only (system v2 — Long Strangle + Calendar
Spread dominant). Pre-Jul-13 data excluded because the optimizer was
replaced over the Jul 11-12 weekend, creating incompatible structure
distributions across the chronological train/test split.

Variants:
  structure_only  structure (one-hot) + DTE
  market_only     17 base-model outputs (same as meta_ensemble.joblib)
  trade_only      structure + DTE + raw snapshot market inputs
  combined        market outputs + structure + DTE

Evaluation:
  AUC, Accuracy, Precision@3/5/10/25, avg predicted P(win) of Top-3,
  avg realized win rate of Top-3, calibration by decile.

Output:
  data/models/trade_win_v1_<variant>.joblib
  Prints a comparison table for all four variants.

Run:
  python -m scripts.train_trade_win_model
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, accuracy_score
from xgboost import XGBClassifier
from scripts.calibrate_models import IsotonicCalibrator as _IsoCal

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)

_MODELS_DIR = _ROOT / "data" / "models"

# ── System v2 cutoff — optimizer replaced Jul 12-13 weekend ──────────────────
# Pre-cutoff data has incompatible structure distribution (debit-spread dominant).
# Training on mixed pre/post data causes train-on-v1 / test-on-v2 leakage.
_SYSTEM_V2_CUTOFF = pd.Timestamp("2026-07-13")

# ── Raw snapshot market-context features (available at entry, not ML outputs) ──
_RAW_MARKET_FEATURES = [
    "atm_iv", "iv_rank_proxy", "hv20", "vix", "rsi", "adx", "pcr",
    "iv_pct_rank", "gamma_pct_rank", "momentum_pct_rank", "oi_pct_rank",
    "is_opex_week", "is_monthly_opex", "days_to_opex",
    "ppi_within_dte", "jobs_within_dte",
]

# ── Trade entry features (populated for all rows) ─────────────────────────────
# Geometry columns (spread_width etc.) are Phase 1 additions — NULL for rows
# collected before Phase 1 deployed to Ubuntu. Use DTE only for v1 experiment.
_ENTRY_FEATURES = ["dte"]

# F5: earnings features joined at training time (NaN if no earnings data available)
from scripts.earnings_features import EARNINGS_FEATURE_COLS, load_earnings_join  # noqa: E402

# ── Valid structures in system v2 (≥50 labeled rows post-Jul-13) ──────────────
_VALID_STRUCTURES = [
    "Calendar Spread",
    "Long Strangle",
]

_KNOWN_STRUCTURES = _VALID_STRUCTURES  # used for one-hot column alignment

_XGB_PARAMS = dict(
    n_estimators=300, max_depth=3, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8,
    objective="binary:logistic", eval_metric="logloss",
    random_state=42, n_jobs=-1, verbosity=0,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _precision_at_k(proba: np.ndarray, y: np.ndarray, ks=(3, 5, 10, 25)) -> dict:
    base = float(y.mean())
    order = np.argsort(proba)[::-1]
    out: dict = {}
    for k in ks:
        if k > len(y):
            continue
        top_k = y[order[:k]]
        prec  = float(top_k.mean())
        out[f"P@{k}"]       = round(prec, 3)
        out[f"Lift@{k}"]    = round(prec / base, 2) if base > 0 else None
    # avg predicted P(win) and realized win rate for top-3
    if 3 <= len(y):
        out["avg_pred_top3"]     = round(float(proba[order[:3]].mean()), 3)
        out["avg_realized_top3"] = round(float(y[order[:3]].mean()), 3)
    return out


def _calibration_by_decile(proba: np.ndarray, y: np.ndarray) -> list[dict]:
    df = pd.DataFrame({"p": proba, "y": y})
    df["decile"] = pd.qcut(df["p"], 10, labels=False, duplicates="drop")
    rows = []
    for d, grp in df.groupby("decile"):
        rows.append({
            "decile":   int(d) + 1,
            "pred_mean": round(float(grp["p"].mean()), 3),
            "obs_mean":  round(float(grp["y"].mean()), 3),
            "n":         len(grp),
        })
    return rows


def _train_variant(
    X_tr: pd.DataFrame, y_tr: np.ndarray,
    X_val: pd.DataFrame, y_val: np.ndarray,
    X_te: pd.DataFrame, y_te: np.ndarray,
    label: str,
) -> dict:
    clf = XGBClassifier(**_XGB_PARAMS)
    clf.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
    proba = clf.predict_proba(X_te)[:, 1]
    auc   = float(roc_auc_score(y_te, proba)) if len(np.unique(y_te)) > 1 else None
    acc   = float(accuracy_score(y_te, (proba >= 0.5).astype(int)))
    pak   = _precision_at_k(proba, y_te)
    cal   = _calibration_by_decile(proba, y_te)
    return {
        "label":        label,
        "n_features":   X_tr.shape[1],
        "features":     list(X_tr.columns),
        "model":        clf,
        "auc":          round(auc, 4) if auc else None,
        "accuracy":     round(acc, 4),
        "base_rate":    round(float(y_te.mean()), 4),
        "calibration":  cal,
        **pak,
    }


# ── Dataset builder ───────────────────────────────────────────────────────────

def build_dataset() -> pd.DataFrame:
    """
    Join training_snapshots (labeled, win target) with regime_training
    (base-model outputs via train_meta_ensemble.build_meta_dataset).
    Returns a DataFrame with all feature families + 'win' column.
    """
    from scripts.db import read_df, connect, SNAPSHOTS_TABLE, TABLE
    from scripts.train_meta_ensemble import (
        build_meta_dataset, _load_base_models, _derive_meta_cutoff, META_FEATURES,
    )

    # ── Labeled snapshots — system v2 only (post Jul-13) ─────────────────────
    snaps = read_df(
        f"""
        SELECT
            ticker,
            LEFT(CAST(collected_at AS VARCHAR), 10) AS date,
            CAST(JSON_EXTRACT(outcome, '$.win') AS BOOLEAN)  AS win,
            JSON_EXTRACT_STRING(candidate, '$.structure')    AS structure,
            dte,
            atm_iv, iv_rank_proxy, hv20, vix, rsi, adx, pcr,
            iv_pct_rank, gamma_pct_rank, momentum_pct_rank, oi_pct_rank,
            is_opex_week, is_monthly_opex, days_to_opex,
            ppi_within_dte, jobs_within_dte
        FROM {SNAPSHOTS_TABLE}
        WHERE labeled = true
          AND JSON_EXTRACT(outcome, '$.win') IS NOT NULL
          AND collected_at >= '2026-07-13'
          AND JSON_EXTRACT_STRING(candidate, '$.structure') IN (
              'Calendar Spread', 'Long Strangle'
          )
        """
    )
    snaps["date"] = pd.to_datetime(snaps["date"])
    snaps = snaps.dropna(subset=["win"])
    snaps["win"] = snaps["win"].astype(int)

    # ── Base-model outputs (regime_training → meta-feature matrix) ────────────
    models     = _load_base_models()
    meta_cutoff = _derive_meta_cutoff(models)

    rt = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    rt = rt.dropna(subset=["forward_return", "rsi", "adx", "hv20"])
    rt["date"] = pd.to_datetime(rt["date"])
    rt = rt[rt["date"] >= meta_cutoff].copy()
    rt["forward_return"] = rt["forward_return"].fillna(0.0)

    X_meta, _ = build_meta_dataset(rt, models)
    meta_df = pd.DataFrame(X_meta.values, columns=META_FEATURES)
    meta_df["ticker"] = rt["ticker"].values
    meta_df["date"]   = rt["date"].values

    # ── Join snapshots + meta-features ────────────────────────────────────────
    df = snaps.merge(meta_df, on=["ticker", "date"], how="inner")
    log.info("Dataset: %d labeled rows after join (snaps=%d, meta=%d)",
             len(df), len(snaps), len(meta_df))

    # F5: add earnings features (NaN for rows without prior earnings data)
    df = load_earnings_join(df)
    return df


def _week_stratified_split(df: pd.DataFrame, fractions: list[float]) -> list[pd.DataFrame]:
    """Split df into len(fractions) folds, chronological *within* each ISO week but
    stratified *across* weeks so every fold gets a proportional slice of every week.

    A pure "last N% by date" split breaks badly when a labeling backlog (e.g. a
    scheduler outage) gets backfilled in one batch: the newest fold ends up
    dominated by a single narrow, previously-absent week, producing a spurious
    train/test distribution shift that looks like model degradation rather than
    the sampling artifact it actually is. Same fix applied to train_pop_model.py
    after diagnosing its identical symptom (2026-08-22).
    """
    assert abs(sum(fractions) - 1.0) < 1e-6, "fractions must sum to 1.0"
    df = df.sort_values("date")
    week_key = df["date"].dt.isocalendar().year.astype(str) + "-W" + \
               df["date"].dt.isocalendar().week.astype(str).str.zfill(2)

    folds: list[list[pd.DataFrame]] = [[] for _ in fractions]
    cum_fractions = np.cumsum(fractions)
    for _, wk_df in df.groupby(week_key, sort=True):
        n = len(wk_df)
        cut_idxs = [0] + [int(round(n * f)) for f in cum_fractions]
        for i in range(len(fractions)):
            folds[i].append(wk_df.iloc[cut_idxs[i]:cut_idxs[i + 1]])

    return [pd.concat(f).sort_values("date") if f else pd.DataFrame(columns=df.columns) for f in folds]


def _print_split_health(train_df: pd.DataFrame, val_df: pd.DataFrame, test_df: pd.DataFrame) -> None:
    """Print date ranges and structure counts for each split."""
    splits = [("train", train_df), ("val", val_df), ("test", test_df)]
    print(f"\n{'─'*60}")
    print("  Split health")
    print(f"{'─'*60}")
    for name, rows in splits:
        d0 = rows["date"].min().date()
        d1 = rows["date"].max().date()
        wr = rows["win"].mean()
        print(f"  {name:<6}  n={len(rows):>4}  {d0} → {d1}  win={wr:.1%}")
        sc = rows.groupby("structure")["win"].agg(["count","mean"])
        for struct, (n, w) in sc.iterrows():
            print(f"           {struct:<22} n={int(n):>4}  win={w:.1%}")
    print(f"{'─'*60}")


def run() -> None:
    from scripts.train_meta_ensemble import META_FEATURES

    print(f"Building dataset (system v2 — post {_SYSTEM_V2_CUTOFF.date()})...")
    df = build_dataset()
    n  = len(df)
    print(f"Total rows: {n:,}  |  Win rate: {df['win'].mean():.1%}  |  "
          f"Structures: {sorted(df['structure'].unique().tolist())}")

    if n < 200:
        print("Too few rows — need more labeled data.")
        return

    # ── One-hot encode structure ───────────────────────────────────────────────
    df["structure"] = df["structure"].fillna("Unknown")
    structure_dummies = pd.get_dummies(df["structure"], prefix="struct")
    for s in _KNOWN_STRUCTURES:
        col = f"struct_{s}"
        if col not in structure_dummies.columns:
            structure_dummies[col] = 0
    struct_cols = sorted(structure_dummies.columns.tolist())
    df = pd.concat([df, structure_dummies[struct_cols]], axis=1)

    # ── Week-stratified split: 60 / 20 / 20 ──────────────────────────────────
    # Chronological *within* each ISO week, stratified *across* weeks — see
    # _week_stratified_split docstring for why a pure "last 20% by date" split
    # is unsafe on this dataset (same labeling-backfill artifact that broke
    # the POP model's split on 2026-08-22).
    train_df, val_df, test_df = _week_stratified_split(df, [0.60, 0.20, 0.20])
    y_tr  = train_df["win"].values
    y_val = val_df["win"].values
    y_te  = test_df["win"].values

    _print_split_health(train_df, val_df, test_df)

    def _split(cols):
        avail = [c for c in cols if c in df.columns]
        return (train_df[avail].fillna(0), val_df[avail].fillna(0), test_df[avail].fillna(0))

    entry_cols  = _ENTRY_FEATURES + struct_cols   # DTE + structure one-hots
    trade_cols  = entry_cols + _RAW_MARKET_FEATURES
    market_cols = META_FEATURES
    # F5: add earnings features to the combined variant only (keep others clean for comparison)
    ef_cols     = [c for c in EARNINGS_FEATURE_COLS if c in df.columns]
    combined    = market_cols + entry_cols + ef_cols

    variants = [
        ("structure_only", _split(entry_cols)),
        ("market_only",    _split(market_cols)),
        ("trade_only",     _split(trade_cols)),
        ("combined",       _split(combined)),
    ]

    print()
    results = []
    for label, (Xtr, Xval, Xte) in variants:
        print(f"  Training {label} ({Xtr.shape[1]} features)...")
        r = _train_variant(Xtr, y_tr, Xval, y_val, Xte, y_te, label)
        results.append(r)
        path = _MODELS_DIR / f"trade_win_v1_{label}.joblib"
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "model":       r["model"],
            "features":    r["features"],
            "target":      "win",
            "variant":     label,
            "system":      "v2",
            "cutoff":      str(_SYSTEM_V2_CUTOFF.date()),
            "auc":         r["auc"],
            "base_rate":   r["base_rate"],
        }, path)

    # ── Comparison table ───────────────────────────────────────────────────────
    base = float(y_te.mean())
    print(f"\n{'='*78}")
    print(f"  System v2 data  |  test n={len(y_te)}  |  base rate={base:.3f}")
    print(f"{'─'*78}")
    print(f"  {'Variant':<18} {'Feats':>5}  {'AUC':>6}  {'P@3':>5}  "
          f"{'P@5':>5}  {'P@10':>5}  {'P@25':>5}  "
          f"{'Top3-pred':>9}  {'Top3-real':>9}")
    print(f"  {'Base rate':<18} {'':>5}  {'':>6}  {base:.3f}  "
          f"{base:.3f}  {base:.3f}  {base:.3f}")
    print(f"{'─'*78}")
    for r in results:
        print(
            f"  {r['label']:<18} {r['n_features']:>5}  {r['auc']:>6.4f}  "
            f"{r.get('P@3', 0):>5.3f}  {r.get('P@5', 0):>5.3f}  "
            f"{r.get('P@10', 0):>5.3f}  {r.get('P@25', 0):>5.3f}  "
            f"{r.get('avg_pred_top3', 0):>9.3f}  "
            f"{r.get('avg_realized_top3', 0):>9.3f}"
        )
    print(f"{'='*78}")

    # ── Canonical calibrated artifact (combined variant + isotonic calibration) ──
    # Raw combined probabilities are severely compressed (all deciles underpredict).
    # IsotonicCalibrator corrects monotonically using the val fold.
    try:
        _combined_r = next(r for r in results if r["label"] == "combined")
        _, _splits_c = next((l, s) for l, s in variants if l == "combined")
        _Xtr_c, _Xval_c, _Xte_c = _splits_c
        _cal_model = _IsoCal(_combined_r["model"], n_classes=2)
        _cal_model.fit(_Xval_c, y_val)
        _cal_proba_te = _cal_model.predict_proba(_Xte_c)[:, 1]
        _cal_auc = float(roc_auc_score(y_te, _cal_proba_te)) if len(np.unique(y_te)) > 1 else None
        _cal_decile = _calibration_by_decile(_cal_proba_te, y_te)
        joblib.dump({
            "model":      _cal_model,
            "features":   _combined_r["features"],
            "target":     "win",
            "variant":    "combined_calibrated",
            "system":     "v2",
            "cutoff":     str(_SYSTEM_V2_CUTOFF.date()),
            "auc":        _cal_auc,
            "base_rate":  _combined_r["base_rate"],
            "calibrated": True,
            "n_classes":  2,
        }, _MODELS_DIR / "trade_win_classifier.joblib")
        print(f"\nCalibration — combined_calibrated (test n={len(y_te)}):")
        print(f"  {'Decile':>6}  {'Pred':>6}  {'Obs':>6}  {'N':>4}  {'Gap':>7}")
        print(f"  {'─'*36}")
        for _row in _cal_decile:
            _gap  = _row["obs_mean"] - _row["pred_mean"]
            _flag = "  ←" if abs(_gap) > 0.10 else ""
            print(f"  {_row['decile']:>6}  {_row['pred_mean']:>6.3f}  "
                  f"{_row['obs_mean']:>6.3f}  {_row['n']:>4}  {_gap:>+7.3f}{_flag}")
        _auc_str = f"{_cal_auc:.4f}" if _cal_auc is not None else "n/a"
        print(f"\nCanonical artifact saved: trade_win_classifier.joblib  "
              f"(calibrated combined  AUC={_auc_str})")
    except Exception as _e:
        print(f"\n[WARN] Could not save canonical trade_win_classifier.joblib: {_e}")

    # ── Calibration by decile — all variants ──────────────────────────────────
    for r in results:
        print(f"\nCalibration — {r['label']} (test n={len(y_te)}):")
        print(f"  {'Decile':>6}  {'Pred':>6}  {'Obs':>6}  {'N':>4}  {'Gap':>7}")
        print(f"  {'─'*36}")
        for row in r["calibration"]:
            gap  = row["obs_mean"] - row["pred_mean"]
            flag = "  ←" if abs(gap) > 0.10 else ""
            print(f"  {row['decile']:>6}  {row['pred_mean']:>6.3f}  "
                  f"{row['obs_mean']:>6.3f}  {row['n']:>4}  {gap:>+7.3f}{flag}")


if __name__ == "__main__":
    run()
