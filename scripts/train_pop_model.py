"""
POP (Probability-of-Profit) Model — trains on data/training_snapshots.jsonl
to predict win/loss for the recommended candidate, from the same features
the live rulebook already sees (spot, iv_env, trend, regime, rsi/macd/adx,
atm_iv, iv_rank_proxy, hv20, pcr, vix, earnings proximity) PLUS the
candidate's own structure/Greeks/rulebook-estimated POP/EV.

Unlike regime_backfill.py, this CANNOT be backfilled — yfinance/Yahoo has no
historical options data, so every row here only exists because
training_data_collector.py captured it from a real (or yfinance-fallback)
option chain at the time. Labels only exist once a candidate's expiry has
passed and label_pending_snapshots() has filled in outcome.win — so this
script legitimately has nothing to train on until enough snapshots have
aged past their expiry. It will say so plainly rather than fabricate a
result.

Run standalone: python -m scripts.train_pop_model
Output: data/models/pop_classifier.joblib
"""
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, classification_report, confusion_matrix, roc_auc_score
from sklearn.preprocessing import LabelEncoder
from sklearn.utils.class_weight import compute_sample_weight
from xgboost import XGBClassifier

from scripts.training_data_collector import _load_all, load_chain_index, enrich_candidate_greeks

_ROOT = Path(__file__).resolve().parent.parent
_MODEL_PATH = _ROOT / "data" / "models" / "pop_classifier.joblib"

log = logging.getLogger(__name__)

NUMERIC_COLS = [
    "spot", "rsi", "adx", "atm_iv", "iv_rank_proxy", "hv20", "pcr", "vix",
    "earnings_days_away", "signal_score",
    # Tier 1 additions
    "vol_oi_ratio",   # total volume / total OI — unusual activity signal
    "iv_skew",        # OTM put IV minus OTM call IV (%) — fear/skew direction
    "iv_term_slope",  # front_iv / back_iv — >1 = backwardation (near-term fear)
    "otm_pcr",        # OTM put OI / OTM call OI — directional flow signal
    "beta_60d",       # 60-day beta vs SPY — stock's amplification of index moves
    "atr_pct",        # ATR(14) as % of spot — sizing signal for strike distance
    "iv_rank_52w",    # true 52-week IV rank — better than 30-day proxy
    # Tier 2
    "sector_rsi",     # RSI of sector ETF
    "sector_iv_ratio",# stock IV / sector ETF IV — stock-specific vol premium
    "spy_rsi", "qqq_rsi", "iwm_rsi",  # index RSI values
    "vvix",           # vol-of-vol — regime stability signal
    "vix_3m",         # 3-month VIX level
    "vix_term_slope", # VIX / VIX3M — >1 near-term fear, <1 contango
    # Tier 3
    "earnings_inside_expiry",  # 1/0: expiry straddles earnings (strongest IV predictor)
    "news_sentiment_score",    # float [-1,+1]: net bullish/bearish from recent headlines
    "analyst_rec_change",      # int: upgrades - downgrades last 5 days
    "short_interest_pct",      # % of float short — squeeze/pin risk signal
    # Tier 4 — chain-snapshot features (available only from ~Jun 26 2026 onward)
    "iv_skew_20d",             # 20d put IV - 20d call IV (E*TRADE) or None (yfinance)
    "gex_proxy",               # Σ gamma×OI×100 calls - puts (E*TRADE only)
    "max_pain_strike",         # strike minimizing aggregate holder intrinsic value
    "oi_concentration",        # % of total OI within ±2 strikes of ATM
    "wings_iv_ratio",          # 10d put IV ÷ ATM IV (E*TRADE only)
    # Tier 5 — macro context (market-wide, collected each snapshot run)
    "yield_10y",               # 10-year Treasury yield (^TNX)
    "yield_3m",                # 3-month T-bill yield (^IRX)
    "yield_curve",             # yield_10y − yield_3m (negative = inverted curve)
    "dollar_index",            # DX-Y.NYB — strong dollar hurts international-revenue names
    "fed_within_dte",          # 1 if FOMC meeting falls within candidate DTE window
    "cpi_within_dte",          # 1 if CPI release falls within candidate DTE window
]
CANDIDATE_NUMERIC_COLS = [
    "pop", "ev", "max_profit", "max_loss", "net_delta", "net_theta",
    "net_gamma", "net_vega", "capital_required", "dte",
]
CATEGORICAL_COLS = ["iv_env", "trend", "weekly_trend", "regime", "macd_trend",
                    "sector_etf", "sector_trend", "spy_trend", "qqq_trend", "iwm_trend"]
CANDIDATE_CATEGORICAL_COLS = ["structure", "is_credit"]

MIN_LABELED_ROWS = 100  # below this, a train/test split is statistically meaningless


