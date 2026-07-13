"""
Rolling accuracy tracker and drift detector for all trained ML models.

How it works:
  After label_pending_regime_rows() fills in forward_return / regime_label for
  newly-elapsed rows, this module runs every trained model against those rows
  and stores per-model accuracy metrics in model_accuracy_log (DuckDB).

  Each evaluation uses only TRULY out-of-sample rows — rows whose date is
  strictly after the model's test_cutoff (stored in the artifact). If a model
  artifact lacks test_cutoff, the last 30d window is used with a warning.
  This prevents inflated metrics when models are retrained on rolling windows.

Rolling window: up to ROLLING_DAYS (30) of labeled rows, but only rows that
  postdate the model's own training test set. MIN_SAMPLES (100) is required
  before any drift threshold fires.

Drift detection — three-layer approach (all three must pass MIN_SAMPLES check):
  1. Hard floor  — absolute minimum metric value (catches catastrophic failure)
  2. Relative    — current < 0.85 × training metric from artifact
  3. Statistical — current < rolling_mean - 2 × rolling_std (10+ history points)
  For accuracy metrics, the Wilson 95% confidence interval lower bound is used
  instead of the raw value — greatly reduces false positives from small windows.

Retrain trigger: DRIFT_STREAK (3) consecutive DAILY snapshots in drift for any
  model. Streak resets if the gap between snapshots exceeds STREAK_MAX_GAP_DAYS.

Run standalone: python -m scripts.model_accuracy_tracker
"""
import logging
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_ROOT       = Path(__file__).resolve().parent.parent
_MODELS_DIR = _ROOT / "data" / "models"

# ── Constants ─────────────────────────────────────────────────────────────────

ROLLING_DAYS       = 30
MIN_SAMPLES        = 100   # minimum rows before drift thresholds fire
DRIFT_STREAK       = 3     # consecutive daily drift snapshots to trigger retrain
STREAK_MAX_GAP_DAYS = 3    # max calendar-day gap to consider snapshots consecutive
LOG_TABLE          = "model_accuracy_log"

# Hard-floor thresholds (point 4): absolute lower bound — catches catastrophic failure.
# Relative thresholds (0.85 × training metric) fire on top of these.
DRIFT_THRESHOLDS = {
    "regime_classifier":       {"metric": "accuracy",          "hard_min": 0.36},
    "direction_classifier":    {"metric": "accuracy",          "hard_min": 0.50},
    "iv_direction_classifier": {"metric": "auc",               "hard_min": 0.52},
    "return_regressor":        {"metric": "r2",                "hard_min": 0.00},
    "volatility_regressor":    {"metric": "r2",                "hard_min": 0.00},
    "meta_ensemble":           {"metric": "auc",               "hard_min": 0.52},
}

# Fields written to the log table for each snapshot
_LOG_COLS = [
    "computed_at", "window_days", "model", "n_samples",
    "accuracy", "balanced_accuracy", "f1_macro",
    "auc", "r2", "rmse", "mae",
    "in_drift", "drift_reason", "drift_streak", "retrain_flag",
]


# ── Artifact cache (point 14) ─────────────────────────────────────────────────
# Avoids re-loading joblib files on every scheduler tick.
# Cache invalidates automatically when mtime changes.

_artifact_cache: dict[str, tuple[float, dict]] = {}


def _load_artifact(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        mtime   = path.stat().st_mtime
        cached  = _artifact_cache.get(str(path))
        if cached is not None and cached[0] == mtime:
            return cached[1]
        art = joblib.load(path)
        _artifact_cache[str(path)] = (mtime, art)
        return art
    except Exception as e:
        log.warning("[tracker] Could not load %s: %s", path.name, e)
        return None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect(read_only: bool = False):
    from scripts.db import _DB_PATH
    import duckdb
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(_DB_PATH), read_only=read_only)


