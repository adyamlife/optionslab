#!/usr/bin/env python
"""
Standalone entry point for cron — training data collect snapshot.
Runs every 30 min during market hours: */30 8-16 * * 1-5
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
        logging.FileHandler(_LOG_DIR / "data_collect.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.training_data_collector import collect_snapshots
from scripts.cron_logger import record_run, acquire_lock, release_lock

from datetime import datetime, timezone
t0 = time.time()
started_at = datetime.now(timezone.utc).isoformat()
acquire_lock("training_collect")
result, error = None, None
try:
    result = collect_snapshots()
    print(json.dumps(result, indent=2, default=str))
except Exception as e:
    error = str(e)
    logging.error(f"data_collect failed: {e}")
finally:
    release_lock("training_collect")

record_run("training_collect", result, time.time() - t0, error, started_at=started_at)
sys.exit(1 if error else 0)
