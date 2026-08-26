"""
Ticker forecast calibration — run on weekends (Saturday or Sunday).

For each expired forecast in ticker_forecast_log:
  1. Fetch actual closing price for the expiry date (yfinance, auto_adjust=True).
  2. Compute coverage / bias / tail metrics.
  3. Write validation rows into ticker_forecast_validation.
  4. Print a calibration report segmented by DTE bucket and overall.

DTE buckets:
  ≤7, 8-14, 15-21, 22+

Metrics:
  coverage_80   — fraction of actual prices inside [p10, p90]
  coverage_50   — fraction inside [p25, p75]
  bias_mean     — mean  (actual - p50) / p50
  bias_median   — median (actual - p50) / p50
  miss_left     — fraction actual < p10
  miss_right    — fraction actual > p90
  n             — count of validated forecasts

vol_adj_factor guidance (config/settings.toml [model]):
  Do NOT adjust for the first 4 weeks of data.
  After that, if consistent directional bias is observed:
    coverage_80 < 0.70 and bias_median > 0  →  model underestimates vol (increase factor)
    coverage_80 < 0.70 and bias_median < 0  →  model overestimates vol (decrease factor)
  Small steps only: ±0.02 per adjustment.

Run:
    python -m scripts.ticker_forecast_calibration
    python -m scripts.ticker_forecast_calibration --dry-run   # fetch + compute, no DB writes
    python -m scripts.ticker_forecast_calibration --since 2026-08-01
"""
from __future__ import annotations

import argparse
import logging
import statistics
import uuid
from datetime import date, datetime, timezone, timedelta

import pandas as pd

_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ticker_forecast_calibration")

# Backfill-tagged forecasts are excluded from calibration (they use today's model,
# not the model at forecast time).
_EXCLUDE_VERSION_PREFIX = "backfill_approx"

# DTE bucket boundaries (inclusive lower, exclusive upper)
_DTE_BUCKETS: list[tuple[str, int, int]] = [
    ("≤7",    0,  8),
    ("8-14",  8, 15),
    ("15-21", 15, 22),
    ("22+",   22, 9999),
]


def _dte_bucket(dte: int) -> str:
    for label, lo, hi in _DTE_BUCKETS:
        if lo <= dte < hi:
            return label
    return "22+"


def _fetch_close(ticker: str, target_date: date) -> tuple[float | None, date | None]:
    """
    Return (close_price, actual_date) for the nearest trading day on or after target_date.
    Uses yfinance with auto_adjust=True.
    Returns (None, None) on failure.
    """
    try:
        import yfinance as yf
        end = target_date + timedelta(days=5)
        df = yf.download(ticker, start=str(target_date), end=str(end),
                         auto_adjust=True, progress=False, timeout=10)
        if df is None or df.empty:
            return None, None
        # yfinance returns MultiIndex columns (Price, Ticker) even for a single
        # symbol — flatten to plain column names so "Close" indexing yields a
        # scalar instead of a sub-Series (which silently broke float() below).
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close_col = "Close"
        if close_col not in df.columns:
            return None, None
        row = df.iloc[0]
        price = float(row[close_col])
        actual_date = df.index[0].date() if hasattr(df.index[0], "date") else date.fromisoformat(str(df.index[0])[:10])
        return price, actual_date
    except Exception as e:
        log.debug("yfinance failed for %s on %s: %s", ticker, target_date, e)
        return None, None


def _load_pending(since: date | None, con) -> list[dict]:
    """Load forecast rows that have expired but haven't been validated yet."""
    today = date.today()
    where_since = f"AND f.forecast_date >= '{since}'" if since else ""
    rows = con.execute(f"""
        SELECT f.id, f.ticker, f.forecast_date, f.expiry_date,
               f.dte, f.model_version, f.p10, f.p25, f.p50, f.p75, f.p90
        FROM ticker_forecast_log f
        LEFT JOIN ticker_forecast_validation v
               ON v.ticker = f.ticker
              AND v.forecast_date = f.forecast_date
              AND v.expiry_date   = f.expiry_date
        WHERE f.expiry_date < '{today}'
          AND f.model_version NOT LIKE '{_EXCLUDE_VERSION_PREFIX}%'
          AND v.id IS NULL
          {where_since}
        ORDER BY f.expiry_date, f.ticker
    """).fetchall()
    cols = ["id", "ticker", "forecast_date", "expiry_date", "dte",
            "model_version", "p10", "p25", "p50", "p75", "p90"]
    return [dict(zip(cols, r)) for r in rows]


def _compute_metrics(vals: list[dict]) -> dict:
    """Aggregate calibration metrics for a group of validation rows."""
    n = len(vals)
    if n == 0:
        return {"n": 0}

    errors = [v["pct_error_p50"] for v in vals if v["pct_error_p50"] is not None]
    return {
        "n":             n,
        "coverage_80":   round(sum(1 for v in vals if v["in_80pct"]) / n, 3),
        "coverage_50":   round(sum(1 for v in vals if v["in_50pct"]) / n, 3),
        "bias_mean":     round(statistics.mean(errors), 4) if errors else None,
        "bias_median":   round(statistics.median(errors), 4) if errors else None,
        "miss_left":     round(sum(1 for v in vals if v["below_p10"]) / n, 3),
        "miss_right":    round(sum(1 for v in vals if v["above_p90"]) / n, 3),
    }