def _ensure_log_table() -> None:
    """Create model_accuracy_log if absent; idempotently add new columns."""
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
        computed_at       TEXT NOT NULL,
        window_days       INTEGER,
        model             TEXT NOT NULL,
        n_samples         INTEGER,
        accuracy          DOUBLE,
        balanced_accuracy DOUBLE,
        f1_macro          DOUBLE,
        auc               DOUBLE,
        r2                DOUBLE,
        rmse              DOUBLE,
        mae               DOUBLE,
        in_drift          BOOLEAN,
        drift_reason      TEXT,
        drift_streak      INTEGER,
        retrain_flag      BOOLEAN DEFAULT FALSE
    )
    """
    # Idempotent column additions for tables created before this schema
    new_cols = [
        ("balanced_accuracy", "DOUBLE"),
        ("f1_macro",          "DOUBLE"),
        ("rmse",              "DOUBLE"),
        ("mae",               "DOUBLE"),
        ("drift_reason",      "TEXT"),
    ]
    try:
        with _connect() as con:
            con.execute(create_sql)
            for col, dtype in new_cols:
                try:
                    con.execute(f"ALTER TABLE {LOG_TABLE} ADD COLUMN IF NOT EXISTS {col} {dtype}")
                except Exception:
                    pass
            con.commit()
    except Exception:
        pass


def _read_log(lookback_days: int = 90) -> pd.DataFrame:
    _ensure_log_table()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    try:
        with _connect(read_only=True) as con:
            return con.execute(
                f"SELECT * FROM {LOG_TABLE} WHERE computed_at >= ? ORDER BY computed_at",
                [cutoff]
            ).df()
    except Exception:
        return pd.DataFrame()


def _save_snapshots(rows: list[dict], max_retries: int = 3) -> None:
    """Insert today's snapshots; replaces any existing rows for the same (model, date)."""
    if not rows:
        return
    _ensure_log_table()

    today          = date.today().isoformat()
    models_in_rows = list({r["model"] for r in rows})
    # Ensure every row has all columns (fill absent ones with None)
    normalized = [{col: r.get(col) for col in _LOG_COLS} for r in rows]
    df = pd.DataFrame(normalized)

    for attempt in range(max_retries):
        try:
            with _connect() as con:
                # Delete today's stale entries for these models before re-inserting (point 8)
                for m in models_in_rows:
                    con.execute(
                        f"DELETE FROM {LOG_TABLE} WHERE model = ? AND computed_at = ?",
                        [m, today]
                    )
                con.register("_acc_rows", df)
                con.execute(f"INSERT INTO {LOG_TABLE} SELECT * FROM _acc_rows")
                con.commit()
            return
        except Exception as e:
            if attempt == max_retries - 1:
                log.error("[tracker] Failed to save snapshots after %d attempts: %s", max_retries, e)
                return
            wait = 0.5 * (2 ** attempt)   # 0.5s → 1s → 2s
            log.warning("[tracker] DB write failed (%s), retrying in %.1fs…", e, wait)
            time.sleep(wait)


# ── OOS filtering (point 1) ───────────────────────────────────────────────────

def _oos_filter(df: pd.DataFrame, art: dict, model_name: str) -> pd.DataFrame:
    """
    Keep only rows that are genuinely out-of-sample for this model artifact.
    Uses the test_cutoff stored in the artifact when available.
    Falls back to the full window with a warning — the reported metrics will
    be optimistic if training used a rolling window.
    """
    for key in ("test_cutoff", "meta_cutoff", "split_cutoff"):
        cutoff_str = art.get(key)
        if cutoff_str:
            cutoff = pd.Timestamp(str(cutoff_str))
            n_before = len(df)
            df = df[df["date"] > cutoff]
            log.debug("[tracker] %s: %d rows after OOS filter (cutoff %s)", model_name, len(df), cutoff.date())
            if len(df) < n_before * 0.1 and len(df) < MIN_SAMPLES:
                log.warning(
                    "[tracker] %s: only %d OOS rows after cutoff %s — "
                    "too little time has elapsed since training. Metrics unreliable.",
                    model_name, len(df), cutoff.date()
                )
            return df
    log.warning(
        "[tracker] %s: artifact has no test_cutoff — cannot verify OOS status. "
        "Metrics may be inflated if model was trained on rolling data.",
        model_name
    )
    return df


