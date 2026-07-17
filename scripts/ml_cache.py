"""
ML Prediction Cache — module-level store of regime_predictor.predict_all() output.

Refreshed by the hourly scheduler so every Live Suggestions scan reads from a
warm cache instead of racing a background thread. Solves the root problem: the
old background-thread approach in _generate_inner meant the first 5-10 tickers
in every scan missed ML data because predict_all() hadn't finished yet.

Usage:
    from scripts.ml_cache import ml_cache

    # Refresh (called by scheduler, not by request handlers):
    ml_cache.refresh()

    # Read per-ticker (called per-ticker in _generate_inner and /api/decision):
    prediction = ml_cache.get("AAPL")   # None if cache empty or ticker missing
    age = ml_cache.age_seconds()        # None if never refreshed

    # Operational health:
    status = ml_cache.status()          # warm, age, size, refresh duration, …
"""
from __future__ import annotations

import copy
import json
import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DISK_PATH = Path(__file__).resolve().parent.parent / "data" / "ml_cache.json"

logger = logging.getLogger(__name__)

# Cache is considered stale at 1.5× the expected refresh interval (1 h).
# Tie this to scheduler expectations: scheduler every 3600 s → stale at 5400 s.
_REFRESH_INTERVAL_SECONDS = 3600
_STALE_SECONDS            = int(_REFRESH_INTERVAL_SECONDS * 1.5)  # 90 min


