"""
One-time migration: regime_training.csv → data/ml_training.duckdb

Run once:  python -m scripts.migrate_csv_to_duckdb

Safe to re-run — checks for existing data first and refuses to double-import.
The original CSV is left untouched as a backup.
"""
import sys
from pathlib import Path

import pandas as pd

_ROOT     = Path(__file__).resolve().parent.parent
_CSV_PATH = _ROOT / "data" / "regime_training.csv"


def migrate():
    from scripts.db import connect, TABLE, table_exists, row_count

    if not _CSV_PATH.exists():
        print(f"ERROR: {_CSV_PATH} not found — nothing to migrate.")
        sys.exit(1)

    print(f"Reading {_CSV_PATH} …")
    df = pd.read_csv(_CSV_PATH)
    print(f"  {len(df):,} rows, {len(df.columns)} columns")

    with connect() as con:
        if table_exists():
            existing = row_count()
            if existing > 0:
                print(f"\nTable '{TABLE}' already has {existing:,} rows.")
                print("Migration already done — exiting. Delete the DuckDB file to redo.")
                sys.exit(0)
            else:
                # Table exists but empty — safe to populate
                con.execute(f"DROP TABLE IF EXISTS {TABLE}")
                con.commit()

        print(f"Creating table '{TABLE}' and importing data …")
        con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM df")
        con.execute(
            f"CREATE INDEX IF NOT EXISTS idx_ticker_date "
            f"ON {TABLE} (ticker, date)"
        )
        con.commit()

    final_count = row_count()
    print(f"\nMigration complete.")
    print(f"  Rows in DuckDB: {final_count:,}")
    print(f"  DB file: {_ROOT / 'data' / 'ml_training.duckdb'}")
    print(f"\nOriginal CSV kept at: {_CSV_PATH}")
    print("Once you've verified training works, you can delete the CSV.")


if __name__ == "__main__":
    migrate()
