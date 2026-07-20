"""
Decision Provider — single source of truth for "is this trade/position good?"

Used by all three pages:
  - Live Suggestions:   evaluate_candidate(row, candidate) — wraps the
    already-computed per-candidate score (analyze.py + app.py's
    signal_score/market_bias/signal_score_adj pipeline) into the same shape
    the other two pages use, so all three speak one vocabulary.
  - Live Positions / Paper Trades: evaluate_position(row, position) — scores
    the structure actually HELD (which may differ from whatever the rulebook
    currently recommends for a NEW trade) by re-running the exact same
    compute_signal_alignment() engine Live Suggestions uses, then layering in
    position-only factors (P&L, time decay, breakeven cushion, strike
    proximity) that don't apply to a not-yet-entered candidate.

This replaces the parallel, simplified scoring that used to live in
lib/position-health.js (JS) — that file is now a pure renderer of whatever
this module returns, so there is exactly one place alignment-scoring logic
lives, and one place to add a new factor so it's correct everywhere at once.
"""
from config import scoring as sc
from config.ml_gates import (
    RETURN_CONFLICT_THRESHOLD, RETURN_SUPPORT_THRESHOLD,
    VOL_HIGH_THRESHOLD, VOL_LOW_THRESHOLD,
    IV_EXPANDING_SHORT_VOL_PENALTY, IV_EXPANDING_LONG_VOL_BONUS,
    IV_CONTRACTING_SHORT_VOL_BONUS, IV_CONTRACTING_LONG_VOL_PENALTY,
    P_UP_STRONG_CONFLICT, P_DOWN_STRONG_CONFLICT,
    P_UP_CONFIRM, P_DOWN_CONFIRM, DIRECTION_CONFLICT_PENALTY,
    ANOMALY_EXTREME_THRESHOLD, ANOMALY_PENALTY, ANOMALY_VERDICT_CAP,
    META_BULLISH_THRESHOLD, META_BEARISH_THRESHOLD,
    META_CONSENSUS_BONUS, META_CONFLICT_PENALTY,
    POP_BANDS,
    LONG_VOL_BAD_REGIMES,
    STRUCT_VOL_LOW_PERCENTILE, STRUCT_VOL_HIGH_PERCENTILE,
    STRUCT_RETURN_MIN_THRESHOLD,
    LONG_OPTION_BAD_REGIME_PENALTY, LONG_OPTION_GOOD_REGIME_BONUS,
    DEBIT_SPREAD_BAD_REGIME_PENALTY, DEBIT_SPREAD_GOOD_REGIME_BONUS,
)
from scripts.analyze import compute_signal_alignment

BULLISH_STRUCTURES = ("Put Credit Spread", "Call Debit Spread", "Diagonal Spread", "Jade Lizard", "Long Call", "Short Put")
BEARISH_STRUCTURES = ("Call Credit Spread", "Put Debit Spread", "Long Put", "Short Call")
NEUTRAL_STRUCTURES  = ("Iron Condor", "Calendar Spread")

# Short-vol structures collect premium and are hurt by IV expansion.
# Long-vol structures pay premium and benefit from IV expansion.
_SHORT_VOL_STRUCTURES = frozenset({
    "Put Credit Spread", "Call Credit Spread", "Iron Condor",
    "Jade Lizard", "Short Put", "Short Call", "Calendar Spread",
})
_LONG_VOL_STRUCTURES = frozenset({
    "Call Debit Spread", "Put Debit Spread", "Long Call", "Long Put",
})

_RATING_CLASS = {
    "Strong":     "pass",
    "Moderate":   "pass",
    "Neutral":    "warn",
    "Weak":       "fail",
    "Conflicted": "fail",
}

# Higher number = stronger rating; used to enforce verdict caps.
_RATING_ORDER = {"Conflicted": 0, "Weak": 1, "Neutral": 2, "Moderate": 3, "Strong": 4}


