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


def _apply_ml_signals(score: float, reasons: list, ml: dict, structure: str) -> tuple:
    """
    Layer ML model outputs (regime classifier, return regressor, vol regressor)
    onto the existing rule-based score as additional signals. Each factor adds
    or subtracts at most 1 point so ML can never fully override the rulebook.
    ml comes from row["ml"] set by app.py from regime_predictor.predict_ticker().
    """
    if not ml or not ml.get("ok"):
        return score, reasons

    ml_regime       = ml.get("regime")
    expected_return = ml.get("expected_return")
    expected_vol    = ml.get("expected_vol")
    bias            = get_structure_bias(structure)

    # Regime alignment
    if ml_regime:
        if ml_regime == "Downtrend" and bias == "bullish":
            score -= 1
            reasons.append(f"ML regime ({ml_regime}) conflicts with bullish structure — headwind.")
        elif ml_regime == "Uptrend" and bias == "bearish":
            score -= 1
            reasons.append(f"ML regime ({ml_regime}) conflicts with bearish structure — headwind.")
        elif ml_regime == "Downtrend" and bias == "bearish":
            score += 0.5
            reasons.append(f"ML regime ({ml_regime}) confirms bearish bias.")
        elif ml_regime == "Uptrend" and bias == "bullish":
            score += 0.5
            reasons.append(f"ML regime ({ml_regime}) confirms bullish bias.")
        elif ml_regime == "Range-bound" and bias == "neutral":
            score += 0.5
            reasons.append(f"ML regime ({ml_regime}) supports neutral structure.")
        elif ml_regime == "Range-bound" and bias != "neutral":
            reasons.append(f"ML regime ({ml_regime}) — directional structure may be premature.")

    # 10-day return direction vs structure
    if expected_return is not None:
        ret_pct = f"{expected_return:+.1%}"
        if expected_return < -0.03 and bias == "bullish":
            score -= 0.5
            reasons.append(f"ML forecasts {ret_pct} 10d return — conflicts with bullish structure.")
        elif expected_return > 0.03 and bias == "bearish":
            score -= 0.5
            reasons.append(f"ML forecasts {ret_pct} 10d return — conflicts with bearish structure.")
        elif expected_return < -0.02 and bias == "bearish":
            reasons.append(f"ML forecasts {ret_pct} 10d return — supports bearish bias.")
        elif expected_return > 0.02 and bias == "bullish":
            reasons.append(f"ML forecasts {ret_pct} 10d return — supports bullish bias.")

    # Vol context + expected move for strike/DTE sizing
    em_pct = ml.get("expected_move_pct")
    if expected_vol is not None:
        if expected_vol > 0.60:
            reasons.append(f"ML vol forecast {expected_vol:.0%} ann. — use wider strikes or shorter DTE.")
        elif expected_vol < 0.20:
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
            score -= 1.0
            reasons.append(
                f"ML IV expanding ({prob_str}) — short-vol structure ({structure}) "
                f"faces headwind. Consider debit spread or wait for IV to peak."
            )
        elif iv_direction == "Expanding" and is_long_vol:
            score += 0.5
            reasons.append(
                f"ML IV expanding ({prob_str}) — long-vol structure ({structure}) aligned. "
                f"Rising IV increases option value."
            )
        elif iv_direction == "Contracting" and is_short_vol:
            score += 0.5
            reasons.append(
                f"ML IV contracting ({prob_str}) — short-vol structure ({structure}) aligned. "
                f"Falling IV benefits premium sellers."
            )
        elif iv_direction == "Contracting" and is_long_vol:
            score -= 0.5
            reasons.append(
                f"ML IV contracting ({prob_str}) — long-vol structure ({structure}) faces "
                f"headwind. Falling IV erodes option value (vol crush risk)."
            )

    # Direction model P(up) — independent signal from regime classifier
    p_up = ml.get("p_up")
    if p_up is not None:
        if p_up >= 0.65 and bias == "bearish":
            score -= 0.5
            reasons.append(f"ML P(up)={p_up:.0%} — strong up signal conflicts with bearish structure.")
        elif p_up <= 0.35 and bias == "bullish":
            score -= 0.5
            reasons.append(f"ML P(up)={p_up:.0%} — strong down signal conflicts with bullish structure.")
        elif p_up >= 0.60 and bias == "bullish":
            reasons.append(f"ML P(up)={p_up:.0%} — direction model supports bullish bias.")
        elif p_up <= 0.40 and bias == "bearish":
            reasons.append(f"ML P(up)={p_up:.0%} — direction model supports bearish bias.")

    # Anomaly detector — flag unusual market conditions; caution, not hard veto.
    is_anomaly   = ml.get("is_anomaly")
    anom_score   = ml.get("anomaly_score")
    anom_flags   = ml.get("anomaly_flags") or []
    if is_anomaly:
        flag_str = "; ".join(anom_flags[:2]) if anom_flags else "multi-feature outlier"
        if anom_score is not None and anom_score <= 20:
            score -= 0.5
            reasons.append(
                f"Anomaly detector: extreme outlier (score {anom_score:.0f}/100) — "
                f"{flag_str}. ML models may be operating outside training distribution."
            )
        else:
            reasons.append(
                f"Anomaly detector: unusual conditions (score {anom_score:.0f}/100) — "
                f"{flag_str}. Verify signal thesis before entering."
            )

    # Meta-ensemble — stacked output of all 5 models; light nudge, no double-counting.
    meta_score = ml.get("meta_score")
    if meta_score is not None:
        if meta_score >= 70 and bias == "bullish":
            score += 0.5
            reasons.append(f"Meta-ensemble {meta_score:.0f}/100 — strong ML bullish consensus.")
        elif meta_score <= 30 and bias == "bearish":
            score += 0.5
            reasons.append(f"Meta-ensemble {meta_score:.0f}/100 — strong ML bearish consensus.")
        elif meta_score >= 70 and bias == "bearish":
            score -= 0.5
            reasons.append(f"Meta-ensemble {meta_score:.0f}/100 — ML leans bullish, conflicts with bearish structure.")
        elif meta_score <= 30 and bias == "bullish":
            score -= 0.5
            reasons.append(f"Meta-ensemble {meta_score:.0f}/100 — ML leans bearish, conflicts with bullish structure.")
        else:
            reasons.append(f"Meta-ensemble {meta_score:.0f}/100 — no strong ML consensus.")

    return score, reasons


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

    score, reasons = _apply_ml_signals(score, reasons, row.get("ml"), candidate.get("structure"))
    verdict = sc.score_to_rating(score, regime)

    return {
        "verdict":     verdict,
        "verdict_cls": _RATING_CLASS.get(verdict, "warn"),
        "score":       round(score, 3),
        "bias":        get_structure_bias(candidate.get("structure")),
        "reasons":     reasons,
        "action":      None,
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

    score, reasons = _apply_ml_signals(score, reasons, row.get("ml"), structure)
    verdict = sc.score_to_rating(score, regime)
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
    }