# ── Wilson confidence interval (point 13) ─────────────────────────────────────

def _wilson_lower(n_correct: int, n_total: int, confidence: float = 0.95) -> float:
    """95% Wilson score CI lower bound for a proportion. Reduces noise on small samples."""
    if n_total <= 0:
        return 0.0
    from scipy.stats import norm
    p  = n_correct / n_total
    z  = float(norm.ppf((1 + confidence) / 2))
    denom  = 1 + z ** 2 / n_total
    center = (p + z ** 2 / (2 * n_total)) / denom
    margin = z * np.sqrt(p * (1 - p) / n_total + z ** 2 / (4 * n_total ** 2)) / denom
    return float(np.clip(center - margin, 0.0, 1.0))


# ── Drift detection (points 4, 13) ────────────────────────────────────────────

def _is_drift(
    model_name: str,
    m: dict,
    art: dict | None = None,
    history: pd.DataFrame | None = None,
) -> tuple[bool, str]:
    """
    Return (in_drift, reason). Three layers:
      1. Hard floor — absolute minimum (catches catastrophic failure)
      2. Relative   — current < 0.85 × training metric from artifact
      3. Statistical — current < rolling_mean - 2σ (requires 10+ history points)
    Accuracy uses Wilson CI lower bound to reduce false positives.
    Returns (False, "") if n_samples < MIN_SAMPLES.
    """
    th = DRIFT_THRESHOLDS.get(model_name)
    if not th or m.get("n", 0) < MIN_SAMPLES:
        return False, ""

    metric_name = th["metric"]
    current     = m.get(metric_name)
    if current is None:
        return False, ""

    # For accuracy, use Wilson CI lower bound instead of raw value
    if metric_name == "accuracy":
        n = int(m.get("n", 0))
        current = _wilson_lower(int(round(current * n)), n)

    # Layer 1: hard floor
    if current < th["hard_min"]:
        return True, f"{metric_name}={current:.4f} < hard_min={th['hard_min']}"

    # Layer 2: relative to training-time metric stored in artifact
    if art is not None:
        train_val = art.get(metric_name)
        if train_val is not None:
            relative_min = 0.85 * float(train_val)
            if current < relative_min:
                return True, (
                    f"{metric_name}={current:.4f} < 0.85×training_{metric_name} "
                    f"({relative_min:.4f})"
                )

    # Layer 3: statistical — 2σ below recent rolling mean
    if history is not None and not history.empty:
        sub = history[history["model"] == model_name][metric_name].dropna()
        if len(sub) >= 10:
            mu, sigma = float(sub.mean()), float(sub.std())
            if sigma > 0 and current < mu - 2 * sigma:
                return True, (
                    f"{metric_name}={current:.4f} < rolling_mean-2σ "
                    f"({mu:.4f}-2×{sigma:.4f}={mu-2*sigma:.4f})"
                )

    return False, ""


# ── Feature builders ──────────────────────────────────────────────────────────

def _build_X(model_name: str, art: dict, df: pd.DataFrame):
    """Route each model to its correct feature builder."""
    if model_name == "iv_direction_classifier" and "dummy_cols" in art:
        from scripts.train_iv_direction_model import build_feature_matrix as _iv_feat
        X, _ = _iv_feat(df, encoders=art.get("feature_encoders") or {}, fit=False)
        return X
    from scripts.train_meta_ensemble import _build_X_batch
    return _build_X_batch(df, art)


# ── Model evaluation helpers ──────────────────────────────────────────────────

