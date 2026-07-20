"""
Model Monitor — weekly health check for all trained ML models.

Runs three checks and writes dated reports under data/model_monitor/:

1. Feature PSI drift   — via feature_drift.compute_drift_report()
2. Brier score trend   — compare Brier score on recent labeled rows vs baseline window;
                         also reports positive_rate per window so class-balance shifts
                         are visible alongside score changes
3. Calibration check   — ECE (Expected Calibration Error), MCE (max bin error),
                         and linear calibration slope/intercept on the recent window

Report layout
-------------
Each run writes:
  data/model_monitor/YYYY-MM-DD.json   — dated archive
  data/model_monitor/latest.json       — always the most recent run

Wire into evening_check.py: called once per week (Monday evening) or whenever
a new batch of labels lands. Logs WARN when a model is degrading.

Run standalone:
  python -m scripts.model_monitor
  python -m scripts.model_monitor --model regime     # one model only
"""
import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.linear_model import LinearRegression
from sklearn.metrics import brier_score_loss

_ROOT        = Path(__file__).resolve().parent.parent
_MONITOR_DIR = _ROOT / "data" / "model_monitor"
_REPORT_PATH = _MONITOR_DIR / "latest.json"  # backward-compat path read by old callers

log = logging.getLogger(__name__)

# Classifiers that output predict_proba and have a binary win/loss target
_CLASSIFIER_TARGETS: dict[str, tuple[Path, str]] = {
    "regime":            (_ROOT / "data/models/regime_classifier.joblib",   "regime_label"),
    "return_classifier": (_ROOT / "data/models/return_classifier.joblib",   "forward_return"),
    "direction":         (_ROOT / "data/models/direction_model.joblib",     "forward_return"),
    "iv_direction":      (_ROOT / "data/models/iv_direction_model.joblib",  "forward_return"),
}

# Brier absolute threshold — alert when recent - baseline > this
_BRIER_DEGRADATION_THRESHOLD = 0.05
# Relative threshold: alert when recent > baseline * multiplier AND baseline > minimum.
# Guards against spurious alerts when baseline Brier is tiny (e.g. 0.001 → 0.003
# is a 200% relative increase but still negligible in absolute terms).
_BRIER_RELATIVE_MULTIPLIER   = 1.20   # 20% relative increase
_BRIER_RELATIVE_MIN_BASELINE = 0.02   # only apply relative check when baseline >= this
# Calibration bins for ECE
_CAL_BINS          = 10
_ECE_WARN_THRESHOLD = 0.05   # ECE above this is flagged in the calibration dict


# ── Artifact validation ───────────────────────────────────────────────────────

def _validate_artifact(art: dict, model_name: str) -> str | None:
    """
    Return an error string if the artifact is missing required keys, else None.
    Required keys differ by model type:
      all models:           model
      regime:               label_encoder
      return_classifier:    feature_encoders
      direction/iv_direction: feature_cols
    """
    if not isinstance(art, dict):
        return "artifact_not_a_dict"
    required = ["model"]
    if model_name == "regime":
        required += ["label_encoder"]
    elif model_name == "return_classifier":
        required += ["feature_encoders"]
    elif model_name in ("direction", "iv_direction"):
        required += ["feature_cols"]
    missing = [k for k in required if art.get(k) is None]
    if missing:
        return f"missing_keys:{','.join(missing)}"
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return None


def _positive_class_col(model, proba: np.ndarray) -> np.ndarray:
    """
    Return the probability column for the positive class (label 1).
    Uses model.classes_ to find the correct column rather than assuming [:, 1],
    which breaks when classes_ is ordered [1, 0] instead of [0, 1].
    """
    classes = list(getattr(model, "classes_", [0, 1]))
    if 1 in classes:
        idx = classes.index(1)
    else:
        idx = 1 if proba.shape[1] > 1 else 0
    return proba[:, idx]


