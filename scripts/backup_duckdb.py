#!/usr/bin/env python
"""
Nightly DuckDB backup — keeps last 7 days.
Runs via cron: 0 2 * * * (2:00 AM daily)
"""
import shutil
from datetime import datetime, timezone
from pathlib import Path

DB_SRC   = Path("/opt/optionlab/data/ml_training.duckdb")
BACKUP_DIR = Path("/opt/optionlab/data/backups/duckdb")
KEEP_DAYS  = 7

BACKUP_DIR.mkdir(parents=True, exist_ok=True)

date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
dest = BACKUP_DIR / f"ml_training_{date_str}.duckdb"

shutil.copy2(DB_SRC, dest)
print(f"Backed up to {dest} ({dest.stat().st_size // 1024 // 1024} MB)")

# Remove backups older than KEEP_DAYS
backups = sorted(BACKUP_DIR.glob("ml_training_*.duckdb"))
for old in backups[:-KEEP_DAYS]:
    old.unlink()
    print(f"Removed old backup: {old.name}")