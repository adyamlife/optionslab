"""
train_all.py — Single entry point: train every model, then calibrate.

Steps (in order):
  1. DB schema migration
  2. Regime backfill        — rebuild regime_training CSV with forward_hv
  3. Regime classifier      → data/models/regime_classifier.joblib
  4. Return regressor       → data/models/return_regressor.joblib
  5. Volatility regressor   → data/models/volatility_regressor.joblib
  6. POP classifier         → data/models/pop_classifier.joblib  (skips if < min data)
  7. Probability calibration — isotonic regression on all classifiers
  8. Grid calibration        — delta/width optimizer grids from labeled outcomes

Run:
  python -m scripts.train_all                   # full pipeline, grid calibration dry-run
  python -m scripts.train_all --write-grids     # also write grids to settings.toml
  python -m scripts.train_all --skip-backfill   # skip step 2 (reuse existing CSV)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("train_all")


# ── Step runner ────────────────────────────────────────────────────────────────

_results: list[dict] = []


def _step(n: int, label: str):
    """Context manager that times a step and records pass/fail."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        log.info("─" * 60)
        log.info("Step %d — %s", n, label)
        t0 = time.monotonic()
        record = {"step": n, "label": label, "ok": False, "elapsed": 0.0, "error": None}
        _results.append(record)
        try:
            yield
            record["ok"] = True
        except Exception as exc:
            record["error"] = str(exc)
            log.error("Step %d FAILED: %s", n, exc, exc_info=True)
        finally:
            record["elapsed"] = round(time.monotonic() - t0, 1)
            status = "OK" if record["ok"] else "FAILED"
            log.info("Step %d %s  (%.1fs)", n, status, record["elapsed"])

    return _ctx()


# ── Steps ──────────────────────────────────────────────────────────────────────

def step_db_migration():
    from scripts.db import connect
    con = connect()
    con.close()
    log.info("DB schema up to date.")


def step_regime_backfill():
    from scripts.regime_backfill import build_regime_dataset
    result = build_regime_dataset()
    log.info(
        "Backfill done: %d rows, %d tickers → %s",
        result.get("n_rows", 0),
        result.get("n_tickers", 0),
        result.get("path", "?"),
    )


def step_train_regime():
    from scripts.train_regime_classifier import train
    result = train()
    _log_train_result("Regime classifier", result)


def step_train_return():
    from scripts.train_return_model import train
    result = train()
    _log_train_result("Return regressor", result)


def step_train_volatility():
    from scripts.train_volatility_model import train
    result = train()
    _log_train_result("Volatility regressor", result)


def step_train_pop():
    from scripts.train_pop_model import train
    result = train()
    if not result.get("ok", True):
        log.warning("POP model skipped: %s", result.get("reason") or result.get("error"))
    else:
        _log_train_result("POP classifier", result)


def step_calibrate_models():
    from scripts.calibrate_models import calibrate_all
    result = calibrate_all()
    for model, info in (result or {}).items():
        if isinstance(info, dict):
            improvement = info.get("brier_improvement") or info.get("improvement")
            log.info("  %-30s  Brier improvement: %s", model, improvement)


def step_calibrate_optimizer(write_grids: bool):
    from scripts.calibrate_optimizer import run_calibration, _print_calibration_result
    result = run_calibration(write=write_grids)
    if not result.get("ok"):
        log.warning("Grid calibration skipped: %s", result.get("error"))
        return
    _print_calibration_result(result)


def _log_train_result(label: str, result: dict):
    if not result:
        return
    metrics = []
    for key in ("accuracy", "auc", "rmse", "r2", "mae"):
        val = result.get(key) or (result.get("metrics") or {}).get(key)
        if val is not None:
            metrics.append(f"{key}={val:.4f}" if isinstance(val, float) else f"{key}={val}")
    saved = result.get("model_path") or result.get("path") or "?"
    log.info("  %-30s  %s  → %s", label, "  ".join(metrics) or "(no metrics)", saved)


# ── Summary ────────────────────────────────────────────────────────────────────

def _print_summary():
    log.info("─" * 60)
    log.info("Pipeline summary:")
    total_ok = sum(1 for r in _results if r["ok"])
    for r in _results:
        icon = "OK" if r["ok"] else "!!"
        err  = f"  ← {r['error']}" if r["error"] else ""
        log.info("  %s  Step %d  %-35s  %.1fs%s", icon, r["step"], r["label"], r["elapsed"], err)
    log.info(
        "%d/%d steps passed  |  total %.1fs",
        total_ok, len(_results), sum(r["elapsed"] for r in _results),
    )
    if total_ok < len(_results):
        log.warning("Some steps failed — review errors above before deploying models.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Train all models and calibrate.")
    parser.add_argument("--write-grids",    action="store_true",
                        help="Write recommended grids to config/settings.toml after grid calibration.")
    parser.add_argument("--skip-backfill",  action="store_true",
                        help="Skip regime backfill (reuse existing regime_training CSV).")
    args = parser.parse_args()

    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("=" * 60)
    log.info("train_all  started %s", started)
    log.info("write-grids=%s  skip-backfill=%s", args.write_grids, args.skip_backfill)
    log.info("=" * 60)

    (Path(_ROOT) / "data" / "models").mkdir(parents=True, exist_ok=True)

    with _step(1, "DB schema migration"):
        step_db_migration()

    if not args.skip_backfill:
        with _step(2, "Regime backfill (forward_hv + CSV rebuild)"):
            step_regime_backfill()
    else:
        log.info("Step 2 — Regime backfill  SKIPPED (--skip-backfill)")

    with _step(3, "Train regime classifier"):
        step_train_regime()

    with _step(4, "Train return regressor"):
        step_train_return()

    with _step(5, "Train volatility regressor"):
        step_train_volatility()

    with _step(6, "Train POP classifier"):
        step_train_pop()

    with _step(7, "Probability calibration (isotonic)"):
        step_calibrate_models()

    with _step(8, "Grid calibration (delta/width optimizer)"):
        step_calibrate_optimizer(write_grids=args.write_grids)

    _print_summary()

    failed = [r for r in _results if not r["ok"]]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
