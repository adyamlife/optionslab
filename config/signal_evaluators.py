"""
config/signal_evaluators.py
Signal evaluators for the generic scoring engine.

Each evaluator signature: (preference: str, market: dict) -> EvaluationResult
  - EvaluationResult.score is None when a required market field is missing (signal skipped).
  - score ∈ [-1.0, +1.0]: +1 = perfect alignment, 0 = neutral, -1 = fully opposed.
  - confidence ∈ [0.0, 1.0]: data quality / decisiveness, independent of score direction.
      1.0 — clear, decisive condition; all required data present and unambiguous
      0.7 — good signal with minor uncertainty (borderline threshold, partial data)
      0.5 — transitional zone; signal is real but not strong
      0.35 — fallback path used (e.g. adx_slope missing, inferred proxy)
      0.0 — required data absent; score is None

Threshold values and score magnitudes are NOT hardcoded here — they come from
config/signal_params.toml.  Edit that file to retune sensitivity.
Score-level aliases (e.g. "@strong" = 0.85) are resolved by the loader.

"directional" is a meta-preference that must be resolved to "bullish" or "bearish"
by the caller (compute_signal_alignment) before calling evaluate().
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

# ── EvaluationResult ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvaluationResult:
    """
    Rich return type from every signal evaluator.

    score       — signed float ∈ [-1.0, +1.0], or None when required data is absent.
                  None causes the caller to skip this signal entirely (excluded from
                  effective_max as well as numerator).
    confidence  — data quality / decisiveness ∈ [0.0, 1.0].
                  Lets callers distinguish "weak signal" from "barely any data."
                  Future: weight contributions by (eff_wt × confidence × score).
    explanation — human-readable sentence for UI / audit trail; empty string when
                  score is None or zero.
    """
    score:       float | None
    confidence:  float
    explanation: str = ""

    @classmethod
    def missing(cls) -> "EvaluationResult":
        """Required market field absent; signal must be skipped."""
        return cls(score=None, confidence=0.0, explanation="")

    @classmethod
    def neutral(cls) -> "EvaluationResult":
        """Signal present but in a zone that contributes no information."""
        return cls(score=0.0, confidence=1.0, explanation="")


# ── Params loader ─────────────────────────────────────────────────────────────

_SP_PATH      = Path(__file__).parent / "signal_params.toml"
_sp_cache: dict | None = None
_sp_cached_at: float   = 0.0
_TTL = 60.0


def _sp_reload() -> dict:
    global _sp_cache, _sp_cached_at
    if _sp_cache is None or time.time() - _sp_cached_at > _TTL:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        raw           = tomllib.loads(_SP_PATH.read_text(encoding="utf-8"))
        levels        = raw.get("score_levels", {})
        # Resolve "@alias" strings in every [signals.X] sub-dict
        resolved: dict = {}
        for sig, params in raw.get("signals", {}).items():
            resolved[sig] = {
                k: levels[v[1:]] if isinstance(v, str) and v.startswith("@") else v
                for k, v in params.items()
            }
        raw["signals"] = resolved
        _sp_cache     = raw
        _sp_cached_at = time.time()
    return _sp_cache


def _p(signal: str) -> dict:
    """Return the resolved param dict for a signal.  Empty dict if not in TOML."""
    return _sp_reload().get("signals", {}).get(signal, {})


def _levels() -> dict:
    """Return the [score_levels] dict."""
    return _sp_reload().get("score_levels", {})


# ── SignalDefinition ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalDefinition:
    name:              str
    valid_preferences: frozenset[str]
    required_fields:   frozenset[str]
    score_range:       tuple[float, float] = (-1.0, 1.0)


# ── Evaluators ────────────────────────────────────────────────────────────────

def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation clamped to [min(y0,y1), max(y0,y1)]."""
    if x1 == x0:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def _eval_ema200(preference: str, market: dict) -> EvaluationResult:
    dist = market.get("ema_distance_pct")
    if dist is None:
        return EvaluationResult.missing()
    p = _p("ema200")

    near     = p.get("near",     0.5)
    moderate = p.get("moderate", 2.0)
    strong   = p.get("strong",   5.0)
    far      = p.get("far",     12.0)

    s_near   = p.get("score_at_near_start",   0.10)
    s_strong = p.get("score_at_strong",       0.85)
    s_far    = p.get("score_at_far",          1.0)
    s_cn     = p.get("score_conflict_near",  -0.15)
    s_cs     = p.get("score_conflict_strong",-0.85)
    s_cf     = p.get("score_conflict_far",   -1.0)

    s_nn = p.get("score_neutral_near",  0.85)
    s_nm = p.get("score_neutral_mid",   0.60)
    s_nf = p.get("score_neutral_far",  -0.85)

    def _directional(align_dist: float) -> EvaluationResult:
        """Score for a preference where positive align_dist = confirming direction."""
        if align_dist >= far:
            return EvaluationResult(s_far, 1.0,
                f"EMA200 distance {dist:+.1f}% — strongly extended in trade direction")
        if align_dist >= strong:
            s = _lerp(align_dist, strong, far, s_strong, s_far)
            return EvaluationResult(round(s, 3), 1.0,
                f"EMA200 distance {dist:+.1f}% — clear institutional trend supports trade")
        if align_dist >= near:
            s = _lerp(align_dist, near, strong, s_near, s_strong)
            return EvaluationResult(round(s, 3), 0.7,
                f"EMA200 distance {dist:+.1f}% — moderate trend displacement supports trade")
        if align_dist > -near:
            return EvaluationResult(0.0, 0.5, "")   # right at EMA200; no directional read
        if align_dist > -strong:
            s = _lerp(align_dist, -near, -strong, s_cn, s_cs)
            return EvaluationResult(round(s, 3), 0.7,
                f"EMA200 distance {dist:+.1f}% — price on wrong side of EMA200, headwind")
        if align_dist > -far:
            s = _lerp(align_dist, -strong, -far, s_cs, s_cf)
            return EvaluationResult(round(s, 3), 1.0,
                f"EMA200 distance {dist:+.1f}% — clear institutional trend opposes trade")
        return EvaluationResult(s_cf, 1.0,
            f"EMA200 distance {dist:+.1f}% — strongly extended against trade direction")

    if preference == "bullish":
        return _directional(dist)
    if preference == "bearish":
        return _directional(-dist)   # flip: below EMA200 is the confirming direction
    if preference == "neutral":
        abs_dist = abs(dist)
        if abs_dist <= near:
            return EvaluationResult(s_nn, 1.0,
                f"EMA200 distance {dist:+.1f}% — at EMA200, ideal for range/neutral trade")
        if abs_dist <= moderate:
            s = _lerp(abs_dist, near, moderate, s_nn, s_nm)
            return EvaluationResult(round(s, 3), 1.0,
                f"EMA200 distance {dist:+.1f}% — close to EMA200, acceptable for range trade")
        if abs_dist <= strong:
            s = _lerp(abs_dist, moderate, strong, s_nm, 0.0)
            return EvaluationResult(round(s, 3), 0.7,
                f"EMA200 distance {dist:+.1f}% — extended; some trend risk for neutral trade")
        s = _lerp(abs_dist, strong, far, 0.0, s_nf)
        return EvaluationResult(round(max(s, s_nf), 3), 1.0,
            f"EMA200 distance {dist:+.1f}% — strongly extended; mean-revert risk for range trade")
    return EvaluationResult.neutral()


