"""
Utility for standalone cron scripts to record their run results into
scheduler_runs.jsonl — the same file the Flask scheduler page reads.
Also manages per-job lock files so Flask can show "Running" state.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR         = Path(__file__).parent.parent / "data"
_RUN_HISTORY_FILE = _DATA_DIR / "scheduler_runs.jsonl"
_LOCK_DIR         = _DATA_DIR / "locks"
_RUN_HISTORY_MAX  = 500


def acquire_lock(job: str) -> None:
    """Write a lock file so Flask knows this job is running."""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    (_LOCK_DIR / f"{job}.lock").write_text(datetime.now(timezone.utc).isoformat())


def release_lock(job: str) -> None:
    """Remove the lock file — job has finished."""
    try:
        (_LOCK_DIR / f"{job}.lock").unlink(missing_ok=True)
    except Exception:
        pass


def record_run(job: str, result: dict | None, duration_s: float, error: str | None = None,
               started_at: str | None = None):
    """Append a run entry to scheduler_runs.jsonl so the scheduler UI stays current."""
    entry = {
        "job":        job,
        "ts":         started_at or datetime.now(timezone.utc).isoformat(),
        "state":      "error" if error else "done",
        "duration_s": round(duration_s, 1),
    }
    if error:
        entry["error"] = error
    elif result:
        # Store a compact summary (first 300 chars of JSON)
        try:
            entry["summary"] = json.dumps(result, default=str)[:300]
        except Exception:
            pass

    try:
        _RUN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _RUN_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # Trim if over 2× cap
        lines = _RUN_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > _RUN_HISTORY_MAX * 2:
            _RUN_HISTORY_FILE.write_text(
                "\n".join(lines[-_RUN_HISTORY_MAX:]) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[cron_logger] WARNING: could not write run history: {e}")
