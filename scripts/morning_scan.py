#!/usr/bin/env python
"""
Standalone entry point for Windows Task Scheduler — runs at 10:00 AM EDT.
Schedule: daily Mon-Fri.
"""
import sys
import json
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent.parent / "data" / "logs" / "morning_scan.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.paper_trade_engine import run_morning_scan

result = run_morning_scan()
print(json.dumps(result, indent=2, default=str))
sys.exit(0)
