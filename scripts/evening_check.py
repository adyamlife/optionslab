#!/usr/bin/env python
"""
Standalone entry point for Windows Task Scheduler — runs at 5:00 PM EDT.
Schedule: daily Mon-Fri.
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
        logging.FileHandler(Path(__file__).parent.parent / "data" / "evening_check.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.paper_trade_engine import run_evening_check

result = run_evening_check()
print(json.dumps(result, indent=2, default=str))
sys.exit(0)
