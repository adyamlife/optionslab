"""
One-time migration: training_snapshots.jsonl + option_chain_snapshots.jsonl → DuckDB

Run once:  python -m scripts.migrate_jsonl_to_duckdb

Safe to re-run — skips if data already exists. Original JSONL files left intact.
"""
import json
import sys
from pathlib import Path

import pandas as pd

_ROOT          = Path(__file__).resolve().parent.parent
_SNAPSHOT_FILE = _ROOT / "data" / "training_snapshots.jsonl"
_CHAIN_FILE    = _ROOT / "data" / "option_chain_snapshots.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def migrate():
    from scripts.db import ensure_snapshot_tables, connect, read_df, SNAPSHOTS_TABLE, CHAIN_TABLE

    ensure_snapshot_tables()

    # ── training_snapshots ────────────────────────────────────────────────────
    existing_snaps = read_df(f"SELECT count(*) AS n FROM {SNAPSHOTS_TABLE}").iloc[0]["n"]
    if existing_snaps > 0:
        print(f"training_snapshots already has {existing_snaps:,} rows — skipping.")
    else:
        records = _read_jsonl(_SNAPSHOT_FILE)
        print(f"Migrating {len(records):,} training snapshots …")
        if records:
            for r in records:
                r["candidate"]      = json.dumps(r.get("candidate"))
                r["news_headlines"] = json.dumps(r.get("news_headlines") or [])
                r["outcome"]        = json.dumps(r.get("outcome"))
            df = pd.DataFrame(records)
            if "snapshot_id" not in df.columns:
                df["snapshot_id"] = [f"snap-{i}" for i in range(len(df))]
            with connect() as con:
                con.execute(f"INSERT OR REPLACE INTO {SNAPSHOTS_TABLE} BY NAME SELECT * FROM df")
                con.commit()
            print(f"  Done — {len(df):,} rows inserted.")
        else:
            print("  No JSONL file found — nothing to migrate.")

    # ── option_chain_snapshots ────────────────────────────────────────────────
    existing_chains = read_df(f"SELECT count(*) AS n FROM {CHAIN_TABLE}").iloc[0]["n"]
    if existing_chains > 0:
        print(f"option_chain_snapshots already has {existing_chains:,} rows — skipping.")
    else:
        chains = _read_jsonl(_CHAIN_FILE)
        print(f"Migrating {len(chains):,} chain snapshots …")
        if chains:
            for r in chains:
                r["strikes"]          = json.dumps(r.get("strikes") or [])
                r["is_position_legs"] = bool(r.get("is_position_legs", False))
            df = pd.DataFrame(chains)
            with connect() as con:
                con.execute(f"INSERT INTO {CHAIN_TABLE} BY NAME SELECT * FROM df")
                con.commit()
            print(f"  Done — {len(df):,} rows inserted.")
        else:
            print("  No JSONL file found — nothing to migrate.")

    print("\nMigration complete.")
    print("Original JSONL files kept — delete manually once verified:")
    print(f"  {_SNAPSHOT_FILE}")
    print(f"  {_CHAIN_FILE}")


if __name__ == "__main__":
    migrate()
