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
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_STALE_SECONDS = 7200  # treat cache as stale if >2 hours old (safety net only)


class MLPredictionCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_ticker: dict[str, dict] = {}
        self._refreshed_at: Optional[datetime] = None
        self._refresh_in_progress = False

    # ── Public read interface (called from request handlers) ──────────────────

    def get(self, ticker: str) -> Optional[dict]:
        """Return the cached prediction for one ticker, or None."""
        with self._lock:
            return self._by_ticker.get(ticker)

    def get_all(self) -> dict[str, dict]:
        """Return a snapshot of the full cache."""
        with self._lock:
            return dict(self._by_ticker)

    def age_seconds(self) -> Optional[float]:
        """Seconds since the last successful refresh, or None if never refreshed."""
        with self._lock:
            if self._refreshed_at is None:
                return None
            return (datetime.now(timezone.utc) - self._refreshed_at).total_seconds()

    def is_warm(self) -> bool:
        """True if the cache has data and is not stale."""
        age = self.age_seconds()
        return age is not None and age < _STALE_SECONDS

    def size(self) -> int:
        with self._lock:
            return len(self._by_ticker)

    # ── Refresh (called by scheduler and startup thread) ──────────────────────

    def refresh(self, tickers: list[str] | None = None) -> dict:
        """
        Re-run predict_all() and atomically swap the cache on success.
        If predict_all() raises, the old cache is preserved and a warning is logged.
        Returns a summary dict {ok, updated, tickers, age_before}.
        """
        # Single concurrent refresh at a time — second caller returns immediately.
        with self._lock:
            if self._refresh_in_progress:
                return {"ok": False, "error": "refresh already in progress"}
            self._refresh_in_progress = True

        age_before = self.age_seconds()
        try:
            from scripts.regime_predictor import predict_all
            result = predict_all(tickers)
            new_cache: dict[str, dict] = {}
            for p in result.get("predictions", []):
                if p.get("ok"):
                    new_cache[p["ticker"]] = p
            if result.get("warnings"):
                for w in result["warnings"]:
                    logger.warning("ml_cache refresh warning: %s", w)
            with self._lock:
                self._by_ticker = new_cache
                self._refreshed_at = datetime.now(timezone.utc)
            logger.info("ml_cache refreshed: %d tickers", len(new_cache))
            return {"ok": True, "updated": len(new_cache), "tickers": list(new_cache), "age_before": age_before}
        except Exception as e:
            logger.warning("ml_cache refresh failed (old cache preserved): %s", e)
            return {"ok": False, "error": str(e), "age_before": age_before}
        finally:
            with self._lock:
                self._refresh_in_progress = False

    def refresh_async(self, tickers: list[str] | None = None) -> None:
        """Fire-and-forget refresh in a daemon thread — used at startup."""
        t = threading.Thread(target=self.refresh, args=(tickers,), daemon=True, name="ml-cache-refresh")
        t.start()


# Module-level singleton — imported everywhere
ml_cache = MLPredictionCache()