def _eval_weekly_trend(preference: str, market: dict) -> EvaluationResult:
    wt = market.get("weekly_trend")
    dt = market.get("daily_trend")
    if not wt or wt in ("N/A",):
        return EvaluationResult.missing()
    p = _p("weekly_trend")

    if preference == "aligned":
        if wt == "Range-bound":
            return EvaluationResult.neutral()
        if wt == dt:
            return EvaluationResult(p.get("score_aligned", 1.0), 1.0,
                                    f"Weekly trend confirms {dt} — multi-timeframe alignment")
        return EvaluationResult(p.get("score_aligned_conflict", -0.85), 1.0,
                                f"Weekly trend ({wt}) conflicts with daily ({dt}) — divergence risk")

    if preference == "neutral":
        if wt == "Range-bound":
            return EvaluationResult(p.get("score_neutral_range", 0.60), 1.0,
                                    "Weekly trend Range-bound — supports neutral/time-spread environment")
        return EvaluationResult(p.get("score_neutral_directional", -0.30), 1.0,
                                f"Weekly trend {wt} — directional weekly adds breakout risk to neutral trade")

    if preference == "divergence_ok":
        if wt == "Range-bound":
            return EvaluationResult(p.get("score_divergence_ok_range", 0.60), 0.7,
                                    "Weekly trend Range-bound — low weekly momentum supports time-decay trade")
        if wt == dt:
            return EvaluationResult(p.get("score_divergence_ok_match", 0.30), 0.7,
                                    f"Weekly trend aligned with daily — mild tailwind for time-spread")
        return EvaluationResult.neutral()   # divergence is not penalised

    if preference == "bullish":
        if wt == "Uptrend":
            return EvaluationResult(p.get("score_directional_confirm", 0.85), 1.0,
                                    "Weekly trend Uptrend — confirms directional bullish bias")
        if wt == "Range-bound":
            return EvaluationResult.neutral()
        return EvaluationResult(p.get("score_directional_conflict", -0.85), 1.0,
                                "Weekly trend Downtrend — opposes bullish bias")

    if preference == "bearish":
        if wt == "Downtrend":
            return EvaluationResult(p.get("score_directional_confirm", 0.85), 1.0,
                                    "Weekly trend Downtrend — confirms directional bearish bias")
        if wt == "Range-bound":
            return EvaluationResult.neutral()
        return EvaluationResult(p.get("score_directional_conflict", -0.85), 1.0,
                                "Weekly trend Uptrend — opposes bearish bias")

    return EvaluationResult.neutral()


def _eval_adx(preference: str, market: dict) -> EvaluationResult:
    adx = market.get("adx")
    if adx is None:
        return EvaluationResult.missing()
    p = _p("adx")

    very_strong = p.get("very_strong",  30.0)
    strong      = p.get("strong",       25.0)
    neutral_lo  = p.get("neutral_lo",   20.0)
    very_weak   = p.get("very_weak",    15.0)

    s_vs  = p.get("score_very_strong",  1.0)
    s_str = p.get("score_strong",       0.60)
    s_opp = p.get("score_opposing",    -0.85)
    s_pen = p.get("score_mild_penalty", -0.30)

    if preference == "strong":
        if adx > very_strong:
            return EvaluationResult(s_vs,  1.0, f"ADX {adx:.0f} very strong trend — excellent for directional trade")
        if adx > strong:
            return EvaluationResult(s_str, 1.0, f"ADX {adx:.0f} trending — supports directional trade")
        if adx > neutral_lo:
            return EvaluationResult(0.0,   0.5, "")   # transitional — some trend, not convincing
        return EvaluationResult(s_opp, 1.0, f"ADX {adx:.0f} choppy — weak case for directional trade")

    if preference == "weak":
        if adx < very_weak:
            return EvaluationResult(s_vs,  1.0, f"ADX {adx:.0f} very low — minimal trend risk for range/premium trade")
        if adx < neutral_lo:
            return EvaluationResult(s_str, 1.0, f"ADX {adx:.0f} subdued — supports range-bound environment")
        if adx < strong:
            return EvaluationResult(0.0,   0.5, "")   # transitional
        return EvaluationResult(s_opp, 1.0, f"ADX {adx:.0f} trending — breakout risk for range/premium trade")

    if preference == "moderate":
        mod_lo     = p.get("moderate_lo",         18.0)
        mod_hi     = p.get("moderate_hi",         28.0)
        too_strong = p.get("moderate_too_strong", 35.0)
        too_quiet  = p.get("moderate_too_quiet",  15.0)
        s_ideal    = p.get("score_moderate_ideal", 0.85)
        if mod_lo <= adx <= mod_hi:
            return EvaluationResult(s_ideal, 1.0,
                                    f"ADX {adx:.0f} moderate — mild trend supports steady drift, low breakout risk")
        if adx > too_strong:
            return EvaluationResult(s_pen, 1.0,
                                    f"ADX {adx:.0f} strong — elevated breakout risk for short-strike trade")
        if adx < too_quiet:
            return EvaluationResult(s_pen, 1.0,
                                    f"ADX {adx:.0f} very quiet — low trend conviction")
        return EvaluationResult(0.0, 0.5, "")   # within acceptable but not ideal range

    if preference == "expanding":
        slope      = market.get("adx_slope")
        coil_hi    = p.get("expanding_coil_hi",      25.0)
        fb_lo      = p.get("expanding_fallback_lo",   15.0)
        min_slope  = p.get("minimum_slope",            1.0)
        s_full     = p.get("score_expanding_full",     1.0)
        s_part     = p.get("score_expanding_partial",  0.60)
        s_coil     = p.get("score_fallback_coil",      0.60)

        if slope is None:
            # No slope data — use ADX level as a coiling proxy
            if adx < fb_lo:
                return EvaluationResult(s_coil, 0.35,
                                        f"ADX {adx:.0f} very low — possible coiling setup (no slope data)")
            return EvaluationResult(0.0, 0.35, "")

        if slope >= min_slope and adx < coil_hi:
            return EvaluationResult(s_full, 1.0,
                                    f"ADX {adx:.0f} rising +{slope:.1f}/bar from low base — breakout coil signal")
        if slope >= min_slope:
            return EvaluationResult(s_part, 0.7,
                                    f"ADX {adx:.0f} expanding +{slope:.1f}/bar — directional move in progress")
        return EvaluationResult.neutral()   # slope positive but below minimum_slope threshold

    return EvaluationResult.neutral()


def _eval_rsi(preference: str, market: dict) -> EvaluationResult:
    rsi = market.get("rsi")
    if rsi is None:
        return EvaluationResult.missing()
    p = _p("rsi")

    overbought  = p.get("overbought",  70.0)
    upper_bull  = p.get("upper_bull",  70.0)
    lower_bull  = p.get("lower_bull",  50.0)
    upper_weak  = p.get("upper_weak",  40.0)
    oversold    = p.get("oversold",    30.0)
    upper_bear  = p.get("upper_bear",  50.0)
    lower_bear  = p.get("lower_bear",  30.0)
    strong_bear = p.get("strong_bear", 60.0)
    neutral_hi  = p.get("neutral_hi",  60.0)
    neutral_lo  = p.get("neutral_lo",  40.0)
    extended_hi = p.get("extended_hi", 70.0)
    extended_lo = p.get("extended_lo", 30.0)

    s_h = p.get("score_healthy",  0.85)
    s_e = p.get("score_extended", -0.85)
    s_w = p.get("score_weak",     0.60)   # magnitude; sign applied below

    if preference == "bullish":
        if lower_bull <= rsi <= upper_bull:
            return EvaluationResult(s_h,  1.0, f"RSI {rsi:.0f} healthy bullish momentum")
        if rsi > overbought:
            return EvaluationResult(s_e,  1.0, f"RSI {rsi:.0f} overbought — pullback risk")
        if rsi < upper_weak:
            return EvaluationResult(-s_w, 1.0, f"RSI {rsi:.0f} weak — poor bullish momentum")
        return EvaluationResult(0.0,  0.5, "")   # transitional zone

    if preference == "bearish":
        if lower_bear <= rsi <= upper_bear:
            return EvaluationResult(s_h,  1.0, f"RSI {rsi:.0f} healthy bearish momentum")
        if rsi < oversold:
            return EvaluationResult(s_e,  1.0, f"RSI {rsi:.0f} oversold — bounce risk")
        if rsi > strong_bear:
            return EvaluationResult(-s_w, 1.0, f"RSI {rsi:.0f} elevated — poor bearish momentum")
        return EvaluationResult(0.0,  0.5, "")   # transitional zone

    if preference == "neutral":
        if neutral_lo <= rsi <= neutral_hi:
            return EvaluationResult(s_h,  1.0, f"RSI {rsi:.0f} neutral — consistent with range-bound environment")
        if rsi > extended_hi or rsi < extended_lo:
            return EvaluationResult(s_e,  1.0, f"RSI {rsi:.0f} extended — breakout/breakdown risk for neutral trade")
        return EvaluationResult(0.0,  0.5, "")

    if preference == "not_overbought":
        if rsi >= overbought:
            return EvaluationResult(s_e,       1.0, f"RSI {rsi:.0f} overbought — momentum exhaustion risk")
        if rsi >= strong_bear:
            return EvaluationResult(0.30,      0.7, f"RSI {rsi:.0f} elevated but not overbought")
        return EvaluationResult(s_h * 0.7,  0.7, f"RSI {rsi:.0f} — not overbought, entry conditions acceptable")

    if preference == "not_oversold":
        if rsi <= oversold:
            return EvaluationResult(s_e,       1.0, f"RSI {rsi:.0f} oversold — bounce risk")
        if rsi <= upper_weak:
            return EvaluationResult(0.30,      0.7, f"RSI {rsi:.0f} low but not oversold")
        return EvaluationResult(s_h * 0.7,  0.7, f"RSI {rsi:.0f} — not oversold, entry conditions acceptable")

    return EvaluationResult.neutral()


