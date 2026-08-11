"""
Ablation study for the meta-ensemble.

Trains the meta-learner incrementally, adding one signal group at a time,
and records AUC + Precision@K after each addition.  Also compares the
full 17-feature set against a pruned 7-feature set (one representative
per independent signal cluster).

Signal groups (in addition order):
  1. Regime       — p_uptrend, p_downtrend, p_rangebound, expected_vol, regime_entropy
  2. + Return     — p_return_positive, p_return_gt5, p_return_gt10, p_top_decile, return_score
  3. + IV         — iv_expanding_prob, iv_confidence
  4. + Direction  — p_up, p_flat, p_down, direction_entropy, pred_std

Extra comparisons:
  Pruned-7        — one representative per cluster + two uncertainty measures
  No-entropy      — Pruned-7 without regime_entropy (ablates that feature)
  No-vol          — Pruned-7 without expected_vol   (ablates that feature)

Run:
  python -m scripts.ablation_meta_ensemble
"""
from __future__ import annotations

import sys
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from xgboost import XGBClassifier

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.WARNING)

# ── Signal groups ──────────────────────────────────────────────────────────────

_GROUPS: list[tuple[str, list[str]]] = [
    ("Regime",    ["p_uptrend", "p_downtrend", "p_rangebound", "expected_vol", "regime_entropy"]),
    ("+ Return",  ["p_return_positive", "p_return_gt5", "p_return_gt10", "p_top_decile", "return_score"]),
    ("+ IV",      ["iv_expanding_prob", "iv_confidence"]),
    ("+ Direction", ["p_up", "p_flat", "p_down", "direction_entropy", "pred_std"]),
]

_PRUNED_7 = [
    "expected_vol",
    "p_return_positive", "p_top_decile",
    "iv_expanding_prob",
    "p_up",
    "pred_std", "regime_entropy",
]

_EXTRA: list[tuple[str, list[str]]] = [
    ("Pruned-7",    _PRUNED_7),
    ("No-entropy",  [f for f in _PRUNED_7 if f != "regime_entropy"]),
    ("No-vol",      [f for f in _PRUNED_7 if f != "expected_vol"]),
]


# ── XGB params (same as meta-ensemble) ────────────────────────────────────────

