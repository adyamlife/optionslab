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

def score_to_rating(pct: float) -> str:
    """
    Convert a normalised percentage to a qualitative label.

    Accepts pct = score / effective_max, where effective_max is the sum of
    weights for sub-factors that were applicable to the structure being scored.
    Callers are responsible for normalisation; this function has no knowledge
    of regimes, budgets, or raw scores — it is a pure threshold lookup.

    Neutral is a ±10% band so that small-magnitude positive and negative
    scores produce the same label rather than jumping between Weak and Neutral
    on tiny score changes (see scoring.toml [rating] for thresholds).
    """
    cfg = _cfg()
    r   = cfg["rating"]
    if pct >= r["strong"]:   return "Strong"
    if pct >= r["moderate"]: return "Moderate"
    if pct >= r["neutral"]:  return "Neutral"
    if pct >= r["weak"]:     return "Weak"
    return "Conflicted"


# ── Regime explainability ────────────────────────────────────────────────────

_REGIME_EXPLANATIONS: dict[str, str] = {
    "fear":       ("High VIX + weak futures — macro context dominates; "
                   "technical signals are less discriminating in correlated sell-offs"),
    "calm_trend": ("Low VIX + strong trend (ADX) — price action is highly reliable; "
                   "technical signals carry double the normal weight"),
    "chop":       ("No clear macro regime — flow and oscillator signals carry more "
                   "weight than trend; technicals are noisier than usual"),
}


def regime_explanation(regime: str) -> str:
    """Human-readable sentence describing why this regime shifted the weight profile."""
    return _REGIME_EXPLANATIONS.get(regime, f"Regime '{regime}' — standard weighting")


def weight_profile(regime: str) -> dict[str, float]:
    """
    Effective budget for each factor in the given regime.

    Returns the actual budget (base × budget_mult) so the UI can show
    'why did flow matter less today?' without the user knowing how weights work.
    """
    cfg = _cfg()
    result: dict[str, float] = {}
    for factor in cfg["factors"]:
        base = float(cfg["factors"][factor]["budget"])
        mult = cfg["regime"][regime]["budget_mult"].get(factor, 1.0)
        result[factor] = round(base * mult, 4)
    return result


# ── Gate / penalty accessors ──────────────────────────────────────────────────

def gate(key: str) -> Any:
    return _cfg()["gates"].get(key)


def penalty(key: str) -> float:
    return float(_cfg()["penalties"].get(key, 0))