def _eval_macd(preference: str, market: dict) -> EvaluationResult:
    mt = market.get("macd_trend")
    if not mt or mt == "N/A":
        return EvaluationResult.missing()
    p   = _p("macd")
    sc  = p.get("score_confirm",  0.85)
    scx = p.get("score_conflict", -0.85)

    if preference == "bullish":
        if mt == "Bullish":
            return EvaluationResult(sc,  1.0, "MACD Bullish — momentum confirms upside bias")
        return EvaluationResult(scx, 1.0, "MACD Bearish — momentum conflicts with upside bias")
    if preference == "bearish":
        if mt == "Bearish":
            return EvaluationResult(sc,  1.0, "MACD Bearish — momentum confirms downside bias")
        return EvaluationResult(scx, 1.0, "MACD Bullish — momentum conflicts with downside bias")
    if preference == "neutral":
        return EvaluationResult.neutral()
    return EvaluationResult.neutral()


def _eval_iv_premium(preference: str, market: dict) -> EvaluationResult:
    iv = market.get("iv_premium")
    if iv is None:
        return EvaluationResult.missing()
    p = _p("iv_premium")
    sp    = p.get("strong_premium",  0.03)
    sd    = p.get("strong_discount", -0.03)
    s_se  = p.get("score_strong_edge",  0.85)
    s_sle = p.get("score_slight_edge",  0.30)
    s_sld = p.get("score_slight_drag", -0.30)
    s_std = p.get("score_strong_drag", -0.85)

    if preference == "high":   # credit/selling — wants rich IV
        if iv > sp:
            return EvaluationResult(s_se,  1.0, f"IV premium {iv*100:+.1f}% over HV20 — selling rich options adds edge")
        if iv > 0:
            return EvaluationResult(s_sle, 0.7, f"IV slight premium {iv*100:+.1f}% — modest credit-selling edge")
        if iv < sd:
            return EvaluationResult(s_std, 1.0, f"IV discount {iv*100:+.1f}% vs HV20 — selling cheap options reduces edge")
        return EvaluationResult(s_sld, 0.7, f"IV below HV20 {iv*100:+.1f}% — option premiums compressed")

    if preference == "low":    # debit/buying — wants cheap IV
        if iv < sd:
            return EvaluationResult(s_se,  1.0, f"IV discount {iv*100:+.1f}% vs HV20 — buying cheap options adds edge")
        if iv < 0:
            return EvaluationResult(s_sle, 0.7, f"IV slight discount {iv*100:+.1f}% — modest debit-buying edge")
        if iv > sp:
            return EvaluationResult(s_std, 1.0, f"IV premium {iv*100:+.1f}% over HV20 — buying expensive options reduces edge")
        return EvaluationResult(s_sld, 0.7, f"IV slightly elevated {iv*100:+.1f}% — entry cost marginally high")

    return EvaluationResult.neutral()


def _eval_iv_term(preference: str, market: dict) -> EvaluationResult:
    # term_slope = front_iv - back_iv (canonical continuous value from get_iv_term_structure)
    # positive → Backwardation (near-term fear premium), negative → Contango (normal curve)
    slope = market.get("term_slope")
    if slope is None:
        return EvaluationResult.missing()
    p   = _p("iv_term")
    sc  = p.get("score_prefer_confirm",  0.85)
    scx = p.get("score_prefer_conflict", 0.60)
    ss  = p.get("score_flat_slight",     0.30)
    thr = p.get("flat_threshold",        0.02)   # |slope| < thr → Flat (same as data_fetch threshold)

    if slope > thr:
        shape = "Backwardation"
    elif slope < -thr:
        shape = "Contango"
    else:
        shape = "Flat"

    _sl = f"slope {slope:+.3f}"

    if preference == "backwardation":
        if shape == "Backwardation":
            return EvaluationResult(sc,   1.0, f"IV backwardation ({_sl}) — selling richer near-term vol adds edge to time spread")
        if shape == "Flat":
            return EvaluationResult.neutral()
        return EvaluationResult(-scx, 1.0, f"IV contango ({_sl}) — no near-term vol premium; time spread edge is reduced")

    if preference == "contango":
        if shape == "Contango":
            return EvaluationResult(sc,   1.0, f"IV contango ({_sl}) — back-month vol cheap; favors buying more back-month options")
        if shape == "Flat":
            return EvaluationResult.neutral()
        return EvaluationResult(-scx, 1.0, f"IV backwardation ({_sl}) — back-month vol elevated; long back-month options cost more")

    if preference == "flat":
        if shape == "Flat":
            return EvaluationResult(sc,  1.0, f"Flat IV term structure ({_sl}) — neutral vol edge, no calendar pressure")
        return EvaluationResult(-ss, 0.7, f"IV term {shape.lower()} ({_sl}) — mild deviation from flat adds one-sided vol pressure")

    return EvaluationResult.neutral()


def _eval_vol_skew(preference: str, market: dict) -> EvaluationResult:
    skew = market.get("vol_skew_pct")
    if skew is None:
        return EvaluationResult.missing()
    p = _p("vol_skew")
    sp    = p.get("strong_put",  5.0)
    ssp   = p.get("slight_put",  2.0)
    ssc   = p.get("slight_call", -2.0)
    sc    = p.get("strong_call", -5.0)
    s_str = p.get("score_strong",   0.85)
    s_slt = p.get("score_slight",   0.30)
    s_bal = p.get("score_balanced", 0.60)
    s_opp = p.get("score_opposing", 0.60)   # magnitude; negated below

    if preference == "put_heavy":
        if skew > sp:
            return EvaluationResult(s_str, 1.0, f"Vol skew +{skew:.1f}% put-heavy — rich puts support the trade")
        if skew > ssp:
            return EvaluationResult(s_slt, 0.7, f"Vol skew +{skew:.1f}% — slight put premium, mild edge")
        if skew < ssc:
            return EvaluationResult(-s_opp, 1.0, f"Vol skew {skew:.1f}% call-heavy — puts relatively cheap, reduces edge")
        return EvaluationResult.neutral()

    if preference == "call_heavy":
        if skew < sc:
            return EvaluationResult(s_str, 1.0, f"Vol skew {skew:.1f}% call-heavy — rich calls support the trade")
        if skew < ssc:
            return EvaluationResult(s_slt, 0.7, f"Vol skew {skew:.1f}% — slight call premium, mild edge")
        if skew > ssp:
            return EvaluationResult(-s_opp, 1.0, f"Vol skew +{skew:.1f}% put-heavy — calls relatively cheap, reduces edge")
        return EvaluationResult.neutral()

    if preference == "balanced":
        if ssc <= skew <= ssp:
            return EvaluationResult(s_bal, 1.0, f"Vol skew {skew:.1f}% balanced — symmetric risk for neutral trade")
        if abs(skew) > sp:
            return EvaluationResult(-s_opp, 1.0,
                                    f"Vol skew {skew:.1f}% — strong directional skew adds one-sided breakout risk")
        return EvaluationResult.neutral()

    return EvaluationResult.neutral()