def _calibration_metrics(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = _CAL_BINS) -> dict:
    """
    Compute ECE, MCE, and a reliability-curve slope/intercept for binary classifiers.

    ECE = sum over non-empty bins of (bin_count / n_total) * |frac_pos - mean_pred|.
         Weights are derived from the same binning used by calibration_curve so that
         non-empty bin counts are correctly aligned with the returned (frac_pos, mean_pred)
         arrays. Empty bins are excluded from both calibration_curve output and ECE.

    MCE = max bin |frac_pos - mean_pred| over non-empty bins.

    reliability_slope / reliability_intercept: linear regression of
         (mean_predicted → fraction_positive) over non-empty bins.
         Named "reliability" to distinguish from the logistic-regression calibration
         slope (logit(p) ~ outcome), which is the standard in model validation literature.
         Ideal: slope=1.0, intercept=0.0.
         Slope <1 → overconfident predictions; slope >1 → underconfident.

    Calibration is only defined for binary classifiers. For multi-class models
    (regime) the caller returns calibration=None explicitly.
    """
    empty = {"ece": None, "mce": None, "reliability_slope": None,
             "reliability_intercept": None, "n_bins_used": 0,
             "ece_flagged": False}
    if len(np.unique(y_true)) < 2 or len(y_true) < n_bins * 2:
        return empty

    try:
        # Compute bin boundaries for uniform strategy to recover per-bin counts
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        bin_edges[-1] = 1.0 + 1e-8   # include prob==1.0 in the last bin

        # Assign each sample to a bin
        bin_ids = np.digitize(y_prob, bin_edges) - 1   # 0-indexed
        bin_ids = np.clip(bin_ids, 0, n_bins - 1)

        # Compute per-bin statistics only for non-empty bins (matches calibration_curve)
        non_empty_bins = [i for i in range(n_bins)
                          if np.any(bin_ids == i)]
        if not non_empty_bins:
            return empty

        bin_counts = np.array([np.sum(bin_ids == i) for i in non_empty_bins])
        mean_pred  = np.array([np.mean(y_prob[bin_ids == i]) for i in non_empty_bins])
        frac_pos   = np.array([np.mean(y_true[bin_ids == i]) for i in non_empty_bins])

        abs_err = np.abs(frac_pos - mean_pred)
        ece     = float(np.sum(bin_counts * abs_err) / len(y_prob))
        mce     = float(np.max(abs_err))

        # Reliability slope/intercept
        if len(non_empty_bins) >= 3:
            lr = LinearRegression().fit(mean_pred.reshape(-1, 1), frac_pos)
            r_slope     = round(float(lr.coef_[0]), 4)
            r_intercept = round(float(lr.intercept_), 4)
        else:
            r_slope = r_intercept = None

        return {
            "ece":                   round(ece, 5),
            "mce":                   round(mce, 5),
            "reliability_slope":     r_slope,
            "reliability_intercept": r_intercept,
            "n_bins_used":           len(non_empty_bins),
            "ece_flagged":           ece > _ECE_WARN_THRESHOLD,
        }
    except Exception as e:
        log.warning("[monitor] calibration_metrics failed: %s", e)
        return empty


