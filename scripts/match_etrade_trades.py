"""
Match E*TRADE transaction legs into completed trades with P&L.

Given the raw transaction list from get_transactions(), groups legs by
(underlying, call_put, strike, expiry) and matches OPEN legs to CLOSE/EXPIRE
legs to reconstruct complete trades. Reports net P&L per completed trade.

The open_action and close_action classification:
  OPEN  = "Bought To Open", "Sold Short"
  CLOSE = "Sold To Close", "Bought To Cover", "Option Expired"

Run standalone to see what's available for POP model labeling:
  python -m scripts.match_etrade_trades
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
import json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

OPEN_TYPES   = {"Bought To Open", "Sold Short"}
CLOSE_TYPES  = {"Sold To Close", "Bought To Cover", "Option Expired"}
SKIP_TYPES   = {"Transfer", "Dividend", "Qualified Dividend", "Margin Interest"}

_DIRECTION = {
    "Bought To Open":  "long",
    "Sold Short":      "short",
    "Sold To Close":   "close_long",
    "Bought To Cover": "close_short",
    "Option Expired":  "expire",
}


def _leg_key(t: dict) -> tuple:
    return (t["underlying"], t["call_put"], t["strike"], t["expiry"])


def match_trades(txns: list[dict]) -> list[dict]:
    """
    Returns list of completed trade dicts:
      underlying, structure (inferred), legs, open_date, close_date,
      net_pnl (sum of all amount fields), outcome (win/loss/breakeven)
    """
    # Group by leg key, sorted by transaction_date ascending
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in txns:
        if t.get("transaction_type") in SKIP_TYPES:
            continue
        if t.get("security_type") != "OPTN":
            continue
        key = _leg_key(t)
        by_key[key].append(t)
    for legs in by_key.values():
        legs.sort(key=lambda x: x.get("transaction_date") or "")

    # Now group multi-leg orders: transactions at the exact same datetime
    # belong to the same order. Collect order groups by timestamp.
    from itertools import groupby

    orders_by_ts: dict[str, list[dict]] = defaultdict(list)
    for t in txns:
        if t.get("transaction_type") in SKIP_TYPES:
            continue
        if t.get("security_type") != "OPTN":
            continue
        ts = t.get("transaction_date", "")
        orders_by_ts[ts].append(t)

    # Sort timestamps
    all_ts = sorted(orders_by_ts.keys())

    # Track open positions: key → list of open transactions still unmatched
    open_positions: dict[tuple, list[dict]] = defaultdict(list)
    completed: list[dict] = []

    for ts in all_ts:
        batch = orders_by_ts[ts]
        opens  = [t for t in batch if t["transaction_type"] in OPEN_TYPES]
        closes = [t for t in batch if t["transaction_type"] in CLOSE_TYPES]

        # Process closes first — match to existing open positions
        for t in closes:
            key = _leg_key(t)
            if open_positions[key]:
                open_t = open_positions[key].pop(0)
                completed.append(_make_trade([open_t, t]))
            else:
                # Close without matching open in window — record as partial
                completed.append(_make_trade([t], partial=True))

        # Register new opens
        for t in opens:
            key = _leg_key(t)
            open_positions[key].append(t)

    # Remaining open positions (no close seen) → still open
    open_trades = []
    for key, legs in open_positions.items():
        for t in legs:
            open_trades.append(t)

    return completed, open_trades


def _make_trade(legs: list[dict], partial: bool = False) -> dict:
    legs_sorted = sorted(legs, key=lambda x: x.get("transaction_date") or "")
    open_legs  = [l for l in legs_sorted if l["transaction_type"] in OPEN_TYPES]
    close_legs = [l for l in legs_sorted if l["transaction_type"] in CLOSE_TYPES]
    net_pnl    = sum(l["amount"] for l in legs_sorted)
    open_date  = open_legs[0]["transaction_date"][:10]  if open_legs  else None
    close_date = close_legs[-1]["transaction_date"][:10] if close_legs else None

    # Infer outcome
    if close_legs and all(c["transaction_type"] == "Option Expired" for c in close_legs):
        # All legs expired — P&L is whatever was received/paid at open
        net_pnl = sum(l["amount"] for l in open_legs)
        outcome = "win" if net_pnl > 0 else ("loss" if net_pnl < 0 else "breakeven")
    else:
        outcome = "win" if net_pnl > 0 else ("loss" if net_pnl < 0 else "breakeven")

    return {
        "underlying":  legs_sorted[0]["underlying"],
        "call_put":    legs_sorted[0]["call_put"],
        "strike":      legs_sorted[0]["strike"],
        "expiry":      legs_sorted[0]["expiry"],
        "open_date":   open_date,
        "close_date":  close_date,
        "legs":        legs_sorted,
        "net_pnl":     round(net_pnl, 2),
        "outcome":     outcome,
        "partial":     partial,
        "n_legs":      len(legs_sorted),
    }


def group_into_spreads(single_legs: list[dict]) -> list[dict]:
    """
    Group individual matched leg-trades that share the same open_date and
    underlying into multi-leg spread trades.
    """
    # Group by (underlying, open_date, close_date)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for leg in single_legs:
        gkey = (leg["underlying"], leg["open_date"], leg["close_date"])
        groups[gkey].append(leg)

    spreads = []
    for (underlying, open_date, close_date), legs in groups.items():
        net_pnl = sum(l["net_pnl"] for l in legs)
        outcome = "win" if net_pnl > 0 else ("loss" if net_pnl < 0 else "breakeven")
        all_strikes = sorted({l["strike"] for l in legs if l["strike"]})
        all_expiries = sorted({l["expiry"] for l in legs if l["expiry"]})
        is_partial = any(l["partial"] for l in legs)
        # Infer structure
        n_legs = sum(l["n_legs"] for l in legs)
        structure = _infer_structure(legs)
        spreads.append({
            "underlying": underlying,
            "open_date":  open_date,
            "close_date": close_date,
            "expiries":   all_expiries,
            "strikes":    all_strikes,
            "structure":  structure,
            "net_pnl":    round(net_pnl, 2),
            "outcome":    outcome,
            "partial":    is_partial,
            "legs":       legs,
        })
    return sorted(spreads, key=lambda s: s["open_date"] or "")


def _infer_structure(legs: list[dict]) -> str:
    calls = [l for l in legs if l["call_put"] == "CALL"]
    puts  = [l for l in legs if l["call_put"] == "PUT"]
    if calls and puts:
        expiries = {l["expiry"] for l in legs}
        return "Iron Condor" if len(expiries) == 1 else "Iron Condor (multi-expiry)"
    if calls:
        expiries = {l["expiry"] for l in legs}
        return "Call Spread" if len(expiries) == 1 else "Call Diagonal"
    if puts:
        expiries = {l["expiry"] for l in legs}
        return "Put Spread" if len(expiries) == 1 else "Put Diagonal"
    return "Unknown"


def print_summary(spreads: list[dict], open_trades: list[dict]):
    complete   = [s for s in spreads if not s["partial"]]
    partial    = [s for s in spreads if s["partial"]]
    wins       = [s for s in complete if s["outcome"] == "win"]
    losses     = [s for s in complete if s["outcome"] == "loss"]

    print(f"\n{'='*60}")
    print(f"  E*TRADE Transaction Trade Reconstruction")
    print(f"{'='*60}")
    print(f"  Complete trades: {len(complete)}  ({len(wins)} W / {len(losses)} L)")
    print(f"  Partial (open missing from window): {len(partial)}")
    print(f"  Still open (no close seen): {len(open_trades)}")
    win_rate = len(wins) / len(complete) * 100 if complete else 0
    print(f"  Win rate: {win_rate:.0f}%")
    total_pnl = sum(s["net_pnl"] for s in complete)
    print(f"  Net P&L (complete trades): ${total_pnl:+.2f}")
    print()

    print("  Complete Trades:")
    print(f"  {'Open':10} {'Close':10} {'Symbol':6} {'Structure':20} {'P&L':>8} {'W/L':4}")
    print(f"  {'-'*10} {'-'*10} {'-'*6} {'-'*20} {'-'*8} {'-'*4}")
    for s in complete:
        symbol    = s["underlying"]
        structure = s["structure"]
        open_d    = s["open_date"]  or "?"
        close_d   = s["close_date"] or "?"
        pnl       = f"${s['net_pnl']:+.2f}"
        wl        = "W" if s["outcome"] == "win" else "L"
        print(f"  {open_d:10} {close_d:10} {symbol:6} {structure:20} {pnl:>8} {wl}")

    if partial:
        print()
        print("  Partial (close only — open outside window):")
        for s in partial:
            print(f"  {s['underlying']:6} {s['structure']:20} close={s['close_date']}")

    if open_trades:
        print()
        print(f"  Still open ({len(open_trades)} legs):")
        seen = set()
        for t in open_trades:
            key = f"{t['underlying']} {t['display_symbol']}"
            if key not in seen:
                seen.add(key)
                print(f"    {t['transaction_date'][:10]}  {key}")


if __name__ == "__main__":
    # Load from file if provided, else try to import live
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text())
        txns = data.get("transactions", data) if isinstance(data, dict) else data
    else:
        sys.path.insert(0, str(_ROOT))
        try:
            from scripts import etrade_client as etrade
            txns = etrade.get_transactions()
            if not txns:
                print("Not authenticated or no transactions.")
                sys.exit(1)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)

    single_legs, open_trades = match_trades(txns)
    spreads = group_into_spreads(single_legs)
    print_summary(spreads, open_trades)