def _eval_news(preference: str, market: dict) -> EvaluationResult:
    ns = market.get("news_sentiment")
    if not ns or ns == "N/A":
        return EvaluationResult.missing()
    p   = _p("news")
    s_m  = p.get("score_direction_match",    0.85)
    s_c  = p.get("score_direction_conflict", -0.85)
    s_mx = p.get("score_mixed_directional",  -0.30)
    s_qn = p.get("score_quiet_neutral",       0.85)
    s_mn = p.get("score_mixed_neutral",       0.30)
    s_dr = p.get("score_directional_risk",    0.60)   # magnitude; negated below
    s_cm = p.get("score_catalyst_major",      0.85)
    s_cp = p.get("score_catalyst_moderate",   0.60)
    s_cq = p.get("score_catalyst_quiet",      0.60)   # magnitude; negated below

    if preference == "bullish":
        if ns == "Bullish": return EvaluationResult(s_m,  1.0, "News sentiment Bullish — supports upside trade")
        if ns == "Bearish": return EvaluationResult(s_c,  1.0, "News sentiment Bearish — headwind for upside trade")
        if ns == "Mixed":   return EvaluationResult(s_mx, 0.5, "Mixed news — some uncertainty for bullish trade")
        return EvaluationResult.neutral()

    if preference == "bearish":
        if ns == "Bearish": return EvaluationResult(s_m,  1.0, "News sentiment Bearish — supports downside trade")
        if ns == "Bullish": return EvaluationResult(s_c,  1.0, "News sentiment Bullish — headwind for downside trade")
        if ns == "Mixed":   return EvaluationResult(s_mx, 0.5, "Mixed news — some uncertainty for bearish trade")
        return EvaluationResult.neutral()

    if preference == "quiet":
        if ns == "Neutral": return EvaluationResult(s_qn,  1.0, "News quiet/neutral — low event risk, supports premium collection")
        if ns == "Mixed":   return EvaluationResult(s_mn,  0.5, "Mixed news — modest background noise for range trade")
        return EvaluationResult(-s_dr, 1.0, f"News {ns} — directional catalyst risk for range/neutral trade")

    if preference == "major_event":
        if ns == "Mixed":                   return EvaluationResult(s_cm, 1.0, "Mixed/conflicting news — catalyst potential supports vol expansion play")
        if ns in ("Bullish", "Bearish"):    return EvaluationResult(s_cp, 0.7, f"News {ns} — directional catalyst may drive vol expansion")
        return EvaluationResult(-s_cq, 1.0, "News quiet — low catalyst risk; vol expansion trade less compelling")

    return EvaluationResult.neutral()


def _eval_pcr(preference: str, market: dict) -> EvaluationResult:
    pcr_s = market.get("pcr_sentiment")
    pcr   = market.get("pcr")
    if not pcr_s or pcr_s == "N/A":
        return EvaluationResult.missing()
    p    = _p("pcr")
    sc    = p.get("score_confirm",      0.85)
    scx   = p.get("score_conflict",    -0.85)
    s_ng  = p.get("score_neutral_good",  0.60)
    s_sr  = p.get("score_skew_risk",     0.30)   # magnitude; negated below
    tag   = f"PCR {pcr:.2f}" if pcr is not None else "PCR"

    if preference == "bullish":
        if pcr_s == "Bullish": return EvaluationResult(sc,   1.0, f"{tag} call-heavy OI — confirms upside bias")
        if pcr_s == "Bearish": return EvaluationResult(scx,  1.0, f"{tag} put-heavy OI — headwind for upside trade")
        return EvaluationResult.neutral()
    if preference == "bearish":
        if pcr_s == "Bearish": return EvaluationResult(sc,   1.0, f"{tag} put-heavy OI — confirms downside bias")
        if pcr_s == "Bullish": return EvaluationResult(scx,  1.0, f"{tag} call-heavy OI — headwind for downside trade")
        return EvaluationResult.neutral()
    if preference == "neutral":
        if pcr_s == "Neutral": return EvaluationResult(s_ng, 1.0, f"{tag} balanced OI — consistent with range-bound expectation")
        return EvaluationResult(-s_sr, 1.0, f"{tag} {pcr_s.lower()} OI skew — directional positioning adds breakout risk")
    return EvaluationResult.neutral()


def _eval_rel_volume(preference: str, market: dict) -> EvaluationResult:
    rv = market.get("rel_volume")
    if rv is None:
        return EvaluationResult.missing()
    p    = _p("rel_volume")
    high = p.get("high",       1.5)
    s_h  = p.get("score_high", 0.85)

    if preference == "high":
        if rv > high:
            return EvaluationResult(s_h, 1.0, f"Rel volume {rv:.1f}x — elevated volume confirms the move")
        # Low volume: absence of confirmation, not active contradiction
        return EvaluationResult.neutral()

    return EvaluationResult.neutral()


def _eval_analyst(preference: str, market: dict) -> EvaluationResult:
    al = market.get("analyst_label")
    if not al or al == "N/A":
        return EvaluationResult.missing()
    p   = _p("analyst")
    sc   = p.get("score_confirm",        0.85)
    scx  = p.get("score_conflict",      -0.85)
    s_ng = p.get("score_neutral_good",   0.30)
    s_dr = p.get("score_directional_risk", 0.30)   # magnitude; negated below

    if preference == "bullish":
        if al == "Bullish": return EvaluationResult(sc,    1.0, "Analyst consensus Bullish — supports upside trade")
        if al == "Bearish": return EvaluationResult(scx,   1.0, "Analyst consensus Bearish — headwind for upside trade")
        return EvaluationResult.neutral()
    if preference == "bearish":
        if al == "Bearish": return EvaluationResult(sc,    1.0, "Analyst consensus Bearish — confirms downside trade")
        if al == "Bullish": return EvaluationResult(scx,   1.0, "Analyst consensus Bullish — headwind for bearish trade")
        return EvaluationResult.neutral()
    if preference == "neutral":
        if al == "Neutral": return EvaluationResult(s_ng,  1.0, "Analyst consensus Neutral — no directional analyst pressure")
        return EvaluationResult(-s_dr, 1.0, f"Analyst consensus {al} — directional analyst view adds noise for neutral trade")
    return EvaluationResult.neutral()


def _eval_short_interest(preference: str, market: dict) -> EvaluationResult:
    si = market.get("short_interest")
    if si is None:
        return EvaluationResult.missing()
    p  = _p("short_interest")
    hs  = p.get("high_squeeze", 20.0)
    mod = p.get("moderate",     10.0)
    lr  = p.get("low_risk",      5.0)
    mn  = p.get("minimal",       3.0)
    s_s  = p.get("score_strong",   0.85)
    s_m  = p.get("score_moderate", 0.30)
    s_sl = p.get("score_slight",   0.30)   # magnitude; negated below
    s_o  = p.get("score_opposing", 0.60)   # magnitude; negated below

    if preference in ("bullish", "high"):
        if si > hs:  return EvaluationResult(s_s,  1.0, f"Short interest {si:.1f}% — high short float, squeeze risk favors upside")
        if si > mod: return EvaluationResult(s_m,  0.7, f"Short interest {si:.1f}% — moderate squeeze potential")
        if si < mn:  return EvaluationResult(-s_sl, 0.7, f"Short interest {si:.1f}% — minimal squeeze risk, limited upside catalyst")
        return EvaluationResult.neutral()

    if preference in ("bearish", "low"):
        if si < mn:  return EvaluationResult(s_s,  1.0, f"Short interest {si:.1f}% — low short float, steady downside supported")
        if si < lr:  return EvaluationResult(s_m,  0.7, f"Short interest {si:.1f}% — limited squeeze risk")
        if si > hs:  return EvaluationResult(-s_o, 1.0, f"Short interest {si:.1f}% — high squeeze risk is headwind for bearish trade")
        return EvaluationResult.neutral()

    return EvaluationResult.neutral()