def _brier_for_model(
    art:        dict,
    df:         pd.DataFrame,
    target_col: str,
    model_name: str,
) -> tuple[float | None, float | None, int, dict]:
    """
    Compute Brier score for a classifier on df rows.

    Returns (brier, positive_rate, n_eval, calibration_metrics).
    positive_rate and calibration_metrics are None/empty when unavailable.
    n_eval is the number of rows actually used after filtering.
    """
    model = art.get("model")
    if model is None:
        return None, None, 0, {}

    # Build binary target
    if target_col == "regime_label":
        label_enc = art.get("label_encoder")
        if label_enc is None or target_col not in df.columns:
            return None, None, 0, {}
        y_str = df[target_col].dropna().astype(str)
        known = set(label_enc.classes_)
        y_str = y_str[y_str.isin(known)]
        if len(y_str) < 10:
            return None, None, 0, {}
        df_sub = df.loc[y_str.index]
    else:
        if target_col not in df.columns:
            return None, None, 0, {}
        df_sub = df.dropna(subset=[target_col])
        if len(df_sub) < 10:
            return None, None, 0, {}

    # Build feature matrix — distinguish feature-key errors from other failures
    try:
        if model_name == "regime":
            from scripts.train_regime_classifier import build_feature_matrix
            encoders = art.get("feature_encoders", {})
            X, _ = build_feature_matrix(df_sub, encoders=encoders, fit=False)
        elif model_name == "return_classifier":
            from scripts.train_return_classifier import build_feature_matrix
            encoders = art.get("feature_encoders", {})
            X, _ = build_feature_matrix(df_sub, encoders=encoders, fit=False)
        elif model_name in ("direction", "iv_direction"):
            feature_cols = art.get("feature_cols") or []
            if not feature_cols:
                return None, None, 0, {}
            missing_cols = [c for c in feature_cols if c not in df_sub.columns]
            if missing_cols:
                log.warning("[monitor] %s feature mismatch — missing: %s", model_name, missing_cols[:5])
                raise KeyError(f"feature_mismatch:{missing_cols[:3]}")
            X = df_sub[feature_cols].apply(pd.to_numeric, errors="coerce").dropna()
        else:
            return None, None, 0, {}
    except KeyError as e:
        log.warning("[monitor] feature key error for %s: %s", model_name, e)
        return None, None, 0, {"error": f"feature_mismatch:{e}"}
    except Exception as e:
        log.warning("[monitor] feature build failed for %s: %s", model_name, e)
        return None, None, 0, {}

    # For direction models, X may have shrunk after dropna(); realign df_sub
    if model_name in ("direction", "iv_direction"):
        df_sub = df_sub.loc[X.index]

    try:
        proba = model.predict_proba(X)
    except Exception as e:
        log.warning("[monitor] predict_proba failed for %s: %s", model_name, e)
        return None, None, 0, {}

    n_eval = len(X)

    # Compute Brier
    if model_name == "regime":
        label_enc  = art["label_encoder"]
        y_idx      = label_enc.transform(df_sub[target_col].astype(str))
        n_cls      = len(label_enc.classes_)
        y_onehot   = np.eye(n_cls)[y_idx]
        brier      = float(np.mean(np.sum((proba - y_onehot) ** 2, axis=1)) / n_cls)
        # Report class distribution instead of a single positive_rate
        classes    = list(label_enc.classes_)
        class_dist = {c: round(float(np.mean(np.array(df_sub[target_col].astype(str)) == c)), 4)
                      for c in classes}
        pos_rate   = class_dist   # caller stores this under positive_rate_recent/baseline
        cal        = None   # calibration undefined for multi-class (documented in docstring)
    else:
        y_binary = (df_sub[target_col].values.astype(float) > 0).astype(int)
        p_pos    = _positive_class_col(model, proba)
        if len(np.unique(y_binary)) < 2:
            return None, None, n_eval, {}
        brier    = float(brier_score_loss(y_binary, p_pos))
        pos_rate = float(np.mean(y_binary))
        cal      = _calibration_metrics(y_binary, p_pos)

    return round(brier, 5), pos_rate, n_eval, cal


# ── Main monitor ──────────────────────────────────────────────────────────────

