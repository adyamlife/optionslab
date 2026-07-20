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

_LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent.parent / "data" / "logs" / "evening_check.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

from scripts.paper_trade_engine import run_evening_check

result = run_evening_check()
print(json.dumps(result, indent=2, default=str))

if result.get("newly_labeled", 0) > 0:
    logging.info(
        "[POP] %d trade(s) newly labeled — triggering POP model retrain.",
        result["newly_labeled"],
    )
    try:
        from pathlib import Path
        from scripts.train_pop_model import train as _pop_train
        _etrade = Path(__file__).parent.parent / "data" / "etrade_labeled_trades.jsonl"
        _kwargs = {"extra_data_path": _etrade if _etrade.exists() else None, "etrade_only": True}
        pop_result = _pop_train(**_kwargs)
        if pop_result.get("ok"):
            logging.info(
                "[POP] Retrain complete — AUC=%.4f  acc=%.4f  threshold=%.2f  rows=%d",
                pop_result.get("auc") or 0,
                pop_result.get("accuracy") or 0,
                pop_result.get("optimal_threshold") or 0.5,
                pop_result.get("train_rows") or 0,
            )
        else:
            logging.warning("[POP] Retrain skipped: %s", pop_result.get("error"))
    except Exception as _e:
        logging.error("[POP] Retrain failed: %s", _e)

    # Run failure analysis whenever new labels land so the report stays current.
    try:
        from scripts.analyze_trade_failures import run as _failure_run
        _fr = _failure_run(source="both")
        if _fr.get("ok"):
            logging.info(
                "[FAILURES] %d trades  win=%.1f%%  gap=%d  iv_crush=%d  theta=%d",
                _fr["total_trades"],
                (_fr["win_rate"] or 0) * 100,
                _fr["failure_breakdown"]["gap_move"],
                _fr["failure_breakdown"]["iv_crush"],
                _fr["failure_breakdown"]["theta_decay"],
            )
            # Log per-structure summary for structures with at least 5 trades
            for struct, counts in _fr["by_structure"].items():
                total_s = sum(counts.values())
                if total_s >= 5:
                    wins_s = counts.get("winner", 0)
                    logging.info(
                        "[FAILURES]   %-30s  %d trades  %.0f%% win  gap=%d theta=%d",
                        struct, total_s, 100 * wins_s / total_s,
                        counts.get("gap_move", 0), counts.get("theta_decay", 0),
                    )
        else:
            logging.warning("[FAILURES] Analysis skipped: %s", _fr.get("error"))
    except Exception as _e:
        logging.error("[FAILURES] Analysis failed: %s", _e)
else:
    logging.info("[POP] No new labels tonight — skipping retrain and failure analysis.")

sys.exit(0)
