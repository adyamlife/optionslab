"""
Regime History — DuckDB-backed rolling store of per-ticker ML regime predictions.

Table: ml_regime_history  (in data/ml_training.duckdb)

One row per (ticker, date). Upserts so re-running predictions on the same day
is safe. Keeps all history (no trim) — 10-day window for streak queries is
handled in SQL, not by deleting rows.

Why DuckDB instead of JSON:
  - Queryable: "which tickers have been Downtrend >= 5 days?"
  - Joinable with regime_training and paper_trades for backtesting
  - Streak computed by window function — no Python list iteration
  - Single file (ml_training.duckdb) — no extra moving parts

Usage (called from regime_predictor.predict_ticker):
    from scripts.regime_history import regime_history
    streak = regime_history.record_and_streak(ticker, date_str, regime,
                                               regime_proba, pred_dist)
    result["ml_regime_streak"] = streak
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ml_regime_history (
    ticker              VARCHAR  NOT NULL,
    date                DATE     NOT NULL,
    regime              VARCHAR,          -- "Uptrend" | "Downtrend" | "Range-bound"
    proba_uptrend       FLOAT,
    proba_downtrend     FLOAT,
    proba_rangebound    FLOAT,
    meta_score          FLOAT,
    p_up                FLOAT,
    p_win               FLOAT,
    ml_confidence       FLOAT,
    PRIMARY KEY (ticker, date)
)
"""

# Streak query: count consecutive days from today backwards where regime matches.
# Uses a "gaps and islands" approach: assign a group number that increments each
# time the regime changes, then count the size of the latest group.
_STREAK_SQL = """
WITH ordered AS (
    SELECT
        date,
        regime,
        SUM(
            CASE WHEN regime = LAG(regime) OVER (ORDER BY date) THEN 0 ELSE 1 END
        ) OVER (ORDER BY date) AS grp
    FROM ml_regime_history
    WHERE ticker = ?
      AND date >= CURRENT_DATE - INTERVAL '10 days'
    ORDER BY date
),
latest_grp AS (
    SELECT MAX(grp) AS g FROM ordered
)
SELECT COUNT(*) AS streak
FROM ordered, latest_grp
WHERE ordered.grp = latest_grp.g
  AND ordered.regime = ?
"""

_UPSERT_SQL = """
INSERT INTO ml_regime_history
    (ticker, date, regime, proba_uptrend, proba_downtrend, proba_rangebound,
     meta_score, p_up, p_win, ml_confidence)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ticker, date) DO UPDATE SET
    regime           = excluded.regime,
    proba_uptrend    = excluded.proba_uptrend,
    proba_downtrend  = excluded.proba_downtrend,
    proba_rangebound = excluded.proba_rangebound,
    meta_score       = excluded.meta_score,
    p_up             = excluded.p_up,
    p_win            = excluded.p_win,
    ml_confidence    = excluded.ml_confidence
"""


class RegimeHistory:
    """Thread-safe DuckDB-backed ML regime history store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ensure_table()

    def _connect(self):
        from scripts.db import connect
        return connect()

    def _ensure_table(self) -> None:
        try:
            with self._connect() as con:
                con.execute(_CREATE_SQL)
                con.commit()
        except Exception as e:
            log.warning(f"[regime_history] table init failed: {e}")

    # ── public API ────────────────────────────────────────────────────────────

    def record_and_streak(
        self,
        ticker: str,
        date_str: Optional[str] = None,
        regime: Optional[str] = None,
        regime_proba: Optional[dict] = None,
        pred_dist: Optional[dict] = None,
    ) -> int:
        """
        Upsert today's ML prediction for ticker and return the current
        consecutive-day streak for that regime (looking back up to 10 days).

        Returns 0 if regime is None (model not trained / prediction error).
        """
        if regime is None:
            return 0

        today = date_str or date.today().isoformat()
        rp = regime_proba or {}
        pd_ = pred_dist or {}

        with self._lock:
            try:
                with self._connect() as con:
                    con.execute(_UPSERT_SQL, [
                        ticker,
                        today,
                        regime,
                        rp.get("Uptrend"),
                        rp.get("Downtrend"),
                        rp.get("Range-bound"),
                        pd_.get("signals", {}).get("meta"),  # meta_score as 0-1
                        pd_.get("signals", {}).get("p_up"),
                        pd_.get("p_win"),
                        pd_.get("confidence"),
                    ])
                    con.commit()

                    row = con.execute(_STREAK_SQL, [ticker, regime]).fetchone()
                    return int(row[0]) if row else 0

            except Exception as e:
                log.warning(f"[regime_history] record_and_streak failed for {ticker}: {e}")
                return 0

    def streak(self, ticker: str, regime: str) -> int:
        """Read-only streak query — does not record."""
        with self._lock:
            try:
                with self._connect() as con:
                    row = con.execute(_STREAK_SQL, [ticker, regime]).fetchone()
                    return int(row[0]) if row else 0
            except Exception as e:
                log.warning(f"[regime_history] streak query failed for {ticker}: {e}")
                return 0

    def recent(self, ticker: str, days: int = 10):
        """Return last N days of regime history for a ticker as a list of dicts."""
        try:
            with self._connect() as con:
                rows = con.execute("""
                    SELECT date, regime, proba_uptrend, proba_downtrend,
                           proba_rangebound, p_win, ml_confidence
                    FROM ml_regime_history
                    WHERE ticker = ?
                      AND date >= CURRENT_DATE - INTERVAL ? DAYS
                    ORDER BY date DESC
                """, [ticker, days]).fetchall()
                cols = ["date", "regime", "proba_uptrend", "proba_downtrend",
                        "proba_rangebound", "p_win", "ml_confidence"]
                return [dict(zip(cols, r)) for r in rows]
        except Exception as e:
            log.warning(f"[regime_history] recent query failed for {ticker}: {e}")
            return []

    def persistent_tickers(self, regime: str, min_streak: int = 3) -> list[str]:
        """
        Return tickers where ML has held `regime` for >= min_streak consecutive days.
        Useful for ad-hoc queries: 'which stocks have been Downtrend for 5+ days?'
        """
        try:
            with self._connect() as con:
                # Compute streak for every ticker and filter
                df = con.execute("""
                    WITH ordered AS (
                        SELECT
                            ticker, date, regime,
                            SUM(
                                CASE WHEN regime = LAG(regime) OVER (
                                    PARTITION BY ticker ORDER BY date
                                ) THEN 0 ELSE 1 END
                            ) OVER (PARTITION BY ticker ORDER BY date) AS grp
                        FROM ml_regime_history
                        WHERE date >= CURRENT_DATE - INTERVAL '10 days'
                    ),
                    latest_grp AS (
                        SELECT ticker, MAX(grp) AS g
                        FROM ordered
                        GROUP BY ticker
                    ),
                    streaks AS (
                        SELECT o.ticker, COUNT(*) AS streak
                        FROM ordered o
                        JOIN latest_grp lg ON o.ticker = lg.ticker AND o.grp = lg.g
                        WHERE o.regime = ?
                        GROUP BY o.ticker
                    )
                    SELECT ticker FROM streaks WHERE streak >= ?
                    ORDER BY ticker
                """, [regime, min_streak]).fetchall()
                return [r[0] for r in df]
        except Exception as e:
            log.warning(f"[regime_history] persistent_tickers failed: {e}")
            return []


# Module-level singleton
regime_history = RegimeHistory()
