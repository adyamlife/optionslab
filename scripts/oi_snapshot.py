#!/usr/bin/env python
"""
Standalone entry point for cron — OI snapshot.
Usage: oi_snapshot.py open   (9:45 AM ET)
       oi_snapshot.py close  (4:00 PM ET)
"""
import sys
import json
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

time_of_day = sys.argv[1] if len(sys.argv) > 1 else "close"
if time_of_day not in ("open", "close"):
    print(f"Usage: oi_snapshot.py open|close", file=sys.stderr)
    sys.exit(1)

_LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / f"oi_snapshot_{time_of_day}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.data_archive import archive_oi_snapshot
from scripts.cron_logger import record_run, acquire_lock, release_lock

from datetime import datetime, timezone
t0 = time.time()
started_at = datetime.now(timezone.utc).isoformat()
_lock_key = f"oi_{time_of_day}"
acquire_lock(_lock_key)
result, error = None, None
try:
    result = archive_oi_snapshot(time_of_day)
    print(json.dumps(result, indent=2, default=str))
except Exception as e:
    error = str(e)
    logging.error(f"oi_snapshot({time_of_day}) failed: {e}")
finally:
    release_lock(_lock_key)

record_run(_lock_key, result, time.time() - t0, error, started_at=started_at)
sys.exit(1 if error else 0)
