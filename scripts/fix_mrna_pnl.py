#!/usr/bin/env python3
"""
One-shot fix for two MRNA Call Debit Spread trades closed 2026-08-19 that
recorded impossible P&L (mark fetched as ~$68.95 due to short-leg quote = $0,
giving pnl_per_share ~$66 on a $10-wide spread).

Corrects pnl_per_share, pnl_total, and pnl_pct_of_max to the true max-profit
ceiling for each trade.

Run once on Ubuntu:
    python -m scripts.fix_mrna_pnl
or directly:
    python scripts/fix_mrna_pnl.py
"""

import json
import shutil
from pathlib import Path
from datetime import datetime, timezone

DATA_FILE = Path(__file__).parent.parent / "data" / "paper_trades.json"


def _max_profit(trade: dict) -> float:
    """Width − entry debit for debit spreads."""
    width = trade.get("width", 0) or 0
    entry = trade.get("entry_credit") or trade.get("entry_mid") or 0
    return round(width - entry, 4)


def main():
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} not found")
        return

    # Backup before touching anything
    backup = DATA_FILE.with_suffix(f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    shutil.copy2(DATA_FILE, backup)
    print(f"Backup saved → {backup}")

    with open(DATA_FILE, encoding="utf-8") as f:
        trades = json.load(f)

    # Identify debit-spread trades with impossible pnl (>100% of max profit).
    # Matches any ticker/date — the >100% threshold is the definitive signal.
    THRESHOLD_PCT = 100.1   # anything above 100% of max is impossible for a debit spread
    patched = 0

    for t in trades:
        if t.get("structure") not in ("Call Debit Spread", "Put Debit Spread"):
            continue
        ex = t.get("exit")
        if not ex:
            continue
        pct = ex.get("pnl_pct_of_max")
        # Only catch impossible POSITIVE gains — negative pnl_pct (e.g. -200%) is valid
        # for debit spreads where full loss > max profit (ec > width/2).
        if pct is None or pct <= THRESHOLD_PCT:
            continue

        mp = _max_profit(t)
        if mp <= 0:
            print(f"  SKIP {t['id']}: cannot compute max_profit (width={t.get('width')}, entry={t.get('entry_credit')})")
            continue

        old_ps    = ex.get("pnl_per_share")
        old_total = ex.get("pnl_total")
        old_pct   = pct

        # Correct to true max profit (spread expired / closed at full width)
        ex["pnl_per_share"]  = round(mp, 4)
        ex["pnl_total"]      = round(mp * 100, 2)
        ex["pnl_pct_of_max"] = 100.0
        ex["win"]            = True
        # Backfill missing exit_date from exit ts
        if not t.get("exit_date") and ex.get("ts"):
            t["exit_date"] = ex["ts"][:10]
        # Annotate that this was a manual correction
        ex["_fix_note"] = (
            f"Corrected {datetime.now(timezone.utc).isoformat()}: "
            f"bad mark ~$68.95 (short-leg quote=$0) inflated pnl_per_share "
            f"from true ${mp:.3f} to ${old_ps}. "
            f"pnl_pct_of_max was {old_pct}%."
        )

        print(f"  FIXED {t['id']}")
        print(f"    pnl_per_share:  ${old_ps}  →  ${ex['pnl_per_share']}")
        print(f"    pnl_total:      ${old_total}  →  ${ex['pnl_total']}")
        print(f"    pnl_pct_of_max: {old_pct}%  →  {ex['pnl_pct_of_max']}%")
        patched += 1

    if patched == 0:
        print("No matching trades found — nothing changed.")
        return

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, default=str)

    # Summary
    closed = [t for t in trades if t.get("exit") and t["exit"].get("pnl_total") is not None]
    total_pnl = sum(t["exit"]["pnl_total"] for t in closed)
    print(f"\nPatched {patched} trade(s). File updated.")
    print(f"New total closed P&L: ${total_pnl:,.2f}")
    print("(Old total was $5,640.50 — inflated by bad MRNA quotes)")


if __name__ == "__main__":
    main()
