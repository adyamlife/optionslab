"""
Backfill entry MC distribution on existing open paper trades.

Populates entry_mc_expiry_p10/p25/p50/p75/p90 and
entry_distribution_model_version on trades that were entered before
the Phase 2A deploy (i.e. those with null entry_mc_expiry_p10).

Data sources used:
  - spot_at_entry  : exact value stored on the trade record
  - dte_at_entry   : exact value stored on the trade record
  - atm_iv         : looked up from training_snapshots for that ticker
                     on or near the trade entry date (closest record)

IMPORTANT — calibration safety:
  entry_distribution_model_version is set to "backfill_approx:<date>"
  which uses today's GARCH model, NOT the model at trade entry time.
  These values MUST NOT be used in any calibration analysis.
  The "backfill_approx:" prefix is the sentinel — all calibration
  queries must filter:
      WHERE distribution_model_version NOT LIKE 'backfill_approx:%'

Run:
  python -m scripts.backfill_paper_trade_mc             # dry-run by default
  python -m scripts.backfill_paper_trade_mc --apply     # write changes
  python -m scripts.backfill_paper_trade_mc --all       # include closed trades too
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_paper_trade_mc")

_TRADES_FILE = _ROOT / "data" / "paper_trades.json"
_VERSION_PREFIX = "backfill_approx"


def _load_trades() -> list[dict]:
    return json.loads(_TRADES_FILE.read_text(encoding="utf-8"))


def _save_trades(trades: list[dict]) -> None:
    # Backup first
    bak = _TRADES_FILE.with_suffix(".json.bak_mc_backfill")
    shutil.copy2(_TRADES_FILE, bak)
    log.info("Backup written to %s", bak)
    _TRADES_FILE.write_text(
        json.dumps(trades, indent=2, default=str), encoding="utf-8"
    )


def _lookup_iv(ticker: str, entry_date_str: str) -> float | None:
    """
    Find the closest atm_iv for ticker on or near entry_date from
    training_snapshots. Returns None if no record found.
    """
    from scripts.db import connect, SNAPSHOTS_TABLE, ensure_snapshot_tables
    ensure_snapshot_tables()
    # entry_date_str is ISO timestamp e.g. "2026-07-15T10:07:44..."
    entry_day = entry_date_str[:10]  # "YYYY-MM-DD"

    from datetime import date, timedelta
    entry = datetime.fromisoformat(entry_day).date()
    lo = str(entry - timedelta(days=3))
    hi = str(entry + timedelta(days=3))

    with connect(read_only=True) as con:
        # Closest snapshot within ±3 days of entry
        rows = con.execute(f"""
            SELECT atm_iv, substr(collected_at, 1, 10) AS snap_day
            FROM {SNAPSHOTS_TABLE}
            WHERE ticker = ?
              AND atm_iv IS NOT NULL
              AND atm_iv > 0
              AND substr(collected_at, 1, 10) BETWEEN ? AND ?
        """, [ticker, lo, hi]).fetchall()

        if rows:
            # Pick the one closest to entry_day
            best = min(rows, key=lambda r: abs(
                (datetime.fromisoformat(r[1]).date() - entry).days
            ))
            return float(best[0])

        # Fallback: most recent snapshot ever for this ticker
        row = con.execute(f"""
            SELECT atm_iv FROM {SNAPSHOTS_TABLE}
            WHERE ticker = ? AND atm_iv IS NOT NULL AND atm_iv > 0
            ORDER BY collected_at DESC LIMIT 1
        """, [ticker]).fetchone()
        return float(row[0]) if row else None


def _run_mc(ticker: str, spot: float, iv: float, dte: int,
            n_sims: int = 500) -> dict | None:
    import numpy as np
    from scripts.monte_carlo import simulate_paths
    from config.rules import RISK_FREE_RATE
    from datetime import date

    try:
        S_T, _, _, vol_source = simulate_paths(
            ticker, spot, iv, dte, RISK_FREE_RATE, n_sims=n_sims
        )
        if S_T is None:
            return None
        pcts = np.percentile(S_T, [10, 25, 50, 75, 90])
        version = f"{_VERSION_PREFIX}:{date.today().isoformat()}:{vol_source}"
        return {
            "entry_mc_expiry_p10": round(float(pcts[0]), 4),
            "entry_mc_expiry_p25": round(float(pcts[1]), 4),
            "entry_mc_expiry_p50": round(float(pcts[2]), 4),
            "entry_mc_expiry_p75": round(float(pcts[3]), 4),
            "entry_mc_expiry_p90": round(float(pcts[4]), 4),
            "entry_distribution_model_version": version,
        }
    except Exception as e:
        log.debug("MC failed for %s: %s", ticker, e)
        return None


def backfill(apply: bool = False, include_closed: bool = False,
             n_sims: int = 500) -> None:
    trades = _load_trades()

    candidates = [
        t for t in trades
        if t.get("entry_mc_expiry_p10") is None
        and (include_closed or t.get("status") == "open")
        and t.get("spot_at_entry")
        and t.get("dte_at_entry")
    ]

    log.info("Trades needing MC backfill: %d  (apply=%s)", len(candidates), apply)

    filled = skipped_no_iv = skipped_mc_fail = 0

    for t in candidates:
        ticker      = t["ticker"]
        spot        = float(t["spot_at_entry"])
        dte         = int(t["dte_at_entry"])
        entered_at  = t.get("entered_at", "")

        iv = _lookup_iv(ticker, entered_at)
        if iv is None:
            log.debug("No IV found for %s (entry %s) — skipping", ticker, entered_at[:10])
            skipped_no_iv += 1
            continue

        result = _run_mc(ticker, spot, iv, dte, n_sims=n_sims)
        if result is None:
            log.debug("MC returned None for %s — skipping", ticker)
            skipped_mc_fail += 1
            continue

        log.info("  %s  spot=%.2f  iv=%.3f  dte=%d  p10=%.2f  p50=%.2f  p90=%.2f  ver=%s",
                 ticker, spot, iv, dte,
                 result["entry_mc_expiry_p10"],
                 result["entry_mc_expiry_p50"],
                 result["entry_mc_expiry_p90"],
                 result["entry_distribution_model_version"])

        if apply:
            t.update(result)
        filled += 1

    log.info("Results: filled=%d  skipped_no_iv=%d  skipped_mc_fail=%d",
             filled, skipped_no_iv, skipped_mc_fail)

    if apply and filled > 0:
        _save_trades(trades)
        log.info("Saved %s", _TRADES_FILE)
    elif not apply:
        log.info("DRY RUN — pass --apply to write changes")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill entry MC distribution on paper trades."
    )
    parser.add_argument("--apply",  action="store_true",
                        help="Write changes to paper_trades.json (default: dry-run)")
    parser.add_argument("--all",    action="store_true",
                        help="Include closed trades (default: open only)")
    parser.add_argument("--n-sims", type=int, default=500,
                        help="MC simulations per trade (default 500)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("backfill_paper_trade_mc  apply=%s  all=%s  n_sims=%d",
             args.apply, args.all, args.n_sims)
    log.info("=" * 60)
    log.info("NOTE: version tagged '%s:*' — DO NOT use for calibration", _VERSION_PREFIX)

    backfill(apply=args.apply, include_closed=args.all, n_sims=args.n_sims)


if __name__ == "__main__":
    main()
