#!/usr/bin/env python
"""
Standalone entry point for the 2 PM EDT afternoon paper-trade scan.
Schedule: daily Mon-Fri at 14:00 EDT on the Ubuntu server.

Mirrors morning_scan.py but passes scan_time="afternoon" so trade IDs
use the PM prefix and the scan_time field is set for ML feature use.
"""
import sys
import json
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent.parent / "data" / "afternoon_scan.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.paper_trade_engine import run_morning_scan

result = run_morning_scan(scan_time="afternoon")
print(json.dumps(result, indent=2, default=str))
sys.exit(0)
