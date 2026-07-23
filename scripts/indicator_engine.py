"""
indicator_engine.py — Compute and cache technical indicators for a price history.

Two problems solved:

  1. Compute-once per ticker.
     trend, RSI, MACD, ADX, rel_volume, EMA200 all read the same hist DataFrame.
     Previously each was a separate function call. IndicatorEngine computes all
     of them at construction so the DataFrame is traversed once per indicator
     group rather than being passed into six independent calls.

  2. SPY history cached across tickers in a batch scan.
     In a 50-ticker morning scan, get_price_history("SPY") was called 50 times
     (once per ticker for the beta calculation) even though SPY history doesn't
     change between tickers. IndicatorEngine.spy_hist() returns a process-level
     cached copy with a 1-hour TTL — 49 of those 50 calls become instant lookups.

Note: weekly_trend stays in the parallel I/O block in analyze_ticker because it
fetches yfinance weekly bars (network I/O) rather than computing from hist.
"""
import time
import sys
import os
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from scripts.data_fetch import (
    get_trend, get_rsi, get_macd, get_adx,
    get_relative_volume, get_ema200, get_price_history,
)


@dataclass(frozen=True, slots=True)
class IndicatorEngine:
    """
    Immutable snapshot of all hist-only technical indicators for one ticker.

    Construct via IndicatorEngine.from_hist(hist, params) — the dataclass
    __init__ accepts pre-computed values directly, but the classmethod is the
    intended entry point.

    Fields:
        trend            — "Uptrend" | "Downtrend" | "Range-bound"
        rsi              — 14-period RSI float or None
        macd_trend       — "Bullish" | "Bearish" | "N/A"
        macd_hist        — MACD histogram float or None
        adx              — 14-period ADX float or None
        adx_slope        — day-over-day change in ADX, or None
        rel_volume       — today's volume / 20-day average, or None
        ema200           — EMA200 price level float or None
        ema200_position  — "above" | "below" | None
        ema_distance_pct — (price - EMA200) / EMA200 × 100, signed; None if < 200 bars
    """

    trend:            str | None
    rsi:              float | None
    macd_trend:       str | None
    macd_hist:        float | None
    adx:              float | None
    adx_slope:        float | None
    rel_volume:       float | None
    ema200:           float | None
    ema200_position:  str | None
    ema_distance_pct: float | None

    # ── Process-level SPY cache ───────────────────────────────────────────────
    # ClassVar so dataclass ignores these — they live on the class, not instances.
    # frozen=True only protects instance attributes; class-level state stays mutable.
    _spy_cache:     ClassVar[pd.DataFrame | None] = None
    _spy_cached_at: ClassVar[float]               = 0.0
    _SPY_TTL:       ClassVar[float]               = 3600.0

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_hist(cls, hist: pd.DataFrame, params: dict) -> "IndicatorEngine":
        """Compute all indicators from a price-history DataFrame and return an immutable snapshot."""
        p = params
        _macd = get_macd(hist)
        _adx, _adx_slope              = get_adx(hist)
        _ema200_val, _ema200_pos, _ema200_dist = get_ema200(hist)
        return cls(
            trend            = get_trend(hist, p["sma_short"], p["sma_long"], p["trend_band_pct"]),
            rsi              = get_rsi(hist),
            macd_trend       = _macd["trend"],
            macd_hist        = _macd["hist"],
            adx              = _adx,
            adx_slope        = _adx_slope,
            rel_volume       = get_relative_volume(hist),
            ema200           = _ema200_val,
            ema200_position  = _ema200_pos,
            ema_distance_pct = _ema200_dist,
        )

    # ── SPY cache helpers ─────────────────────────────────────────────────────

    @classmethod
    def spy_hist(cls) -> pd.DataFrame:
        """
        Return cached SPY price history, refreshing at most once per hour.

        Call this anywhere beta or relative-strength vs SPY is needed.
        Replaces the per-ticker get_price_history("SPY") call in analyze_ticker.
        """
        now = time.time()
        if cls._spy_cache is None or now - cls._spy_cached_at > cls._SPY_TTL:
            cls._spy_cache     = get_price_history("SPY")
            cls._spy_cached_at = now
        return cls._spy_cache

    @classmethod
    def invalidate_spy_cache(cls) -> None:
        """Force the next spy_hist() call to re-fetch. Useful in tests."""
        cls._spy_cache     = None
        cls._spy_cached_at = 0.0