def run_monitor(model_name: str | None = None) -> dict:
    """
    Run full model health check.

    Returns dict with keys:
      generated_at, ok, drift_status, flagged_features, n_flagged,
      models (per-model detail), degraded_models
    """
    from scripts.db import read_df, TABLE
    from scripts.feature_drift import compute_drift_report

    today          = date.today()
    recent_cutoff  = today - timedelta(days=30)
    baseline_start = today - timedelta(days=90)
    baseline_end   = today - timedelta(days=30)

    # ── 1. PSI drift ─────────────────────────────────────────────────────────
    try:
        drift_report = compute_drift_report()
        # Derive status directly from per-feature flags (avoids secondary list mismatch)
        feature_flags = {
            f: meta.get("flag")
            for f, meta in drift_report.get("features", {}).items()
        }
        flagged_features  = [f for f, fl in feature_flags.items() if fl and fl != "ok"]
        major_shift_feats = [f for f, fl in feature_flags.items() if fl == "major_shift"]
        drift_status = (
            "major_shift"    if major_shift_feats else
            "moderate_shift" if flagged_features  else
            "ok"
        )
    except Exception as e:
        log.warning("[monitor] feature drift failed: %s", e)
        flagged_features  = []
        major_shift_feats = []
        drift_status      = "error"

    # ── 2. Load labeled data ──────────────────────────────────────────────────
    try:
        df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
        df["date"] = pd.to_datetime(df["date"])
    except Exception as e:
        return {"ok": False, "error": f"DB read failed: {e}"}

    recent_df   = df[df["date"] >= pd.Timestamp(recent_cutoff)]
    baseline_df = df[(df["date"] >= pd.Timestamp(baseline_start)) &
                     (df["date"] <  pd.Timestamp(baseline_end))]

    # ── 3. Per-model Brier trend + calibration ────────────────────────────────
    targets = [model_name] if model_name else list(_CLASSIFIER_TARGETS.keys())
    model_results: dict = {}

    for name in targets:
        art_path, target_col = _CLASSIFIER_TARGETS[name]
        if not art_path.exists():
            model_results[name] = {"status": "missing_artifact"}
            continue

        try:
            art = joblib.load(art_path)
        except Exception as e:
            model_results[name] = {"status": "load_error", "detail": str(e)}
            continue

        err = _validate_artifact(art, name)
        if err:
            model_results[name] = {"status": "invalid_artifact", "detail": err}
            continue

        brier_recent,   pos_rate_recent,   n_recent,   cal_recent   = \
            _brier_for_model(art, recent_df,   target_col, name)
        brier_baseline, pos_rate_baseline, n_baseline, _            = \
            _brier_for_model(art, baseline_df, target_col, name)

        # Degradation: absolute OR relative threshold (both must be sane values)
        if brier_recent is None or brier_baseline is None:
            # Surface feature_mismatch separately from plain insufficient data
            cal_err = cal_recent.get("error", "") if isinstance(cal_recent, dict) else ""
            status  = "feature_mismatch" if "feature_mismatch" in cal_err else "insufficient_data"
            degraded        = False
            reason          = None
            brier_delta     = None
        else:
            brier_delta  = round(brier_recent - brier_baseline, 5)
            abs_trigger  = brier_delta > _BRIER_DEGRADATION_THRESHOLD
            # Relative trigger only when baseline is large enough to be meaningful.
            # Without the minimum, baseline=0.001 → recent=0.003 (200% increase but
            # still negligible) would generate a spurious alert.
            rel_trigger  = (brier_baseline >= _BRIER_RELATIVE_MIN_BASELINE and
                            brier_recent > brier_baseline * _BRIER_RELATIVE_MULTIPLIER)
            degraded     = abs_trigger or rel_trigger
            if degraded:
                reason = "brier_delta_absolute" if abs_trigger else "brier_delta_relative"
                log.warning(
                    "[monitor] %s DEGRADED (%s) — baseline=%.4f recent=%.4f delta=%+.4f",
                    name, reason, brier_baseline, brier_recent, brier_delta,
                )
            else:
                reason = None
            status = "degraded" if degraded else "ok"

        model_results[name] = {
            "status":             status,
            "degraded":           degraded,
            # Reason and threshold info — actionable detail for the operator
            "reason":             reason,
            "recommended_action": "retrain" if degraded else None,
            "threshold_absolute": _BRIER_DEGRADATION_THRESHOLD,
            "threshold_relative": _BRIER_RELATIVE_MULTIPLIER,
            # Brier scores — actual evaluation sample sizes, not window sizes
            "brier_baseline":     brier_baseline,
            "brier_recent":       brier_recent,
            "brier_delta":        brier_delta,
            "n_eval_recent":      n_recent,
            "n_eval_baseline":    n_baseline,
            # Class balance — helps distinguish harder conditions from degradation
            "positive_rate_recent":   _safe_float(pos_rate_recent),
            "positive_rate_baseline": _safe_float(pos_rate_baseline),
            # Calibration (binary models only; None for multi-class regime)
            "calibration": cal_recent if isinstance(cal_recent, dict) and cal_recent else None,
        }

    report = {
        "generated_at":      today.isoformat(),
        "ok":                True,
        "drift_status":      drift_status,
        "flagged_features":  flagged_features,
        "major_shift_feats": major_shift_feats,
        "n_flagged":         len(flagged_features),
        "models":            model_results,
        "degraded_models":   [n for n, r in model_results.items() if r.get("degraded")],
    }

    # Write dated archive + latest
    _MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    dated_path = _MONITOR_DIR / f"{today.isoformat()}.json"
    payload    = json.dumps(report, indent=2)
    dated_path.write_text(payload)
    _REPORT_PATH.write_text(payload)   # latest.json — backward-compat

    log.info(
        "[monitor] report written — drift=%s  degraded=%s  flagged=%d",
        drift_status,
        report["degraded_models"] or "none",
        len(flagged_features),
    )
    return report


