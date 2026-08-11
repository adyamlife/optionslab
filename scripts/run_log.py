"""
Minimal helper: append a run-history entry to data/scheduler_runs.jsonl
so Layer B cron scripts appear in the scheduler page Run History.
"""
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

_RUNS_FILE = Path(__file__).resolve().parent.parent / "data" / "scheduler_runs.jsonl"


def record(job: str, fn, *args, **kwargs):
    """
    Call fn(*args, **kwargs), time it, then append a JSONL entry.
    Returns the fn's return value (or re-raises after logging the error).
    """
    import time
    t0 = time.time()
    ts = datetime.now(timezone.utc).isoformat()
    try:
        result = fn(*args, **kwargs)
        dur = round(time.time() - t0, 1)
        entry = {
            "job": job, "ts": ts, "state": "done", "duration_s": dur,
            "summary": _compact(result),
        }
        _write(entry)
        return result
    except Exception as e:
        dur = round(time.time() - t0, 1)
        entry = {
            "job": job, "ts": ts, "state": "error", "duration_s": dur,
            "error": str(e), "trace": traceback.format_exc()[:1000],
        }
        _write(entry)
        raise


def _compact(result, max_len=500) -> str:
    if not isinstance(result, dict):
        return str(result)[:max_len]
    compact = {}
    for k, v in result.items():
        if isinstance(v, list):
            compact[f"{k}_count"] = len(v)
        else:
            compact[k] = v
    return str(compact)[:max_len]


def _write(entry: dict):
    try:
        _RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_RUNS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never crash the calling script over a log write
