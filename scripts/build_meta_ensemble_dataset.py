"""
Build the meta-ensemble training dataset by joining ML predictions with labeled
trade outcomes.

Left side:  data/ml_training.duckdb → ml_predictions table
            (one row per ticker per scan; populated by ml_cache.save() each evening)

Right side: data/paper_trades.json + data/etrade_labeled_trades.jsonl
            (labeled closed trades with win/loss outcomes)

Join key:   ticker  +  prediction_date == trade entry_date
            (the ML snapshot closest to but not after trade entry)

Output:     data/meta_ensemble_dataset.csv  (when MIN_ROWS reached)
            Schema validation report always printed regardless of row count.

Run:
    python -m scripts.build_meta_ensemble_dataset
    python -m scripts.build_meta_ensemble_dataset --min-rows 50   # lower gate for testing
    python -m scripts.build_meta_ensemble_dataset --dry-run        # schema report only
"""
import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_ROOT        = Path(__file__).parent.parent
_OUT_PATH    = _ROOT / "data" / "meta_ensemble_dataset.csv"
_MIN_ROWS    = 200   # minimum to train a stacking model reliably

# ML feature columns available in ml_predictions for the meta-ensemble.
# Ordered by model confidence (high → low).
_FEATURE_COLS = [
    # Regime classifier (R²~0.45, most reliable)
    "regime",
    "regime_proba",          # JSON; expand to regime_p_trending etc at training time
    # Volatility model (R²=0.51, second most reliable)
    "expected_vol",
    "expected_move_pct",
    # IV direction model
    "iv_expanding_prob",
    "iv_direction",
    # Direction model
    "p_up",
    # Return model (R²<0, weakest — include but low weight in stacker)
    "expected_return",
    # POP model
    "pop_score",
    # Meta / composite (built from the above — include for completeness)
    "meta_score",
    "composite_score",
    "p_win",
    "confidence",
    # Anomaly
    "anomaly_score",
    "is_anomaly",
]

# Target columns from the labeled outcome.
_TARGET_COLS = ["win", "net_pnl", "structure", "entry_date", "exit_date"]


def _load_paper_outcomes() -> list[dict]:
    """Load closed paper trades as normalized outcome records."""
    path = _ROOT / "data" / "paper_trades.json"
    if not path.exists():
        return []
    with open(path) as f:
        trades = json.load(f)
    out = []
    for t in trades:
        if not t.get("exit") or t.get("status") == "open":
            continue
        ex = t["exit"]
        entry_dt = t.get("entered_at", "")
        entry_date = entry_dt[:10] if entry_dt else None
        out.append({
            "source":      "paper",
            "ticker":      t.get("ticker"),
            "entry_date":  entry_date,
            "exit_date":   (ex.get("ts") or "")[:10],
            "structure":   t.get("structure"),
            "win":         bool(ex.get("win")),
            "net_pnl":     ex.get("pnl_total"),
        })
    return out


def _load_etrade_outcomes() -> list[dict]:
    """Load labeled E*TRADE trades as normalized outcome records."""
    path = _ROOT / "data" / "etrade_labeled_trades.jsonl"
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            outcome = r.get("outcome") or {}
            if not outcome.get("exit_date"):
                continue
            entry_date = (r.get("collected_at") or "")[:10]
            out.append({
                "source":      "etrade",
                "ticker":      r.get("ticker"),
                "entry_date":  entry_date,
                "exit_date":   outcome.get("exit_date"),
                "structure":   (r.get("candidate") or {}).get("structure") or r.get("structure"),
                "win":         bool(outcome.get("win")),
                "net_pnl":     outcome.get("net_pnl"),
            })
    return out


