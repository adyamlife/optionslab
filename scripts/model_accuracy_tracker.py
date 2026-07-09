"""
Rolling accuracy tracker and drift detector for all trained ML models.

How it works:
  After label_pending_regime_rows() fills in forward_return / regime_label for
  newly-elapsed rows, this module runs every trained model against those same rows
  (out-of-sample — these rows were never in any model's training window) and stores
  per-model accuracy metrics in model_accuracy_log (DuckDB).

  A rolling 30-day window is used: we compute accuracy over the last 30 calendar
  days of labeled rows. This gives a continuously-updated real-world accuracy estimate
  that reflects recent market conditions, not just the held-out test set from training.

Drift thresholds (conservative — flag when clearly degrading, not on normal variance):
  regime_classifier:       accuracy < 0.36   (random baseline = 0.33 for 3 classes)
  direction_classifier:    accuracy < 0.50   (random baseline = 0.50)
  iv_direction_classifier: AUC < 0.52
  return_regressor:        R² < 0.0          (worse than mean predictor)
  volatility_regressor:    R² < 0.0
  meta_ensemble:           AUC < 0.52

Retrain trigger: if ANY model is in drift for 3+ consecutive daily snapshots,
  a retrain flag is set in model_accuracy_log that the scheduler checks.

Run standalone: python -m scripts.model_accuracy_tracker
"""
import sys
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

_ROOT       = Path(__file__).resolve().parent.parent
_MODELS_DIR = _ROOT / "data" / "models"

# ── Drift thresholds ──────────────────────────────────────────────────────────
DRIFT_THRESHOLDS = {
    "regime_classifier":       {"metric": "accuracy", "min": 0.36},
    "direction_classifier":    {"metric": "accuracy", "min": 0.50},
    "iv_direction_classifier": {"metric": "auc",      "min": 0.52},
    "return_regressor":        {"metric": "r2",        "min": 0.00},
    "volatility_regressor":    {"metric": "r2",        "min": 0.00},
    "meta_ensemble":           {"metric": "auc",       "min": 0.52},
}

ROLLING_DAYS  = 30   # window for rolling accuracy
DRIFT_STREAK  = 3    # consecutive drift snapshots before triggering retrain
LOG_TABLE     = "model_accuracy_log"


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect(read_only: bool = False):
    from scripts.db import _DB_PATH
    import duckdb
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(_DB_PATH), read_only=read_only)


def _ensure_log_table():
    """Create model_accuracy_log if it doesn't exist (skipped on read-only connections)."""
    sql = f"""
    CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
        computed_at    TEXT NOT NULL,
        window_days    INTEGER,
        model          TEXT NOT NULL,
        n_samples      INTEGER,
        accuracy       DOUBLE,
        auc            DOUBLE,
        r2             DOUBLE,
        in_drift       BOOLEAN,
        drift_streak   INTEGER,
        retrain_flag   BOOLEAN DEFAULT FALSE
    )
    """
    try:
        with _connect() as con:
            con.execute(sql)
            con.commit()
    except Exception:
        pass  # file locked by another process (e.g. DBeaver) — skip table creation


def _read_log(lookback_days: int = 90) -> pd.DataFrame:
    _ensure_log_table()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    with _connect(read_only=True) as con:
        try:
            return con.execute(
                f"SELECT * FROM {LOG_TABLE} WHERE computed_at >= ? ORDER BY computed_at",
                [cutoff]
            ).df()
        except Exception:
            return pd.DataFrame()


def _save_snapshots(rows: list[dict]) -> None:
    if not rows:
        return
    _ensure_log_table()
    df = pd.DataFrame(rows)
    with _connect() as con:
        con.register("_acc_rows", df)
        con.execute(f"INSERT INTO {LOG_TABLE} SELECT * FROM _acc_rows")
        con.commit()


# ── Model evaluation helpers ──────────────────────────────────────────────────

def _eval_classifier(model_art: dict, df: pd.DataFrame, target_col: str) -> dict:
    """Return accuracy + AUC for a classifier on df[target_col]."""
    from scripts.train_meta_ensemble import _build_X_batch
    try:
        from sklearn.metrics import accuracy_score, roc_auc_score
        X = _build_X_batch(df, model_art)
        y_true = df[target_col].values
        y_pred = model_art["model"].predict(X)
        acc = float(accuracy_score(y_true, y_pred))
        try:
            proba = model_art["model"].predict_proba(X)
            if proba.shape[1] == 2:
                auc = float(roc_auc_score(y_true, proba[:, 1]))
            else:
                auc = float(roc_auc_score(
                    pd.get_dummies(y_true).values, proba,
                    multi_class="ovr", average="macro"
                ))
        except Exception:
            auc = None
        return {"accuracy": acc, "auc": auc, "r2": None, "n": len(y_true)}
    except Exception as e:
        return {"accuracy": None, "auc": None, "r2": None, "n": 0, "error": str(e)}