def load_monitor_report() -> dict | None:
    """Load the most recent monitor report from disk, or None if not yet generated."""
    if not _REPORT_PATH.exists():
        return None
    try:
        return json.loads(_REPORT_PATH.read_text())
    except Exception:
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Weekly model health monitor")
    parser.add_argument("--model", default=None,
                        choices=list(_CLASSIFIER_TARGETS.keys()),
                        help="Check one model; omit for all")
    args = parser.parse_args()

    report = run_monitor(model_name=args.model)

    print(f"\n=== Model Monitor === {report.get('generated_at', 'today')} ===")
    print(f"Feature drift: {report['drift_status']}  ({report['n_flagged']} flagged)")
    if report["flagged_features"]:
        print(f"  Flagged: {', '.join(report['flagged_features'][:10])}")
    print()

    for name, m in report.get("models", {}).items():
        status  = m.get("status", "?")
        br_base = m.get("brier_baseline")
        br_rec  = m.get("brier_recent")
        delta   = m.get("brier_delta")
        flag    = "  <-- DEGRADED" if m.get("degraded") else ""
        n_r     = m.get("n_eval_recent",   0)
        n_b     = m.get("n_eval_baseline", 0)
        pr_r    = m.get("positive_rate_recent")
        pr_b    = m.get("positive_rate_baseline")

        if br_base is not None and br_rec is not None:
            pr_str = (f"  pos_rate baseline={pr_b:.2f} recent={pr_r:.2f}"
                      if pr_b is not None else "")
            print(f"  {name:<22} {status:<18}  "
                  f"Brier baseline={br_base:.4f}(n={n_b}) recent={br_rec:.4f}(n={n_r})"
                  f"  delta={delta:+.4f}{flag}")
            if pr_str:
                print(f"  {'':22} {pr_str}")
            cal = m.get("calibration") or {}
            if cal and cal.get("ece") is not None:
                flag = "  [ECE HIGH]" if cal.get("ece_flagged") else ""
                print(f"  {'':22}  ECE={cal['ece']:.4f}  MCE={cal.get('mce', '?')}"
                      f"  rel_slope={cal.get('reliability_slope')}"
                      f"  rel_intercept={cal.get('reliability_intercept')}{flag}")
            if m.get("reason"):
                print(f"  {'':22}  reason={m['reason']}  "
                      f"recommended_action={m.get('recommended_action')}")
        else:
            print(f"  {name:<22} {status}")

    if report.get("degraded_models"):
        print(f"\n  ACTION NEEDED: retrain {report['degraded_models']}")
    print()