def _load_ml_predictions() -> dict[tuple[str, str], dict]:
    """
    Load all ml_predictions rows from DuckDB.
    Returns {(ticker, date_str): pred_dict}.
    When multiple rows exist for the same ticker+date (multiple scans),
    keep the last one (latest scanned_at).
    """
    try:
        from scripts.db import connect
    except ImportError:
        log.error("scripts.db not importable — is the virtualenv active?")
        return {}

    with connect() as con:
        rows = con.execute(
            "SELECT ticker, date, scanned_at, regime, regime_proba, "
            "expected_return, expected_vol, expected_move_pct, "
            "p_up, iv_expanding_prob, iv_direction, "
            "pop_score, meta_score, composite_score, p_win, confidence, "
            "anomaly_score, is_anomaly "
            "FROM ml_predictions WHERE date IS NOT NULL "
            "ORDER BY ticker, date, scanned_at DESC"
        ).fetchall()
        cols = [d[0] for d in con.description]

    preds: dict[tuple, dict] = {}
    for row in rows:
        d = dict(zip(cols, row))
        key = (d["ticker"], d["date"])
        if key not in preds:          # first = latest scanned_at (ORDER BY DESC)
            preds[key] = d
    return preds


def _find_nearest_prediction(
    ticker: str,
    entry_date: str,
    preds: dict,
    max_lookback_days: int = 3,
) -> dict | None:
    """
    Return the ML prediction for ticker on entry_date, or the nearest prior
    date within max_lookback_days (handles weekends / non-scan days).
    """
    try:
        target = date.fromisoformat(entry_date)
    except (ValueError, TypeError):
        return None
    for delta in range(max_lookback_days + 1):
        candidate_date = (target - timedelta(days=delta)).isoformat()
        pred = preds.get((ticker, candidate_date))
        if pred:
            return pred
    return None


def _validate_schema(preds: dict, outcomes: list[dict]) -> dict:
    """
    Report schema completeness without requiring a full join.
    Returns a dict with coverage stats per feature column.
    """
    report = {
        "ml_prediction_rows":  len(preds),
        "ml_prediction_dates": sorted({d for (_, d) in preds}),
        "ml_tickers":          len({t for (t, _) in preds}),
        "outcome_rows":        len(outcomes),
        "outcome_sources":     {},
        "feature_coverage":    {},
        "target_coverage":     {},
        "joinable_estimate":   0,
    }

    # Outcome source breakdown
    from collections import Counter
    report["outcome_sources"] = dict(Counter(o["source"] for o in outcomes))

    # Feature coverage in ml_predictions
    if preds:
        sample_preds = list(preds.values())
        for col in _FEATURE_COLS:
            filled = sum(1 for p in sample_preds if p.get(col) is not None)
            report["feature_coverage"][col] = {
                "filled": filled,
                "total":  len(sample_preds),
                "pct":    round(100 * filled / len(sample_preds), 1) if sample_preds else 0,
            }

    # Target coverage in outcomes
    for col in _TARGET_COLS:
        filled = sum(1 for o in outcomes if o.get(col) is not None)
        report["target_coverage"][col] = {
            "filled": filled,
            "total":  len(outcomes),
            "pct":    round(100 * filled / len(outcomes), 1) if outcomes else 0,
        }

    # Estimate joinable rows: outcomes whose ticker+entry_date has a nearby prediction
    joinable = 0
    for o in outcomes:
        if _find_nearest_prediction(o["ticker"], o["entry_date"], preds):
            joinable += 1
    report["joinable_estimate"] = joinable

    return report


def _build_join(preds: dict, outcomes: list[dict]) -> list[dict]:
    """Perform the join and return merged rows."""
    joined = []
    for o in outcomes:
        pred = _find_nearest_prediction(o["ticker"], o["entry_date"], preds)
        if pred is None:
            continue
        row = {
            # identity
            "ticker":        o["ticker"],
            "entry_date":    o["entry_date"],
            "exit_date":     o["exit_date"],
            "source":        o["source"],
            "structure":     o["structure"],
            # targets
            "win":           int(o["win"]),
            "net_pnl":       o["net_pnl"],
            # ML features (prediction date may be up to 3 days prior)
            "pred_date":     pred["date"],
        }
        for col in _FEATURE_COLS:
            if col == "regime_proba":
                # Expand JSON blob to flat columns
                try:
                    proba = json.loads(pred.get("regime_proba") or "{}")
                except Exception:
                    proba = {}
                row["regime_p_trending"]      = proba.get("Trending")
                row["regime_p_mean_reverting"] = proba.get("Mean-reverting")
                row["regime_p_hv_breakout"]   = proba.get("High-vol-breakout")
                row["regime_p_lv_squeeze"]    = proba.get("Low-vol-squeeze")
            else:
                row[col] = pred.get(col)
        joined.append(row)
    return joined