def _eval_regressor(model_art: dict, df: pd.DataFrame, target_col: str) -> dict:
    """Return R² for a regressor on df[target_col]."""
    from scripts.train_meta_ensemble import _build_X_batch
    try:
        from sklearn.metrics import r2_score
        X = _build_X_batch(df, model_art)
        y_true = df[target_col].values.astype(float)
        y_pred = model_art["model"].predict(X)
        r2 = float(r2_score(y_true, y_pred))
        return {"accuracy": None, "auc": None, "r2": r2, "n": len(y_true)}
    except Exception as e:
        return {"accuracy": None, "auc": None, "r2": None, "n": 0, "error": str(e)}


def _eval_meta(model_art: dict, df: pd.DataFrame, base_models: dict) -> dict:
    """Evaluate meta-ensemble on df using base model outputs as features."""
    try:
        from scripts.train_meta_ensemble import build_meta_dataset
        from sklearn.metrics import accuracy_score, roc_auc_score
        X_meta, y = build_meta_dataset(df, base_models)
        if len(X_meta) < 10:
            return {"accuracy": None, "auc": None, "r2": None, "n": 0}
        y_pred = model_art["model"].predict(X_meta)
        y_prob = model_art["model"].predict_proba(X_meta)[:, 1]
        acc = float(accuracy_score(y, y_pred))
        auc = float(roc_auc_score(y, y_prob))
        return {"accuracy": acc, "auc": auc, "r2": None, "n": len(y)}
    except Exception as e:
        return {"accuracy": None, "auc": None, "r2": None, "n": 0, "error": str(e)}


# ── Direction target builder ──────────────────────────────────────────────────

def _add_direction_target(df: pd.DataFrame) -> pd.DataFrame:
    """Add binary up/down column from forward_return (mirrors train_direction_model logic)."""
    BAND = 0.005
    df = df.copy()
    df["_direction"] = np.where(
        df["forward_return"] >= BAND, 1,
        np.where(df["forward_return"] <= -BAND, 0, np.nan)
    )
    return df.dropna(subset=["_direction"])


# ── Main public function ──────────────────────────────────────────────────────

