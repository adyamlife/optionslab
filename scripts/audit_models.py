"""
Model artifact audit — run on Ubuntu to establish current state before retraining.

Usage:
    python -m scripts.audit_models
    python -m scripts.audit_models --scan   # also run a live scan on SPY and check outputs

Checks:
  1. Which .joblib artifacts exist and when they were last written
  2. Key metrics per model (R², AUC, accuracy, training rows, cutoff dates)
  3. Whether return regressor is active (R² vs threshold)
  4. GARCH artifact count and whether fitted_at metadata is present
  5. Optional: one-ticker scan to confirm non-null outputs
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# ── helpers ──────────────────────────────────────────────────────────────────

def _load(path):
    try:
        import joblib
        return joblib.load(path)
    except Exception as e:
        return None


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


# ── main audit ───────────────────────────────────────────────────────────────

MODELS = {
    "regime_classifier":       "data/models/regime_classifier.joblib",
    "direction_classifier":    "data/models/direction_classifier.joblib",
    "iv_direction_classifier": "data/models/iv_direction_classifier.joblib",
    "return_regressor":        "data/models/return_regressor.joblib",
    "volatility_regressor":    "data/models/volatility_regressor.joblib",
    "pop_classifier":          "data/models/pop_classifier.joblib",
    "trade_win_classifier":    "data/models/trade_win_v1_combined.joblib",
    "meta_ensemble":           "data/models/meta_ensemble.joblib",
    "anomaly_detector":        "data/models/anomaly_detector.joblib",
}

METRIC_KEYS = [
    "r2", "rmse", "mae", "accuracy", "balanced_accuracy", "auc",
    "trained_on_rows", "train_rows", "val_rows", "test_rows",
    "split_cutoff", "val_cutoff", "test_cutoff", "best_n_estimators",
    "optimal_threshold", "train_expanding_pct",
]

R2_THRESHOLD = 0.10  # mirrors regime_predictor default


def audit_artifacts() -> list[dict]:
    results = []
    for name, rel_path in MODELS.items():
        path = _ROOT / rel_path
        entry = {"name": name, "path": str(path)}

        if not path.exists():
            entry["status"] = "MISSING"
            results.append(entry)
            continue

        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        entry["mtime"] = mtime.strftime("%Y-%m-%d %H:%M:%S")
        entry["age_days"] = (datetime.datetime.now() - mtime).days

        art = _load(path)
        if art is None:
            entry["status"] = "LOAD_ERROR"
            results.append(entry)
            continue

        entry["status"] = "OK"
        for k in METRIC_KEYS:
            v = art.get(k)
            if v is not None:
                entry[k] = v

        # Return regressor: flag active/inactive
        if name == "return_regressor":
            r2 = art.get("r2", -999.0)
            entry["active"] = r2 >= R2_THRESHOLD
            entry["r2_vs_threshold"] = f"{r2:.4f} vs {R2_THRESHOLD} — {'ACTIVE' if r2 >= R2_THRESHOLD else 'INACTIVE'}"

        results.append(entry)
    return results


def audit_garch() -> dict:
    garch_dir = _ROOT / "data" / "models" / "garch"
    if not garch_dir.exists():
        return {"count": 0, "has_fitted_at": 0, "missing_fitted_at": 0,
                "has_data_end_date": 0, "sample": [], "error": "directory missing"}

    files = list(garch_dir.glob("*.joblib"))
    has_fitted_at = 0
    has_data_end_date = 0
    sample = []

    for f in files[:5]:  # sample first 5
        art = _load(f)
        if art is None:
            continue
        fitted_at = art.get("fitted_at") or art.get("trained_at")
        data_end = art.get("data_end_date")
        if fitted_at:
            has_fitted_at += 1
        if data_end:
            has_data_end_date += 1
        sample.append({
            "ticker": f.stem,
            "fitted_at": fitted_at,
            "data_end_date": data_end,
            "last_fit_date": art.get("last_fit_date"),
            "n_obs": art.get("n_obs"),
            "persistence": art.get("persistence"),
        })

    # count all
    total = len(files)
    for f in files[5:]:
        art = _load(f)
        if art is None:
            continue
        if art.get("fitted_at") or art.get("trained_at"):
            has_fitted_at += 1
        if art.get("data_end_date"):
            has_data_end_date += 1

    return {
        "count": total,
        "has_fitted_at": has_fitted_at,
        "missing_fitted_at": total - has_fitted_at,
        "has_data_end_date": has_data_end_date,
        "sample": sample,
    }


def run_scan_check(ticker: str = "SPY") -> dict:
    """Run regime_predictor on one ticker and report which outputs are non-null."""
    try:
        from scripts.regime_predictor import predict_ticker
        result = predict_ticker(ticker)
    except Exception as e:
        return {"error": str(e)}

    check_keys = [
        "regime", "expected_return", "expected_vol", "p_up", "p_flat", "p_down",
        "iv_expanding_prob", "iv_direction", "meta_score", "pop_score",
        "p_return_positive", "p_return_gt5", "p_return_gt10",
    ]
    outputs = {}
    for k in check_keys:
        v = result.get(k)
        outputs[k] = "NULL" if v is None else _fmt(v)

    warnings = result.get("warnings", [])
    return {"outputs": outputs, "warnings": warnings}


# ── report ────────────────────────────────────────────────────────────────────

def print_report(artifacts, garch, scan=None):
    print("\n" + "=" * 70)
    print("MODEL ARTIFACT AUDIT")
    print("=" * 70)

    all_ok = True
    for a in artifacts:
        status = a["status"]
        name = a["name"]
        mtime = a.get("mtime", "—")
        age = a.get("age_days", "?")

        if status == "MISSING":
            print(f"  {'[MISSING]':12s} {name}")
            all_ok = False
            continue
        if status == "LOAD_ERROR":
            print(f"  {'[ERR]':12s} {name}  {mtime}")
            all_ok = False
            continue

        # key metric line
        metric = ""
        if "r2_vs_threshold" in a:
            metric = a["r2_vs_threshold"]
            if "INACTIVE" in metric:
                all_ok = False
        elif "auc" in a:
            metric = f"AUC={a['auc']:.4f}"
            if "accuracy" in a:
                metric += f"  acc={a['accuracy']:.4f}"
        elif "r2" in a:
            metric = f"R²={a['r2']:.4f}"
        elif "accuracy" in a:
            metric = f"acc={a['accuracy']:.4f}"

        rows = a.get("trained_on_rows") or a.get("train_rows") or "?"
        cutoff = a.get("test_cutoff") or a.get("split_cutoff") or "?"
        if cutoff and len(str(cutoff)) > 10:
            cutoff = str(cutoff)[:10]

        tag = "[OK]" if status == "OK" else f"[{status}]"
        print(f"  {tag:12s} {name:30s} {mtime}  (age {age}d)  rows={rows}  cutoff={cutoff}  {metric}")

    print()
    print("─" * 70)
    print(f"GARCH ARTIFACTS: {garch['count']} files")
    print(f"  fitted_at present : {garch['has_fitted_at']} / {garch['count']}")
    print(f"  fitted_at MISSING : {garch['missing_fitted_at']} / {garch['count']}  ← I-2 fix needed")
    if garch.get("sample"):
        print("  Sample (first 5):")
        for s in garch["sample"]:
            print(f"    {s['ticker']:8s}  last_fit_date={s.get('last_fit_date')}  "
                  f"fitted_at={s.get('fitted_at')}  n_obs={s.get('n_obs')}")

    if scan:
        print()
        print("─" * 70)
        print("LIVE SCAN CHECK (SPY)")
        if "error" in scan:
            print(f"  ERROR: {scan['error']}")
        else:
            null_keys = [k for k, v in scan["outputs"].items() if v == "NULL"]
            ok_keys   = [k for k, v in scan["outputs"].items() if v != "NULL"]
            print(f"  Non-null outputs ({len(ok_keys)}): {', '.join(ok_keys)}")
            if null_keys:
                print(f"  NULL outputs    ({len(null_keys)}): {', '.join(null_keys)}")
                all_ok = False
            if scan["warnings"]:
                print("  Warnings:")
                for w in scan["warnings"]:
                    print(f"    - {w}")

    print()
    print("─" * 70)
    verdict = "ALL CLEAR — no retraining needed" if all_ok else "ACTION REQUIRED — see above"
    print(f"VERDICT: {verdict}")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", action="store_true", help="Run live scan check on SPY")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    artifacts = audit_artifacts()
    garch     = audit_garch()
    scan      = run_scan_check() if args.scan else None

    if args.json:
        print(json.dumps({"artifacts": artifacts, "garch": garch, "scan": scan}, indent=2, default=str))
    else:
        print_report(artifacts, garch, scan)


if __name__ == "__main__":
    main()