def _apply_ml_signals(score: float, reasons: list, ml: dict, structure: str) -> tuple:
    """
    Layer ML model outputs onto the rule-based score.

    Returns (score, reasons, adjustments, ml_summary, verdict_cap) where:
      adjustments — list of {source, value} dicts for UI traceability
      ml_summary  — structured snapshot of all ML outputs for the frontend
      verdict_cap — a rating string to cap the verdict at, or None
    """
    adjustments = []
    verdict_cap = None

    ml_summary = {
        "regime":          None,
        "expected_return": None,
        "expected_move":   None,
        "iv_direction":    None,
        "iv_expanding_prob": None,
        "pop":             None,
        "meta_score":      None,
        "anomaly":         False,
        "anomaly_score":   None,
    }

    if not ml or not ml.get("ok"):
        return score, reasons, adjustments, ml_summary, verdict_cap

    ml_regime       = ml.get("regime")
    expected_return = ml.get("expected_return")
    expected_vol    = ml.get("expected_vol")
    bias            = get_structure_bias(structure)

    ml_summary["regime"]          = ml_regime
    ml_summary["expected_return"] = expected_return
    ml_summary["expected_move"]   = ml.get("expected_move_pct")
    ml_summary["iv_direction"]    = ml.get("iv_direction")
    ml_summary["iv_expanding_prob"] = ml.get("iv_expanding_prob")

    def _adj(source, value, reason):
        nonlocal score
        score += value
        adjustments.append({"source": source, "value": value})
        reasons.append(reason)

    # Regime alignment
    if ml_regime:
        if ml_regime == "Downtrend" and bias == "bullish":
            _adj("regime", -1.0, f"ML regime ({ml_regime}) conflicts with bullish structure — headwind.")
        elif ml_regime == "Uptrend" and bias == "bearish":
            _adj("regime", -1.0, f"ML regime ({ml_regime}) conflicts with bearish structure — headwind.")
        elif ml_regime == "Downtrend" and bias == "bearish":
            _adj("regime", +0.5, f"ML regime ({ml_regime}) confirms bearish bias.")
        elif ml_regime == "Uptrend" and bias == "bullish":
            _adj("regime", +0.5, f"ML regime ({ml_regime}) confirms bullish bias.")
        elif ml_regime == "Range-bound" and bias == "neutral":
            _adj("regime", +0.5, f"ML regime ({ml_regime}) supports neutral structure.")
        elif ml_regime == "Range-bound" and bias != "neutral":
            reasons.append(f"ML regime ({ml_regime}) — directional structure may be premature.")

    # 10-day return direction vs structure
    if expected_return is not None:
        ret_pct = f"{expected_return:+.1%}"
        if expected_return < -RETURN_CONFLICT_THRESHOLD and bias == "bullish":
            _adj("return", -0.5, f"ML forecasts {ret_pct} 10d return — conflicts with bullish structure.")
        elif expected_return > RETURN_CONFLICT_THRESHOLD and bias == "bearish":
            _adj("return", -0.5, f"ML forecasts {ret_pct} 10d return — conflicts with bearish structure.")
        elif expected_return < -RETURN_SUPPORT_THRESHOLD and bias == "bearish":
            reasons.append(f"ML forecasts {ret_pct} 10d return — supports bearish bias.")
        elif expected_return > RETURN_SUPPORT_THRESHOLD and bias == "bullish":
            reasons.append(f"ML forecasts {ret_pct} 10d return — supports bullish bias.")

    # Vol context + expected move for strike/DTE sizing
    em_pct = ml.get("expected_move_pct")
    if expected_vol is not None:
        if expected_vol > VOL_HIGH_THRESHOLD:
            reasons.append(f"ML vol forecast {expected_vol:.0%} ann. — use wider strikes or shorter DTE.")
        elif expected_vol < VOL_LOW_THRESHOLD:
            reasons.append(f"ML vol forecast {expected_vol:.0%} ann. — tighter spreads or longer DTE viable.")
    if em_pct is not None:
        reasons.append(f"ML 10d expected move: ±{em_pct:.1%} — size strikes at least that far from spot.")

    # IV Direction — structure selection signal (most actionable ML output)
    iv_direction   = ml.get("iv_direction")
    iv_expand_prob = ml.get("iv_expanding_prob")
    if iv_direction and iv_expand_prob is not None:
        prob_str = f"{iv_expand_prob:.0%}"
        is_short_vol = structure in _SHORT_VOL_STRUCTURES
        is_long_vol  = structure in _LONG_VOL_STRUCTURES
        if iv_direction == "Expanding" and is_short_vol:
            _adj("iv_direction", IV_EXPANDING_SHORT_VOL_PENALTY,
                 f"ML IV expanding ({prob_str}) — short-vol structure ({structure}) "
                 f"faces headwind. Consider debit spread or wait for IV to peak.")
        elif iv_direction == "Expanding" and is_long_vol:
            _adj("iv_direction", IV_EXPANDING_LONG_VOL_BONUS,
                 f"ML IV expanding ({prob_str}) — long-vol structure ({structure}) aligned. "
                 f"Rising IV increases option value.")
        elif iv_direction == "Contracting" and is_short_vol:
            _adj("iv_direction", IV_CONTRACTING_SHORT_VOL_BONUS,
                 f"ML IV contracting ({prob_str}) — short-vol structure ({structure}) aligned. "
                 f"Falling IV benefits premium sellers.")
        elif iv_direction == "Contracting" and is_long_vol:
            _adj("iv_direction", IV_CONTRACTING_LONG_VOL_PENALTY,
                 f"ML IV contracting ({prob_str}) — long-vol structure ({structure}) faces "
                 f"headwind. Falling IV erodes option value (vol crush risk).")

    # Direction model P(up) — independent signal from regime classifier
    p_up = ml.get("p_up")
    if p_up is not None:
        if p_up >= P_UP_STRONG_CONFLICT and bias == "bearish":
            _adj("direction", DIRECTION_CONFLICT_PENALTY,
                 f"ML P(up)={p_up:.0%} — strong up signal conflicts with bearish structure.")
        elif p_up <= P_DOWN_STRONG_CONFLICT and bias == "bullish":
            _adj("direction", DIRECTION_CONFLICT_PENALTY,
                 f"ML P(up)={p_up:.0%} — strong down signal conflicts with bullish structure.")
        elif p_up >= P_UP_CONFIRM and bias == "bullish":
            reasons.append(f"ML P(up)={p_up:.0%} — direction model supports bullish bias.")
        elif p_up <= P_DOWN_CONFIRM and bias == "bearish":
            reasons.append(f"ML P(up)={p_up:.0%} — direction model supports bearish bias.")

    # Anomaly detector — extreme anomaly caps the verdict to prevent false confidence.
    is_anomaly   = ml.get("is_anomaly")
    anom_score   = ml.get("anomaly_score")
    anom_flags   = ml.get("anomaly_flags") or []
    ml_summary["anomaly"]       = bool(is_anomaly)
    ml_summary["anomaly_score"] = anom_score
    if is_anomaly:
        flag_str = "; ".join(anom_flags[:2]) if anom_flags else "multi-feature outlier"
        if anom_score is not None and anom_score <= ANOMALY_EXTREME_THRESHOLD:
            _adj("anomaly", ANOMALY_PENALTY,
                 f"Anomaly detector: extreme outlier (score {anom_score:.0f}/100) — "
                 f"{flag_str}. ML models may be operating outside training distribution.")
            verdict_cap = ANOMALY_VERDICT_CAP
        else:
            reasons.append(
                f"Anomaly detector: unusual conditions (score {anom_score:.0f}/100) — "
                f"{flag_str}. Verify signal thesis before entering."
            )

    # Meta-ensemble — stacked output of all models; light nudge, no double-counting.
    # NOTE: meta_score does not yet include POP as an input — retrain after POP
    # has sufficient live data to avoid an uninformed input corrupting the ensemble.
    meta_score = ml.get("meta_score")
    ml_summary["meta_score"] = meta_score
    if meta_score is not None:
        if meta_score >= META_BULLISH_THRESHOLD and bias == "bullish":
            _adj("meta", META_CONSENSUS_BONUS,
                 f"Meta-ensemble {meta_score:.0f}/100 — strong ML bullish consensus.")
        elif meta_score <= META_BEARISH_THRESHOLD and bias == "bearish":
            _adj("meta", META_CONSENSUS_BONUS,
                 f"Meta-ensemble {meta_score:.0f}/100 — strong ML bearish consensus.")
        elif meta_score >= META_BULLISH_THRESHOLD and bias == "bearish":
            _adj("meta", META_CONFLICT_PENALTY,
                 f"Meta-ensemble {meta_score:.0f}/100 — ML leans bullish, conflicts with bearish structure.")
        elif meta_score <= META_BEARISH_THRESHOLD and bias == "bullish":
            _adj("meta", META_CONFLICT_PENALTY,
                 f"Meta-ensemble {meta_score:.0f}/100 — ML leans bearish, conflicts with bullish structure.")
        else:
            reasons.append(f"Meta-ensemble {meta_score:.0f}/100 — no strong ML consensus.")

    # Structure × Regime — conditional penalty/bonus based on empirical failure analysis.
    # Long Calls accounted for 38% of all losses (91% theta-decay); debit spreads another 22%.
    # Adjustments fire only when regime OR vol context confirms the risk / opportunity.
    # Return model weight is deliberately low (R²<0) — vol model carries the main signal.
    _is_long_option   = structure in ("Long Call", "Long Put")
    _is_debit_spread  = structure in ("Call Debit Spread", "Put Debit Spread")
    _fwd_vol          = ml.get("expected_vol")        # from volatility_regressor (R²=0.51)
    _fwd_ret          = ml.get("expected_return")     # from return_regressor (weak — low weight)
    _regime_bad_longvol = ml_regime in LONG_VOL_BAD_REGIMES

    if _is_long_option or _is_debit_spread:
        _penalty = LONG_OPTION_BAD_REGIME_PENALTY if _is_long_option else DEBIT_SPREAD_BAD_REGIME_PENALTY
        _bonus   = LONG_OPTION_GOOD_REGIME_BONUS  if _is_long_option else DEBIT_SPREAD_GOOD_REGIME_BONUS
        _label   = "Long option" if _is_long_option else "Debit spread"

        # Vol-based thresholds: compare against percentile constants (annualised vol proxy)
        _vol_low  = _fwd_vol is not None and _fwd_vol <= STRUCT_VOL_LOW_PERCENTILE
        _vol_high = _fwd_vol is not None and _fwd_vol >= STRUCT_VOL_HIGH_PERCENTILE

        # BAD condition: regime is unfavourable OR predicted vol is in bottom tercile
        if _regime_bad_longvol or _vol_low:
            _reasons = []
            if _regime_bad_longvol:
                _reasons.append(f"regime={ml_regime}")
            if _vol_low:
                _reasons.append(f"predicted vol={_fwd_vol:.0%} (low)")
            _adj("structure_regime", _penalty,
                 f"{_label} ({structure}) in unfavourable environment "
                 f"({', '.join(_reasons)}) — elevated theta-decay risk.")

        # GOOD condition: regime favourable AND vol in top tercile AND (optionally) return aligned
        elif not _regime_bad_longvol and ml_regime and _vol_high:
            _ret_aligned = (
                _fwd_ret is not None
                and abs(_fwd_ret) >= STRUCT_RETURN_MIN_THRESHOLD
                and (
                    (bias == "bullish" and _fwd_ret > 0)
                    or (bias == "bearish" and _fwd_ret < 0)
                )
            )
            _bonus_reasons = [f"regime={ml_regime}", f"predicted vol={_fwd_vol:.0%} (high)"]
            if _ret_aligned:
                _bonus_reasons.append(f"return forecast {_fwd_ret:+.1%} aligned")
            _adj("structure_regime", _bonus,
                 f"{_label} ({structure}) in favourable environment "
                 f"({', '.join(_bonus_reasons)}) — conditions support premium buyers.")

    # POP classifier — historical trade success probability; nudges score only.
    pop_score = ml.get("pop_score")
    ml_summary["pop"] = pop_score
    if pop_score is not None:
        adj_val = POP_BANDS[-1][1]
        for threshold, adjustment in POP_BANDS:
            if pop_score >= threshold:
                adj_val = adjustment
                break
        if adj_val != 0.0:
            _adj("pop", adj_val,
                 f"Historical win probability {pop_score:.0%} — "
                 f"score adjustment {adj_val:+.1f}.")

    return score, reasons, adjustments, ml_summary, verdict_cap