def compute_rolling_accuracy(window_days: int = ROLLING_DAYS) -> dict:
    """
    Run all trained models against the last `window_days` of labeled regime_training rows.
    Returns per-model metrics dict and list of drift alerts.
    Saves results to model_accuracy_log in DuckDB.
    """
    from scripts.db import read_df, table_exists

    if not table_exists():
        return {"ok": False, "error": "regime_training table does not exist"}

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    df = read_df(
        f"SELECT * FROM regime_training WHERE labeled = true AND date >= '{cutoff}'"
    )
    df = df.dropna(subset=["forward_return", "rsi", "adx", "hv20"])
    df["date"] = pd.to_datetime(df["date"])

    if len(df) < 20:
        return {"ok": False, "error": f"Only {len(df)} labeled rows in last {window_days}d — too few to score"}

    # Load all base model artifacts
    model_files = {
        "regime_classifier":       _MODELS_DIR / "regime_classifier.joblib",
        "return_regressor":        _MODELS_DIR / "return_regressor.joblib",
        "volatility_regressor":    _MODELS_DIR / "volatility_regressor.joblib",
        "direction_classifier":    _MODELS_DIR / "direction_classifier.joblib",
        "iv_direction_classifier": _MODELS_DIR / "iv_direction_classifier.joblib",
        "meta_ensemble":           _MODELS_DIR / "meta_ensemble.joblib",
    }
    artifacts = {}
    for name, path in model_files.items():
        if path.exists():
            try:
                artifacts[name] = joblib.load(path)
            except Exception as e:
                print(f"[tracker] Could not load {name}: {e}")

    base_model_keys = ["regime", "return", "vol", "direction", "iv_direction"]
    base_artifacts_for_meta = {
        "regime":       artifacts.get("regime_classifier"),
        "return":       artifacts.get("return_regressor"),
        "vol":          artifacts.get("volatility_regressor"),
        "direction":    artifacts.get("direction_classifier"),
        "iv_direction": artifacts.get("iv_direction_classifier"),
    }

    computed_at = datetime.now(timezone.utc).isoformat()
    metrics = {}
    log_rows = []

    # ── Regime classifier ──────────────────────────────────────────────────────
    if "regime_classifier" in artifacts:
        sub = df.dropna(subset=["regime_label"])
        art = artifacts["regime_classifier"]
        # Encode target with the stored label encoder
        le = art.get("label_encoder")
        if le and len(sub) >= 10:
            sub = sub.copy()
            sub["_regime_enc"] = le.transform(
                sub["regime_label"].map(lambda v: v if v in le.classes_ else le.classes_[0])
            )
            m = _eval_classifier(art, sub, "_regime_enc")
        else:
            m = {"accuracy": None, "auc": None, "r2": None, "n": 0}
        metrics["regime_classifier"] = m
        in_drift = _is_drift("regime_classifier", m)
        log_rows.append(_make_log_row(computed_at, window_days, "regime_classifier", m, in_drift))

    # ── Return regressor ───────────────────────────────────────────────────────
    if "return_regressor" in artifacts:
        m = _eval_regressor(artifacts["return_regressor"], df, "forward_return")
        metrics["return_regressor"] = m
        in_drift = _is_drift("return_regressor", m)
        log_rows.append(_make_log_row(computed_at, window_days, "return_regressor", m, in_drift))

    # ── Volatility regressor ───────────────────────────────────────────────────
    if "volatility_regressor" in artifacts:
        sub = df.dropna(subset=["forward_hv"])
        if len(sub) >= 10:
            m = _eval_regressor(artifacts["volatility_regressor"], sub, "forward_hv")
        else:
            m = {"accuracy": None, "auc": None, "r2": None, "n": 0}
        metrics["volatility_regressor"] = m
        in_drift = _is_drift("volatility_regressor", m)
        log_rows.append(_make_log_row(computed_at, window_days, "volatility_regressor", m, in_drift))

    # ── Direction classifier ───────────────────────────────────────────────────
    if "direction_classifier" in artifacts:
        sub = _add_direction_target(df)
        if len(sub) >= 10:
            art = artifacts["direction_classifier"]
            le = art.get("label_encoder")
            if le:
                known = set(le.classes_)
                # direction stored as 0/1 int — need to map to the label encoder's classes
                # The direction model uses forward_return > BAND → "Up" / < -BAND → "Down"
                sub = sub.copy()
                sub["_dir_str"] = sub["_direction"].map({1: "Up", 0: "Down"})
                sub["_dir_enc"] = sub["_dir_str"].map(
                    lambda v: le.transform([v])[0] if v in known else 0
                )
                m = _eval_classifier(art, sub, "_dir_enc")
            else:
                m = _eval_classifier(art, sub, "_direction")
        else:
            m = {"accuracy": None, "auc": None, "r2": None, "n": 0}
        metrics["direction_classifier"] = m
        in_drift = _is_drift("direction_classifier", m)
        log_rows.append(_make_log_row(computed_at, window_days, "direction_classifier", m, in_drift))

    # ── IV direction classifier ────────────────────────────────────────────────
    if "iv_direction_classifier" in artifacts:
        sub = df.dropna(subset=["iv_expanding"])
        if len(sub) >= 10:
            m = _eval_classifier(artifacts["iv_direction_classifier"], sub, "iv_expanding")
        else:
            m = {"accuracy": None, "auc": None, "r2": None, "n": 0}
        metrics["iv_direction_classifier"] = m
        in_drift = _is_drift("iv_direction_classifier", m)
        log_rows.append(_make_log_row(computed_at, window_days, "iv_direction_classifier", m, in_drift))

    # ── Meta-ensemble ──────────────────────────────────────────────────────────
    if "meta_ensemble" in artifacts and all(v is not None for v in base_artifacts_for_meta.values()):
        sub = df.dropna(subset=["forward_return"])
        m = _eval_meta(artifacts["meta_ensemble"], sub, base_artifacts_for_meta)
        metrics["meta_ensemble"] = m
        in_drift = _is_drift("meta_ensemble", m)
        log_rows.append(_make_log_row(computed_at, window_days, "meta_ensemble", m, in_drift))

    # ── Streak tracking ────────────────────────────────────────────────────────
    history = _read_log(lookback_days=DRIFT_STREAK * 7 + 5)
    for row in log_rows:
        model = row["model"]
        streak = _compute_streak(history, model)
        row["drift_streak"] = streak + (1 if row["in_drift"] else 0)
        row["retrain_flag"] = row["drift_streak"] >= DRIFT_STREAK

    _save_snapshots(log_rows)

    drift_alerts = [
        {"model": r["model"], "streak": r["drift_streak"], **metrics.get(r["model"], {})}
        for r in log_rows if r["in_drift"]
    ]
    retrain_needed = any(r["retrain_flag"] for r in log_rows)

    print(f"[tracker] Rolling accuracy ({window_days}d, {len(df)} rows):")
    for r in log_rows:
        acc_str  = f"acc={r['accuracy']:.3f}" if r["accuracy"] is not None else "acc=—"
        auc_str  = f"auc={r['auc']:.3f}" if r["auc"] is not None else ""
        r2_str   = f"r²={r['r2']:.3f}" if r["r2"] is not None else ""
        drift_str = " [DRIFT]" if r["in_drift"] else ""
        parts = " ".join(filter(None, [acc_str, auc_str, r2_str]))
        print(f"  {r['model']:<30} {parts}{drift_str}")
    if retrain_needed:
        print("[tracker] RETRAIN TRIGGER -- one or more models in sustained drift")

    return {
        "ok":            True,
        "computed_at":   computed_at,
        "window_days":   window_days,
        "n_rows":        len(df),
        "metrics":       metrics,
        "drift_alerts":  drift_alerts,
        "retrain_needed": retrain_needed,
    }


