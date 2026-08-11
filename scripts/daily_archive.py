#!/usr/bin/env python
"""
Standalone entry point for cron — daily archive (T0-A/B/D). Runs at 4:30 PM ET Mon-Fri.
"""
import sys
import json
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "daily_archive.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.data_archive import run_daily_archive
from scripts.cron_logger import record_run, acquire_lock, release_lock
from datetime import datetime, timezone

t0 = time.time()
started_at = datetime.now(timezone.utc).isoformat()
acquire_lock("daily_archive")
result, error = None, None
try:
    result = run_daily_archive()
    print(json.dumps(result, indent=2, default=str))
except Exception as e:
    error = str(e)
    logging.error(f"daily_archive failed: {e}")
finally:
    release_lock("daily_archive")

record_run("daily_archive", result, time.time() - t0, error, started_at=started_at)
sys.exit(1 if error else 0)