def get_structure_bias(structure: str) -> str:
    """Bullish/bearish/neutral classification for any structure name."""
    structure = (structure or "").strip()
    if structure in BULLISH_STRUCTURES:
        return "bullish"
    if structure in BEARISH_STRUCTURES:
        return "bearish"
    if structure in NEUTRAL_STRUCTURES:
        return "neutral"
    if structure.startswith("Long"):
        return "bullish"   # plain long stock
    if structure.startswith("Short"):
        return "bearish"   # plain short stock
    return "neutral"


def evaluate_candidate(row: dict, candidate: dict) -> dict:
    """
    Live Suggestions: normalize an already-scored candidate into the shared
    decision shape, layering in ML model signals from row["ml"] if available.
    """
    score   = candidate.get("signal_score", row.get("signal_score", 0)) or 0
    regime  = row.get("regime", "chop")

    reasons = list(row.get("signal_notes") or [])
    mb = candidate.get("market_bias")
    if mb and mb.get("notes"):
        reasons.extend(mb["notes"])

    score, reasons, adjustments, ml_summary, verdict_cap = _apply_ml_signals(
        score, reasons, row.get("ml"), candidate.get("structure")
    )
    verdict = sc.score_to_rating(score, regime)
    if verdict_cap and _RATING_ORDER.get(verdict, 0) > _RATING_ORDER.get(verdict_cap, 0):
        verdict = verdict_cap

    return {
        "verdict":     verdict,
        "verdict_cls": _RATING_CLASS.get(verdict, "warn"),
        "score":       round(score, 3),
        "bias":        get_structure_bias(candidate.get("structure")),
        "reasons":     reasons,
        "action":      None,
        "adjustments": adjustments,
        "ml_summary":  ml_summary,
        "confidence":  "Low" if verdict_cap else "Normal",
    }


