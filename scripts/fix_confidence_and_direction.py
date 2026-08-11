"""
One-off correction for the 778 labeled rows where:
  1. label_confidence is wrong (~0.44 instead of 1.0) because settlement_source="expiry_settlement"
     fell through the inline else-branch in label_rejected_candidates().
  2. direction_correct was computed from forward_5d proxy instead of exact settlement.

Runs in-process; safe to re-run (idempotent if confidence is already 1.0).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.db import load_all_snapshots, update_snapshot_labels, connect as _dbconnect
from scripts import training_data_collector as tdc
from datetime import date

records = load_all_snapshots()

fixed_conf = 0
fixed_dir  = 0
skipped    = 0
changed    = []

with _dbconnect(read_only=True) as con:
    for r in records:
        o = r.get("outcome") or {}
        if o.get("settlement_source") != "expiry_settlement":
            continue

        conf = o.get("label_confidence")
        needs_conf_fix = conf is not None and abs(float(conf) - 1.0) > 1e-9

        # Try to recompute direction from exact settlement
        ticker = r.get("ticker") or ""
        expiry_raw = r.get("expiry") or ""
        expiry_str = str(expiry_raw)[:10] if expiry_raw else ""
        spot_raw = r.get("spot")
        candidate = r.get("candidate") or {}
        structure = candidate.get("structure", "")

        direction_fn = tdc._DIRECTION_WIN_MAP.get(structure)
        exact_dir = None

        if expiry_str and spot_raw and direction_fn:
            try:
                from scripts.db import lookup_expiry_settlement
                s_t, _ = lookup_expiry_settlement(con, ticker, expiry_str)
                if s_t is not None:
                    exact_fwd = float(s_t) / float(spot_raw) - 1.0
                    try:
                        em = float(candidate.get("expected_move") or candidate.get("expected_move_pct") or 0)
                    except (TypeError, ValueError):
                        em = 0.0
                    exact_dir = bool(direction_fn(exact_fwd, em))
            except Exception as e:
                pass

        needs_dir_fix = exact_dir is not None and o.get("direction_correct") != exact_dir

        if not needs_conf_fix and not needs_dir_fix:
            skipped += 1
            continue

        if needs_conf_fix:
            o["label_confidence"] = 1.0
            fixed_conf += 1

        if needs_dir_fix:
            o["direction_correct"] = exact_dir
            fixed_dir += 1

        r["outcome"] = o
        changed.append(r)

print(f"Fixed confidence: {fixed_conf}")
print(f"Fixed direction : {fixed_dir}")
print(f"Skipped (ok)    : {skipped}")
print(f"Writing {len(changed)} rows...")

update_snapshot_labels(changed)
print("Done.")
