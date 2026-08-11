#!/usr/bin/env python
"""
Standalone entry point for cron — runs at 10:00 AM EDT Mon-Fri.
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
        logging.FileHandler(_LOG_DIR / "morning_scan.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.paper_trade_engine import run_morning_scan
from scripts.cron_logger import record_run, acquire_lock, release_lock

from datetime import datetime, timezone
t0 = time.time()
started_at = datetime.now(timezone.utc).isoformat()
acquire_lock("morning_scan")
result, error = None, None
try:
    result = run_morning_scan()
    print(json.dumps(result, indent=2, default=str))
except Exception as e:
    error = str(e)
    logging.error(f"morning_scan failed: {e}")
finally:
    release_lock("morning_scan")

record_run("morning_scan", result, time.time() - t0, error, started_at=started_at)
sys.exit(1 if error else 0)
