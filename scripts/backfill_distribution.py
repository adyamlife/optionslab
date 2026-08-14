"""
Backfill distribution columns for existing labeled training_snapshots.

Step 1 — Promote spot_at_expiry from outcome JSON to top-level column.
           5,000+ labeled rows already have it stored inside outcome{} —
           this just copies it to the queryable column.

Step 2 — Compute zone_at_expiry from candidate strikes + spot_at_expiry.
           Pure logic, no network calls, no model needed.

Step 3 — Re-run MC on stored candidate data to backfill mc_expiry_* and
           zone probability columns.  Uses today's GARCH model (not the
           model at trade entry time), so results are tagged
           'backfill_gbm:v1' or 'backfill_garch:<date>' to keep them
           separate from live data in calibration queries.
           Skipped by default; pass --mc to enable.

Run:
  python -m scripts.backfill_distribution           # steps 1 + 2
  python -m scripts.backfill_distribution --mc      # steps 1 + 2 + 3
  python -m scripts.backfill_distribution --dry-run # show counts only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_distribution")

_BATCH = 500   # rows per DB commit


# ---------------------------------------------------------------------------
# Shared zone logic (mirrors training_data_collector._zone_at_expiry)
# ---------------------------------------------------------------------------

def _zone_at_expiry(candidate: dict, s_t: float) -> str | None:
    if s_t is None or not candidate:
        return None
    structure = candidate.get("structure", "")

    if structure == "Iron Condor":
        pl = candidate.get("put_long_strike")
        ps = candidate.get("put_short_strike")
        cs = candidate.get("call_short_strike")
        cl = candidate.get("call_long_strike")
        if None in (pl, ps, cs, cl):
            return None
        if ps <= s_t <= cs:
            return "full_win"
        if (pl <= s_t < ps) or (cs < s_t <= cl):
            return "partial_win"
        return "loss"

    if structure in ("Call Debit Spread", "Call Credit Spread",
                     "Ratio Call Backspread"):
        k_long  = candidate.get("long_strike")
        k_short = candidate.get("short_strike")
        if None in (k_long, k_short):
            return None
        lo, hi = sorted([k_long, k_short])
        if s_t >= hi:
            return "full_win"
        if s_t >= lo:
            return "partial_win"
        return "loss"

    if structure in ("Put Debit Spread", "Put Credit Spread",
                     "Ratio Put Backspread"):
        k_long  = candidate.get("long_strike")
        k_short = candidate.get("short_strike")
        if None in (k_long, k_short):
            return None
        lo, hi = sorted([k_long, k_short])
        if s_t <= lo:
            return "full_win"
        if s_t <= hi:
            return "partial_win"
        return "loss"

    if structure == "Cash Secured Put":
        k = candidate.get("short_strike")
        if k is None:
            return None
        return "full_win" if s_t >= k else "loss"

    if structure == "Covered Call":
        k = candidate.get("short_strike")
        if k is None:
            return None
        return "partial_win" if s_t >= k else "full_win"

    if structure in ("Long Call", "Long Put"):
        k = candidate.get("long_strike") or candidate.get("short_strike")
        if k is None:
            return None
        return "full_win" if (s_t > k if structure == "Long Call" else s_t < k) else "loss"

    if structure in ("Long Strangle", "Long Straddle"):
        k_put  = candidate.get("put_short_strike") or candidate.get("short_strike")
        k_call = candidate.get("call_short_strike") or candidate.get("long_strike")
        if None in (k_put, k_call):
            return None
        lo, hi = sorted([k_put, k_call])
        return "loss" if lo <= s_t <= hi else "full_win"

    if structure == "Short Strangle":
        k_put  = candidate.get("put_short_strike")
        k_call = candidate.get("call_short_strike")
        if None in (k_put, k_call):
            return None
        lo, hi = sorted([k_put, k_call])
        return "full_win" if lo <= s_t <= hi else "loss"

    if structure == "Risk Reversal":
        k_put  = candidate.get("short_strike")
        k_call = candidate.get("long_strike")
        if None in (k_put, k_call):
            return None
        if s_t >= k_call:
            return "full_win"
        if s_t <= k_put:
            return "loss"
        return "partial_win"

    return None


# ---------------------------------------------------------------------------
# Step 1 + 2 — promote spot_at_expiry and compute zone_at_expiry
# ---------------------------------------------------------------------------

def backfill_spot_and_zone(dry_run: bool = False) -> dict:
    from scripts.db import connect, SNAPSHOTS_TABLE, ensure_snapshot_tables
    ensure_snapshot_tables()

    log.info("Loading labeled snapshots...")
    with connect(read_only=True) as con:
        rows = con.execute(f"""
            SELECT snapshot_id, outcome, candidate
            FROM {SNAPSHOTS_TABLE}
            WHERE labeled = true
              AND outcome IS NOT NULL
              AND spot_at_expiry IS NULL
        """).fetchall()

    log.info("Candidates for backfill: %d", len(rows))

    updated = skipped_no_spot = skipped_no_cand = zone_computed = zone_skipped = 0
    batch = []

    for snap_id, outcome_raw, cand_raw in rows:
        try:
            outcome = json.loads(outcome_raw) if isinstance(outcome_raw, str) else (outcome_raw or {})
        except Exception:
            outcome = {}

        s_t = outcome.get("spot_at_expiry")
        if s_t is None:
            skipped_no_spot += 1
            continue

        try:
            candidate = json.loads(cand_raw) if isinstance(cand_raw, str) else (cand_raw or {})
        except Exception:
            candidate = {}
            skipped_no_cand += 1

        zone = _zone_at_expiry(candidate, float(s_t))
        if zone:
            zone_computed += 1
        else:
            zone_skipped += 1

        batch.append((round(float(s_t), 2), zone, snap_id))
        updated += 1

        if not dry_run and len(batch) >= _BATCH:
            _flush_spot_zone(batch)
            batch.clear()
            log.info("  committed %d rows so far...", updated)

    if not dry_run and batch:
        _flush_spot_zone(batch)

    result = {
        "updated":         updated,
        "skipped_no_spot": skipped_no_spot,
        "skipped_no_cand": skipped_no_cand,
        "zone_computed":   zone_computed,
        "zone_skipped":    zone_skipped,
    }
    log.info("Step 1+2 %s: %s", "DRY RUN" if dry_run else "done", result)
    return result


def _flush_spot_zone(batch: list):
    from scripts.db import connect, SNAPSHOTS_TABLE
    with connect() as con:
        con.executemany(
            f"UPDATE {SNAPSHOTS_TABLE} SET spot_at_expiry=?, zone_at_expiry=? "
            f"WHERE snapshot_id=?",
            batch,
        )
        con.commit()


# ---------------------------------------------------------------------------
# Step 3 — MC backfill
# ---------------------------------------------------------------------------

_MC_ZONE_FIELDS = (
    "mc_zone_below_long", "mc_zone_between", "mc_zone_above_short",
    "mc_zone_in_profit", "mc_zone_below_put_long", "mc_zone_in_loss_put",
    "mc_zone_in_loss_call", "mc_zone_above_call_long", "mc_zone_below_short",
)

_MC_SET_COLS = (
    "mc_expiry_mean=?, mc_expiry_median=?, "
    "mc_expiry_p10=?, mc_expiry_p25=?, mc_expiry_p50=?, mc_expiry_p75=?, mc_expiry_p90=?, "
    "distribution_model_version=?, "
    "mc_zone_below_long=?, mc_zone_between=?, mc_zone_above_short=?, "
    "mc_zone_in_profit=?, mc_zone_below_put_long=?, mc_zone_in_loss_put=?, "
    "mc_zone_in_loss_call=?, mc_zone_above_call_long=?, mc_zone_below_short=?"
)


def backfill_mc(dry_run: bool = False, n_sims: int = 1000) -> dict:
    """
    Re-run MC on stored candidate data to populate distribution columns.
    Uses today's GARCH model; tags results as backfill_* version.
    Only processes rows that have no mc_expiry_p10 yet.
    """
    from scripts.db import connect, SNAPSHOTS_TABLE, ensure_snapshot_tables
    from scripts.monte_carlo import run_mc
    ensure_snapshot_tables()

    log.info("Loading snapshots needing MC backfill...")
    with connect(read_only=True) as con:
        rows = con.execute(f"""
            SELECT snapshot_id, candidate, spot, dte, ticker
            FROM {SNAPSHOTS_TABLE}
            WHERE mc_expiry_p10 IS NULL
              AND candidate IS NOT NULL
              AND labeled = true
        """).fetchall()

    log.info("Candidates for MC backfill: %d  (n_sims=%d per row)", len(rows), n_sims)
    if dry_run:
        log.info("DRY RUN — no changes written.")
        return {"dry_run": True, "candidates": len(rows)}

    done = skipped = errors = 0
    batch = []
    t0 = time.monotonic()

    for snap_id, cand_raw, spot, dte, ticker in rows:
        try:
            candidate = json.loads(cand_raw) if isinstance(cand_raw, str) else (cand_raw or {})
        except Exception:
            skipped += 1
            continue

        # Build a minimal row dict for run_mc
        row = {
            "spot":    candidate.get("spot_at_entry") or spot,
            "dte":     candidate.get("dte") or dte,
            "atm_iv":  candidate.get("atm_iv") or candidate.get("hv20"),
            "hv20":    candidate.get("hv20"),
            "ticker":  ticker,
        }
        if not row["spot"] or not row["dte"]:
            skipped += 1
            continue
        if not row["atm_iv"]:
            # No IV available — cannot run MC meaningfully
            skipped += 1
            continue

        try:
            mc = run_mc(ticker, row, candidate, n_sims=n_sims)
        except Exception as e:
            log.debug("MC failed for %s: %s", snap_id, e)
            errors += 1
            continue

        # Tag version as backfill so calibration can filter it
        version = mc.get("distribution_model_version", "gbm:v1")
        version = f"backfill_{version}"

        record = (
            mc.get("mc_expiry_mean"),   mc.get("mc_expiry_median"),
            mc.get("mc_expiry_p10"),    mc.get("mc_expiry_p25"),
            mc.get("mc_expiry_p50"),    mc.get("mc_expiry_p75"),
            mc.get("mc_expiry_p90"),
            version,
            mc.get("mc_zone_below_long"),    mc.get("mc_zone_between"),
            mc.get("mc_zone_above_short"),   mc.get("mc_zone_in_profit"),
            mc.get("mc_zone_below_put_long"),mc.get("mc_zone_in_loss_put"),
            mc.get("mc_zone_in_loss_call"),  mc.get("mc_zone_above_call_long"),
            mc.get("mc_zone_below_short"),
            snap_id,
        )
        batch.append(record)
        done += 1

        if len(batch) >= _BATCH:
            _flush_mc(batch)
            batch.clear()
            elapsed = time.monotonic() - t0
            rate = done / elapsed
            log.info("  committed %d rows  (%.1f rows/s, ~%.0fs remaining)",
                     done, rate, (len(rows) - done) / max(rate, 0.01))

    if batch:
        _flush_mc(batch)

    result = {"updated": done, "skipped": skipped, "errors": errors}
    log.info("Step 3 done: %s  (%.1fs)", result, time.monotonic() - t0)
    return result


def _flush_mc(batch: list):
    from scripts.db import connect, SNAPSHOTS_TABLE
    with connect() as con:
        con.executemany(
            f"UPDATE {SNAPSHOTS_TABLE} SET {_MC_SET_COLS} WHERE snapshot_id=?",
            batch,
        )
        con.commit()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(verbose: bool = False):
    from scripts.db import connect, SNAPSHOTS_TABLE, ensure_snapshot_tables
    ensure_snapshot_tables()
    with connect(read_only=True) as con:
        r = con.execute(f"""
            SELECT
                count(*)                                        AS total_labeled,
                count(spot_at_expiry)                           AS has_spot,
                count(zone_at_expiry)                           AS has_zone,
                count(mc_expiry_p10)                            AS has_mc_p10,
                count(CASE WHEN distribution_model_version LIKE 'backfill_%'
                           THEN 1 END)                          AS mc_backfill,
                count(CASE WHEN distribution_model_version NOT LIKE 'backfill_%'
                           AND distribution_model_version IS NOT NULL
                           THEN 1 END)                          AS mc_live
            FROM {SNAPSHOTS_TABLE}
            WHERE labeled = true
        """).fetchone()

    print("\nBackfill verification:")
    print(f"  Total labeled snapshots : {r[0]:>7}")
    print(f"  spot_at_expiry filled   : {r[1]:>7}  ({r[1]/max(r[0],1)*100:.1f}%)")
    print(f"  zone_at_expiry filled   : {r[2]:>7}  ({r[2]/max(r[0],1)*100:.1f}%)")
    print(f"  mc_expiry_p10 filled    : {r[3]:>7}  ({r[3]/max(r[0],1)*100:.1f}%)")
    print(f"    of which backfill     : {r[4]:>7}")
    print(f"    of which live         : {r[5]:>7}")

    if verbose:
        with connect(read_only=True) as con:
            zones = con.execute(f"""
                SELECT zone_at_expiry, count(*) AS n
                FROM {SNAPSHOTS_TABLE}
                WHERE zone_at_expiry IS NOT NULL
                GROUP BY zone_at_expiry ORDER BY n DESC
            """).fetchall()
        print("\n  zone_at_expiry breakdown:")
        for zone, n in zones:
            print(f"    {zone:<15} {n:>6}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backfill distribution columns.")
    parser.add_argument("--mc",      action="store_true",
                        help="Also run MC backfill (step 3). Slower; uses today's GARCH model.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show counts only, write nothing.")
    parser.add_argument("--n-sims",  type=int, default=1000,
                        help="MC simulations per row for step 3 (default 1000).")
    parser.add_argument("--verify",  action="store_true",
                        help="Just print fill-rate stats and exit.")
    args = parser.parse_args()

    if args.verify:
        verify(verbose=True)
        return

    log.info("=" * 60)
    log.info("backfill_distribution  dry_run=%s  mc=%s", args.dry_run, args.mc)
    log.info("=" * 60)

    backfill_spot_and_zone(dry_run=args.dry_run)

    if args.mc:
        backfill_mc(dry_run=args.dry_run, n_sims=args.n_sims)

    if not args.dry_run:
        verify(verbose=True)


if __name__ == "__main__":
    main()
