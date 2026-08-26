"""
F4: Prospective IV snapshot collector for earnings_iv_timeline.

Run daily by the scheduler. For each upcoming earnings event, checks whether
today falls on a target offset (-30, -14, -7, -3, -1, +1 days from earnings_date)
and, if so, snapshots the current IV from training_snapshots.

Rows written have has_historical_iv=1 (real prospective IV, not yfinance backfill).

Schema: earnings_iv_timeline
  ticker, earnings_date, days_offset, snapshot_date, has_historical_iv,
  atm_iv, iv_rank_52w, expected_move_pct, implied_move_pct, iv_crush_pct,
  realized_move_pct, source

Run:
  python -m scripts.collect_earnings_iv_snapshots             # today's offsets
  python -m scripts.collect_earnings_iv_snapshots --date 2026-08-20
  python -m scripts.collect_earnings_iv_snapshots --lookahead-days 35
  python -m scripts.collect_earnings_iv_snapshots --dry-run
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path
import sys

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.db import connect, ensure_financial_results_tables

log = logging.getLogger(__name__)

# Target offsets relative to earnings_date. Negative = days before.
TARGET_OFFSETS = [-30, -14, -7, -3, -1, 1]

# How far forward to scan for upcoming earnings events
DEFAULT_LOOKAHEAD_DAYS = 35


def _get_upcoming_earnings(today: date, lookahead: int) -> list[tuple]:
    """Return [(ticker, earnings_date)] for events within [today-1, today+lookahead]."""
    window_lo = today - timedelta(days=1)
    window_hi = today + timedelta(days=lookahead)
    with connect() as con:
        rows = con.execute(
            """
            SELECT ticker, earnings_date
            FROM earnings_fundamentals
            WHERE earnings_date BETWEEN ? AND ?
            ORDER BY earnings_date, ticker
            """,
            [window_lo.isoformat(), window_hi.isoformat()],
        ).fetchall()
    return [(r[0], date.fromisoformat(r[1])) for r in rows]


def _get_current_iv(ticker: str, snapshot_date: date) -> dict | None:
    """
    Get the most recent IV metrics for ticker from training_snapshots on or before snapshot_date.
    Returns dict with atm_iv, iv_rank_52w, expected_move_pct or None if unavailable.
    """
    with connect() as con:
        row = con.execute(
            """
            SELECT atm_iv, iv_rank_proxy, expected_move_pct
            FROM training_snapshots
            WHERE ticker = ?
              AND CAST(collected_at AS DATE) <= ?
              AND atm_iv IS NOT NULL
              AND atm_iv > 0
            ORDER BY collected_at DESC
            LIMIT 1
            """,
            [ticker, snapshot_date.isoformat()],
        ).fetchone()

    if not row:
        return None

    atm_iv, iv_rank_52w, expected_move_pct = row
    return {
        "atm_iv":            round(float(atm_iv), 6)            if atm_iv            else None,
        "iv_rank_52w":       round(float(iv_rank_52w), 4)       if iv_rank_52w       else None,
        "expected_move_pct": round(float(expected_move_pct), 6) if expected_move_pct else None,
    }


def _upsert_snapshot(row: dict, dry_run: bool) -> None:
    if dry_run:
        return
    cols = [
        "ticker", "earnings_date", "days_offset", "snapshot_date",
        "has_historical_iv", "atm_iv", "iv_rank_52w",
        "expected_move_pct", "source",
    ]
    ph = ", ".join(["?"] * len(cols))
    sql = (
        f"INSERT OR REPLACE INTO earnings_iv_timeline "
        f"({', '.join(cols)}) VALUES ({ph})"
    )
    with connect() as con:
        con.execute(sql, [row.get(c) for c in cols])
        con.commit()


def run(
    today: date | None = None,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    dry_run: bool = False,
) -> None:
    ensure_financial_results_tables()
    today = today or date.today()

    upcoming = _get_upcoming_earnings(today, lookahead_days)
    if not upcoming:
        log.info("No upcoming earnings events in next %d days", lookahead_days)
        return

    log.info(
        "%d upcoming earnings events; checking offsets %s for %s",
        len(upcoming), TARGET_OFFSETS, today,
    )

    n_snapshots = 0
    n_miss = 0

    for ticker, earnings_date in upcoming:
        for offset in TARGET_OFFSETS:
            target_date = earnings_date + timedelta(days=offset)
            if target_date != today:
                continue

            # Today matches this offset — take a snapshot
            iv = _get_current_iv(ticker, today)
            if iv is None:
                log.warning("  %s %s offset=%+d: no IV data in training_snapshots", ticker, earnings_date, offset)
                n_miss += 1
                continue

            row = {
                "ticker":          ticker,
                "earnings_date":   earnings_date.isoformat(),
                "days_offset":     offset,
                "snapshot_date":   today.isoformat(),
                "has_historical_iv": 1,
                "atm_iv":          iv["atm_iv"],
                "iv_rank_52w":     iv["iv_rank_52w"],
                "expected_move_pct": iv["expected_move_pct"],
                "implied_move_pct": None,   # populated post-earnings when we have the term structure
                "iv_crush_pct":    None,    # populated post-earnings: (pre_iv - post_iv) / pre_iv
                "realized_move_pct": None,  # populated post-earnings from price history
                "source":          "training_snapshots",
            }
            _upsert_snapshot(row, dry_run)
            log.info(
                "  %s %s offset=%+d: atm_iv=%.4f iv_rank=%.1f",
                ticker, earnings_date, offset, iv["atm_iv"] or 0, iv["iv_rank_52w"] or 0,
            )
            n_snapshots += 1

    print(f"\nDone ({today}).")
    print(f"  Snapshots {'would write' if dry_run else 'written'}: {n_snapshots}")
    print(f"  Missing IV data : {n_miss}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Earnings IV snapshot collector")
    ap.add_argument("--date",           default=None,
                    help="Override today's date (YYYY-MM-DD)")
    ap.add_argument("--lookahead-days", type=int, default=DEFAULT_LOOKAHEAD_DAYS)
    ap.add_argument("--dry-run",        action="store_true")
    args = ap.parse_args()

    run_date = date.fromisoformat(args.date) if args.date else None
    run(today=run_date, lookahead_days=args.lookahead_days, dry_run=args.dry_run)
