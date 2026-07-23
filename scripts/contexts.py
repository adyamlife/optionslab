"""
Typed data objects that carry signals between analysis stages.

Before: compute_signal_alignment(structure, trend, weekly_trend, rsi, macd_trend,
        news_sentiment, adx=..., rel_volume=..., pcr=..., ...)
        — a wrong key in the caller's dict silently passed None.

After:  compute_signal_alignment(structure, TechnicalContext(...), FlowContext(...), regime)
        — construction fails immediately with TypeError on any unknown field.

slots=True: prevents accidental attribute creation and reduces per-instance memory.
from_row() uses Mapping[str, Any] so it accepts dicts, DuckDB rows, or any read-only
mapping — and passes raw row.get() values directly so a missing key surfaces as None
rather than being hidden by a hardcoded fallback.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TechnicalContext:
    trend:           str | None   = None
    weekly_trend:    str | None   = None
    rsi:             float | None = None
    macd_trend:      str | None   = None
    adx:              float | None = None
    adx_slope:        float | None = None
    ema200_position:  str | None   = None
    ema_distance_pct: float | None = None
    iv_term_shape:   str | None   = None
    vol_skew_pct:    float | None = None
    iv_premium:      float | None = None
    atr_pct:              float | None = None   # ATR as % of spot (regime volatility gauge)
    hv30:                 float | None = None   # 30-day realised vol (annualised fraction)
    iv_rank_52w:          float | None = None   # IV rank vs 52-week range [0.0–1.0]
    max_pain_distance_pct: float | None = None  # (max_pain - spot) / spot × 100; negative = below spot
    oi_concentration:     float | None = None   # fraction of total OI within ±10% of spot [0–1]
    iv_change_5d:         float | None = None   # ATM IV change over last 5 trading days (annualised)
    unusual_activity:     float | None = None   # max per-strike OI spike ratio vs rolling avg (0=quiet; 1.0=doubled)
    iv_hv_ratio:          float | None = None   # ATM IV / 20-day HV; >1 = IV rich, <1 = IV cheap
    expected_move_pct:    float | None = None   # (ATM call mid + ATM put mid) / spot; market-implied ±1σ move
    term_slope:           float | None = None   # (front_iv - back_iv); positive = backwardation

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TechnicalContext":
        return cls(
            trend           = row.get("trend"),
            weekly_trend    = row.get("weekly_trend"),
            rsi             = row.get("rsi"),
            macd_trend      = row.get("macd_trend"),
            adx              = row.get("adx"),
            adx_slope        = row.get("adx_slope"),
            ema200_position  = row.get("ema200_position"),
            ema_distance_pct = row.get("ema_distance_pct"),
            iv_term_shape   = row.get("iv_term_shape"),
            vol_skew_pct    = row.get("vol_skew_pct"),
            iv_premium      = row.get("iv_premium"),
            atr_pct               = row.get("atr_pct"),
            hv30                  = row.get("hv30"),
            iv_rank_52w           = row.get("iv_rank_52w"),
            max_pain_distance_pct = row.get("max_pain_distance_pct"),
            oi_concentration      = row.get("oi_concentration"),
            iv_change_5d          = row.get("iv_change_5d"),
            unusual_activity      = row.get("unusual_activity"),
            iv_hv_ratio           = row.get("iv_hv_ratio"),
            expected_move_pct     = row.get("expected_move_pct"),
            term_slope            = row.get("term_slope"),
        )


@dataclass(slots=True)
class FlowContext:
    news_sentiment: str | None   = None
    rel_volume:     float | None = None
    pcr:            float | None = None
    pcr_sentiment:  str | None   = None
    short_interest: float | None = None
    analyst_label:  str | None   = None
    earnings_dte:   int | None   = None   # calendar days to next earnings (None = unknown)
    div_days_to_ex: int | None   = None   # calendar days to next ex-dividend (None = no div)
    vol_pcr:        float | None = None   # put_vol / call_vol (today's volume ratio; fast/tactical)
    pcr_diverge:    float | None = None   # oi_pcr - vol_pcr; positive = OI bearish but vol bullish

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "FlowContext":
        return cls(
            news_sentiment = row.get("news_sentiment"),
            rel_volume     = row.get("rel_volume"),
            pcr            = row.get("pcr"),
            pcr_sentiment  = row.get("pcr_sentiment"),
            short_interest = row.get("short_interest"),
            analyst_label  = row.get("analyst_label"),
            earnings_dte   = row.get("earnings_dte"),
            div_days_to_ex = row.get("div_days_to_ex"),
            vol_pcr        = row.get("vol_pcr"),
            pcr_diverge    = row.get("pcr_diverge"),
        )