def _eval_atr(preference: str, market: dict) -> EvaluationResult:
    """ATR as % of spot — measures intraday volatility / trend strength."""
    atr = market.get("atr_pct")
    if atr is None:
        return EvaluationResult.missing()
    p = _p("atr")
    low_hi   = p.get("low_hi",    1.0)   # below → quiet market
    mod_lo   = p.get("mod_lo",    1.0)
    mod_hi   = p.get("mod_hi",    2.5)
    high_lo  = p.get("high_lo",   2.5)
    extreme  = p.get("extreme",   5.0)
    s_s      = p.get("score_strong",   0.85)
    s_m      = p.get("score_moderate", 0.60)
    s_e      = p.get("score_extreme", -0.85)

    if preference == "low":
        if atr < low_hi:
            return EvaluationResult(s_s, 1.0, f"ATR {atr:.1f}% — low volatility, ideal for premium collection")
        if atr < mod_hi:
            return EvaluationResult(s_m, 0.7, f"ATR {atr:.1f}% — moderate volatility, acceptable for premium trade")
        return EvaluationResult(-s_m, 1.0, f"ATR {atr:.1f}% — elevated volatility adds risk to premium collection")

    if preference == "high":
        if atr > extreme:
            return EvaluationResult(s_e, 1.0, f"ATR {atr:.1f}% — extreme volatility, assignment / stop-out risk")
        if atr >= high_lo:
            return EvaluationResult(s_s, 1.0, f"ATR {atr:.1f}% — elevated ATR, strong directional environment")
        if atr >= mod_lo:
            return EvaluationResult(s_m, 0.7, f"ATR {atr:.1f}% — moderate ATR, some trend support")
        return EvaluationResult(-s_m, 1.0, f"ATR {atr:.1f}% — too quiet for a directional trade")

    if preference == "moderate":
        if mod_lo <= atr <= mod_hi:
            return EvaluationResult(s_s, 1.0, f"ATR {atr:.1f}% — moderate, balanced risk environment")
        if atr > extreme:
            return EvaluationResult(s_e, 1.0, f"ATR {atr:.1f}% — extreme volatility, spread risk elevated")
        return EvaluationResult(s_m * 0.5, 0.5, "")

    return EvaluationResult.neutral()


def _eval_hv30(preference: str, market: dict) -> EvaluationResult:
    """30-day realised volatility — longer-window vol regime signal."""
    hv30 = market.get("hv30")
    if hv30 is None:
        return EvaluationResult.missing()
    p = _p("hv30")
    low_hi  = p.get("low_hi",   0.15)
    high_lo = p.get("high_lo",  0.30)
    s_s     = p.get("score_strong",   0.85)
    s_m     = p.get("score_moderate", 0.60)

    if preference == "low":
        if hv30 < low_hi:
            return EvaluationResult(s_s, 1.0, f"HV30 {hv30*100:.0f}% — low realised vol; premium collection environment")
        if hv30 < high_lo:
            return EvaluationResult(s_m, 0.7, f"HV30 {hv30*100:.0f}% — moderate realised vol, acceptable")
        return EvaluationResult(-s_m, 1.0, f"HV30 {hv30*100:.0f}% — high realised vol; premium-collection edge reduced")

    if preference == "high":
        if hv30 >= high_lo:
            return EvaluationResult(s_s, 1.0, f"HV30 {hv30*100:.0f}% — elevated realised vol supports debit/long-vol trade")
        if hv30 >= low_hi:
            return EvaluationResult(s_m, 0.7, f"HV30 {hv30*100:.0f}% — moderate realised vol, some support")
        return EvaluationResult(-s_m, 1.0, f"HV30 {hv30*100:.0f}% — low realised vol; debit-buying edge reduced")

    return EvaluationResult.neutral()


def _eval_iv_percentile(preference: str, market: dict) -> EvaluationResult:
    """IV rank vs 52-week range — absolute vol level context."""
    ivr = market.get("iv_rank_52w")
    if ivr is None:
        return EvaluationResult.missing()
    p = _p("iv_percentile")
    high_lo  = p.get("high_lo",   0.60)
    high_top = p.get("high_top",  0.80)
    low_hi   = p.get("low_hi",    0.30)
    mod_lo   = p.get("mod_lo",    0.30)
    mod_hi   = p.get("mod_hi",    0.60)
    s_s      = p.get("score_strong",   0.85)
    s_m      = p.get("score_moderate", 0.60)
    s_e      = p.get("score_extreme", -0.30)

    if preference == "high":
        if ivr >= high_top:
            return EvaluationResult(s_e, 0.7, f"IV rank {ivr*100:.0f}% — extremely elevated; mean-reversion risk for seller")
        if ivr >= high_lo:
            return EvaluationResult(s_s, 1.0, f"IV rank {ivr*100:.0f}% — high relative to 52-week range; rich premium environment")
        if ivr >= mod_lo:
            return EvaluationResult(s_m, 0.7, f"IV rank {ivr*100:.0f}% — moderate IV rank, some premium support")
        return EvaluationResult(-s_m, 1.0, f"IV rank {ivr*100:.0f}% — low; selling premium in cheap IV environment")

    if preference == "low":
        if ivr <= low_hi:
            return EvaluationResult(s_s, 1.0, f"IV rank {ivr*100:.0f}% — low; cheap options support debit/long-vol entry")
        if ivr <= mod_hi:
            return EvaluationResult(s_m, 0.7, f"IV rank {ivr*100:.0f}% — moderate, acceptable for debit trade")
        return EvaluationResult(-s_m, 1.0, f"IV rank {ivr*100:.0f}% — elevated; buying expensive options reduces edge")

    if preference == "moderate":
        if mod_lo <= ivr <= mod_hi:
            return EvaluationResult(s_s, 1.0, f"IV rank {ivr*100:.0f}% — moderate, balanced premium environment")
        return EvaluationResult(0.0, 0.5, "")

    return EvaluationResult.neutral()


def _eval_earnings_dte(preference: str, market: dict) -> EvaluationResult:
    """Days to next earnings — event risk proximity signal."""
    ed = market.get("earnings_dte")
    p  = _p("earnings_dte")

    danger_lo = p.get("danger_lo",  3)
    danger_hi = p.get("danger_hi", 10)
    near_hi   = p.get("near_hi",   21)
    safe_lo   = p.get("safe_lo",   14)
    s_safe    = p.get("score_safe",    0.60)
    s_warn    = p.get("score_warning", 0.30)
    s_danger  = p.get("score_danger", -0.85)

    if ed is None:
        return EvaluationResult(s_warn, 0.35, "Earnings date unknown — event risk unquantified")

    if preference == "safe":
        if ed < danger_lo:
            return EvaluationResult(s_danger, 1.0, f"Earnings in {ed}d — inside event window, premium spike risk")
        if ed <= danger_hi:
            return EvaluationResult(s_danger * 0.5, 1.0, f"Earnings in {ed}d — approaching, elevated pin/gap risk")
        if ed <= near_hi:
            return EvaluationResult(s_warn, 0.7, f"Earnings in {ed}d — near horizon, some event risk")
        return EvaluationResult(s_safe, 1.0, f"Earnings in {ed}d — clear of event window")

    if preference == "near":
        if ed <= danger_hi:
            return EvaluationResult(s_safe, 1.0, f"Earnings in {ed}d — upcoming catalyst, IV expansion likely")
        return EvaluationResult(-s_warn, 0.7, f"Earnings in {ed}d — too far out for catalyst-driven trade")

    return EvaluationResult.neutral()


def _eval_div_dte(preference: str, market: dict) -> EvaluationResult:
    """Days to ex-dividend date — assignment / early-exercise risk for short calls."""
    dd = market.get("div_days_to_ex")
    p  = _p("div_dte")
    danger_hi = p.get("danger_hi",  7)
    warn_hi   = p.get("warn_hi",   21)
    s_safe    = p.get("score_safe",    0.60)
    s_warn    = p.get("score_warning", 0.30)
    s_danger  = p.get("score_danger", -0.85)

    if dd is None:
        return EvaluationResult(s_safe, 1.0, "No dividend — no assignment risk from ex-date")

    if preference == "safe":
        if dd <= danger_hi:
            return EvaluationResult(s_danger, 1.0, f"Ex-div in {dd}d — early assignment risk on short calls")
        if dd <= warn_hi:
            return EvaluationResult(s_warn, 0.7, f"Ex-div in {dd}d — approaching; monitor short call legs")
        return EvaluationResult(s_safe, 1.0, f"Ex-div in {dd}d — well clear of assignment window")

    return EvaluationResult.neutral()