def _print_report(validated: list[dict]) -> None:
    print("\n" + "=" * 64)
    print("  TICKER FORECAST CALIBRATION REPORT")
    print(f"  Run date: {date.today()}")
    print("=" * 64)

    # By DTE bucket
    for label, lo, hi in _DTE_BUCKETS:
        bucket = [v for v in validated if lo <= v["dte"] < hi]
        m = _compute_metrics(bucket)
        if m["n"] == 0:
            continue
        print(f"\nDTE {label:>5}  n={m['n']:>4}")
        print(f"  coverage_80={m['coverage_80']:.1%}  coverage_50={m['coverage_50']:.1%}")
        print(f"  bias_mean={m['bias_mean']:+.3f}  bias_median={m['bias_median']:+.3f}")
        print(f"  miss_left={m['miss_left']:.1%}  miss_right={m['miss_right']:.1%}")

    # Overall
    m = _compute_metrics(validated)
    print(f"\nOVERALL  n={m['n']}")
    if m["n"] > 0:
        print(f"  coverage_80={m['coverage_80']:.1%}  coverage_50={m['coverage_50']:.1%}")
        print(f"  bias_mean={m['bias_mean']:+.3f}  bias_median={m['bias_median']:+.3f}")
        print(f"  miss_left={m['miss_left']:.1%}  miss_right={m['miss_right']:.1%}")

    # Calibration action guidance
    print("\nCALIBRATION GUIDANCE (apply only after 4+ weeks of data)")
    print("  coverage_80 < 0.70 & bias_median > 0  →  increase vol_adj_factor by 0.02")
    print("  coverage_80 < 0.70 & bias_median < 0  →  decrease vol_adj_factor by 0.02")
    print("  coverage_80 >= 0.80                   →  model is well-calibrated")
    print("  coverage_50 < 0.40                    →  check for systematic skew by DTE")
    print("=" * 64 + "\n")


def calibrate(dry_run: bool = False, since: date | None = None) -> None:
    from scripts.db import connect, ensure_forecast_tables

    ensure_forecast_tables()

    with connect(read_only=True) as rcon:
        pending = _load_pending(since, rcon)

    log.info("Pending forecasts to validate: %d", len(pending))
    if not pending:
        log.info("Nothing to validate — all expired forecasts are already processed.")
        return

    # Fetch prices — group by (expiry_date, ticker) to avoid duplicate downloads
    price_cache: dict[tuple[str, str], tuple[float | None, date | None]] = {}
    validated: list[dict] = []

    for fc in pending:
        key = (fc["ticker"], fc["expiry_date"])
        if key not in price_cache:
            exp_date = date.fromisoformat(str(fc["expiry_date"]))
            price_cache[key] = _fetch_close(fc["ticker"], exp_date)

        actual_price, actual_date = price_cache[key]
        if actual_price is None:
            log.debug("No price for %s on %s — skipping", fc["ticker"], fc["expiry_date"])
            continue

        p10, p25, p50, p75, p90 = fc["p10"], fc["p25"], fc["p50"], fc["p75"], fc["p90"]
        dte   = int(fc["dte"])
        pct_error = (actual_price - p50) / p50 if p50 else None

        val = {
            "id":               str(uuid.uuid4()),
            "ticker":           fc["ticker"],
            "forecast_date":    str(fc["forecast_date"]),
            "expiry_date":      str(fc["expiry_date"]),
            "actual_price":     round(actual_price, 4),
            "actual_price_date": str(actual_date) if actual_date else None,
            "dte_bucket":       _dte_bucket(dte),
            "dte":              dte,
            "in_80pct":         p10 is not None and p90 is not None and p10 <= actual_price <= p90,
            "in_50pct":         p25 is not None and p75 is not None and p25 <= actual_price <= p75,
            "above_p50":        p50 is not None and actual_price > p50,
            "below_p10":        p10 is not None and actual_price < p10,
            "above_p90":        p90 is not None and actual_price > p90,
            "pct_error_p50":    round(pct_error, 6) if pct_error is not None else None,
            "validated_at":     datetime.now(timezone.utc).isoformat(),
        }
        validated.append(val)

    log.info("Validated: %d  (no-price skipped: %d)", len(validated), len(pending) - len(validated))

    if validated:
        _print_report(validated)

    if dry_run:
        log.info("DRY RUN — pass without --dry-run to write validation rows to DB")
        return

    if not validated:
        return

    with connect() as wcon:
        for v in validated:
            try:
                wcon.execute("""
                    INSERT INTO ticker_forecast_validation
                        (id, ticker, forecast_date, expiry_date,
                         actual_price, actual_price_date, dte_bucket,
                         in_80pct, in_50pct, above_p50, below_p10, above_p90,
                         pct_error_p50, validated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    v["id"], v["ticker"], v["forecast_date"], v["expiry_date"],
                    v["actual_price"], v["actual_price_date"], v["dte_bucket"],
                    v["in_80pct"], v["in_50pct"], v["above_p50"],
                    v["below_p10"], v["above_p90"],
                    v["pct_error_p50"], v["validated_at"],
                ])
            except Exception as e:
                log.warning("Insert failed for %s/%s: %s", v["ticker"], v["expiry_date"], e)
        wcon.commit()

    log.info("Wrote %d validation rows to ticker_forecast_validation", len(validated))


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and calibrate ticker MC forecasts.")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Fetch prices and compute metrics but do not write to DB")
    parser.add_argument("--since",    type=str, default=None,
                        help="Only process forecasts with forecast_date >= YYYY-MM-DD")
    args = parser.parse_args()

    since = date.fromisoformat(args.since) if args.since else None
    calibrate(dry_run=args.dry_run, since=since)


if __name__ == "__main__":
    main()
