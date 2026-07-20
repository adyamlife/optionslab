"""
Post-hoc analysis of closed paper trades and E*TRADE history.

Classifies each losing trade into one of three failure modes:
  - iv_crush:    IV collapsed after entry (short-vol positions hurt by falling premium)
  - gap_move:    Underlying made a large directional move that breached the strike
  - theta_decay: Trade expired worthless / small loss from slow theta erosion
  - winner:      Trade was profitable (not a failure)

Uses:
  - data/paper_trades.json   (closed DuckDB paper trades with snapshots)
  - data/etrade_labeled_trades.jsonl (E*TRADE history with exit data)

Run:  python -m scripts.analyze_trade_failures
      python -m scripts.analyze_trade_failures --source etrade
      python -m scripts.analyze_trade_failures --source paper
      python -m scripts.analyze_trade_failures --min-rows 10
"""
import json
import argparse
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# Thresholds (all configurable via CLI flags)
_GAP_MOVE_PCT   = 0.05   # underlying moved ≥5% from entry to exit → gap/directional
_IV_CRUSH_RATIO = 0.75   # IV at exit / IV at entry ≤ this → IV crush
_THETA_DEFAULT  = "theta_decay"  # catch-all when no other signal fires


def _classify(trade: dict, iv_crush_ratio: float, gap_move_pct: float) -> str:
    """Return failure mode label for one closed trade."""
    exit_info = trade.get("exit") or {}
    win = exit_info.get("win", False)
    if win or (trade.get("status") or "").startswith("closed_target"):
        return "winner"

    # Directional gap: compare spot_at_entry vs ul_price at exit
    spot_entry = trade.get("spot_at_entry")
    spot_exit  = exit_info.get("ul_price")
    if spot_entry and spot_exit and spot_entry > 0:
        move_pct = abs(spot_exit - spot_entry) / spot_entry
        if move_pct >= gap_move_pct:
            return "gap_move"

    # IV crush: compare IV in first snapshot vs last snapshot
    snaps = trade.get("snapshots") or []
    iv_series = [s.get("atm_iv") or s.get("iv") for s in snaps if s.get("atm_iv") or s.get("iv")]
    if len(iv_series) >= 2:
        iv_entry = iv_series[0]
        iv_exit  = iv_series[-1]
        if iv_entry and iv_exit and iv_entry > 0:
            if iv_exit / iv_entry <= iv_crush_ratio:
                return "iv_crush"

    return _THETA_DEFAULT


def _load_paper_trades() -> list:
    path = _ROOT / "data" / "paper_trades.json"
    if not path.exists():
        return []
    with open(path) as f:
        trades = json.load(f)
    return [t for t in trades if t.get("status") not in ("open",) and t.get("exit")]


def _load_etrade_trades() -> list:
    path = _ROOT / "data" / "etrade_labeled_trades.jsonl"
    if not path.exists():
        return []
    trades = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            outcome = r.get("outcome") or {}
            # Normalise into paper-trade shape for the classifier
            trades.append({
                "id":            r.get("trade_id", "?"),
                "ticker":        r.get("ticker"),
                "structure":     (r.get("candidate") or {}).get("structure") or r.get("structure"),
                "spot_at_entry": r.get("spot"),
                "status":        "expired_profit" if outcome.get("win") else "expired_loss",
                "snapshots":     [],   # no intra-trade snapshots in E*TRADE history
                "exit": {
                    "win":       outcome.get("win"),
                    "ul_price":  None,
                    "pnl_total": outcome.get("net_pnl"),
                },
                "_source": "etrade",
            })
    return trades


def run(source: str = "both", min_rows: int = 5,
        iv_crush_ratio: float = _IV_CRUSH_RATIO,
        gap_move_pct:   float = _GAP_MOVE_PCT) -> dict:

    paper  = _load_paper_trades()  if source in ("both", "paper")  else []
    etrade = _load_etrade_trades() if source in ("both", "etrade") else []
    all_trades = paper + etrade

    if len(all_trades) < min_rows:
        return {
            "ok": False,
            "error": f"Only {len(all_trades)} closed trades — need at least {min_rows} to analyse.",
            "total": len(all_trades),
        }

    labels    = [_classify(t, iv_crush_ratio, gap_move_pct) for t in all_trades]
    counts    = Counter(labels)
    total     = len(labels)
    losses    = total - counts["winner"]

    by_structure: dict = {}
    for t, label in zip(all_trades, labels):
        struct = t.get("structure") or "Unknown"
        if struct not in by_structure:
            by_structure[struct] = Counter()
        by_structure[struct][label] += 1

    return {
        "ok":           True,
        "total_trades": total,
        "winners":      counts["winner"],
        "losers":       losses,
        "win_rate":     round(counts["winner"] / total, 3) if total else None,
        "failure_breakdown": {
            "gap_move":    counts["gap_move"],
            "iv_crush":    counts["iv_crush"],
            "theta_decay": counts["theta_decay"],
        },
        "failure_pct": {
            "gap_move":    round(counts["gap_move"]    / losses, 3) if losses else None,
            "iv_crush":    round(counts["iv_crush"]    / losses, 3) if losses else None,
            "theta_decay": round(counts["theta_decay"] / losses, 3) if losses else None,
        },
        "by_structure": {k: dict(v) for k, v in sorted(by_structure.items())},
        "sources": {"paper": len(paper), "etrade": len(etrade)},
    }


def _print_report(r: dict) -> None:
    if not r.get("ok"):
        print(f"NOT READY: {r.get('error')}")
        return

    print(f"\n{'='*55}")
    print(f"  Trade Failure Analysis")
    print(f"{'='*55}")
    print(f"  Total closed trades : {r['total_trades']}  "
          f"(paper: {r['sources']['paper']}, etrade: {r['sources']['etrade']})")
    print(f"  Winners             : {r['winners']}  ({r['win_rate']:.1%})")
    print(f"  Losers              : {r['losers']}")

    if r['losers'] > 0:
        fb = r['failure_breakdown']
        fp = r['failure_pct']
        print(f"\n  Failure modes (of {r['losers']} losses):")
        print(f"    Directional gap move : {fb['gap_move']:>3}  ({fp['gap_move']:.0%})")
        print(f"    IV crush             : {fb['iv_crush']:>3}  ({fp['iv_crush']:.0%})")
        print(f"    Theta / slow decay   : {fb['theta_decay']:>3}  ({fp['theta_decay']:.0%})")

    print(f"\n  By structure:")
    for struct, counts in r['by_structure'].items():
        total_s = sum(counts.values())
        wins_s  = counts.get('winner', 0)
        print(f"    {struct:<30}  {total_s:>3} trades  "
              f"{wins_s}/{total_s} wins  "
              f"  gap={counts.get('gap_move',0)}  "
              f"iv={counts.get('iv_crush',0)}  "
              f"theta={counts.get('theta_decay',0)}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["both", "paper", "etrade"], default="both")
    parser.add_argument("--min-rows",       type=int,   default=5)
    parser.add_argument("--iv-crush-ratio", type=float, default=_IV_CRUSH_RATIO,
                        help="IV at exit / IV at entry ≤ this → classified as IV crush")
    parser.add_argument("--gap-move-pct",   type=float, default=_GAP_MOVE_PCT,
                        help="Underlying move ≥ this fraction → classified as gap move")
    args = parser.parse_args()

    result = run(
        source=args.source,
        min_rows=args.min_rows,
        iv_crush_ratio=args.iv_crush_ratio,
        gap_move_pct=args.gap_move_pct,
    )
    _print_report(result)
    sys.exit(0 if result.get("ok") else 1)