def _eval_max_pain(preference: str, market: dict) -> EvaluationResult:
    """Max pain distance from spot — pin/gravity signal heading into expiry.

    market field: max_pain_distance_pct  → (max_pain - spot) / spot × 100
    Positive = max pain above spot; negative = below spot.

    Preferences:
      "above"   — max pain above spot (favours calls, IC/credit call side)
      "below"   — max pain below spot (favours puts, IC/credit put side)
      "neutral" — max pain close to spot (pinning; good for short-premium structures)
    """
    dist = market.get("max_pain_distance_pct")
    if dist is None:
        return EvaluationResult.missing()
    p         = _p("max_pain")
    near_band = p.get("near_band",   3.0)   # ±% considered "near" (pinning)
    s_s       = p.get("score_strong",   0.70)
    s_m       = p.get("score_moderate", 0.40)

    if preference == "neutral":
        if abs(dist) <= near_band:
            return EvaluationResult(s_s, 1.0, f"Max pain {dist:+.1f}% from spot — near ATM, pinning likely")
        return EvaluationResult(-s_m, 0.7, f"Max pain {dist:+.1f}% from spot — off-centre, less pinning")

    if preference == "above":
        if dist > near_band:
            return EvaluationResult(s_s, 1.0, f"Max pain {dist:+.1f}% above spot — upward gravity into expiry")
        if dist >= 0:
            return EvaluationResult(s_m, 0.6, f"Max pain {dist:+.1f}% above spot — mild upward bias")
        return EvaluationResult(-s_m, 0.7, f"Max pain {dist:+.1f}% below spot — gravity works against upside")

    if preference == "below":
        if dist < -near_band:
            return EvaluationResult(s_s, 1.0, f"Max pain {dist:+.1f}% below spot — downward gravity into expiry")
        if dist <= 0:
            return EvaluationResult(s_m, 0.6, f"Max pain {dist:+.1f}% below spot — mild downward bias")
        return EvaluationResult(-s_m, 0.7, f"Max pain {dist:+.1f}% above spot — gravity works against downside")

    return EvaluationResult.neutral()


def _eval_oi_concentration(preference: str, market: dict) -> EvaluationResult:
    """OI concentration within ±10% of spot — pinning / liquidity signal.

    market field: oi_concentration  → fraction 0–1 (e.g. 0.65 = 65% of chain OI near ATM)

    High concentration → most OI is clustered ATM → pinning pressure → range-bound.
    Low concentration  → OI spread across many strikes → less anchoring, larger moves.

    Preferences:
      "high"  — want pinning (credit structures: IC, IBF, short straddle)
      "low"   — want dispersion (debit structures, long vol)
    """
    oi_c = market.get("oi_concentration")
    if oi_c is None:
        return EvaluationResult.missing()
    p       = _p("oi_concentration")
    high_lo = p.get("high_lo", 0.50)
    low_hi  = p.get("low_hi",  0.30)
    s_s     = p.get("score_strong",   0.70)
    s_m     = p.get("score_moderate", 0.40)

    if preference == "high":
        if oi_c >= high_lo:
            return EvaluationResult(s_s, 1.0, f"OI concentration {oi_c*100:.0f}% near ATM — pinning pressure supports range trade")
        if oi_c >= low_hi:
            return EvaluationResult(s_m, 0.6, f"OI concentration {oi_c*100:.0f}% — moderate clustering, some anchor")
        return EvaluationResult(-s_m, 0.7, f"OI concentration {oi_c*100:.0f}% — dispersed OI, breakout risk elevated")

    if preference == "low":
        if oi_c <= low_hi:
            return EvaluationResult(s_s, 1.0, f"OI concentration {oi_c*100:.0f}% — OI spread out, less pinning, supports directional move")
        if oi_c <= high_lo:
            return EvaluationResult(s_m, 0.6, f"OI concentration {oi_c*100:.0f}% — moderate dispersion, some room to move")
        return EvaluationResult(-s_m, 0.7, f"OI concentration {oi_c*100:.0f}% — concentrated near ATM, pin risk for debit")

    return EvaluationResult.neutral()


def _eval_iv_change_5d(preference: str, market: dict) -> EvaluationResult:
    """5-day ATM IV change — vol trend / direction signal.

    market field: iv_change_5d → current_atm_iv - past_atm_iv (annualised fraction)
    Positive = IV rising; negative = IV compressing.

    Preferences:
      "falling"  — want IV compression (credit: lock in rich premium that will contract)
      "rising"   — want IV expansion (debit/long-vol: buy before vol spike)
      "stable"   — want flat IV (no surprise regime shift during the trade)
    """
    chg = market.get("iv_change_5d")
    if chg is None:
        return EvaluationResult.missing()
    p          = _p("iv_change_5d")
    rise_lo    = p.get("rise_lo",     0.02)   # >+2pp = meaningfully rising
    fall_hi    = p.get("fall_hi",    -0.02)   # <-2pp = meaningfully falling
    stable_abs = p.get("stable_abs",  0.02)   # ±2pp band = stable
    s_s        = p.get("score_strong",   0.75)
    s_m        = p.get("score_moderate", 0.40)

    chg_pp = chg * 100   # convert to percentage points for messages

    if preference == "falling":
        if chg <= fall_hi:
            return EvaluationResult(s_s, 1.0, f"IV down {abs(chg_pp):.1f}pp over 5d — compressing; credit entry window")
        if chg < rise_lo:
            return EvaluationResult(s_m, 0.6, f"IV change {chg_pp:+.1f}pp — roughly flat, acceptable for credit")
        return EvaluationResult(-s_m, 0.8, f"IV rising {chg_pp:+.1f}pp over 5d — selling into expanding vol; margin at risk")

    if preference == "rising":
        if chg >= rise_lo:
            return EvaluationResult(s_s, 1.0, f"IV up {chg_pp:+.1f}pp over 5d — expanding; debit/long-vol entry window")
        if chg > fall_hi:
            return EvaluationResult(s_m, 0.6, f"IV change {chg_pp:+.1f}pp — flat; some support for long-vol")
        return EvaluationResult(-s_m, 0.8, f"IV down {abs(chg_pp):.1f}pp over 5d — compressing into long-vol trade")

    if preference == "stable":
        if abs(chg) <= stable_abs:
            return EvaluationResult(s_s, 1.0, f"IV change {chg_pp:+.1f}pp — stable vol regime")
        return EvaluationResult(-s_m * 0.5, 0.6, f"IV {chg_pp:+.1f}pp shift over 5d — regime in motion")

    return EvaluationResult.neutral()


def _eval_unusual_activity(preference: str, market: dict) -> EvaluationResult:
    """Per-strike OI spike detection — unusual positioning vs rolling average.

    market field: unusual_activity → max OI spike ratio for near-ATM strikes (float ≥ 0)
    0 = OI at or below average (quiet); 1.0 = OI doubled; 2.0 = tripled.
    Returns None until min_history (3) days of oi_changes have accumulated.

    Preferences:
      "detected" — want spike (confirms smart-money positioning / vol catalyst)
      "quiet"    — want no spike (clean setup; no directional bets placed against the trade)
    """
    spike = market.get("unusual_activity")
    if spike is None:
        return EvaluationResult.missing()
    p     = _p("unusual_activity")
    hi_lo = p.get("spike_hi",      1.0)   # ratio ≥ hi_lo = strong spike
    lo_hi = p.get("spike_lo",      0.25)  # ratio < lo_hi = quiet
    s_s   = p.get("score_strong",  0.70)
    s_m   = p.get("score_moderate", 0.40)

    if preference == "detected":
        if spike >= hi_lo:
            return EvaluationResult(s_s, 1.0, f"OI {spike:.1f}× above avg — unusual positioning detected")
        if spike >= lo_hi:
            return EvaluationResult(s_m, 0.7, f"OI +{spike*100:.0f}% above avg — moderate activity uptick")
        return EvaluationResult(-s_m * 0.5, 0.8, f"OI near avg (+{spike*100:.0f}%) — no unusual activity")

    if preference == "quiet":
        if spike < lo_hi:
            return EvaluationResult(s_s, 1.0, f"OI near avg (+{spike*100:.0f}%) — clean setup, no unusual positioning")
        if spike < hi_lo:
            return EvaluationResult(s_m * 0.3, 0.7, f"OI +{spike*100:.0f}% above avg — some unusual activity; proceed with caution")
        return EvaluationResult(-s_m, 0.8, f"OI spike {spike:.1f}× — heavy unusual positioning against clean setup")

    return EvaluationResult.neutral()


