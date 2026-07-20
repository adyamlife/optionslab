"""
Backfill ul_price (underlying close on exit date) into etrade_labeled_trades.jsonl.

For each record that has outcome.exit_date but no outcome.ul_price, fetches the
closing price of the ticker on that date via yfinance and writes it back.  Also
computes spot_at_entry from the top-level `spot` field so analyze_trade_failures
can fire the gap_move classifier on E*TRADE trades.

Run:
    python -m scripts.enrich_etrade_exit_spot
    python -m scripts.enrich_etrade_exit_spot --dry-run
"""
import argparse
import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_PATH = Path(__file__).parent.parent / "data" / "etrade_labeled_trades.jsonl"


def _fetch_close_on_date(ticker: str, target: date) -> float | None:
    """Return closing price for ticker on target date (or nearest prior trading day)."""
    start = (target - timedelta(days=5)).isoformat()
    end   = (target + timedelta(days=1)).isoformat()
    try:
        hist = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if hist.empty:
            return None
        close = hist["Close"]
        if hasattr(close, "columns"):          # multi-ticker shape guard
            close = close[ticker]
        close.index = pd.to_datetime(close.index).date
        # find the target date or the nearest prior trading day
        for d in sorted(close.index, reverse=True):
            if d <= target:
                return round(float(close.loc[d]), 4)
    except Exception as e:
        log.warning("yfinance error for %s on %s: %s", ticker, target, e)
    return None


def run(dry_run: bool = False) -> dict:
    records = []
    with open(_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Group tickers by exit date for batching (one download per ticker, covers all dates)
    ticker_dates: dict[str, set[date]] = defaultdict(set)
    needs_update = []
    for i, r in enumerate(records):
        out = r.get("outcome") or {}
        exit_date_str = out.get("exit_date")
        ul_price = out.get("ul_price")
        if exit_date_str and ul_price is None:
            ticker = r.get("ticker", "")
            if ticker:
                d = date.fromisoformat(exit_date_str)
                ticker_dates[ticker].add(d)
                needs_update.append(i)

    log.info("%d records need exit spot enrichment across %d tickers",
             len(needs_update), len(ticker_dates))

    if not needs_update:
        return {"ok": True, "updated": 0, "skipped": 0}

    # Fetch price history per ticker (one call covers all exit dates for that ticker)
    price_cache: dict[tuple[str, date], float | None] = {}
    for ticker, dates in ticker_dates.items():
        min_d = min(dates)
        max_d = max(dates)
        start = (min_d - timedelta(days=5)).isoformat()
        end   = (max_d + timedelta(days=2)).isoformat()
        log.info("Fetching %s  %s → %s", ticker, start, end)
        try:
            hist = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if hist.empty:
                for d in dates:
                    price_cache[(ticker, d)] = None
                continue
            close = hist["Close"]
            if hasattr(close, "columns"):
                close = close[ticker]
            close.index = pd.to_datetime(close.index).date
            trading_days = sorted(close.index)
            for target in dates:
                val = None
                for d in reversed(trading_days):
                    if d <= target:
                        val = round(float(close.loc[d]), 4)
                        break
                price_cache[(ticker, target)] = val
        except Exception as e:
            log.warning("Error fetching %s: %s", ticker, e)
            for d in dates:
                price_cache[(ticker, d)] = None

    # Apply to records
    updated = skipped = 0
    for i in needs_update:
        r = records[i]
        out = r.get("outcome") or {}
        ticker = r.get("ticker", "")
        exit_date = date.fromisoformat(out["exit_date"])
        ul_price = price_cache.get((ticker, exit_date))
        if ul_price is None:
            log.warning("No price found for %s on %s — skipping", ticker, exit_date)
            skipped += 1
            continue
        out["ul_price"] = ul_price
        # Also ensure spot_at_entry is accessible in the same shape analyze_trade_failures expects
        # (it reads trade["spot_at_entry"] from the normalised dict, sourced from r["spot"])
        updated += 1

    log.info("Updated: %d  |  Skipped (no price): %d", updated, skipped)

    if dry_run:
        log.info("Dry run — no file written")
        # Show a sample
        for i in needs_update[:3]:
            r = records[i]
            out = r.get("outcome") or {}
            log.info("  Sample: %s exit=%s ul_price=%s spot_entry=%s",
                     r.get("ticker"), out.get("exit_date"),
                     out.get("ul_price"), r.get("spot"))
        return {"ok": True, "updated": updated, "skipped": skipped, "dry_run": True}

    with open(_PATH, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    log.info("Wrote %d records to %s", len(records), _PATH)
    return {"ok": True, "updated": updated, "skipped": skipped}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and report without writing the file")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    print(result)