def _eval_classifier(model_name: str, art: dict, df: pd.DataFrame, target_col: str) -> dict:
    """Return accuracy, balanced_accuracy, f1_macro, AUC for a classifier."""
    from sklearn.metrics import (
        accuracy_score, balanced_accuracy_score, f1_score, roc_auc_score,
    )
    try:
        X      = _build_X(model_name, art, df)
        y_true = df[target_col].values
        y_pred = art["model"].predict(X)
        acc      = float(accuracy_score(y_true, y_pred))
        bal_acc  = float(balanced_accuracy_score(y_true, y_pred))
        f1       = float(f1_score(y_true, y_pred, average="macro", zero_division=0))

        auc = None
        try:
            proba = art["model"].predict_proba(X)
            if proba.shape[1] == 2:
                auc = float(roc_auc_score(y_true, proba[:, 1]))
            else:
                auc = float(roc_auc_score(
                    pd.get_dummies(y_true).values, proba,
                    multi_class="ovr", average="macro"
                ))
        except ValueError as e:
            log.warning("[tracker] AUC unavailable for %s: %s", model_name, e)

        return {"accuracy": acc, "balanced_accuracy": bal_acc, "f1_macro": f1,
                "auc": auc, "r2": None, "rmse": None, "mae": None, "n": len(y_true)}
    except Exception as e:
        return {"accuracy": None, "balanced_accuracy": None, "f1_macro": None,
                "auc": None, "r2": None, "rmse": None, "mae": None, "n": 0, "error": str(e)}


def _eval_regressor(model_name: str, art: dict, df: pd.DataFrame, target_col: str) -> dict:
    """Return R², RMSE, MAE for a regressor."""
    from sklearn.metrics import r2_score, mean_squared_error, mean_absolute_error
    try:
        X      = _build_X(model_name, art, df)
        y_true = df[target_col].values.astype(float)
        y_pred = art["model"].predict(X)
        r2   = float(r2_score(y_true, y_pred))
        rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
        mae  = float(mean_absolute_error(y_true, y_pred))
        return {"accuracy": None, "balanced_accuracy": None, "f1_macro": None,
                "auc": None, "r2": r2, "rmse": rmse, "mae": mae, "n": len(y_true)}
    except Exception as e:
        return {"accuracy": None, "balanced_accuracy": None, "f1_macro": None,
                "auc": None, "r2": None, "rmse": None, "mae": None, "n": 0, "error": str(e)}


def _eval_meta(art: dict, df: pd.DataFrame, base_models: dict) -> dict:
    """Evaluate meta-ensemble using base model outputs as features."""
    from sklearn.metrics import accuracy_score, roc_auc_score, balanced_accuracy_score, f1_score
    from scripts.train_meta_ensemble import build_meta_dataset
    try:
        X_meta, y = build_meta_dataset(df, base_models)
        if len(X_meta) < 10:
            return {"accuracy": None, "balanced_accuracy": None, "f1_macro": None,
                    "auc": None, "r2": None, "rmse": None, "mae": None, "n": 0}
        y_pred = art["model"].predict(X_meta)
        y_prob = art["model"].predict_proba(X_meta)[:, 1]
        auc = None
        try:
            auc = float(roc_auc_score(y, y_prob))
        except ValueError as e:
            log.warning("[tracker] AUC unavailable for meta_ensemble: %s", e)
        return {
            "accuracy":          float(accuracy_score(y, y_pred)),
            "balanced_accuracy": float(balanced_accuracy_score(y, y_pred)),
            "f1_macro":          float(f1_score(y, y_pred, average="macro", zero_division=0)),
            "auc": auc, "r2": None, "rmse": None, "mae": None, "n": len(y),
        }
    except Exception as e:
        return {"accuracy": None, "balanced_accuracy": None, "f1_macro": None,
                "auc": None, "r2": None, "rmse": None, "mae": None, "n": 0, "error": str(e)}


# ── Log row helpers ───────────────────────────────────────────────────────────

def _add_direction_target(df: pd.DataFrame) -> pd.DataFrame:
    BAND = 0.005
    df   = df.copy()
    df["_direction"] = np.where(
        df["forward_return"] >= BAND,  1,
        np.where(df["forward_return"] <= -BAND, 0, np.nan)
    )
    return df.dropna(subset=["_direction"])