class MLPredictionCache:
    def __init__(self) -> None:
        # RLock so nested acquisitions within this class never deadlock
        self._lock               = threading.RLock()
        self._by_ticker:          dict[str, dict] = {}
        self._refreshed_at:       Optional[datetime] = None
        self._refresh_in_progress = False
        self._last_refresh_secs:  Optional[float] = None  # wall-clock duration of last refresh

    # ── Public read interface (called from request handlers) ──────────────────

    def get(self, ticker: str) -> Optional[dict]:
        """
        Return a shallow copy of the cached prediction for one ticker, or None.
        A copy is returned so callers cannot accidentally mutate the cache.
        Use get_deep(ticker) if values contain nested mutable objects.
        """
        with self._lock:
            p = self._by_ticker.get(ticker)
            return p.copy() if p is not None else None

    def get_deep(self, ticker: str) -> Optional[dict]:
        """Like get(), but returns a deep copy — safe for nested mutable values."""
        with self._lock:
            p = self._by_ticker.get(ticker)
            return copy.deepcopy(p) if p is not None else None

    def get_all(self) -> dict[str, dict]:
        """
        Return a deep-copied snapshot of the full cache.
        Mutations to the returned dict do not affect the cache.
        """
        with self._lock:
            return copy.deepcopy(self._by_ticker)

    def age_seconds(self) -> Optional[float]:
        """Seconds since the last successful refresh, or None if never refreshed."""
        with self._lock:
            if self._refreshed_at is None:
                return None
            return (datetime.now(timezone.utc) - self._refreshed_at).total_seconds()

    def is_warm(self) -> bool:
        """True if the cache has data and is not stale (age < _STALE_SECONDS)."""
        age = self.age_seconds()
        return age is not None and age < _STALE_SECONDS

    def size(self) -> int:
        with self._lock:
            return len(self._by_ticker)

    def status(self) -> dict:
        """
        Operational health snapshot — suitable for /api/health or monitoring.
        Returns warm, age_seconds, size, refreshing, last_refresh_secs, last_refresh_at.
        """
        with self._lock:
            age = (
                (datetime.now(timezone.utc) - self._refreshed_at).total_seconds()
                if self._refreshed_at else None
            )
            return {
                "warm":              age is not None and age < _STALE_SECONDS,
                "age_seconds":       round(age, 1) if age is not None else None,
                "size":              len(self._by_ticker),
                "refreshing":        self._refresh_in_progress,
                "last_refresh_secs": (
                    round(self._last_refresh_secs, 2)
                    if self._last_refresh_secs is not None else None
                ),
                "last_refresh_at":   (
                    self._refreshed_at.isoformat()
                    if self._refreshed_at else None
                ),
                "stale_after_secs":  _STALE_SECONDS,
            }

    # ── Refresh (called by scheduler and startup thread) ──────────────────────

    def refresh(self, tickers: list[str] | None = None) -> dict:
        """
        Re-run predict_all() and atomically swap the cache on success.
        If predict_all() raises, the old cache is preserved and an exception
        is logged with its full traceback.
        Returns a summary dict with ok, updated, age_before, duration_secs.
        On concurrent call: returns ok=True, already_refreshing=True (not an error).
        """
        with self._lock:
            if self._refresh_in_progress:
                return {"ok": True, "already_refreshing": True}
            self._refresh_in_progress = True
            age_before = (
                (datetime.now(timezone.utc) - self._refreshed_at).total_seconds()
                if self._refreshed_at else None
            )

        t_start = time.perf_counter()
        try:
            from scripts.regime_predictor import predict_all
            result    = predict_all(tickers)
            new_cache: dict[str, dict] = {}
            for p in result.get("predictions", []):
                if p.get("ok"):
                    new_cache[p["ticker"]] = p
            for w in result.get("warnings", []):
                logger.warning("ml_cache refresh warning: %s", w)

            elapsed = time.perf_counter() - t_start
            refreshed_at = datetime.now(timezone.utc)

            with self._lock:
                self._by_ticker       = new_cache
                self._refreshed_at    = refreshed_at
                self._last_refresh_secs = elapsed
                n = len(new_cache)

            logger.info(
                "ml_cache refreshed: %d tickers in %.2fs", n, elapsed
            )
            self.save(source="morning_scan")
            # Return ticker count only — full list can be hundreds of symbols
            return {
                "ok":           True,
                "updated":      n,
                "duration_secs": round(elapsed, 2),
                "age_before":   round(age_before, 1) if age_before is not None else None,
                "refreshed_at": refreshed_at.isoformat(),
            }
        except Exception:
            elapsed = time.perf_counter() - t_start
            # log.exception preserves the full traceback in log output
            logger.exception(
                "ml_cache refresh failed after %.2fs (old cache preserved)", elapsed
            )
            return {
                "ok":           False,
                "duration_secs": round(elapsed, 2),
                "age_before":   round(age_before, 1) if age_before is not None else None,
            }
        finally:
            with self._lock:
                self._refresh_in_progress = False

    def refresh_async(self, tickers: list[str] | None = None) -> None:
        """Fire-and-forget refresh in a daemon thread — used at startup."""
        t = threading.Thread(
            target=self.refresh, args=(tickers,),
            daemon=True, name="ml-cache-refresh",
        )
        t.start()

    # ── Disk persistence ──────────────────────────────────────────────────────

    def save(self, source: str = "scan") -> None:
        """
        Write current cache to disk (JSON for fast cross-process IPC) and to
        DuckDB (for historical tracking, drift analysis, and training-data joins).
        """
        with self._lock:
            refreshed_at = self._refreshed_at
            snapshot = copy.deepcopy(self._by_ticker)

        # ── JSON fast path (cross-process IPC) ───────────────────────────────
        payload = {
            "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
            "by_ticker":    snapshot,
        }
        try:
            _DISK_PATH.parent.mkdir(parents=True, exist_ok=True)
            _DISK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            logger.debug("ml_cache saved to disk: %d tickers", len(snapshot))
        except Exception:
            logger.exception("ml_cache.save() failed — disk write error")

        # ── DuckDB historical record ──────────────────────────────────────────
        if snapshot and refreshed_at:
            try:
                from scripts.db import insert_ml_predictions
                n = insert_ml_predictions(
                    snapshot,
                    scanned_at=refreshed_at.isoformat(),
                    source=source,
                )
                logger.debug("ml_cache saved %d rows to DuckDB ml_predictions", n)
            except Exception:
                logger.exception("ml_cache.save() — DuckDB write failed (non-fatal)")

    def load_from_disk(self) -> bool:
        """
        Load cache from disk (JSON first, DuckDB fallback if JSON is missing/stale).
        Returns True if data was loaded, False otherwise.
        Called once at module init so the Flask process starts warm.
        """
        # ── JSON fast path ────────────────────────────────────────────────────
        if _DISK_PATH.exists():
            try:
                payload       = json.loads(_DISK_PATH.read_text(encoding="utf-8"))
                refreshed_str = payload.get("refreshed_at")
                by_ticker     = payload.get("by_ticker") or {}
                if refreshed_str and by_ticker:
                    refreshed_at = datetime.fromisoformat(refreshed_str)
                    age = (datetime.now(timezone.utc) - refreshed_at).total_seconds()
                    if age <= _STALE_SECONDS:
                        with self._lock:
                            self._by_ticker    = by_ticker
                            self._refreshed_at = refreshed_at
                        logger.info(
                            "ml_cache loaded from disk: %d tickers, age %.0fs",
                            len(by_ticker), age,
                        )
                        return True
                    logger.info("ml_cache disk file is stale (%.0fs) — trying DuckDB", age)
            except Exception:
                logger.exception("ml_cache.load_from_disk() JSON read failed — trying DuckDB")

        # ── DuckDB fallback ───────────────────────────────────────────────────
        try:
            from scripts.db import load_latest_ml_predictions
            preds = load_latest_ml_predictions()
            if preds:
                with self._lock:
                    self._by_ticker    = preds
                    self._refreshed_at = datetime.now(timezone.utc)
                logger.info("ml_cache loaded from DuckDB fallback: %d tickers", len(preds))
                return True
        except Exception:
            logger.exception("ml_cache.load_from_disk() DuckDB fallback failed")

        return False

    def set_from_snapshot(self, snapshot: dict[str, dict]) -> None:
        """
        Populate the cache from an externally-computed predict_all snapshot dict
        {ticker: pred_result}. Called by paper_trade_engine after it runs predict_all
        so the Flask process inherits the data via disk persistence.
        """
        if not snapshot:
            return
        with self._lock:
            self._by_ticker    = {k: v for k, v in snapshot.items() if v.get("ok")}
            self._refreshed_at = datetime.now(timezone.utc)
            n = len(self._by_ticker)
        logger.info("ml_cache populated from external snapshot: %d tickers", n)
        self.save(source="paper_trade_engine")


# Module-level singleton — imported everywhere
ml_cache = MLPredictionCache()
# Auto-load from disk so the Flask process starts warm without a fresh scan
ml_cache.load_from_disk()
