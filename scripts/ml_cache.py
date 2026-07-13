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
import logging
import time
import threading
from datetime import datetime, timezone
from typing import Optional

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


# Module-level singleton — imported everywhere
ml_cache = MLPredictionCache()
