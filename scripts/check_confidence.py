import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.db import load_all_snapshots
from collections import defaultdict

records = load_all_snapshots()
labeled_rows = [r for r in records if r.get("labeled")]

# Group confidence by settlement_source
by_src = defaultdict(list)
for r in labeled_rows:
    o = r.get("outcome") or {}
    src = o.get("settlement_source")
    conf = o.get("label_confidence")
    if src is not None:
        by_src[src].append(conf)

print(f"{'source':<30} {'cnt':>5} {'avg':>6} {'min':>6} {'max':>6}")
print("-" * 55)
for src, confs in sorted(by_src.items(), key=lambda x: -len(x[1])):
    valid = [c for c in confs if c is not None]
    avg = round(sum(valid)/len(valid), 3) if valid else None
    mn = min(valid) if valid else None
    mx = max(valid) if valid else None
    print(f"{src:<30} {len(confs):>5} {str(avg):>6} {str(mn):>6} {str(mx):>6}")

# Train mask summary
print()
to_ct = sum(1 for r in labeled_rows if (r.get("outcome") or {}).get("train_outcome") is True)
td_ct = sum(1 for r in labeled_rows if (r.get("outcome") or {}).get("train_direction") is True)
print(f"train_outcome=True: {to_ct}")
print(f"train_direction=True: {td_ct}")