_XGB_PARAMS = dict(
    n_estimators=300,
    max_depth=3,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    use_label_encoder=False,
    eval_metric="logloss",
    random_state=42,
    verbosity=0,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _precision_at_k(proba: np.ndarray, y: np.ndarray, ks=(10, 25, 50)) -> dict:
    base = float(y.mean())
    order = np.argsort(proba)[::-1]
    out = {}
    for k in ks:
        if k > len(y):
            continue
        top = y[order[:k]]
        prec = float(top.mean())
        out[f"P@{k}"] = round(prec, 3)
        out[f"Lift@{k}"] = round(prec / base, 2) if base > 0 else None
    return out


def _train_eval(X_tr, y_tr, X_val, y_val, X_te, y_te, features: list[str]) -> dict:
    cols = [c for c in features if c in X_tr.columns]
    if not cols:
        return {"auc": None, "accuracy": None}

    clf = XGBClassifier(**_XGB_PARAMS)
    clf.fit(
        X_tr[cols], y_tr,
        eval_set=[(X_val[cols], y_val)],
        verbose=False,
    )
    proba = clf.predict_proba(X_te[cols])[:, 1]
    preds = (proba >= 0.5).astype(int)

    auc  = round(float(roc_auc_score(y_te, proba)), 4)
    acc  = round(float((preds == y_te).mean()), 4)
    pak  = _precision_at_k(proba, y_te)
    return {"auc": auc, "accuracy": acc, "n_features": len(cols), **pak}


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    from scripts.db import connect
    from scripts.train_meta_ensemble import build_meta_dataset, _load_base_models

    print("Loading base models and regime data...")
    models = _load_base_models()

    with connect(read_only=True) as con:
        df_raw = con.execute("SELECT * FROM regime_training ORDER BY date, ticker").df()

    df_raw["date"] = pd.to_datetime(df_raw["date"])

    from scripts.train_meta_ensemble import _derive_meta_cutoff
    meta_cutoff = _derive_meta_cutoff(models)
    df = df_raw[df_raw["date"] >= meta_cutoff].copy()

    # build_meta_dataset only uses forward_return to construct y — inject a
    # dummy so inference runs, then build y ourselves from labeled win outcomes.
    df["forward_return"] = 0.0

    print(f"Meta-dataset: {len(df):,} rows  (cutoff: {meta_cutoff.date()})")

    if len(df) < 200:
        print("Too few rows for ablation — need more labeled data after meta-cutoff.")
        return

    X_meta, _ = build_meta_dataset(df, models)   # _ = dummy y

    # Build real target: join to labeled rows, use win as the binary outcome.
    # win=1 means the trade succeeded (structure-specific condition met).
    from scripts.db import read_df, SNAPSHOTS_TABLE
    labeled = read_df(
        f"SELECT ticker, LEFT(CAST(collected_at AS VARCHAR), 10) AS date, "
        f"CAST(JSON_EXTRACT(outcome, '$.win') AS BOOLEAN) AS win "
        f"FROM {SNAPSHOTS_TABLE} WHERE labeled = true"
    )
    labeled["date"] = pd.to_datetime(labeled["date"])
    labeled = labeled.dropna(subset=["win"])

    meta_with_y = X_meta.reset_index(drop=True)
    meta_with_y["ticker"] = df["ticker"].values
    meta_with_y["date"]   = df["date"].values
    meta_with_y = meta_with_y.merge(labeled[["ticker", "date", "win"]], on=["ticker", "date"], how="inner")

    if len(meta_with_y) < 100:
        print(f"Too few labeled matches after meta-cutoff ({len(meta_with_y)} rows). "
              "Falling back to forward_return > 0 as target (from regime_training).")
        # Fallback: use actual forward_return from regime_training if available
        col = "forward_return_x" if "forward_return_x" in df.columns else \
              "forward_return"   if "forward_return"   in df.columns else None
        if col:
            df2 = df.copy()
            df2["_fr"] = df2[col].fillna(0.0)
            df2["forward_return"] = df2["_fr"]
            X_meta, y_ser = build_meta_dataset(df2, models)
            y = y_ser
        else:
            print("No usable target column found — cannot run ablation.")
            return
        X_for_split = X_meta
    else:
        feature_cols = [c for c in X_meta.columns if c in meta_with_y.columns]
        X_for_split = meta_with_y[feature_cols]
        y = meta_with_y["win"].astype(int).values
        print(f"Labeled matches after cutoff: {len(meta_with_y):,}")

    # Chronological train / val / test split (60 / 20 / 20)
    n = len(X_for_split)
    i1, i2 = int(n * 0.60), int(n * 0.80)
    X_tr, y_tr   = X_for_split.iloc[:i1],  y[:i1]
    X_val, y_val = X_for_split.iloc[i1:i2], y[i1:i2]
    X_te, y_te   = X_for_split.iloc[i2:],  y[i2:]

    print(f"Split — train: {len(X_tr)}  val: {len(X_val)}  test: {len(X_te)}")
    print(f"Test Up%: {float(y_te.mean()):.1%}  (majority-class naive: {float(y_te.mean()):.4f})\n")

    # ── Incremental group ablation ─────────────────────────────────────────────
    print("=" * 76)
    print(f"{'Step':<20} {'Features':>8}  {'AUC':>6}  {'Acc':>6}  "
          f"{'P@10':>6}  {'Lift@10':>8}  {'P@25':>6}  {'Lift@25':>8}")
    print("=" * 76)

    cumulative: list[str] = []
    for label, group in _GROUPS:
        cumulative.extend(group)
        r = _train_eval(X_tr, y_tr, X_val, y_val, X_te, y_te, cumulative)
        print(
            f"  {label:<18} {r['n_features']:>8}  {r['auc']:>6.4f}  {r['accuracy']:>6.4f}  "
            f"{r.get('P@10', 0):>6.3f}  {r.get('Lift@10', 0):>8.2f}x  "
            f"{r.get('P@25', 0):>6.3f}  {r.get('Lift@25', 0):>8.2f}x"
        )

    print("-" * 76)

    # ── Extra comparisons ──────────────────────────────────────────────────────
    for label, features in _EXTRA:
        r = _train_eval(X_tr, y_tr, X_val, y_val, X_te, y_te, features)
        print(
            f"  {label:<18} {r['n_features']:>8}  {r['auc']:>6.4f}  {r['accuracy']:>6.4f}  "
            f"{r.get('P@10', 0):>6.3f}  {r.get('Lift@10', 0):>8.2f}x  "
            f"{r.get('P@25', 0):>6.3f}  {r.get('Lift@25', 0):>8.2f}x"
        )

    print("=" * 76)
    print("\nNote: rows are chronologically ordered — test set is the most recent 20%.")

    # ── Leave-one-out ablation ─────────────────────────────────────────────────
    all_features = [f for grp in _GROUPS for f in grp[1]]
    r_full = _train_eval(X_tr, y_tr, X_val, y_val, X_te, y_te, all_features)
    full_auc  = r_full["auc"]
    full_p25  = r_full.get("P@25", 0.0) or 0.0
    full_p10  = r_full.get("P@10", 0.0) or 0.0

    print(f"\n{'=' * 72}")
    print("Leave-one-out — ΔAUC and ΔP@25 when a signal group is removed")
    print(f"  Baseline (all {r_full['n_features']} features):  "
          f"AUC={full_auc:.4f}  P@10={full_p10:.3f}  P@25={full_p25:.3f}")
    print(f"{'=' * 72}")
    print(f"  {'Removed group':<20} {'Feats':>5}  {'AUC':>6}  {'ΔAUC':>7}  "
          f"{'P@25':>6}  {'ΔP@25':>7}  {'Verdict'}")
    print(f"  {'-' * 68}")

    for label, group in _GROUPS:
        remaining = [f for f in all_features if f not in group]
        r = _train_eval(X_tr, y_tr, X_val, y_val, X_te, y_te, remaining)
        dauc = r["auc"] - full_auc
        dp25 = (r.get("P@25", 0.0) or 0.0) - full_p25
        verdict = (
            "unique signal" if dauc < -0.005 else
            "marginal"     if -0.005 <= dauc <= 0.005 else
            "adds noise"
        )
        print(
            f"  {'– ' + label:<20} {r['n_features']:>5}  {r['auc']:>6.4f}  "
            f"{dauc:>+7.4f}  {r.get('P@25', 0.0) or 0.0:>6.3f}  "
            f"{dp25:>+7.3f}  {verdict}"
        )

    print(f"{'=' * 72}")
    print("Δ = (without group) − (full baseline).  "
          "Negative ΔAUC = removing the group hurts = group has unique value.")

    # ── Group-level permutation importance ────────────────────────────────────
    # Train the full model once, then permute each group's columns jointly on
    # the test set and re-score — no retraining.  This measures how much the
    # fitted stacker *relies* on each group at inference time.
    cols_full = [c for c in all_features if c in X_tr.columns]
    clf_full  = XGBClassifier(**_XGB_PARAMS)
    clf_full.fit(
        X_tr[cols_full], y_tr,
        eval_set=[(X_val[cols_full], y_val)],
        verbose=False,
    )
    proba_base = clf_full.predict_proba(X_te[cols_full])[:, 1]
    auc_base   = float(roc_auc_score(y_te, proba_base))

    rng = np.random.default_rng(42)

    print(f"\n{'=' * 72}")
    print("Group-level permutation importance  (fitted model, test set, 10 shuffles)")
    print(f"  Baseline AUC: {auc_base:.4f}")
    print(f"{'=' * 72}")
    print(f"  {'Group':<20} {'Mean ΔAUC':>10}  {'Std':>6}  {'Verdict'}")
    print(f"  {'-' * 58}")

    perm_results: list[tuple[str, float, float]] = []
    for label, group in _GROUPS:
        grp_cols = [c for c in group if c in X_te.columns]
        if not grp_cols:
            continue
        deltas: list[float] = []
        X_perm = X_te[cols_full].copy()
        for _ in range(50):
            saved = X_perm[grp_cols].values.copy()
            for col in grp_cols:
                X_perm[col] = rng.permuted(X_perm[col].values)
            p = clf_full.predict_proba(X_perm[cols_full])[:, 1]
            deltas.append(float(roc_auc_score(y_te, p)) - auc_base)
            # restore for next iteration
            X_perm[grp_cols] = saved
        mean_d = float(np.mean(deltas))
        std_d  = float(np.std(deltas))
        verdict = (
            "relied upon"  if mean_d < -0.005 else
            "marginal"     if -0.005 <= mean_d <= 0.005 else
            "ignored"
        )
        perm_results.append((label, mean_d, std_d))
        print(
            f"  {label:<20} {mean_d:>+10.4f}  {std_d:>6.4f}  {verdict}"
        )

    print(f"{'=' * 72}")
    print("Δ = permuted − baseline.  Negative = model relied on that group.")
    print("Compare ranking to leave-one-out: agreement = robust finding; "
          "divergence = interaction effect worth investigating.")


if __name__ == "__main__":
    run()
