"""
Merge two paper_trades.json files into one.

Usage:
    python -m scripts.merge_paper_trades <other_trades.json> [--output <path>]

Typical workflow — sync laptop trades into Ubuntu:

  # 1. On laptop: copy this file to Ubuntu
  scp "C:/Project Y/data/paper_trades.json" user@server:/tmp/laptop_trades.json

  # 2. On Ubuntu: merge (Ubuntu file wins on duplicate IDs)
  python -m scripts.merge_paper_trades /tmp/laptop_trades.json

  # Done — data/paper_trades.json on Ubuntu now has all trades.

Merge rules:
  - Union of both files by trade id
  - On duplicate id: the file that is NOT passed as <other> wins
    (i.e. the local data/paper_trades.json takes priority — it has
    fresher snapshots/marks for shared trades)
  - Result is sorted by entered_at ascending
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LOCAL = _ROOT / "data" / "paper_trades.json"


def load(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        trades = json.loads(path.read_text(encoding="utf-8"))
        return {t["id"]: t for t in trades}
    except Exception as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        sys.exit(1)


def merge(local: dict, other: dict) -> list[dict]:
    """Union of both dicts; local wins on duplicate id."""
    merged = {**other, **local}   # local overwrites other on conflict
    return sorted(merged.values(), key=lambda t: t.get("entered_at", ""))


def main():
    ap = argparse.ArgumentParser(description="Merge two paper_trades.json files")
    ap.add_argument("other", help="Path to the other paper_trades.json (e.g. /tmp/laptop_trades.json)")
    ap.add_argument("--output", default=None, help="Output path (default: overwrites local data/paper_trades.json)")
    ap.add_argument("--dry-run", action="store_true", help="Print summary without writing")
    args = ap.parse_args()

    other_path = Path(args.other)
    out_path   = Path(args.output) if args.output else _LOCAL

    local = load(_LOCAL)
    other = load(other_path)

    local_ids = set(local)
    other_ids = set(other)
    only_local = local_ids - other_ids
    only_other = other_ids - local_ids
    both       = local_ids & other_ids

    print(f"Local  ({_LOCAL.name}): {len(local)} trades")
    print(f"Other  ({other_path.name}): {len(other)} trades")
    print(f"  Only in local : {len(only_local)}")
    print(f"  Only in other : {len(only_other)}")
    print(f"  In both (local wins): {len(both)}")

    if only_other:
        print("\nAdding from other:")
        for tid in sorted(only_other):
            t = other[tid]
            print(f"  + {t['id']}  {t['ticker']}  {t['structure']}  {t['status']}")

    merged = merge(local, other)
    print(f"\nMerged total: {len(merged)} trades")

    if args.dry_run:
        print("[dry-run] Not writing.")
        return

    class _Enc(json.JSONEncoder):
        def default(self, obj):
            t = type(obj).__name__
            if "int" in t:   return int(obj)
            if "float" in t: return float(obj)
            if "bool" in t:  return bool(obj)
            return str(obj)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(merged, indent=2, cls=_Enc), encoding="utf-8")
    print(f"Written → {out_path}")


if __name__ == "__main__":
    main()