def _make_log_row(computed_at: str, window_days: int, model: str, m: dict,
                  in_drift: bool, drift_reason: str = "") -> dict:
    return {
        "computed_at":       computed_at,
        "window_days":       window_days,
        "model":             model,
        "n_samples":         m.get("n") or 0,
        "accuracy":          m.get("accuracy"),
        "balanced_accuracy": m.get("balanced_accuracy"),
        "f1_macro":          m.get("f1_macro"),
        "auc":               m.get("auc"),
        "r2":                m.get("r2"),
        "rmse":              m.get("rmse"),
        "mae":               m.get("mae"),
        "in_drift":          in_drift,
        "drift_reason":      drift_reason,
        "drift_streak":      0,
        "retrain_flag":      False,
    }


def _compute_streak(history: pd.DataFrame, model: str) -> int:
    """Count consecutive DAILY drift snapshots going backwards (point 5).
    Streak breaks if the gap between adjacent snapshots exceeds STREAK_MAX_GAP_DAYS.
    """
    if history.empty or "model" not in history.columns:
        return 0
    sub       = history[history["model"] == model].sort_values("computed_at", ascending=False)
    streak    = 0
    prev_date = None
    for _, row in sub.iterrows():
        if not row.get("in_drift"):
            break
        try:
            row_date = pd.Timestamp(str(row["computed_at"])).date()
        except Exception:
            break
        if prev_date is not None and abs((prev_date - row_date).days) > STREAK_MAX_GAP_DAYS:
            break
        streak    += 1
        prev_date  = row_date
    return streak


# ── Main public function ──────────────────────────────────────────────────────

