"""
config/scoring.py
Adaptive weight engine — reads scoring.toml and computes per-sub-factor weights.

Key guarantee: sub-weight allocation within a factor always sums to the factor
budget, regardless of how individual sub-weights are tuned. Budget is controlled
separately by the regime multiplier table.

Auto-reloads from disk every 60 seconds — edit scoring.toml, next scan picks it up.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

_PATH      = Path(__file__).parent / "scoring.toml"
_cache: dict | None = None
_cached_at: float   = 0.0
_TTL = 60.0


def _cfg() -> dict:
    global _cache, _cached_at
    if _cache is None or time.time() - _cached_at > _TTL:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        _cache     = tomllib.loads(_PATH.read_text(encoding="utf-8"))
        _cached_at = time.time()
    return _cache


# ── Regime detection ──────────────────────────────────────────────────────────

def detect_regime(mkt_ctx: dict | None, adx: float | None = None) -> str:
    """
    Classify the current market into one of three regimes:
      "fear"       — VIX high + futures down sharply
      "calm_trend" — VIX low + strong ADX trend
      "chop"       — everything else (fallback)

    mkt_ctx: dict from market_context.get_market_context()
    adx:     current ticker ADX (optional; used for calm_trend detection)
    """
    if not mkt_ctx:
        return "chop"

    cfg       = _cfg()
    fear_cfg  = cfg["regime"]["fear"]
    trend_cfg = cfg["regime"]["calm_trend"]

    vix_price  = (mkt_ctx.get("vix") or {}).get("price")
    futures    = mkt_ctx.get("futures", [])
    valid_pcts = [f["change_pct"] for f in futures if f.get("change_pct") is not None]
    avg_fut    = sum(valid_pcts) / len(valid_pcts) if valid_pcts else 0.0

    if (vix_price is not None
            and vix_price > fear_cfg["vix_above"]
            and avg_fut   < fear_cfg["futures_below"]):
        return "fear"

    if (vix_price is not None
            and vix_price < trend_cfg["vix_below"]
            and adx is not None
            and adx > trend_cfg["adx_above"]):
        return "calm_trend"

    return "chop"


# ── Weight accessor ───────────────────────────────────────────────────────────

def get_sub_weight(factor: str, sub: str, regime: str) -> float:
    """
    Effective weight for one sub-factor given the current regime.

    Formula:
        budget   = base_budget × regime_budget_mult
        share    = merged_sub_weight[sub] / Σ merged_sub_weights
        result   = share × budget

    The sub-weight allocation always sums to budget — editing any sub-weight
    only redistributes the pot, never inflates the total.
    """
    cfg = _cfg()
    f   = cfg["factors"].get(factor)
    if f is None:
        return 0.0

    base_budget = float(f["budget"])
    mult        = cfg["regime"][regime]["budget_mult"].get(factor, 1.0)
    budget      = base_budget * mult

    defaults    = dict(f["sub_weights"])
    overrides   = f.get("regime_overrides", {}).get(regime, {})
    merged      = {**defaults, **overrides}

    total = sum(merged.values())
    if total == 0:
        return 0.0

    share = merged.get(sub, 0) / total
    return share * budget


def get_factor_budget(factor: str, regime: str) -> float:
    """Total budget for a factor in a given regime."""
    cfg  = _cfg()
    f    = cfg["factors"].get(factor)
    if f is None:
        return 0.0
    base = float(f["budget"])
    mult = cfg["regime"][regime]["budget_mult"].get(factor, 1.0)
    return base * mult


def max_score(regime: str) -> float:
    """Theoretical maximum total score (all sub-factors fire +1)."""
    cfg   = _cfg()
    total = 0.0
    for factor in cfg["factors"]:
        total += get_factor_budget(factor, regime)
    return total


# ── Rating ────────────────────────────────────────────────────────────────────

def score_to_rating(score: float, regime: str) -> str:
    """Convert a raw weighted score to a human label using toml thresholds."""
    cfg = _cfg()
    r   = cfg["rating"]
    mx  = max_score(regime)
    if mx == 0:
        return "Neutral"
    pct = score / mx
    if pct >= r["strong"]:   return "Strong"
    if pct >= r["moderate"]: return "Moderate"
    if pct >= r["neutral"]:  return "Neutral"
    if pct >= r["weak"]:     return "Weak"
    return "Conflicted"


# ── Gate / penalty accessors ──────────────────────────────────────────────────

def gate(key: str) -> Any:
    return _cfg()["gates"].get(key)


def penalty(key: str) -> float:
    return float(_cfg()["penalties"].get(key, 0))
