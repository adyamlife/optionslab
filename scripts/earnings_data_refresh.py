#!/usr/bin/env python
"""
Standalone entry point for cron — weekly refresh of financial-results data
(F1 earnings fundamentals, F2 corporate events, F3 earnings NLP).
Schedule: weekly, e.g. Sunday 7:00 PM ET (before weekly_profile_build at 8 PM).

These three collectors (scripts/collect_earnings_fundamentals.py,
collect_corporate_events.py, collect_earnings_nlp.py) previously had no
scheduler entry at all — they only ran once as a manual backfill. Earnings
land quarterly per ticker, so leaving them unscheduled means the JOIN in
scripts/earnings_features.py (consumed by POP/return-classifier/trade-win
training) silently drifts stale as new quarters land with no fundamentals/
NLP data behind them. Diagnosed 2026-08-22.

Incremental, not a full re-pull: F1/F3 only reprocess tickers whose latest
known earnings_date is missing or older than STALE_DAYS (~1 quarter + buffer)
— a full 101-ticker x 8-quarter re-pull every week would be wasteful API load
for data that's ~99% unchanged week to week. F2 (dividends/splits) always
checks the full universe but only looks back a short window since it's cheap.
"""
import sys
import logging
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "earnings_data_refresh.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("earnings_data_refresh")

STALE_DAYS = 75  # ~1 quarter (91d) minus a buffer, so a new quarter is caught promptly


def _stale_tickers() -> list[str]:
    """Tickers with no earnings_fundamentals row, or whose latest row is stale."""
    from scripts.db import connect

    cutoff = date.today() - timedelta(days=STALE_DAYS)
    with connect(read_only=True) as con:
        universe = [
            r[0] for r in con.execute(
                "SELECT DISTINCT ticker FROM regime_training ORDER BY ticker"
            ).fetchall()
        ]
        fresh = {
            r[0] for r in con.execute(
                "SELECT ticker FROM earnings_fundamentals "
                "GROUP BY ticker HAVING MAX(earnings_date) >= ?",
                [cutoff],
            ).fetchall()
        }
    return [t for t in universe if t not in fresh]


def main() -> None:
    stale = _stale_tickers()
    if not stale:
        log.info("No stale tickers — earnings_fundamentals is current for the full universe.")
    else:
        log.info("[F1] Refreshing earnings fundamentals for %d stale ticker(s): %s",
                  len(stale), stale[:20])
        try:
            from scripts.collect_earnings_fundamentals import run as _f1_run
            _f1_run(tickers=stale)
        except Exception as e:
            log.error("[F1] Earnings fundamentals refresh failed: %s", e)

    log.info("[F2] Refreshing corporate events (last 14 days) for the full universe")
    try:
        from scripts.collect_corporate_events import run as _f2_run
        _f2_run(since=date.today() - timedelta(days=14))
    except Exception as e:
        log.error("[F2] Corporate events refresh failed: %s", e)

    if stale:
        log.info("[F3] Refreshing earnings NLP for the same %d stale ticker(s)", len(stale))
        try:
            from scripts.collect_earnings_nlp import run as _f3_run
            _f3_run(tickers=stale, max_quarters=1)
        except Exception as e:
            log.error("[F3] Earnings NLP refresh failed: %s", e)
    else:
        log.info("[F3] Skipped — no stale tickers to refresh NLP for.")

    log.info("Weekly earnings-data refresh complete.")


if __name__ == "__main__":
    main()