def load_labeled_dataframe() -> pd.DataFrame:
    records = _load_all()
    chain_index = load_chain_index()   # {ticker: {date: {(strike,opt_type): greeks}}}
    enriched_count = 0
    rows = []
    for r in records:
        if not r.get("labeled"):
            continue
        outcome = r.get("outcome") or {}
        if "win" not in outcome:
            continue  # unlabelable structure (Calendar/Diagonal/etc.)
        # Substitute chain-snapshot Greeks when available — more accurate than
        # the candidate's rulebook estimates which are re-priced at collection time.
        r = enrich_candidate_greeks(r, chain_index)
        if (r.get("candidate") or {}).get("_chain_enriched"):
            enriched_count += 1
        candidate = r.get("candidate") or {}
        row = {col: r.get(col) for col in NUMERIC_COLS + CATEGORICAL_COLS}
        row.update({col: candidate.get(col) for col in CANDIDATE_NUMERIC_COLS + CANDIDATE_CATEGORICAL_COLS})
        # Use chain avg IV as atm_iv override when available
        chain_iv = candidate.get("_chain_avg_iv")
        if chain_iv is not None:
            row["atm_iv"] = chain_iv
        row["collected_at"] = r.get("collected_at")
        row["win"] = bool(outcome["win"])
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        log.info("[POP] %d labeled rows loaded — %d enriched with chain Greeks", len(df), enriched_count)
    return df


# Alias used by calibrate_models.py and model_audit.py
load_dataset = load_labeled_dataframe


def time_based_split(df: pd.DataFrame, test_fraction=0.2):
    """Row-count chronological split → (train, test, cutoff_collected_at).
    Returns 3 values for compatibility with calibrate_models / model_audit callers.
    """
    df = df.copy()
    df["collected_at"] = pd.to_datetime(df["collected_at"])
    df = df.sort_values("collected_at")
    cutoff_idx = int(len(df) * (1 - test_fraction))
    train = df.iloc[:cutoff_idx]
    test  = df.iloc[cutoff_idx:]
    cutoff_val = df["collected_at"].iloc[cutoff_idx] if cutoff_idx < len(df) else None
    return train, test, cutoff_val


def _three_way_time_split(df: pd.DataFrame, val_fraction=0.15, test_fraction=0.15):
    """Row-count three-way chronological split: train / val / test.
    val is used to fit the probability calibrator; test is the uncontaminated holdout.
    """
    df = df.copy()
    df["collected_at"] = pd.to_datetime(df["collected_at"])
    df = df.sort_values("collected_at")
    n = len(df)
    val_idx  = int(n * (1 - val_fraction - test_fraction))
    test_idx = int(n * (1 - test_fraction))
    train = df.iloc[:val_idx]
    val   = df.iloc[val_idx:test_idx]
    test  = df.iloc[test_idx:]
    return train, val, test