def _is_drift(model_name: str, m: dict) -> bool:
    th = DRIFT_THRESHOLDS.get(model_name)
    if not th or m.get("n", 0) < 10:
        return False
    val = m.get(th["metric"])
    if val is None:
        return False
    return val < th["min"]


def _make_log_row(computed_at, window_days, model, m, in_drift) -> dict:
    return {
        "computed_at": computed_at,
        "window_days": window_days,
        "model":       model,
        "n_samples":   m.get("n") or 0,
        "accuracy":    m.get("accuracy"),
        "auc":         m.get("auc"),
        "r2":          m.get("r2"),
        "in_drift":    in_drift,
        "drift_streak": 0,
        "retrain_flag": False,
    }


def _compute_streak(history: pd.DataFrame, model: str) -> int:
    """Count consecutive in_drift=True snapshots from most recent going backwards."""
    if history.empty or "model" not in history.columns:
        return 0
    sub = history[history["model"] == model].sort_values("computed_at", ascending=False)
    streak = 0
    for _, row in sub.iterrows():
        if row.get("in_drift"):
            streak += 1
        else:
            break
    return streak


def get_model_health() -> dict:
    """
    Return the latest accuracy snapshot per model plus overall health status.
    Used by the API and UI to show drift warnings.
    """
    _ensure_log_table()
    try:
        with _connect(read_only=True) as con:
            # Check table exists before querying (avoids error when no accuracy run yet)
            tbl_exists = con.execute(
                f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{LOG_TABLE}'"
            ).fetchone()[0]
            if not tbl_exists:
                return {"ok": True, "models": {}, "any_drift": False, "retrain_needed": False, "computed_at": None}
            df = con.execute(f"""
                SELECT * FROM {LOG_TABLE}
                WHERE computed_at = (SELECT MAX(computed_at) FROM {LOG_TABLE})
            """).df()
    except Exception as _e:
        return {"ok": False, "error": str(_e), "models": {}, "any_drift": False, "retrain_needed": False}

    if df.empty:
        return {"ok": True, "models": {}, "any_drift": False, "retrain_needed": False, "computed_at": None}

    def _clean(v):
        """Convert NaN/inf floats to None for JSON-safe output."""
        if v is None:
            return None
        try:
            f = float(v)
            return None if (f != f or f == float("inf") or f == float("-inf")) else f
        except (TypeError, ValueError):
            return v

    models = {}
    for _, row in df.iterrows():
        models[row["model"]] = {
            "accuracy":     _clean(row.get("accuracy")),
            "auc":          _clean(row.get("auc")),
            "r2":           _clean(row.get("r2")),
            "n_samples":    int(row.get("n_samples") or 0),
            "in_drift":     bool(row.get("in_drift")),
            "drift_streak": int(row.get("drift_streak") or 0),
            "retrain_flag": bool(row.get("retrain_flag")),
        }
    return {
        "ok":             True,
        "computed_at":    df["computed_at"].max(),
        "window_days":    int(df["window_days"].iloc[0]) if "window_days" in df.columns else ROLLING_DAYS,
        "models":         models,
        "any_drift":      any(v["in_drift"] for v in models.values()),
        "retrain_needed": any(v["retrain_flag"] for v in models.values()),
    }


def get_accuracy_history(model_name: str, lookback_days: int = 90) -> list[dict]:
    """Return time-series accuracy log for one model (for chart rendering)."""
    df = _read_log(lookback_days=lookback_days)  # already uses read_only
    if df.empty:
        return []
    sub = df[df["model"] == model_name].sort_values("computed_at")
    return sub[["computed_at", "accuracy", "auc", "r2", "n_samples", "in_drift"]].to_dict("records")


if __name__ == "__main__":
    result = compute_rolling_accuracy()
    import json
    print(json.dumps({k: v for k, v in result.items() if k != "metrics"}, indent=2, default=str))