def evaluate_position(row: dict, position: dict) -> dict:
    """
    Live Positions / Paper Trades: score the structure actually HELD by
    re-running compute_signal_alignment() against it — the same engine Live
    Suggestions uses for new candidates — then layering in position-only
    factors that don't apply to a not-yet-entered trade.

    @param row: analysis row from /api/analyze (trend, rsi, macd_trend, etc.)
    @param position: {structure, pnl_pct, dte, move_to_be_pct, proximity}
        proximity (optional): {"strike": float, "distance_pct": float, "risk_level": "Danger Zone"|"Caution"|"Safe"}
    """
    structure = position.get("structure", "")
    regime    = row.get("regime", "chop")

    alignment = compute_signal_alignment(
        structure,
        row.get("trend"), row.get("weekly_trend"), row.get("rsi"),
        row.get("macd_trend"), row.get("news_sentiment"),
        adx=row.get("adx"), rel_volume=row.get("rel_volume"),
        pcr=row.get("pcr"), pcr_sentiment=row.get("pcr_sentiment"),
        ema200_position=row.get("ema200_position"),
        iv_term_shape=row.get("iv_term_shape"),
        short_interest=row.get("short_interest"),
        vol_skew_pct=row.get("vol_skew_pct"),
        analyst_label=row.get("analyst_label"),
        iv_premium=row.get("iv_premium"),
        regime=regime,
    )
    score   = alignment["score"]
    reasons = list(alignment["notes"])

    # P&L status relative to cost basis
    pnl_pct = position.get("pnl_pct")
    if pnl_pct is not None:
        if pnl_pct >= 50:
            score += 1
            reasons.append(f"Up {pnl_pct:.0f}% — inside profit-taking territory.")
        elif pnl_pct <= -50:
            score -= 1
            reasons.append(f"Down {abs(pnl_pct):.0f}% — approaching max-loss territory.")

    # Time decay urgency
    dte = position.get("dte")
    urgent = False
    if dte is not None:
        if dte <= 5:
            urgent = True
            reasons.append(f"Only {dte}d to expiry — theta decay accelerating, act soon.")
        elif dte <= 14:
            reasons.append(f"{dte}d to expiry — time decay becoming a factor.")

    # Cushion to breakeven — also compare against ML expected move
    move_to_be_pct = position.get("move_to_be_pct")
    if move_to_be_pct is not None and abs(move_to_be_pct) <= 3:
        score -= 1
        reasons.append(f"Only {abs(move_to_be_pct):.1f}% from breakeven — thin margin for error.")
    ml_em = (row.get("ml") or {}).get("expected_move_pct")
    if move_to_be_pct is not None and ml_em is not None:
        if abs(move_to_be_pct) < ml_em * 100:
            score -= 0.5
            reasons.append(
                f"Breakeven only {abs(move_to_be_pct):.1f}% away — inside ML 10d expected move "
                f"(±{ml_em:.1%}). High probability of being tested."
            )

    # Distance to the strike that defines max loss
    proximity = position.get("proximity")
    if proximity:
        if proximity["risk_level"] == "Danger Zone":
            score -= 1
            reasons.append(f"Price is only {proximity['distance_pct']}% from the ${proximity['strike']} strike — danger zone.")
        elif proximity["risk_level"] == "Caution":
            reasons.append(f"Price is {proximity['distance_pct']}% from the ${proximity['strike']} strike — watch closely.")

    score, reasons, adjustments, ml_summary, verdict_cap = _apply_ml_signals(
        score, reasons, row.get("ml"), structure
    )
    verdict = sc.score_to_rating(score, regime)
    if verdict_cap and _RATING_ORDER.get(verdict, 0) > _RATING_ORDER.get(verdict_cap, 0):
        verdict = verdict_cap
    if urgent and verdict in ("Strong", "Moderate", "Neutral"):
        verdict = "Weak"  # time pressure should never read as fine

    if verdict in ("Strong", "Moderate"):
        action = ("Consider taking profits or trailing a stop."
                  if (pnl_pct is not None and pnl_pct >= 50)
                  else "Hold — thesis intact.")
    elif verdict in ("Weak", "Conflicted"):
        action = ("Close or roll soon — limited time left and conditions have turned."
                  if urgent
                  else "Re-evaluate thesis — consider rolling, hedging, or closing.")
    else:
        action = "No action needed yet — keep watching trend and time decay."

    return {
        "verdict":     verdict,
        "verdict_cls": _RATING_CLASS.get(verdict, "warn"),
        "score":       round(score, 3),
        "bias":        get_structure_bias(structure),
        "reasons":     reasons,
        "action":      action,
        "adjustments": adjustments,
        "ml_summary":  ml_summary,
        "confidence":  "Low" if verdict_cap else "Normal",
    }