def build_feature_matrix(df: pd.DataFrame, encoders: dict = None, fit: bool = False):
    encoders = encoders or {}
    X = pd.DataFrame(index=df.index)
    for col in NUMERIC_COLS + CANDIDATE_NUMERIC_COLS:
        X[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else np.nan

    for col in CATEGORICAL_COLS + CANDIDATE_CATEGORICAL_COLS:
        vals = (df[col].fillna("unknown").astype(str)
                if col in df.columns
                else pd.Series(["unknown"] * len(df), index=df.index))
        if fit:
            enc = LabelEncoder()
            all_classes = sorted(set(vals.tolist()) | {"unknown"})
            enc.fit(all_classes)
            X[col] = enc.transform(vals)
            encoders[col] = enc
        else:
            enc = encoders.get(col)
            if enc is None:
                X[col] = 0
            else:
                known = set(enc.classes_)
                safe_vals = vals.map(lambda v: v if v in known else "unknown")
                X[col] = enc.transform(safe_vals)

    return X, encoders


def train(out_path=_MODEL_PATH) -> dict:
    df = load_labeled_dataframe()
    if len(df) < MIN_LABELED_ROWS:
        return {
            "ok": False,
            "error": (
                f"Only {len(df)} labeled snapshot(s) available "
                f"(need at least {MIN_LABELED_ROWS} for a meaningful train/test split). "
                "Snapshots only get labeled once their candidate's expiry has passed — "
                "let training_data_collector.py keep running and try again later."
            ),
            "labeled_rows_available": len(df),
        }

    train_df, val_df, test_df = _three_way_time_split(df)
    if train_df.empty or val_df.empty or test_df.empty:
        return {"ok": False, "error": "Three-way split produced an empty fold."}

    X_train, encoders = build_feature_matrix(train_df, fit=True)
    X_val,   _        = build_feature_matrix(val_df,  encoders=encoders, fit=False)
    X_test,  _        = build_feature_matrix(test_df, encoders=encoders, fit=False)
    y_train = train_df["win"].astype(int).values
    y_val   = val_df["win"].astype(int).values
    y_test  = test_df["win"].astype(int).values

    model = XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        objective="binary:logistic", eval_metric="logloss",
        random_state=42, n_jobs=-1,
    )
    sample_weight = compute_sample_weight(class_weight="balanced", y=y_train)
    model.fit(X_train, y_train, sample_weight=sample_weight)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    acc    = float(accuracy_score(y_test, y_pred))
    auc    = float(roc_auc_score(y_test, y_prob)) if len(set(y_test)) > 1 else None
    report = classification_report(y_test, y_pred, target_names=["Loss", "Win"], output_dict=True)
    cm     = confusion_matrix(y_test, y_pred).tolist()

    # Feature importances keyed by actual column name — immune to ordering drift
    feature_importances = dict(zip(X_train.columns.tolist(), model.feature_importances_.tolist()))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model":            model,
        "feature_encoders": encoders,
        "numeric_cols":     NUMERIC_COLS + CANDIDATE_NUMERIC_COLS,
        "categorical_cols": CATEGORICAL_COLS + CANDIDATE_CATEGORICAL_COLS,
        "trained_on_rows":  len(train_df),
        "val_rows":         len(val_df),
        "test_rows":        len(test_df),
        "accuracy":         round(acc, 4),
        "auc":              round(auc, 4) if auc is not None else None,
    }
    joblib.dump(artifact, out_path)

    # ── Calibrate on val fold (not test fold) to keep test uncontaminated ───────
    brier_before = brier_after = None
    try:
        brier_before = float(brier_score_loss(y_test, y_prob))
        from scripts.calibrate_models import IsotonicCalibrator
        cal_model = IsotonicCalibrator(model)
        cal_model.fit(X_val, y_val)                              # val, not test
        brier_after = float(brier_score_loss(y_test,
                            cal_model.predict_proba(X_test)[:, 1]))
        joblib.dump({**artifact, "model": cal_model, "calibrated": True,
                     "brier_before": round(brier_before, 4),
                     "brier_after":  round(brier_after, 4)},
                    out_path.with_name(out_path.stem + "_calibrated.joblib"))
    except Exception as e:
        log.warning("Calibration failed: %s", e)

    # ── Random Forest comparison (stronger model, not a naive baseline) ───────
    rf_comparison = None
    try:
        rf = RandomForestClassifier(
            n_estimators=200, max_depth=None, min_samples_leaf=5,
            class_weight="balanced", n_jobs=-1, random_state=42,
        )
        rf.fit(X_train, y_train)
        rf_pred  = rf.predict(X_test)
        rf_prob  = rf.predict_proba(X_test)[:, 1]
        rf_acc   = float(accuracy_score(y_test, rf_pred))
        rf_auc   = float(roc_auc_score(y_test, rf_prob)) if len(set(y_test)) > 1 else None
        rf_brier = float(brier_score_loss(y_test, rf_prob))
        rf_comparison = {
            "accuracy": round(rf_acc, 4),
            "auc":   round(rf_auc, 4) if rf_auc is not None else None,
            "brier": round(rf_brier, 4),
        }
    except Exception as e:
        rf_comparison = {"error": str(e)}

    return {
        "ok":           True,
        "accuracy":     round(acc, 4),
        "auc":          round(auc, 4) if auc is not None else None,
        "train_rows":   len(train_df),
        "val_rows":     len(val_df),
        "test_rows":    len(test_df),
        "confusion_matrix": cm,
        "classification_report": report,
        "feature_importances": feature_importances,
        "model_path":   str(out_path),
        "brier_before": round(brier_before, 4) if brier_before is not None else None,
        "brier_after":  round(brier_after, 4)  if brier_after  is not None else None,
        "rf_comparison": rf_comparison,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train()
    if not result.get("ok"):
        print("NOT READY:", result.get("error"))
        sys.exit(0)
    print(f"Train rows: {result['train_rows']} | Val rows: {result['val_rows']} | Test rows: {result['test_rows']}")
    rf = result.get("rf_comparison") or {}
    print(f"\n{'Metric':<26} {'XGBoost':>10} {'RandomForest':>14}")
    print("-" * 52)
    print(f"  {'Accuracy':<24} {result['accuracy']:>10.4f} {rf.get('accuracy', 'n/a'):>14}")
    auc_xgb = result.get("auc")
    auc_rf  = rf.get("auc")
    print(f"  {'AUC':<24} {(f'{auc_xgb:.4f}' if auc_xgb else 'n/a'):>10} {(f'{auc_rf:.4f}' if auc_rf else 'n/a'):>14}")
    brier_xgb = result.get("brier_before")
    brier_rf  = rf.get("brier")
    print(f"  {'Brier (raw)':<24} {(f'{brier_xgb:.4f}' if brier_xgb else 'n/a'):>10} {(f'{brier_rf:.4f}' if brier_rf else 'n/a'):>14}")
    if result.get("brier_after"):
        print(f"  {'Brier (calibrated)':<24} {result['brier_after']:>10.4f} {'—':>14}")
    if rf.get("error"):
        print(f"  RF error: {rf['error']}")
    print("\nConfusion matrix (rows=actual, cols=predicted) [Loss, Win]:")
    for row in result["confusion_matrix"]:
        print(row)
    print(f"\nModel saved to {result['model_path']}")