def _eval_iv_hv_ratio(preference: str, market: dict) -> EvaluationResult:
    """IV/HV ratio — relative richness of implied vs realised vol.

    market field: iv_hv_ratio → ATM IV / 20-day HV (e.g. 1.3 = IV 30% above HV)
    Orthogonal to iv_premium (absolute spread): two stocks with the same 4pp premium
    can have very different ratios depending on their base vol level.

    Preferences:
      "high"     — IV rich vs HV (selling edge; favour credit structures)
      "low"      — IV cheap vs HV (buying edge; favour debit/long-vol structures)
      "moderate" — IV near HV (ratio near 1.0; favour balanced structures)
    """
    ratio = market.get("iv_hv_ratio")
    if ratio is None:
        return EvaluationResult.missing()
    p        = _p("iv_hv_ratio")
    hi_thr   = p.get("high_threshold",     1.20)
    lo_thr   = p.get("low_threshold",      0.85)
    s_s      = p.get("score_strong",       0.75)
    s_m      = p.get("score_moderate",     0.40)
    s_neg    = p.get("score_conflict",     0.55)

    if preference == "high":
        if ratio >= hi_thr:
            return EvaluationResult(s_s, 1.0, f"IV/HV {ratio:.2f} — IV rich; selling edge confirmed")
        if ratio >= lo_thr:
            return EvaluationResult(s_m, 0.7, f"IV/HV {ratio:.2f} — IV near HV; modest selling edge")
        return EvaluationResult(-s_neg, 0.9, f"IV/HV {ratio:.2f} — IV below HV; selling edge absent")

    if preference == "low":
        if ratio <= lo_thr:
            return EvaluationResult(s_s, 1.0, f"IV/HV {ratio:.2f} — IV cheap; buying edge confirmed")
        if ratio <= hi_thr:
            return EvaluationResult(s_m, 0.7, f"IV/HV {ratio:.2f} — IV near HV; modest buying edge")
        return EvaluationResult(-s_neg, 0.9, f"IV/HV {ratio:.2f} — IV rich; buying edge absent")

    if preference == "moderate":
        dist = abs(ratio - 1.0)
        if dist <= 0.10:
            return EvaluationResult(s_s, 1.0, f"IV/HV {ratio:.2f} — IV near HV; balanced vol environment")
        if dist <= 0.25:
            return EvaluationResult(s_m, 0.7, f"IV/HV {ratio:.2f} — IV slightly {'rich' if ratio > 1 else 'cheap'}")
        return EvaluationResult(-s_m * 0.5, 0.8, f"IV/HV {ratio:.2f} — IV {'significantly rich' if ratio > 1 else 'significantly cheap'}; less balanced")

    return EvaluationResult.neutral()


def _eval_vol_pcr(preference: str, market: dict) -> EvaluationResult:
    """Volume PCR — today's put/call volume ratio (fast, tactical signal).

    market field: vol_pcr → put_vol / call_vol for the chosen expiry
    Orthogonal to OI PCR (slow/structural). Vol PCR reflects today's activity.

    Preferences:
      "bullish"  — volume flow is call-skewed (buyers of calls = bullish bet)
      "bearish"  — volume flow is put-skewed (buyers of puts = bearish/hedging)
      "neutral"  — volume is balanced
    """
    vpcr = market.get("vol_pcr")
    if vpcr is None:
        return EvaluationResult.missing()
    p       = _p("vol_pcr")
    bear_hi = p.get("bearish_threshold",  1.30)
    bear_lo = p.get("slight_bearish",     1.05)
    bull_lo = p.get("bullish_threshold",  0.75)
    bull_hi = p.get("slight_bullish",     0.95)
    s_s     = p.get("score_strong",       0.65)
    s_m     = p.get("score_moderate",     0.35)

    if preference == "bullish":
        if vpcr <= bull_lo:
            return EvaluationResult(s_s, 1.0, f"Vol PCR {vpcr:.2f} — call volume dominant; bullish flow")
        if vpcr <= bull_hi:
            return EvaluationResult(s_m, 0.7, f"Vol PCR {vpcr:.2f} — slight call bias")
        if vpcr >= bear_hi:
            return EvaluationResult(-s_s, 0.9, f"Vol PCR {vpcr:.2f} — heavy put volume; bearish flow")
        return EvaluationResult(-s_m * 0.4, 0.6, f"Vol PCR {vpcr:.2f} — mixed to bearish volume flow")

    if preference == "bearish":
        if vpcr >= bear_hi:
            return EvaluationResult(s_s, 1.0, f"Vol PCR {vpcr:.2f} — put volume dominant; bearish flow")
        if vpcr >= bear_lo:
            return EvaluationResult(s_m, 0.7, f"Vol PCR {vpcr:.2f} — slight put bias")
        if vpcr <= bull_lo:
            return EvaluationResult(-s_s, 0.9, f"Vol PCR {vpcr:.2f} — heavy call volume; bullish flow")
        return EvaluationResult(-s_m * 0.4, 0.6, f"Vol PCR {vpcr:.2f} — mixed to bullish volume flow")

    if preference == "neutral":
        if bull_hi < vpcr < bear_lo:
            return EvaluationResult(s_s, 1.0, f"Vol PCR {vpcr:.2f} — balanced volume flow")
        if bull_lo < vpcr <= bull_hi or bear_lo <= vpcr < bear_hi:
            return EvaluationResult(s_m, 0.7, f"Vol PCR {vpcr:.2f} — mostly balanced")
        return EvaluationResult(-s_m, 0.8, f"Vol PCR {vpcr:.2f} — strongly skewed volume; not neutral")

    return EvaluationResult.neutral()


def _eval_pcr_diverge(preference: str, market: dict) -> EvaluationResult:
    """OI PCR vs Volume PCR divergence — structural vs tactical positioning.

    market field: pcr_diverge → oi_pcr - vol_pcr (signed float)
    Positive = OI bearish but today's volume is bullish (bears unwinding / bulls entering)
    Negative = OI bullish but today's volume is bearish (bulls unwinding / bears entering)
    Near 0 = OI and volume agree (no divergence)

    Preferences:
      "unwinding_bears" — OI bearish, vol bullish (positive diverge); tactical shift in progress
      "building_bears"  — OI bullish, vol bearish (negative diverge); new bearish flow building
      "neutral"         — OI and vol agree; no useful signal from divergence
    """
    div = market.get("pcr_diverge")
    if div is None:
        return EvaluationResult.missing()
    p       = _p("pcr_diverge")
    sig_thr = p.get("significant_threshold", 0.30)  # |diverge| >= this = meaningful
    mod_thr = p.get("moderate_threshold",    0.10)
    s_s     = p.get("score_strong",          0.60)
    s_m     = p.get("score_moderate",        0.30)

    if preference == "unwinding_bears":
        if div >= sig_thr:
            return EvaluationResult(s_s, 1.0, f"PCR diverge +{div:.2f} — OI bearish but vol bullish; bears may be unwinding")
        if div >= mod_thr:
            return EvaluationResult(s_m, 0.7, f"PCR diverge +{div:.2f} — mild bullish-vol bias vs OI")
        return EvaluationResult(-s_m * 0.4, 0.6, f"PCR diverge {div:.2f} — no unwinding signal detected")

    if preference == "building_bears":
        if div <= -sig_thr:
            return EvaluationResult(s_s, 1.0, f"PCR diverge {div:.2f} — OI bullish but vol bearish; new put buying building")
        if div <= -mod_thr:
            return EvaluationResult(s_m, 0.7, f"PCR diverge {div:.2f} — mild bearish-vol vs OI")
        return EvaluationResult(-s_m * 0.4, 0.6, f"PCR diverge {div:.2f} — no bearish-build signal detected")

    if preference == "neutral":
        if abs(div) < mod_thr:
            return EvaluationResult(s_s, 1.0, f"PCR diverge {div:.2f} — OI and vol aligned; no structural shift")
        if abs(div) < sig_thr:
            return EvaluationResult(s_m, 0.7, f"PCR diverge {div:.2f} — minor OI/vol discrepancy")
        return EvaluationResult(-s_m, 0.8, f"PCR diverge {div:.2f} — significant OI/vol split; not neutral")

    return EvaluationResult.neutral()