def compute_rolling_accuracy(window_days: int = ROLLING_DAYS) -> dict:
    """
    Run all trained models against the last `window_days` of labeled rows
    that postdate each model's own training cutoff (truly OOS).
    Saves results to model_accuracy_log. Returns metrics + drift alerts.
    """
    from scripts.db import read_df, table_exists

    if not table_exists():
        return {"ok": False, "error": "regime_training table does not exist"}

    window_cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    df_full = read_df(
        f"SELECT * FROM regime_training WHERE labeled = true AND date >= '{window_cutoff}'"
    )
    df_full = df_full.dropna(subset=["forward_return", "rsi", "adx", "hv20"])
    df_full["date"] = pd.to_datetime(df_full["date"])

    if len(df_full) < 20:
        return {"ok": False, "error": f"Only {len(df_full)} labeled rows in last {window_days}d"}

    # Load artifacts via mtime-aware cache (point 14)
    model_files = {
        "regime_classifier":       _MODELS_DIR / "regime_classifier.joblib",
        "return_regressor":        _MODELS_DIR / "return_regressor.joblib",
        "volatility_regressor":    _MODELS_DIR / "volatility_regressor.joblib",
        "direction_classifier":    _MODELS_DIR / "direction_classifier.joblib",
        "iv_direction_classifier": _MODELS_DIR / "iv_direction_classifier.joblib",
        "meta_ensemble":           _MODELS_DIR / "meta_ensemble.joblib",
    }
    artifacts = {name: _load_artifact(path) for name, path in model_files.items()}
    artifacts = {k: v for k, v in artifacts.items() if v is not None}

    # Read recent history for statistical drift layer
    history = _read_log(lookback_days=DRIFT_STREAK * 7 + 5)

    # computed_at is daily granularity (point 8 — prevents duplicate snapshots)
    computed_at = date.today().isoformat()
    metrics: dict[str, dict] = {}
    log_rows: list[dict]     = []

    def _record(model_name: str, m: dict, art: dict) -> None:
        in_drift, reason = _is_drift(model_name, m, art=art, history=history)
        metrics[model_name] = m
        log_rows.append(_make_log_row(computed_at, window_days, model_name, m, in_drift, reason))

    # ── Regime classifier ──────────────────────────────────────────────────────
    if "regime_classifier" in artifacts:
        art = artifacts["regime_classifier"]
        sub = df_full.dropna(subset=["regime_label"])
        sub = _oos_filter(sub, art, "regime_classifier")
        le  = art.get("label_encoder")
        if le and len(sub) >= 10:
            # Drop rows whose label isn't in the encoder — never map to a wrong class (point 2)
            known_mask = sub["regime_label"].isin(le.classes_)
            n_unknown  = (~known_mask).sum()
            if n_unknown:
                log.warning("[tracker] regime_classifier: dropping %d rows with unknown labels", n_unknown)
            sub = sub[known_mask].copy()
            sub["_regime_enc"] = le.transform(sub["regime_label"])
            m = _eval_classifier("regime_classifier", art, sub, "_regime_enc")
        else:
            m = {"n": 0}
        _record("regime_classifier", m, art)

    # ── Direction classifier ───────────────────────────────────────────────────
    if "direction_classifier" in artifacts:
        art = artifacts["direction_classifier"]
        sub = _oos_filter(_add_direction_target(df_full), art, "direction_classifier")
        if len(sub) >= 10:
            le = art.get("label_encoder")
            if le:
                sub        = sub.copy()
                dir_strs   = sub["_direction"].map({1: "Up", 0: "Down"})
                known_mask = dir_strs.isin(le.classes_)
                n_unknown  = (~known_mask).sum()
                if n_unknown:
                    log.warning("[tracker] direction_classifier: dropping %d unknown labels", n_unknown)
                sub = sub[known_mask].copy()
                sub["_dir_enc"] = le.transform(dir_strs[known_mask])
                m = _eval_classifier("direction_classifier", art, sub, "_dir_enc")
            else:
                m = _eval_classifier("direction_classifier", art, sub, "_direction")
        else:
            m = {"n": 0}
        _record("direction_classifier", m, art)

    # ── IV direction classifier ────────────────────────────────────────────────
    if "iv_direction_classifier" in artifacts:
        art = artifacts["iv_direction_classifier"]
        sub = _oos_filter(df_full.dropna(subset=["iv_expanding"]), art, "iv_direction_classifier")
        m   = _eval_classifier("iv_direction_classifier", art, sub, "iv_expanding") if len(sub) >= 10 else {"n": 0}
        _record("iv_direction_classifier", m, art)

    # ── Return regressor ───────────────────────────────────────────────────────
    if "return_regressor" in artifacts:
        art = artifacts["return_regressor"]
        sub = _oos_filter(df_full, art, "return_regressor")
        m   = _eval_regressor("return_regressor", art, sub, "forward_return") if len(sub) >= 10 else {"n": 0}
        _record("return_regressor", m, art)

    # ── Volatility regressor ───────────────────────────────────────────────────
    if "volatility_regressor" in artifacts:
        art = artifacts["volatility_regressor"]
        sub = _oos_filter(df_full.dropna(subset=["forward_hv"]), art, "volatility_regressor")
        m   = _eval_regressor("volatility_regressor", art, sub, "forward_hv") if len(sub) >= 10 else {"n": 0}
        _record("volatility_regressor", m, art)

    # ── Meta-ensemble (point 7: explicit missing-dependency reporting) ─────────
    if "meta_ensemble" in artifacts:
        art = artifacts["meta_ensemble"]
        base_artifacts_for_meta = {
            "regime":       artifacts.get("regime_classifier"),
            "return":       artifacts.get("return_regressor"),
            "vol":          artifacts.get("volatility_regressor"),
            "direction":    artifacts.get("direction_classifier"),
            "iv_direction": artifacts.get("iv_direction_classifier"),
        }
        missing = [k for k, v in base_artifacts_for_meta.items() if v is None]
        if missing:
            log.warning("[tracker] meta_ensemble skipped — missing base models: %s", missing)
            m = {"n": 0, "status": "skipped", "reason": f"missing base models: {missing}"}
        else:
            sub = _oos_filter(df_full.dropna(subset=["forward_return"]), art, "meta_ensemble")
            m   = _eval_meta(art, sub, base_artifacts_for_meta) if len(sub) >= 10 else {"n": 0}
        _record("meta_ensemble", m, art)

    # ── Streak tracking ────────────────────────────────────────────────────────
    for row in log_rows:
        streak          = _compute_streak(history, row["model"])
        row["drift_streak"] = streak + (1 if row["in_drift"] else 0)
        row["retrain_flag"] = row["drift_streak"] >= DRIFT_STREAK

    _save_snapshots(log_rows)

    drift_alerts   = [
        {"model": r["model"], "streak": r["drift_streak"],
         "reason": r["drift_reason"], **metrics.get(r["model"], {})}
        for r in log_rows if r["in_drift"]
    ]
    retrain_needed = any(r["retrain_flag"] for r in log_rows)

    log.info("[tracker] Rolling accuracy (%dd, %d total labeled rows):", window_days, len(df_full))
    for r in log_rows:
        parts = " ".join(filter(None, [
            f"acc={r['accuracy']:.3f}"          if r.get("accuracy")          is not None else None,
            f"bal={r['balanced_accuracy']:.3f}" if r.get("balanced_accuracy") is not None else None,
            f"auc={r['auc']:.3f}"               if r.get("auc")               is not None else None,
            f"r²={r['r2']:.3f}"                 if r.get("r2")                is not None else None,
            f"rmse={r['rmse']:.4f}"             if r.get("rmse")              is not None else None,
            f"n={r['n_samples']}",
        ]))
        drift_str = f" [DRIFT: {r['drift_reason']}]" if r["in_drift"] else ""
        log.info("  %-30s %s%s", r["model"], parts, drift_str)
    if retrain_needed:
        log.warning("[tracker] RETRAIN TRIGGER — one or more models in sustained drift")

    return {
        "ok":             True,
        "computed_at":    computed_at,
        "window_days":    window_days,
        "n_rows":         len(df_full),
        "metrics":        metrics,
        "drift_alerts":   drift_alerts,
        "retrain_needed": retrain_needed,
    }


