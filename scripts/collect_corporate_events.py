"""
F2: Corporate events collector — dividends and stock splits.

Sources (yfinance):
  Ticker.dividends  → cash dividend per share, indexed by ex-date
  Ticker.splits     → split ratio, indexed by effective date

Schema: corporate_events (see db.py _CORPORATE_EVENTS_DDL)
  id             = f"{ticker}_{event_type}_{event_date}"  (deterministic, upsafe-safe)
  event_type     = 'dividend' | 'split'
  magnitude_usd_m = dividend per share (dividends) | None (splits)
  magnitude_pct  = None (dividends) | split ratio e.g. 4.0 for 4:1 (splits)
  description    = human-readable summary

Run:
  python -m scripts.collect_corporate_events
  python -m scripts.collect_corporate_events --tickers AAPL MSFT
  python -m scripts.collect_corporate_events --since 2020-01-01
  python -m scripts.collect_corporate_events --dry-run
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date
from pathlib import Path
import sys

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.db import connect, ensure_financial_results_tables

log = logging.getLogger(__name__)

SLEEP_BETWEEN = 1.5

_NO_EVENTS = frozenset({
    "SPY", "QQQ", "TQQQ", "IWM",
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "^VIX", "VIX",
})


def _collect_ticker(ticker: str, since: date) -> list[dict]:
    import yfinance as yf

    tkr = yf.Ticker(ticker)
    rows: list[dict] = []

    # Dividends
    try:
        divs = tkr.dividends
        if divs is not None and not divs.empty:
            divs.index = pd.to_datetime(divs.index).tz_localize(None)
            divs = divs[divs.index.date >= since]
            for dt, amount in divs.items():
                if amount <= 0:
                    continue
                ev_date = pd.Timestamp(dt).date()
                rows.append({
                    "id":             f"{ticker}_dividend_{ev_date.isoformat()}",
                    "ticker":         ticker,
                    "event_date":     ev_date.isoformat(),
                    "event_type":     "dividend",
                    "magnitude_pct":  None,
                    "magnitude_usd_m": round(float(amount), 6),
                    "description":    f"Cash dividend ${amount:.4f}/share",
                    "source":         "yfinance",
                })
    except Exception as exc:
        log.warning("  %s: dividends failed (%s)", ticker, exc)

    # Splits
    try:
        splits = tkr.splits
        if splits is not None and not splits.empty:
            splits.index = pd.to_datetime(splits.index).tz_localize(None)
            splits = splits[splits.index.date >= since]
            for dt, ratio in splits.items():
                if ratio <= 0:
                    continue
                ev_date = pd.Timestamp(dt).date()
                rows.append({
                    "id":             f"{ticker}_split_{ev_date.isoformat()}",
                    "ticker":         ticker,
                    "event_date":     ev_date.isoformat(),
                    "event_type":     "split",
                    "magnitude_pct":  round(float(ratio), 6),
                    "magnitude_usd_m": None,
                    "description":    f"{ratio}:1 stock split",
                    "source":         "yfinance",
                })
    except Exception as exc:
        log.warning("  %s: splits failed (%s)", ticker, exc)

    return rows


_INSERT_COLS = [
    "id", "ticker", "event_date", "event_type",
    "magnitude_pct", "magnitude_usd_m", "description", "source",
]

def _upsert(rows: list[dict], dry_run: bool) -> int:
    if not rows or dry_run:
        return len(rows)
    ph = ", ".join(["?"] * len(_INSERT_COLS))
    sql = (
        f"INSERT OR REPLACE INTO corporate_events "
        f"({', '.join(_INSERT_COLS)}) VALUES ({ph})"
    )
    with connect() as con:
        for row in rows:
            con.execute(sql, [row.get(c) for c in _INSERT_COLS])
        con.commit()
    return len(rows)


def run(
    tickers: list[str] | None = None,
    since: date | None = None,
    dry_run: bool = False,
) -> None:
    ensure_financial_results_tables()

    if tickers is None:
        with connect() as con:
            tickers = [
                r[0] for r in con.execute(
                    "SELECT DISTINCT ticker FROM regime_training ORDER BY ticker"
                ).fetchall()
            ]

    tickers = [t for t in tickers if t not in _NO_EVENTS]

    if since is None:
        # Default: 3 years back — covers all training data with margin
        from datetime import timedelta
        since = date.today().replace(year=date.today().year - 3)

    log.info("Collecting corporate events for %d tickers since %s", len(tickers), since)

    all_rows: list[dict] = []
    n_skip = 0
    n_div = 0
    n_split = 0

    for i, ticker in enumerate(tickers):
        log.info("[%d/%d] %s", i + 1, len(tickers), ticker)
        try:
            rows = _collect_ticker(ticker, since)
            divs   = [r for r in rows if r["event_type"] == "dividend"]
            splits = [r for r in rows if r["event_type"] == "split"]
            all_rows.extend(rows)
            n_div   += len(divs)
            n_split += len(splits)
            log.info("  %s: %d dividends, %d splits", ticker, len(divs), len(splits))
        except Exception as exc:
            log.warning("  %s: ERROR — %s", ticker, exc)
            n_skip += 1

        if i < len(tickers) - 1:
            time.sleep(SLEEP_BETWEEN)

    inserted = _upsert(all_rows, dry_run)

    print(f"\nDone.")
    print(f"  Tickers attempted : {len(tickers)}")
    print(f"  Tickers skipped   : {n_skip}")
    print(f"  Dividends found   : {n_div}")
    print(f"  Splits found      : {n_split}")
    print(f"  Rows {'would insert' if dry_run else 'inserted'}: {inserted}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Corporate events backfill (dividends + splits)")
    ap.add_argument("--tickers", nargs="+", default=None)
    ap.add_argument("--since",   default=None,
                    help="Start date ISO format YYYY-MM-DD (default: 3 years ago)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since_date = date.fromisoformat(args.since) if args.since else None
    run(tickers=args.tickers, since=since_date, dry_run=args.dry_run)
