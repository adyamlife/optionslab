"""
One-off diagnostic: breakdown of the 245 remaining unlabelable rows.
Run on Ubuntu: python -c "exec(open('scripts/audit_unlabelable.py').read())"
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import Counter, defaultdict
import duckdb
_LOCAL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml_training_live.duckdb")
connect = lambda read_only=False: duckdb.connect(_LOCAL_DB, read_only=read_only)

with connect(read_only=True) as con:
    # First: show all columns available in the table
    cols = con.execute("PRAGMA table_info(training_snapshots)").fetchall()
    print("=== Table columns ===")
    for c in cols:
        print(f"  {c}")
    print()

    rows = con.execute("""
        SELECT candidate, outcome, collected_at, spot, ticker
        FROM training_snapshots
        WHERE labeled = TRUE
          AND json_extract_string(outcome, '$.unlabelable') = 'true'
        ORDER BY collected_at DESC
    """).fetchall()

print(f"Total unlabelable rows: {len(rows)}\n")

# Date range summary
dates = [r[2] for r in rows if r[2] is not None]
if dates:
    print(f"Snapshot date range: {min(dates)}  to  {max(dates)}")
    by_date = Counter(str(d)[:10] for d in dates)
    print("By date:")
    for d, n in sorted(by_date.items()):
        print(f"  {d}: {n} rows")
    print()

by_structure  = Counter()
by_reason     = Counter()
sample_by_str = defaultdict(list)

for (cand_raw, out_raw, snap_date, spot, ticker) in rows:
    cand = json.loads(cand_raw) if isinstance(cand_raw, str) else (cand_raw or {})
    out  = json.loads(out_raw)  if isinstance(out_raw,  str) else (out_raw  or {})

    structure = cand.get("structure", "<missing>")
    reason    = out.get("reason",    "<no reason>")

    by_structure[structure] += 1
    by_reason[reason]       += 1

    if len(sample_by_str[structure]) < 1:
        sample_by_str[structure].append({
            "reason":         reason,
            "snap_date":      str(snap_date),
            "ticker":         ticker,
            "spot":           spot,
            "full_candidate": cand,
        })

print("=== By structure ===")
for s, n in by_structure.most_common():
    print(f"  {n:4d}  {s}")

print("\n=== By reason ===")
for r, n in by_reason.most_common():
    print(f"  {n:4d}  {r}")

print("\n=== Samples (up to 2 per structure — FULL candidate JSON) ===")
for structure, samples in sorted(sample_by_str.items()):
    print(f"\n--- {structure} ---")
    for s in samples:
        print(f"  date={s['snap_date']}  ticker={s['ticker']}  spot={s['spot']}  reason={s['reason']}")
        import pprint
        pprint.pprint(s["full_candidate"], width=120)
