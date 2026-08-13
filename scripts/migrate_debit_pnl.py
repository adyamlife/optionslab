"""
Migration: Fix swapped max_profit/max_loss and recompute closed-trade P&L
for Call Debit Spread and Put Debit Spread trades entered with live pricing.

Root cause: old paper_trade_engine stored max_profit=ec (debit) and
max_loss=width-ec (max profit) for live-quoted debit spreads — exactly
backwards.  entry_credit and width were always stored correctly.

What this script fixes:
  - max_profit  → width - entry_credit   (correct for all CDS/PDS)
  - max_loss    → entry_credit           (correct for all CDS/PDS)
  - closed trades: recomputes pnl_per_share / pnl_total / win from
    exit.spread_val and entry_credit (spread_val was always stored correctly)

Open trades: snapshots are left as-is; the next morning scan will append
a fresh snapshot computed with the fixed code, making latest_unrealized
correct automatically.

Run on Ubuntu AFTER deploying the fixed paper_trade_engine.py:
    python scripts/migrate_debit_pnl.py [--dry-run]
"""

import json, sys, pathlib, shutil, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
TRADES_PATH = ROOT / "data" / "paper_trades.json"
DEBIT_STRUCTS = {"Call Debit Spread", "Put Debit Spread"}

dry_run = "--dry-run" in sys.argv


def load():
    with open(TRADES_PATH) as f:
        return json.load(f)


def save(trades):
    backup = TRADES_PATH.with_suffix(
        f".json.bak_{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}"
    )
    shutil.copy2(TRADES_PATH, backup)
    print(f"Backup written -> {backup}")
    with open(TRADES_PATH, "w") as f:
        json.dump(trades, f, indent=2)
    print(f"Saved {len(trades)} trades -> {TRADES_PATH}")


def migrate(trades):
    changed = 0
    for t in trades:
        if t.get("structure") not in DEBIT_STRUCTS:
            continue

        ec     = t.get("entry_credit") or 0
        width  = t.get("width") or 0

        if ec <= 0 or width <= 0:
            print(f"  SKIP {t['id']}: ec={ec} width={width} (zero/missing)")
            continue

        correct_max_profit = round(width - ec, 4)
        correct_max_loss   = round(ec, 4)

        old_mp = t.get("max_profit")
        old_ml = t.get("max_loss")

        mp_changed = abs((old_mp or 0) - correct_max_profit) > 0.001
        ml_changed = abs((old_ml or 0) - correct_max_loss)   > 0.001

        status = t.get("status", "open")
        exit_  = t.get("exit") or {}
        spread_val = exit_.get("spread_val")

        # Recompute closed-trade P&L if we have a spread_val
        pnl_changed = False
        new_pnl_ps = new_pnl_tot = new_win = None
        if status != "open" and spread_val is not None:
            new_pnl_ps  = round(float(spread_val) - ec, 4)
            new_pnl_tot = round(new_pnl_ps * 100, 2)
            new_win     = new_pnl_ps > 0
            old_pnl_ps  = exit_.get("pnl_per_share")
            if old_pnl_ps is None or abs(old_pnl_ps - new_pnl_ps) > 0.001:
                pnl_changed = True

        if not mp_changed and not ml_changed and not pnl_changed:
            continue  # already correct, skip

        fill = t.get("fill_source", "?")
        print(
            f"  {'DRY ' if dry_run else ''}FIX {t['id']} [{status}] fill={fill}"
            f"\n    max_profit: {old_mp} -> {correct_max_profit}"
            f"\n    max_loss:   {old_ml} -> {correct_max_loss}"
            + (f"\n    pnl_ps:     {exit_.get('pnl_per_share')} -> {new_pnl_ps}"
               f"  pnl_tot: {exit_.get('pnl_total')} -> {new_pnl_tot}" if pnl_changed else "")
        )

        if not dry_run:
            t["max_profit"] = correct_max_profit
            t["max_loss"]   = correct_max_loss
            if pnl_changed:
                t["exit"]["pnl_per_share"] = new_pnl_ps
                t["exit"]["pnl_total"]     = new_pnl_tot
                t["exit"]["win"]           = new_win

        changed += 1

    return changed


def main():
    trades = load()
    print(f"Loaded {len(trades)} trades from {TRADES_PATH}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE (will write)'}\n")

    changed = migrate(trades)

    print(f"\n{'Would fix' if dry_run else 'Fixed'} {changed} trade(s).")

    if not dry_run and changed > 0:
        save(trades)
    elif dry_run:
        print("Re-run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