# ── Public read helpers ───────────────────────────────────────────────────────

def get_model_health() -> dict:
    """Return the latest accuracy snapshot per model. Used by API and UI."""
    _ensure_log_table()
    try:
        with _connect(read_only=True) as con:
            n = con.execute(
                f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{LOG_TABLE}'"
            ).fetchone()[0]
            if not n:
                return {"ok": True, "models": {}, "any_drift": False,
                        "retrain_needed": False, "computed_at": None}
            df = con.execute(f"""
                SELECT * FROM {LOG_TABLE}
                WHERE computed_at = (SELECT MAX(computed_at) FROM {LOG_TABLE})
            """).df()
    except Exception as e:
        return {"ok": False, "error": str(e), "models": {}, "any_drift": False,
                "retrain_needed": False}

    if df.empty:
        return {"ok": True, "models": {}, "any_drift": False,
                "retrain_needed": False, "computed_at": None}

    def _clean(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if (f != f or abs(f) == float("inf")) else f
        except (TypeError, ValueError):
            return v

    models = {}
    for _, row in df.iterrows():
        models[row["model"]] = {
            "accuracy":          _clean(row.get("accuracy")),
            "balanced_accuracy": _clean(row.get("balanced_accuracy")),
            "f1_macro":          _clean(row.get("f1_macro")),
            "auc":               _clean(row.get("auc")),
            "r2":                _clean(row.get("r2")),
            "rmse":              _clean(row.get("rmse")),
            "mae":               _clean(row.get("mae")),
            "n_samples":         int(row.get("n_samples") or 0),
            "in_drift":          bool(row.get("in_drift")),
            "drift_reason":      row.get("drift_reason") or "",
            "drift_streak":      int(row.get("drift_streak") or 0),
            "retrain_flag":      bool(row.get("retrain_flag")),
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
    df = _read_log(lookback_days=lookback_days)
    if df.empty:
        return []
    cols = [c for c in [
        "computed_at", "accuracy", "balanced_accuracy", "f1_macro",
        "auc", "r2", "rmse", "mae", "n_samples", "in_drift", "drift_reason",
    ] if c in df.columns]
    return df[df["model"] == model_name].sort_values("computed_at")[cols].to_dict("records")


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = compute_rolling_accuracy()
    print(json.dumps(
        {k: v for k, v in result.items() if k != "metrics"},
        indent=2, default=str,
    ))