# ── Dispatch tables ───────────────────────────────────────────────────────────

_EVALUATORS: dict[str, callable] = {
    "ema200":          _eval_ema200,
    "weekly_trend":    _eval_weekly_trend,
    "adx":             _eval_adx,
    "rsi":             _eval_rsi,
    "macd":            _eval_macd,
    "iv_premium":      _eval_iv_premium,
    "iv_term":         _eval_iv_term,
    "vol_skew":        _eval_vol_skew,
    "news":            _eval_news,
    "pcr":             _eval_pcr,
    "rel_volume":      _eval_rel_volume,
    "analyst":         _eval_analyst,
    "short_interest":  _eval_short_interest,
    "atr":             _eval_atr,
    "hv30":            _eval_hv30,
    "iv_percentile":   _eval_iv_percentile,
    "earnings_dte":    _eval_earnings_dte,
    "div_dte":         _eval_div_dte,
    "max_pain":        _eval_max_pain,
    "oi_concentration": _eval_oi_concentration,
    "iv_change_5d":       _eval_iv_change_5d,
    "unusual_activity":   _eval_unusual_activity,
    "iv_hv_ratio":        _eval_iv_hv_ratio,
    "vol_pcr":            _eval_vol_pcr,
    "pcr_diverge":        _eval_pcr_diverge,
}

SIGNAL_DEFINITIONS: dict[str, SignalDefinition] = {
    "ema200": SignalDefinition(
        name="ema200",
        valid_preferences=frozenset({"bullish", "bearish", "neutral", "directional"}),
        required_fields=frozenset({"ema_distance_pct"}),
    ),
    "weekly_trend": SignalDefinition(
        name="weekly_trend",
        valid_preferences=frozenset({"aligned", "neutral", "bullish", "bearish",
                                     "divergence_ok", "directional"}),
        required_fields=frozenset({"weekly_trend", "daily_trend"}),
    ),
    "adx": SignalDefinition(
        name="adx",
        valid_preferences=frozenset({"strong", "weak", "moderate", "expanding"}),
        required_fields=frozenset({"adx"}),
    ),
    "rsi": SignalDefinition(
        name="rsi",
        valid_preferences=frozenset({"bullish", "bearish", "neutral", "not_overbought",
                                     "not_oversold", "directional"}),
        required_fields=frozenset({"rsi"}),
    ),
    "macd": SignalDefinition(
        name="macd",
        valid_preferences=frozenset({"bullish", "bearish", "neutral", "directional"}),
        required_fields=frozenset({"macd_trend"}),
    ),
    "iv_premium": SignalDefinition(
        name="iv_premium",
        valid_preferences=frozenset({"high", "low"}),
        required_fields=frozenset({"iv_premium"}),
    ),
    "iv_term": SignalDefinition(
        name="iv_term",
        valid_preferences=frozenset({"backwardation", "contango", "flat"}),
        required_fields=frozenset({"term_slope"}),
    ),
    "vol_skew": SignalDefinition(
        name="vol_skew",
        valid_preferences=frozenset({"put_heavy", "call_heavy", "balanced"}),
        required_fields=frozenset({"vol_skew_pct"}),
    ),
    "news": SignalDefinition(
        name="news",
        valid_preferences=frozenset({"bullish", "bearish", "quiet", "major_event", "directional"}),
        required_fields=frozenset({"news_sentiment"}),
    ),
    "pcr": SignalDefinition(
        name="pcr",
        valid_preferences=frozenset({"bullish", "bearish", "neutral", "directional"}),
        required_fields=frozenset({"pcr_sentiment"}),
    ),
    "rel_volume": SignalDefinition(
        name="rel_volume",
        valid_preferences=frozenset({"high"}),
        required_fields=frozenset({"rel_volume"}),
    ),
    "analyst": SignalDefinition(
        name="analyst",
        valid_preferences=frozenset({"bullish", "bearish", "neutral", "directional"}),
        required_fields=frozenset({"analyst_label"}),
    ),
    "short_interest": SignalDefinition(
        name="short_interest",
        valid_preferences=frozenset({"bullish", "bearish", "high", "low", "directional"}),
        required_fields=frozenset({"short_interest"}),
    ),
    "atr": SignalDefinition(
        name="atr",
        valid_preferences=frozenset({"low", "high", "moderate"}),
        required_fields=frozenset({"atr_pct"}),
    ),
    "hv30": SignalDefinition(
        name="hv30",
        valid_preferences=frozenset({"low", "high"}),
        required_fields=frozenset({"hv30"}),
    ),
    "iv_percentile": SignalDefinition(
        name="iv_percentile",
        valid_preferences=frozenset({"high", "low", "moderate"}),
        required_fields=frozenset({"iv_rank_52w"}),
    ),
    "earnings_dte": SignalDefinition(
        name="earnings_dte",
        valid_preferences=frozenset({"safe", "near"}),
        required_fields=frozenset({"earnings_dte"}),
    ),
    "div_dte": SignalDefinition(
        name="div_dte",
        valid_preferences=frozenset({"safe"}),
        required_fields=frozenset({"div_days_to_ex"}),
    ),
    "max_pain": SignalDefinition(
        name="max_pain",
        valid_preferences=frozenset({"above", "below", "neutral"}),
        required_fields=frozenset({"max_pain_distance_pct"}),
    ),
    "oi_concentration": SignalDefinition(
        name="oi_concentration",
        valid_preferences=frozenset({"high", "low"}),
        required_fields=frozenset({"oi_concentration"}),
    ),
    "iv_change_5d": SignalDefinition(
        name="iv_change_5d",
        valid_preferences=frozenset({"falling", "rising", "stable"}),
        required_fields=frozenset({"iv_change_5d"}),
    ),
    "unusual_activity": SignalDefinition(
        name="unusual_activity",
        valid_preferences=frozenset({"detected", "quiet"}),
        required_fields=frozenset({"unusual_activity"}),
    ),
    "iv_hv_ratio": SignalDefinition(
        name="iv_hv_ratio",
        valid_preferences=frozenset({"high", "low", "moderate"}),
        required_fields=frozenset({"iv_hv_ratio"}),
    ),
    "vol_pcr": SignalDefinition(
        name="vol_pcr",
        valid_preferences=frozenset({"bullish", "bearish", "neutral"}),
        required_fields=frozenset({"vol_pcr"}),
    ),
    "pcr_diverge": SignalDefinition(
        name="pcr_diverge",
        valid_preferences=frozenset({"unwinding_bears", "building_bears", "neutral"}),
        required_fields=frozenset({"pcr_diverge"}),
    ),
}


def evaluate(signal: str, preference: str, market: dict) -> EvaluationResult:
    """
    Score how well the current market satisfies the given signal preference.

    Parameters
    ----------
    signal     : canonical signal name (must be in SIGNAL_DEFINITIONS)
    preference : what the structure wants (must be in signal's valid_preferences,
                 after "directional" has been resolved by the caller)
    market     : flat dict with all available market field values

    Returns EvaluationResult(score, confidence, explanation).
    result.score is None when a required market field is missing — the caller
    must exclude this signal from both numerator and effective_max.

    Raises ValueError on unknown signal or invalid preference (caught at startup
    by validate_structure_scores() to turn config errors into loud failures).
    """
    defn = SIGNAL_DEFINITIONS.get(signal)
    if defn is None:
        raise ValueError(f"Unknown signal: {signal!r}. Valid: {sorted(SIGNAL_DEFINITIONS)}")
    if preference not in defn.valid_preferences:
        raise ValueError(
            f"Invalid preference {preference!r} for signal {signal!r}. "
            f"Valid: {sorted(defn.valid_preferences)}"
        )
    return _EVALUATORS[signal](preference, market)