def _print_schema_report(report: dict, joined: list[dict]) -> None:
    print()
    print("=" * 60)
    print("  Meta-Ensemble Dataset — Schema Validation Report")
    print("=" * 60)
    print(f"  ML predictions in DuckDB : {report['ml_prediction_rows']} rows  "
          f"({report['ml_tickers']} tickers)")
    print(f"  Prediction dates on file : {', '.join(report['ml_prediction_dates']) or 'none'}")
    print(f"  Labeled outcomes         : {report['outcome_rows']} trades  "
          f"({report['outcome_sources']})")
    print(f"  Joinable (ticker+date)   : {report['joinable_estimate']}")
    print(f"  Min rows for training    : {_MIN_ROWS}")
    gap = max(0, _MIN_ROWS - report["joinable_estimate"])
    print(f"  Still needed             : {gap}  "
          f"({'READY' if gap == 0 else f'~{gap // 2} weeks at current rate'})")

    print()
    print("  Feature column coverage (in ml_predictions):")
    for col, stats in report["feature_coverage"].items():
        bar = "OK" if stats["pct"] >= 80 else ("PARTIAL" if stats["pct"] > 0 else "MISSING")
        print(f"    {col:<30}  {stats['pct']:>5.1f}%  [{bar}]")

    print()
    print("  Target column coverage (in outcomes):")
    for col, stats in report["target_coverage"].items():
        bar = "OK" if stats["pct"] >= 80 else ("PARTIAL" if stats["pct"] > 0 else "MISSING")
        print(f"    {col:<30}  {stats['pct']:>5.1f}%  [{bar}]")

    if joined:
        print()
        print(f"  Joined rows available now: {len(joined)}")
        print(f"  Sample joined row:")
        sample = joined[0]
        for k, v in sample.items():
            print(f"    {k:<30}  {v}")
    print()


def run(min_rows: int = _MIN_ROWS, dry_run: bool = False) -> dict:
    preds    = _load_ml_predictions()
    outcomes = _load_paper_outcomes() + _load_etrade_outcomes()

    report = _validate_schema(preds, outcomes)
    joined = _build_join(preds, outcomes)

    _print_schema_report(report, joined)

    if dry_run:
        log.info("Dry run — skipping CSV write")
        return {"ok": True, "dry_run": True, "joinable": len(joined),
                "report": report}

    if len(joined) < min_rows:
        log.warning(
            "Only %d joined rows — need %d to train meta-ensemble. "
            "Re-run once more predictions accumulate.",
            len(joined), min_rows,
        )
        return {"ok": False, "joinable": len(joined), "min_rows": min_rows,
                "report": report}

    # Write CSV
    import csv
    if not joined:
        return {"ok": False, "joinable": 0}

    fieldnames = list(joined[0].keys())
    with open(_OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(joined)

    log.info("Wrote %d rows to %s", len(joined), _OUT_PATH)
    return {"ok": True, "joinable": len(joined), "out_path": str(_OUT_PATH),
            "report": report}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-rows", type=int, default=_MIN_ROWS)
    parser.add_argument("--dry-run",  action="store_true",
                        help="Schema report only — do not write CSV")
    args = parser.parse_args()
    result = run(min_rows=args.min_rows, dry_run=args.dry_run)
    sys.exit(0 if result.get("ok") else 1)
