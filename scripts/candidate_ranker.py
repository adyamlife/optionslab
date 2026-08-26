"""
Candidate filtering and ranking for Live Suggestions and paper-trade morning scan.

Pipeline
--------
1. Candidate Universe  — wider net: recommended OR ev>0 OR ML conviction OR big expected move
2. Hard Gates          — non-negotiable eliminations (profit floor, strikes, IV, EV, confidence, volume)
3. Composite Score     — 0-100 weighted across ML + EV + rulebook signals, with bonuses and penalties
4. Best per Ticker     — keep only the highest-scoring candidate per ticker
5. Rank Tickers        — quality gate then top-n by composite score

All tunable constants (weights, gate thresholds, penalties) live in config/ranking.toml.
"""
import logging
from dataclasses import dataclass, field as _dc_field
from config.rules import MIN_PROFIT_AMOUNT, IV_EDGE_SKIP_VP

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repair subsystem
# ---------------------------------------------------------------------------

@dataclass
class RepairResult:
    status: str                          # "PASS" | "FAILED" | "NOT_REPAIRABLE"
    replacement_candidates: list = _dc_field(default_factory=list)
    failure_reason: str | None = None    # e.g. "NO_VALID_STRIKES", "STRUCTURE_NOT_SUPPORTED"


def _generate_ibf_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for an Iron Butterfly candidate."""
    puts  = candidate.get("_ibf_puts")
    calls = candidate.get("_ibf_calls")
    if puts is None or calls is None:
        return []
    from scripts.analyze import enumerate_ibf_repair_variants
    return enumerate_ibf_repair_variants(
        puts, calls, candidate,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        width_target=float(context.get("width_target", 10)),
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_ds_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Call or Put Debit Spread candidate."""
    chain       = candidate.get("_ds_chain")
    option_type = candidate.get("_ds_option_type")
    if chain is None or option_type is None:
        return []
    from scripts.analyze import enumerate_ds_repair_variants
    return enumerate_ds_repair_variants(
        chain, candidate, option_type,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        width_target=float(context.get("width_target", 10)),
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_csp_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Cash Secured Put or Naked Put candidate."""
    puts = candidate.get("_csp_puts")
    if puts is None:
        return []
    from scripts.analyze import enumerate_csp_repair_variants
    return enumerate_csp_repair_variants(
        puts, candidate,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_ls_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Long Strangle candidate."""
    puts  = candidate.get("_ls_puts")
    calls = candidate.get("_ls_calls")
    if puts is None or calls is None:
        return []
    from scripts.analyze import enumerate_ls_repair_variants
    return enumerate_ls_repair_variants(
        puts, calls, candidate,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_pcs_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Put Credit Spread (narrow the spread width)."""
    puts = candidate.get("_pcs_puts")
    if puts is None:
        return []
    from scripts.analyze import enumerate_cs_repair_variants
    return enumerate_cs_repair_variants(
        puts, candidate, "put",
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_ccs_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Call Credit Spread (narrow the spread width)."""
    calls = candidate.get("_ccs_calls")
    if calls is None:
        return []
    from scripts.analyze import enumerate_cs_repair_variants
    return enumerate_cs_repair_variants(
        calls, candidate, "call",
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_ic_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for an Iron Condor (narrow the put spread)."""
    puts  = candidate.get("_ic_puts")
    calls = candidate.get("_ic_calls")
    if puts is None or calls is None:
        return []
    from scripts.analyze import enumerate_ic_repair_variants
    return enumerate_ic_repair_variants(
        puts, calls, candidate,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_straddle_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Short Straddle (move both legs OTM → strangle)."""
    puts  = candidate.get("_sstr_puts")
    calls = candidate.get("_sstr_calls")
    if puts is None or calls is None:
        return []
    from scripts.analyze import enumerate_ss_repair_variants
    return enumerate_ss_repair_variants(
        puts, calls, candidate,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_ss_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Short Strangle candidate."""
    puts  = candidate.get("_ss_puts")
    calls = candidate.get("_ss_calls")
    if puts is None or calls is None:
        return []
    from scripts.analyze import enumerate_ss_repair_variants
    return enumerate_ss_repair_variants(
        puts, calls, candidate,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


def _generate_jl_variants(candidate: dict, context: dict) -> list:
    """Return repair variants for a Jade Lizard candidate."""
    puts = candidate.get("_jl_puts")
    if puts is None:
        return []
    from scripts.analyze import enumerate_jl_repair_variants
    return enumerate_jl_repair_variants(
        puts, candidate,
        spot=float(candidate.get("spot_at_entry") or 0),
        T=float(context.get("dte") or 30) / 365.0,
        min_profit_amount=float(context.get("min_profit", 0.5)),
        recommended_structure=context.get("recommended_structure", ""),
    )


# Maps structure name → variant generator.  Add new structures here as needed.
_VARIANT_GENERATORS: dict = {
    "Iron Butterfly":    _generate_ibf_variants,
    "Call Debit Spread": _generate_ds_variants,
    "Put Debit Spread":  _generate_ds_variants,
    "Cash Secured Put":  _generate_csp_variants,
    "Naked Put":         _generate_csp_variants,
    "Long Strangle":     _generate_ls_variants,
    "Short Strangle":    _generate_ss_variants,
    "Jade Lizard":       _generate_jl_variants,
    "Put Credit Spread": _generate_pcs_variants,
    "Call Credit Spread": _generate_ccs_variants,
    "Iron Condor":       _generate_ic_variants,
    "Short Straddle":    _generate_straddle_variants,
}


def attempt_repairs(candidate: dict, context: dict) -> RepairResult:
    """Try to find a capital- and gate-eligible narrower variant of a candidate.

    Gate-agnostic: callers pass the failed candidate and a context dict; this
    function generates replacement candidates but does NOT run any gates.
    Repaired candidates are appended to the caller's evaluation queue and will
    restart the full gate pipeline from Gate 1.

    A candidate with repair_of set is never re-repaired, preventing loops.
    """
    if candidate.get("repair_of") is not None:
        return RepairResult(status="NOT_REPAIRABLE", failure_reason="ALREADY_REPAIR")

    generator = _VARIANT_GENERATORS.get(candidate.get("structure"))
    if generator is None:
        return RepairResult(status="NOT_REPAIRABLE", failure_reason="STRUCTURE_NOT_SUPPORTED")

    variants = generator(candidate, context)
    if not variants:
        return RepairResult(status="FAILED", failure_reason="NO_VALID_STRIKES")

    return RepairResult(status="PASS", replacement_candidates=variants)


# ---------------------------------------------------------------------------
# P1-B: Evidence-n helper — count comparable closed paper trades
# ---------------------------------------------------------------------------

_EVIDENCE_CACHE: dict = {}   # (structure, regime) → n;  cleared each morning scan

def _count_comparable_trades(structure: str, market_regime: str | None) -> int:
    """Count closed paper trades with matching structure + market_regime.

    Comparable-trade definition is intentional and documented here so future
    changes to it are visible: structure exact-match, regime bucket match.
    Returns 0 on any DB error so evidence_n is never None.
    """
    regime_key = market_regime or ""
    cache_key  = (structure, regime_key)
    if cache_key in _EVIDENCE_CACHE:
        return _EVIDENCE_CACHE[cache_key]
    try:
        from scripts.db import read_df as _rdf
        _df = _rdf(
            "SELECT COUNT(*) AS n FROM paper_trades "
            "WHERE closed_at IS NOT NULL AND structure = ? AND market_regime = ?",
            [structure, regime_key],
        )
        n = int(_df.iloc[0]["n"]) if len(_df) else 0
    except Exception:
        n = 0
    _EVIDENCE_CACHE[cache_key] = n
    return n


def _confidence_tier(evidence_n: int) -> str:
    """Map evidence count to a named tier.

    Tiers are intentionally non-overlapping and use strict boundaries so the
    definition is deterministic and testable:
      INSUFFICIENT  n < 5
      LOW           5 ≤ n < 10
      MEDIUM        10 ≤ n < 30
      HIGH          n ≥ 30
    """
    if evidence_n < 5:
        return "INSUFFICIENT"
    if evidence_n < 10:
        return "LOW"
    if evidence_n < 30:
        return "MEDIUM"
    return "HIGH"


def _load_ranking_cfg() -> dict:
    """Load config/ranking.toml. Cached per process via module-level singleton."""
    try:
        from pathlib import Path
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        path = Path(__file__).resolve().parent.parent / "config" / "ranking.toml"
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not load config/ranking.toml ({e}); using defaults")
        return {}


_RANKING_CFG: dict = {}   # populated on first call to _cfg()


def _cfg() -> dict:
    global _RANKING_CFG
    if not _RANKING_CFG:
        _RANKING_CFG = _load_ranking_cfg()
    return _RANKING_CFG


def _g(section: str, key: str, default):
    """Read a value from ranking.toml with a fallback default."""
    return _cfg().get(section, {}).get(key, default)


def _strikes_complete(c) -> bool:
    """Return True only when all required strikes for this structure are non-None."""
    from config.structures import get_or_none as _gst
    from config.structures._base import StrikeSchema as _SS
    st = _gst(c.get("structure", ""))
    if st is None:
        return False
    if st.strike_schema == _SS.IRON_CONDOR:
        return all(c.get(k) is not None for k in (
            "put_long_strike", "put_short_strike", "call_short_strike", "call_long_strike"))
    if st.strike_schema == _SS.SINGLE_LEG:
        return c.get("short_strike") is not None
    name = c.get("structure", "")
    if name == "Bear Combo":
        return all(c.get(k) is not None for k in (
            "long_put_strike", "short_put_strike", "short_call_strike", "long_call_strike"))
    if name == "Financed Long Call":
        return all(c.get(k) is not None for k in (
            "short_put_strike", "long_put_strike", "call_strike"))
    if name == "Financed Long Put":
        return all(c.get(k) is not None for k in (
            "short_call_strike", "long_call_strike", "put_strike"))
    # Calendar: both legs share one ATM strike — only short_strike is required
    if name in ("Calendar Spread", "Double Calendar"):
        return c.get("short_strike") is not None
    # Diagonal: two different strikes, one per expiry
    if name == "Diagonal Spread":
        return c.get("short_strike") is not None and c.get("long_strike") is not None
    return c.get("short_strike") is not None and c.get("long_strike") is not None


def _composite_score(row, c, ev, ev_is_proxy: bool = False) -> float:
    """
    Compute a 0-100 composite score for one (row, candidate) pair.

    Each component is normalized to [0, 100] before weighting.
    Bonuses and penalties are added/subtracted from the weighted total.
    All constants come from config/ranking.toml.

    ev_is_proxy: when True, the EV was estimated from POP (not from actual P&L math).
      POP already appears in Component 4, so counting it again via EV double-weights it.
      We halve w_ev in that case and redistribute the freed weight to signal and liquidity.
    """
    from config.structures import CREDIT_STRUCTURES, get_or_none as _gst
    ml        = row.get("ml") or {}
    pred_dist = ml.get("pred_dist") or {}

    # ── Weights ───────────────────────────────────────────────────────────────
    w_conf = _g("weights", "confidence", 0.30)
    w_ev   = _g("weights", "ev",         0.25)
    w_meta = _g("weights", "meta_score", 0.10)
    w_pop  = _g("weights", "pop",        0.10)
    w_sig  = _g("weights", "signal",     0.15)
    w_iv   = _g("weights", "iv_edge",    0.05)
    w_liq  = _g("weights", "liquidity",  0.05)

    # #6 — EV proxy de-weighting: when EV is estimated from POP, POP would be
    # counted twice (once here, once in Component 4). Halve w_ev and redistribute
    # the freed weight equally to signal and liquidity to keep weights summing to 1.
    if ev_is_proxy:
        freed  = w_ev * 0.5
        w_ev  -= freed
        w_sig += freed * 0.6
        w_liq += freed * 0.4

    # ── Component 1: ML confidence (0-1 → 0-100) ─────────────────────────────
    confidence = pred_dist.get("confidence")
    s_conf = (confidence * 100) if confidence is not None else 50.0

    # ── Component 2: Expected Value — percentile rank within this batch ──────
    # #3 percentile normalization: EV distributions are right-skewed; linear
    # clip-and-scale gave disproportionate scores to outliers. Percentile rank
    # adapts automatically to whatever range the batch produces.
    # ev_pct_rank is injected by rank_candidates() after computing it across all
    # surviving candidates; falls back to linear ratio when not yet available.
    ev_pct_rank = c.get("_ev_pct_rank")
    if ev_pct_rank is not None:
        s_ev = float(ev_pct_rank)   # already 0-100
    else:
        ev_fallback = _g("ev_norm", "fallback_scale", 5.0)
        ev_clip_min = _g("ev_norm", "clip_min",       -1.0)
        ev_clip_max = _g("ev_norm", "clip_max",         2.0)
        max_loss = c.get("max_loss")
        capital  = c.get("capital_required")
        if max_loss and max_loss > 0:
            ev_ratio = ev / max_loss
        elif capital and capital > 0:
            ev_ratio = ev / (capital / 100.0)
        else:
            ev_ratio = ev / ev_fallback
        ev_ratio = min(max(ev_ratio, ev_clip_min), ev_clip_max)
        ev_range = ev_clip_max - ev_clip_min
        s_ev = (ev_ratio - ev_clip_min) / ev_range * 100

    # When the return regressor is active (expected_return present), replace s_ev
    # with the regressor's output normalized to [0, 100]. The regressor directly
    # predicts the forward return; s_ev (structure EV) proxies the same quantity
    # but through POP × credit — using both would double-count the same signal.
    _exp_ret = ml.get("expected_return")
    if _exp_ret is not None:
        _ret_lo = _g("return_norm", "clip_lo", -0.30)
        _ret_hi = _g("return_norm", "clip_hi",  0.30)
        s_ev = max(0.0, min(100.0,
            (float(_exp_ret) - _ret_lo) / (_ret_hi - _ret_lo) * 100
        ))

    # ── Component 3: ML meta_score + return classifier composite ─────────────
    meta = ml.get("meta_score")
    s_meta = float(meta) if meta is not None else 50.0

    # composite_score from regime_predictor (cross-model weighted signal):
    # 0.40×P(return>10%) + 0.25×P(IV expanding) + 0.20×P(up) + 0.15×vol_norm
    # Blends with meta_score: 60% composite, 40% meta when both available.
    comp = ml.get("composite_score")
    if comp is not None and meta is not None:
        s_meta = 0.60 * float(comp) + 0.40 * float(meta)
    elif comp is not None:
        s_meta = float(comp)

    # ── Component 4: POP — probability of profit ──────────────────────────────
    # Prefer the calibrated ML model (structure-family, else pooled pop_score) over
    # the naive delta-based candidate pop (c["pop"], a raw Black-Scholes 1-|delta|
    # heuristic set unconditionally by every structure builder in analyze.py).
    # Previously pop_c was checked first, so pop_m's branch never fired in
    # practice — the calibrated model (AUC 0.83, see
    # [[project_trade_win_calibration]]) was computed every scan and silently
    # discarded in favor of the uncalibrated heuristic. Fixed 2026-08-26 to match
    # this comment's original stated intent.
    pop_c = c.get("pop")           # candidate pop: 0-100
    pop_m = c.get("_family_pop_score") or ml.get("pop_score")
    if pop_m is not None:
        s_pop = float(pop_m) * 100
    elif pop_c is not None:
        s_pop = float(pop_c)
    else:
        s_pop = 50.0

    # ── Component 5: Signal score — normalized from natural [-4, +4] range ────
    # Clips entire negative range to 0 was the old bug; now maps linearly.
    sig_min = _g("signal_norm", "min", -4.0)
    sig_max = _g("signal_norm", "max",  4.0)
    raw_sig = c.get("signal_score") if c.get("signal_score") is not None else row.get("signal_score")
    sig = max(sig_min, min(sig_max, float(raw_sig or 0)))
    s_signal = (sig - sig_min) / (sig_max - sig_min) * 100

    # ── Component 6: IV edge — structural alignment ───────────────────────────
    iv_clip = _g("iv_norm", "clip_vp", 5.0)
    iv_edge_vp = c.get("iv_edge_vp")
    if iv_edge_vp is not None:
        is_credit = c.get("structure", "") in CREDIT_STRUCTURES
        aligned_edge = iv_edge_vp if is_credit else -iv_edge_vp
        s_iv = min(max((aligned_edge + iv_clip) / (iv_clip * 2) * 100, 0.0), 100.0)
    else:
        s_iv = 50.0

    # ── Component 7: Liquidity ────────────────────────────────────────────────
    liq_ceil = _g("liquidity_norm", "full_volume", 1.5)
    rel_vol  = row.get("rel_volume")
    s_liq = min(float(rel_vol) / liq_ceil * 100, 100.0) if rel_vol is not None else 50.0

    # ── Weighted composite ────────────────────────────────────────────────────
    score = (
        w_conf * s_conf
        + w_ev   * s_ev
        + w_meta * s_meta
        + w_pop  * s_pop
        + w_sig  * s_signal
        + w_iv   * s_iv
        + w_liq  * s_liq
    )

    # ── Penalties ─────────────────────────────────────────────────────────────
    # Synthetic-quote penalty: chain bid/ask was manufactured from lastPrice when
    # market was closed — spread and credit estimates are unreliable.
    if row.get("synthetic_quotes"):
        score -= _g("penalties", "synthetic_quotes", 15)

    p_trend        = _g("penalties", "trend_conflict",        10)
    p_trend_regime = _g("penalties", "trend_conflict_regime",  5)
    p_vol          = _g("penalties", "low_volume",             8)
    p_iv_exp       = _g("penalties", "iv_expensive",           8)
    p_news_bear    = _g("penalties", "news_per_unit_bearish",  2)
    p_news_bull    = _g("penalties", "news_per_unit_bullish",  1)
    p_news_max     = _g("penalties", "news_max_penalty",      10)
    p_news_bonus   = _g("penalties", "news_max_bonus",         5)

    pt_bearish = _g("penalty_triggers", "p_win_bearish_threshold", 0.45)
    pt_bullish = _g("penalty_triggers", "p_win_bullish_threshold", 0.55)
    pt_low_vol = _g("penalty_triggers", "low_volume_threshold",    0.80)

    # Trend conflict — p_win direction vs structure bias
    p_win = pred_dist.get("p_win")
    if p_win is not None:
        st = _gst(c.get("structure", ""))
        st_trend = getattr(st, "trend", "Any") if st else "Any"
        if st_trend == "Uptrend" and p_win < pt_bearish:
            score -= p_trend
        elif st_trend == "Downtrend" and p_win > pt_bullish:
            score -= p_trend

        # Additional penalty when ML regime label OR rulebook trend also conflicts
        if st_trend not in ("Any",):
            ml_regime   = ml.get("regime", "")
            rule_regime = row.get("trend", "")
            if st_trend == "Uptrend" and (ml_regime == "Downtrend" or rule_regime == "Downtrend"):
                score -= p_trend_regime
            elif st_trend == "Downtrend" and (ml_regime == "Uptrend" or rule_regime == "Uptrend"):
                score -= p_trend_regime

    if rel_vol is not None and rel_vol < pt_low_vol:
        score -= p_vol

    if c.get("iv_edge_label") in ("overpay", "undersell"):
        score -= p_iv_exp

    # ── ML classifier screening (soft penalties, not hard gates) ─────────────
    # Applied only when the return classifier is trained (p_return_gt10 present).
    # Uses graduated penalties so borderline candidates aren't hard-excluded —
    # the ranker can still surface them if other signals are strong enough.
    # Thresholds: P(return>10%)>0.60, IV exp>0.50, direction>0.55, vol above median.
    p_screen = _g("penalties", "ml_screen", 6)   # per failed screen, default 6pts
    p_return_gt10   = ml.get("p_return_gt10")
    p_iv_expanding  = ml.get("iv_expanding_prob")
    p_direction_up  = ml.get("p_up")
    exp_vol         = ml.get("expected_vol")
    if p_return_gt10   is not None and float(p_return_gt10)  < 0.35:
        score -= p_screen
    _net_vega = c.get("net_vega") or 0
    if p_iv_expanding is not None:
        _piv = float(p_iv_expanding)
        # Long-vega structures benefit from IV expansion; short-vega from contraction.
        if _net_vega >= 0 and _piv < 0.30:   # long-vol: penalise low IV-expansion prob
            score -= p_screen
        elif _net_vega < 0 and _piv > 0.70:  # short-vol: penalise high IV-expansion prob
            score -= p_screen
    if p_direction_up  is not None and float(p_direction_up) < 0.45:
        score -= p_screen
    if exp_vol is not None and float(exp_vol) < 0.12:   # below ~12% annualised vol → thin edge
        score -= p_screen

    # ── Event risk penalties (#Missing risk filters) ─────────────────────────
    # These fields are collected by get_macro_context() and stored on each row.
    # Soft penalties rather than hard gates — the ranker can still surface a
    # high-conviction trade, but event risk is priced into the score.
    p_event = _g("penalties", "event_risk", 8)   # per active event risk flag
    _dte = c.get("dte") or 14

    # Earnings within 2 days — IV crush risk, most dangerous timing
    _earn_days = row.get("earnings_days_away")
    if _earn_days is not None and 0 <= _earn_days <= 2:
        score -= p_event * 2
        log.debug(f"[score] {row.get('ticker')} earnings in {_earn_days}d → -{p_event * 2}pts")

    # Fed meeting within trade's DTE — macro vol risk
    if row.get("fed_within_dte") == 1:
        score -= p_event
        log.debug(f"[score] {row.get('ticker')} FOMC within DTE → -{p_event}pts")

    # CPI release within DTE
    if row.get("cpi_within_dte") == 1:
        score -= p_event
        log.debug(f"[score] {row.get('ticker')} CPI within DTE → -{p_event}pts")

    # PPI release within DTE (correlated with CPI, slightly lower impact)
    if row.get("ppi_within_dte") == 1:
        score -= round(p_event * 0.5)
        log.debug(f"[score] {row.get('ticker')} PPI within DTE → -{round(p_event * 0.5)}pts")

    # Jobs Report within DTE
    if row.get("jobs_within_dte") == 1:
        score -= round(p_event * 0.75)
        log.debug(f"[score] {row.get('ticker')} Jobs Report within DTE → -{round(p_event * 0.75)}pts")

    # OPEX week — gamma risk spikes, fills degrade for short-gamma structures
    if row.get("is_opex_week") == 1:
        from config.structures import CREDIT_STRUCTURES
        if c.get("structure", "") in CREDIT_STRUCTURES:
            score -= round(p_event * 0.5)
            log.debug(f"[score] {row.get('ticker')} OPEX week + credit structure → -{round(p_event * 0.5)}pts")

    # Low ATR — thin daily range makes breakeven harder to reach for debit structures
    _atr_pct = row.get("atr_pct")
    if _atr_pct is not None and _atr_pct < 0.008:   # <0.8% daily ATR
        score -= round(p_event * 0.5)
        log.debug(f"[score] {row.get('ticker')} low ATR {_atr_pct:.3f} → -{round(p_event * 0.5)}pts")

    # News penalty/bonus using net article count (graduated, not binary)
    news_bullish_ct = row.get("news_bullish") or 0
    news_bearish_ct = row.get("news_bearish") or 0
    net_news = news_bullish_ct - news_bearish_ct
    if net_news < 0:
        score -= min(abs(net_news) * p_news_bear, p_news_max)
    elif net_news > 0:
        score += min(net_news * p_news_bull, p_news_bonus)

    # ── Bonuses ───────────────────────────────────────────────────────────────

    # Anomaly score bonus — unusual setups worth surfacing regardless of rulebook fit
    anomaly_score = ml.get("anomaly_score") or 0
    a_low  = _g("anomaly", "low_threshold",  40)
    a_mid  = _g("anomaly", "mid_threshold",  60)
    a_high = _g("anomaly", "high_threshold", 80)
    if anomaly_score >= a_high:
        score += _g("anomaly", "high_bonus", 8)
    elif anomaly_score >= a_mid:
        score += _g("anomaly", "mid_bonus",  5)
    elif anomaly_score >= a_low:
        score += _g("anomaly", "low_bonus",  2)

    # IV expansion probability — reward vega alignment with IV forecast
    iv_expand_prob = ml.get("iv_expanding_prob")
    if iv_expand_prob is not None:
        iv_wt    = _g("iv_expansion", "weight", 8.0)
        net_vega = c.get("net_vega") or 0
        if net_vega > 0:
            score += iv_expand_prob * iv_wt
        elif net_vega < 0:
            score -= iv_expand_prob * iv_wt

    # ── CVaR tail-risk penalty (Phase 2) ─────────────────────────────────────
    # Only applied when MC EV is available (ev_is_proxy=False) — the proxy path
    # has no reliable tail metric to penalize. cvar_loss is negative (a loss),
    # so abs() gives the magnitude; w_cvar scales it into composite-score points.
    # Default w_cvar=0 keeps Phase 1 behaviour until explicitly enabled in
    # ranking.toml after the A/B backtest confirms MC EV adds value.
    _w_cvar   = _g("mc", "cvar_penalty_weight", 0.0)
    _cvar_cap = _g("mc", "cvar_penalty_cap",   20.0)  # max pts deducted
    if _w_cvar > 0:
        _cvar = c.get("mc_cvar_loss")
        if _cvar is not None and _cvar < 0:
            # Normalize: abs(cvar_loss) / max_loss gives severity in [0,1]
            _max_loss = c.get("max_loss") or 1.0
            _cvar_severity = min(abs(_cvar) / _max_loss, 1.0)
            score -= min(_w_cvar * _cvar_severity * 100, _cvar_cap)

    # ── Calibration multiplier (empirical win-rate feedback) ──────────────────
    # Applies the observed avg_return from closed trades in this score bucket as
    # a final multiplier. Returns 1.0 (neutral) until ≥5 closed trades exist in
    # the bucket, so no effect until enough real outcome data has accumulated.
    # Bounded to [0.80, 1.20] to prevent catastrophic reordering during ramp-up.
    try:
        from scripts.offline_eval import get_calibration_multiplier as _gcm
        score *= _gcm(score, min_bucket_n=_g("calibration", "min_bucket_n", 5))
    except Exception:
        pass

    return round(score, 2)


def filter_candidates(rows, paper_trade: bool = False, buying_power: float | None = None):
    """
    Steps 1-2: Build the candidate universe and apply hard gates.

    Returns a flat list of enriched dicts — one per candidate that survived —
    ready for composite scoring.
    """
    min_conf     = _g("gate", "min_confidence",         0.70)
    min_rvol     = _g("gate", "min_rel_volume",         0.02)
    conv_meta    = _g("gate", "ml_conviction_meta",    10)
    conv_conf    = _g("gate", "ml_conviction_conf",     0.80)
    exp_move_thr = _g("gate", "expected_move_threshold", 0.12)

    # [min_trade] gates from settings.toml
    try:
        from pathlib import Path as _P
        try:
            import tomllib as _tl
        except ImportError:
            import tomli as _tl
        _settings_raw = _tl.loads(
            (_P(__file__).resolve().parent.parent / "config" / "settings.toml")
            .read_text(encoding="utf-8")
        )
        _mt = _settings_raw.get("min_trade", {})
    except Exception:
        _mt = {}
    _min_roi      = float(_mt.get("min_expected_roi",    0.10))
    _max_theta    = float(_mt.get("max_theta_per_day",   0.05))
    _min_liq      = float(_mt.get("min_liquidity_score", 0.60))
    _max_dd_proxy = float(_mt.get("max_drawdown_proxy",  0.95))

    # Read ml_gate enabled flag from settings.toml (operational on/off switch)
    try:
        from pathlib import Path
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        _s = tomllib.loads(
            (Path(__file__).resolve().parent.parent / "config" / "settings.toml")
            .read_text(encoding="utf-8")
        )
        gate_enabled = bool(_s.get("ml_gate", {}).get("enabled", True))
    except Exception:
        gate_enabled = True

    # Paper-trade overrides: lower gates since all outcomes are training data
    _min_profit = MIN_PROFIT_AMOUNT
    if paper_trade:
        try:
            from pathlib import Path as _Ppt
            try:
                import tomllib as _tlpt
            except ImportError:
                import tomli as _tlpt
            _pt_cfg = _tlpt.loads(
                (_Ppt(__file__).resolve().parent.parent / "config" / "settings.toml")
                .read_text(encoding="utf-8")
            ).get("paper_trades", {})
            if "min_profit_amount" in _pt_cfg:
                _min_profit = float(_pt_cfg["min_profit_amount"])
            if "min_confidence" in _pt_cfg:
                min_conf = float(_pt_cfg["min_confidence"])
            if "max_theta_per_day" in _pt_cfg:
                _max_theta = float(_pt_cfg["max_theta_per_day"])
            if "min_expected_roi" in _pt_cfg:
                _min_roi = float(_pt_cfg["min_expected_roi"])
            if "min_liquidity_score" in _pt_cfg:
                _min_liq = float(_pt_cfg["min_liquidity_score"])
            # Merge regime-aware ROI overrides into _mt so Gate 9 picks them up
            for _k in ("roi_high_iv", "roi_normal_iv", "roi_low_iv"):
                if _k in _pt_cfg:
                    _mt[_k] = _pt_cfg[_k]
        except Exception:
            pass

    # Preload ticker profiles once for the whole batch (advisory note, Task 5).
    # Best-effort: silently empty if the table doesn't exist yet.
    _profiles: dict = {}
    try:
        from scripts.db import load_all_ticker_profiles as _latp
        _profiles = _latp()
    except Exception:
        pass

    # Structured gate rejection log — written at DEBUG, cheap to collect, useful for tuning
    _rejections: list[dict] = []

    def _reject(ticker, struct, gate, threshold, actual):
        # trend/expiry/mc_expiry_* captured via closure from the enclosing
        # `for row in rows:` loop (row resolved at call time, not definition
        # time). mc_expiry_p10-p90 now come from analyze_ticker()'s ticker/
        # expiry-level simulation (2026-08-25, see
        # [[project_mc_forecast_structure_eligibility]]) — computed once per
        # ticker independent of any structure's strikes, so it's populated
        # here even for strikes_incomplete rejections, unlike the old
        # per-candidate MC (which only ran for survivors, much later).
        _rejections.append({
            "ticker": ticker, "structure": struct, "gate": gate,
            "threshold": threshold, "actual": actual,
            "trend": row.get("trend"), "expiry": row.get("expiry"),
            "mc_expiry_p10": row.get("mc_expiry_p10"),
            "mc_expiry_p25": row.get("mc_expiry_p25"),
            "mc_expiry_p50": row.get("mc_expiry_p50"),
            "mc_expiry_p75": row.get("mc_expiry_p75"),
            "mc_expiry_p90": row.get("mc_expiry_p90"),
        })
        log.debug(f"[gate:{gate}] {ticker} {struct} → rejected (threshold={threshold}, actual={actual})")

    result = []
    _cap_rejected: list[dict] = []
    for row in rows:
        if (row.get("status") or "").startswith("SKIP"):
            continue

        ml         = row.get("ml") or {}
        pred_dist  = ml.get("pred_dist") or {}
        confidence = pred_dist.get("confidence")
        meta_score = ml.get("meta_score")

        # Use a list so repaired variants can be appended and restart from Gate 1.
        _repair_context = {
            "dte":                  row.get("dte") or 30,
            "width_target":         float(_g("defaults", "width_target", 10)),
            "min_profit":           _min_profit,
            "recommended_structure": row.get("recommended_structure") or "",
        }
        _cqueue = list(row.get("candidates", []))
        _ci = 0
        while _ci < len(_cqueue):
            c = _cqueue[_ci]
            _ci += 1
            struct = c.get("structure", "")

            # ── Step 1: Candidate Universe ─────────────────────────────────────
            ev_raw     = c.get("ev") or 0.0
            sig_raw    = c.get("signal_score") or 0.0
            ml_conviction = (
                abs((meta_score or 50) - 50) > conv_meta
                and (confidence or 0) >= conv_conf
            )
            expected_move = ml.get("expected_move_pct") or 0.0
            if not (c.get("recommended")
                    or ev_raw > 0
                    or sig_raw > 0
                    or ml_conviction
                    or expected_move > exp_move_thr):
                continue

            # ── Step 2: Hard Gates ─────────────────────────────────────────────

            _t = row.get("ticker", "")

            # Gate 1: minimum profit floor (unlimited-profit structures exempt)
            if c.get("max_profit") is not None and c["max_profit"] < _min_profit:
                _reject(_t, struct, "min_profit", _min_profit, round(c["max_profit"], 2))
                continue

            # Gate 1b: debit spread reward/risk — reject if max_profit < max_loss (R/R < 1:1)
            # Debit paid should not exceed the potential gain; otherwise the trade is
            # structurally upside-down before any market movement.
            if struct in ("Call Debit Spread", "Put Debit Spread"):
                _mp = c.get("max_profit")
                _ml = c.get("max_loss")
                if _mp is not None and _ml is not None and _ml > 0 and _mp < _ml:
                    _reject(_t, struct, "reward_risk", f"max_profit>={round(_ml,2)}", round(_mp, 2))
                    continue

            # Gate 2: all required strikes present
            if not _strikes_complete(c):
                _reject(_t, struct, "strikes_incomplete", True, False)
                continue

            # Gate 3: IV edge hard skip
            iv_edge_vp    = c.get("iv_edge_vp")
            iv_edge_label = c.get("iv_edge_label", "fair")
            if (iv_edge_vp is not None
                    and iv_edge_label in ("overpay", "undersell")
                    and abs(iv_edge_vp) > IV_EDGE_SKIP_VP):
                _reject(_t, struct, "iv_edge", IV_EDGE_SKIP_VP, round(iv_edge_vp, 2))
                continue

            # Gate 4: EV computable — compute delta-proxy as fallback baseline.
            # MC EV (expected_pnl from simulation) is computed after all gates pass
            # and replaces this proxy when available. Proxy is retained as ev_delta_proxy
            # for diagnostic comparison and as the fallback for unsupported structures.
            _ev_raw = c.get("ev")
            if _ev_raw is None:
                pop = c.get("pop")
                if pop is None:
                    _reject(_t, struct, "no_ev_or_pop", "ev or pop required", None)
                    continue
                if c.get("max_profit") is None:
                    # Use actual capital at risk (debit paid) as the expected-profit cap
                    # for unlimited-upside structures (Long Strangle, etc.).
                    # Using MAX_LOSS_PER_TRADE here inflated ev_ratio to 20-40x for cheap
                    # debits, clipping s_ev to 100 and dominating the composite score.
                    from config.rules import MAX_LOSS_PER_TRADE as _MLPT
                    _ev_raw = pop / 100 * (c.get("max_loss") or _MLPT)
                else:
                    _ev_raw = pop / 100 * c["max_profit"]
            ev_delta_proxy = round(_ev_raw, 4)

            # Gate 5: minimum ML confidence
            if gate_enabled and confidence is not None and confidence < min_conf:
                _reject(_t, struct, "ml_confidence", min_conf, round(confidence, 3))
                continue

            # Gate 6: minimum relative volume (hard floor; 0.40-0.80 incurs penalty in scoring)
            rel_vol = row.get("rel_volume")
            if gate_enabled and rel_vol is not None and rel_vol < min_rvol:
                _reject(_t, struct, "rel_volume", min_rvol, round(rel_vol, 3))
                continue

            # Gate 7: theta decay — skip when daily theta cost is excessive vs potential credit
            # net_theta is negative for short-theta (credit) trades; positive for long-theta (debit).
            # For credit trades a very large |theta| means rapid decay risk if trade goes wrong.
            _theta = c.get("net_theta")
            if gate_enabled and _max_theta > 0 and _theta is not None and abs(_theta) > _max_theta:
                _reject(_t, struct, "theta", _max_theta, round(abs(_theta), 4))
                continue

            # Gate 8: liquidity score — composite of rel_volume (40%), call OI (30%), put OI (30%)
            # normalised: rel_volume already 0-1+; OI normalised against 500 (min_open_interest × 2.5)
            if gate_enabled and _min_liq > 0:
                _rvol_norm = min(1.0, (rel_vol or 0.0))
                _coi  = row.get("call_oi") or 0
                _poi  = row.get("put_oi")  or 0
                _oi_norm = min(1.0, ((_coi + _poi) / 2) / 500.0)
                _liq_score = round(0.40 * _rvol_norm + 0.60 * _oi_norm, 3)
                if _liq_score < _min_liq:
                    _reject(_t, struct, "liquidity_score", _min_liq, _liq_score)
                    continue
            else:
                _liq_score = None

            # Gate 9: regime-aware ROI — (max_profit × POP) / capital_per_share
            # Threshold scales with IV rank: high IV demands stronger return (premiums
            # are rich), low IV relaxes the floor (avoid starving in compressed envs).
            if gate_enabled and _min_roi > 0:
                from scripts.candidate_provider import compute_capital_required as _ccr
                _cap = _ccr(c)
                if _cap and _cap > 0:
                    _pop_est  = (c.get("pop") or 50.0) / 100.0
                    _mp       = c.get("max_profit") or 0.0
                    _roi      = (_mp * _pop_est) / (_cap / 100.0)

                    # Pick threshold based on IV rank — row-level field, not in candidate JSON
                    _ivr = row.get("iv_rank_52w") or row.get("iv_rank_proxy")
                    _iv_lo  = float(_mt.get("iv_low_threshold",  30))
                    _iv_hi  = float(_mt.get("iv_high_threshold", 60))
                    if _ivr is not None:
                        if _ivr >= _iv_hi:
                            _roi_thresh = float(_mt.get("roi_high_iv",   _min_roi))
                            _regime_lbl = f"high({_ivr:.0f}%)"
                        elif _ivr < _iv_lo:
                            _roi_thresh = float(_mt.get("roi_low_iv",    _min_roi))
                            _regime_lbl = f"low({_ivr:.0f}%)"
                        else:
                            _roi_thresh = float(_mt.get("roi_normal_iv", _min_roi))
                            _regime_lbl = f"normal({_ivr:.0f}%)"
                    else:
                        _roi_thresh = _min_roi  # fallback when IV rank unavailable
                        _regime_lbl = "unknown"

                    # Trending regime multiplier: 17.1% 5-day containment on Ubuntu backfill
                    # means directional tickers are far more likely to breach than the
                    # IV-rank-based threshold assumes. Raise the bar before granting entry.
                    # Skip for delta-neutral structures: their POP already degrades in trending
                    # markets via the delta formula — applying the multiplier double-penalizes them.
                    _ml_regime = (row.get("ml") or {}).get("regime", "")
                    _trending_mult = float(_mt.get("trending_roi_multiplier", 1.0))
                    _neutral_thresh = float(_mt.get("neutral_delta_threshold", 0.10))
                    _cand_net_delta = abs(c.get("net_delta") or 0.0)
                    _is_neutral = _cand_net_delta <= _neutral_thresh
                    if _ml_regime == "Trending" and _trending_mult > 1.0 and not _is_neutral:
                        _roi_thresh = round(_roi_thresh * _trending_mult, 3)
                        _regime_lbl = f"{_regime_lbl}+trending(×{_trending_mult})"

                    if _roi < _roi_thresh:
                        _reject(_t, struct, "expected_roi", round(_roi_thresh, 3), round(_roi, 3))
                        if struct == "Iron Condor":
                            log.info(
                                "[gate:roi] %s Iron Condor — put_credit=$%.2f call_credit=$%.2f "
                                "total=$%.2f capital=$%.2f roi=%.1f%% thresh=%.1f%% regime=%s",
                                _t,
                                c.get("ic_put_credit", 0), c.get("ic_call_credit", 0),
                                _mp, _cap / 100.0, _roi * 100, _roi_thresh * 100, _regime_lbl,
                            )
                        else:
                            log.debug(
                                "[gate:roi] %s %s — roi=%.3f < thresh=%.3f regime=%s",
                                _t, struct, _roi, _roi_thresh, _regime_lbl,
                            )
                        _rr = attempt_repairs(c, _repair_context)
                        if _rr.status == "PASS":
                            log.info(
                                "[repair] %s %s — roi gate failed; "
                                "injecting %d variant(s) for full gate re-evaluation",
                                _t, struct, len(_rr.replacement_candidates),
                            )
                            _cqueue.extend(_rr.replacement_candidates)
                        _cap_rejected.append({
                            "ticker":           _t,
                            "structure":        struct,
                            "required_capital": None,
                            "buying_power":     round(buying_power, 2) if buying_power else None,
                            "bp_limit":         None,
                            "shortfall":        None,
                            "put_long_strike":   c.get("put_long_strike"),
                            "put_short_strike":  c.get("put_short_strike"),
                            "call_short_strike": c.get("call_short_strike"),
                            "call_long_strike":  c.get("call_long_strike"),
                            "long_strike":       c.get("long_strike") or c.get("put_long_strike") or c.get("call_long_strike"),
                            "short_strike":      c.get("short_strike") or c.get("put_short_strike") or c.get("call_short_strike"),
                            "expiry":           row.get("expiry") or c.get("expiry"),
                            "dte":              row.get("dte")    or c.get("dte"),
                            "pop":              c.get("pop"),
                            "net_credit":       c.get("net_credit") or c.get("max_profit"),
                            "ev":               round(float(c.get("ev") or 0), 4),
                            "max_profit":       c.get("max_profit"),
                            "max_loss":         c.get("max_loss"),
                            "signal_score":     c.get("signal_score"),
                            "net_delta":        c.get("net_delta"),
                            "net_theta":        c.get("net_theta"),
                            "iv_edge_vp":       c.get("iv_edge_vp"),
                            "short_leg_oi":     c.get("short_leg_oi"),
                            "short_leg_volume": c.get("short_leg_volume"),
                            "short_leg_ba_pct": c.get("short_leg_ba_pct"),
                            "candidate_id":     c.get("candidate_id"),
                            "repair_attempted": _rr.status == "PASS",
                            "repair_failure_reason": _rr.failure_reason,
                            "gate":             "expected_roi",
                        })
                        continue

            # Gate 10: capital feasibility — skip trades that exceed the usable
            # fraction of available buying power. Runs only when buying_power is
            # supplied (paper-trade morning scan); live suggestions skip this gate.
            # `> 0` here used to double as "buying_power was actually passed" —
            # but that conflates "not supplied" (None, live suggestions) with
            # "supplied and legitimately <=0" (an over-committed book), silently
            # skipping this gate exactly when it matters most. Fixed 2026-08-24
            # alongside the check_circuit_breakers() capital-accounting fix that
            # made buying_power correctly go negative for the first time.
            if buying_power is not None:
                from scripts.candidate_provider import compute_capital_required as _ccr
                _cap_needed = _ccr(c) or 0.0
                _util_cap   = _g("gate", "max_capital_utilization", 1.0)
                _bp_limit   = buying_power * _util_cap
                if _cap_needed > 0 and _cap_needed > _bp_limit:
                    log.info(
                        "[gate:capital] %s %s rejected — required $%.2f > "
                        "buying_power $%.2f × %.0f%% = $%.2f",
                        _t, struct, _cap_needed, buying_power, _util_cap * 100, _bp_limit,
                    )
                    _reject(_t, struct, "capital_feasibility", round(_bp_limit, 2), round(_cap_needed, 2))
                    _rr = attempt_repairs(c, _repair_context)
                    if _rr.status == "PASS":
                        log.info(
                            "[repair] %s %s — capital gate failed; "
                            "injecting %d variant(s) for full gate re-evaluation",
                            _t, struct, len(_rr.replacement_candidates),
                        )
                        _cqueue.extend(_rr.replacement_candidates)
                    _cap_rejected.append({
                        "ticker":           _t,
                        "structure":        struct,
                        "required_capital": round(_cap_needed, 2),
                        "buying_power":     round(buying_power, 2),
                        "bp_limit":         round(_bp_limit, 2),
                        "shortfall":        round(_cap_needed - _bp_limit, 2),
                        "long_strike":      c.get("long_strike") or c.get("put_long_strike") or c.get("call_long_strike"),
                        "short_strike":     c.get("short_strike") or c.get("put_short_strike") or c.get("call_short_strike"),
                        "put_long_strike":   c.get("put_long_strike"),
                        "put_short_strike":  c.get("put_short_strike"),
                        "call_short_strike": c.get("call_short_strike"),
                        "call_long_strike":  c.get("call_long_strike"),
                        "long_put_strike":   c.get("long_put_strike"),
                        "short_put_strike":  c.get("short_put_strike"),
                        "short_call_strike": c.get("short_call_strike"),
                        "long_call_strike":  c.get("long_call_strike"),
                        "call_strike":       c.get("call_strike"),
                        "put_strike":        c.get("put_strike"),
                        "expiry":           row.get("expiry") or c.get("expiry"),
                        "dte":              row.get("dte")    or c.get("dte"),
                        "pop":              c.get("pop"),
                        "net_credit":       c.get("net_credit") or c.get("max_profit"),
                        "ev":               round(float(c.get("ev") or 0), 4),
                        "max_profit":       c.get("max_profit"),
                        "max_loss":         c.get("max_loss"),
                        "signal_score":     c.get("signal_score"),
                        "net_delta":        c.get("net_delta"),
                        "net_theta":        c.get("net_theta"),
                        "iv_edge_vp":       c.get("iv_edge_vp"),
                        "short_leg_oi":     c.get("short_leg_oi"),
                        "short_leg_volume": c.get("short_leg_volume"),
                        "short_leg_ba_pct": c.get("short_leg_ba_pct"),
                        "candidate_id":     c.get("candidate_id"),
                        "repair_attempted": _rr.status == "PASS",
                        "repair_failure_reason": _rr.failure_reason,
                        "gate":             "capital_feasibility",
                    })
                    continue

            # Gate 11: liquidity — short leg OI, volume, and bid-ask spread.
            # Thresholds from scoring.toml [gate]; defaults are conservative floors
            # that still allow small-cap names (OI≥50 is already enforced per-leg
            # by leg_liquid() during chain fetch; ranker floor is a second check
            # on the worst leg across the whole candidate after optimization).
            _min_oi_ranker  = _g("gate", "ranker_min_oi",     50)
            _max_ba_ranker  = _g("gate", "ranker_max_ba_pct",  0.30)
            _min_vol_ranker = _g("gate", "ranker_min_volume",  10)
            _cand_oi  = c.get("short_leg_oi")
            _cand_ba  = c.get("short_leg_ba_pct")
            _cand_vol = c.get("short_leg_volume")
            if _min_oi_ranker and _cand_oi is not None and _cand_oi < _min_oi_ranker:
                _reject(_t, struct, "liquidity", _min_oi_ranker, _cand_oi)
                log.debug("[gate:liquidity] %s %s — OI %d < %d", _t, struct, _cand_oi, _min_oi_ranker)
                continue
            if _max_ba_ranker and _cand_ba is not None and _cand_ba > _max_ba_ranker:
                _reject(_t, struct, "liquidity", _max_ba_ranker, round(_cand_ba, 3))
                log.debug("[gate:liquidity] %s %s — spread %.1f%% > %.0f%%", _t, struct, _cand_ba * 100, _max_ba_ranker * 100)
                continue
            if _min_vol_ranker and _cand_vol is not None and _cand_vol < _min_vol_ranker:
                _reject(_t, struct, "liquidity", _min_vol_ranker, _cand_vol)
                log.debug("[gate:liquidity] %s %s — volume %d < %d", _t, struct, _cand_vol, _min_vol_ranker)
                continue

            # Strip transient chain refs before storing in result
            c.pop("_ibf_puts",    None)
            c.pop("_ibf_calls",   None)
            c.pop("_ds_chain",    None)
            c.pop("_ds_option_type", None)
            c.pop("_csp_puts",    None)
            c.pop("_ls_puts",     None)
            c.pop("_ls_calls",    None)

            # Advisory note: historical survival from ticker_profile_snapshots.
            # Zero effect on EV, gates, or ranking — informational only.
            _prof = _profiles.get(_t)
            if _prof and (_prof.get("profile_quality") or 0) >= 0.40:
                _ml_reg   = (row.get("ml") or {}).get("regime", "")
                _reg_key  = {
                    "Mean-reverting":    "mr",
                    "Trending":          "tr",
                    "Low-vol-squeeze":   "lv",
                    "High-vol-breakout": "hv",
                }.get(_ml_reg)
                if _reg_key:
                    _surv  = _prof.get(f"bayes_survival_{_reg_key}")
                    _n_reg = _prof.get(f"n_{_reg_key}", 0) or 0
                    _lo    = _prof.get("survival_lo95")
                    _hi    = _prof.get("survival_hi95")
                    _pq    = _prof.get("profile_quality", 0)
                    if _surv is not None and _lo is not None and _hi is not None:
                        c["historical_note"] = (
                            f"Historical: {_t} in {_ml_reg} regime survived "
                            f"{_surv:.0%} (n={_n_reg}, 95% CI: {_lo:.0%}–{_hi:.0%}). "
                            f"Profile quality: {_pq:.2f}."
                        )

            # MC EV — run after all gates pass to avoid wasting simulation time
            # on rejected candidates. Use 2,000 sims for batch ranking (rank
            # stability test confirmed identical ordering vs 5,000 sims).
            # ev_mc replaces the delta-proxy as the primary EV when available.
            # Direct import of run_mc (GARCH(1,1) + GBM fallback); bypasses the
            # candidate_provider wrapper which adds no logic beyond delegation.
            try:
                from scripts.monte_carlo import run_mc as _run_mc
                _mc = _run_mc(_t, row, c, n_sims=2000)
            except Exception:
                _mc = None

            ev_mc       = round(_mc["expected_pnl"], 4) if _mc else None
            ev          = ev_mc if ev_mc is not None else ev_delta_proxy
            ev_is_proxy = ev_mc is None

            # Store MC metrics on candidate dict for downstream use (CVaR penalty,
            # prob_of_touch display, trade record snapshotting).
            if _mc:
                c["mc_expected_pnl"]    = ev_mc
                c["mc_cvar_loss"]       = _mc.get("cvar_loss")
                c["mc_prob_profit_sim"] = _mc.get("prob_profit_sim")
                c["mc_prob_of_touch"]   = _mc.get("prob_of_touch")
                c["mc_worst_loss_95"]   = _mc.get("worst_loss_95")
                c["mc_vol_source"]      = _mc.get("vol_source")
                c["mc_p10_pnl"]         = _mc.get("p10_pnl")
                c["mc_p90_pnl"]         = _mc.get("p90_pnl")
                # Phase 2A: S_T distribution summary (observational — not used in ranking)
                c["mc_expiry_mean"]     = _mc.get("mc_expiry_mean")
                c["mc_expiry_median"]   = _mc.get("mc_expiry_median")
                c["mc_expiry_p10"]      = _mc.get("mc_expiry_p10")
                c["mc_expiry_p25"]      = _mc.get("mc_expiry_p25")
                c["mc_expiry_p50"]      = _mc.get("mc_expiry_p50")
                c["mc_expiry_p75"]      = _mc.get("mc_expiry_p75")
                c["mc_expiry_p90"]      = _mc.get("mc_expiry_p90")
                c["distribution_model_version"] = _mc.get("distribution_model_version")
                # Zone probabilities (price-space, structure-specific)
                for _zk in ("mc_zone_below_long", "mc_zone_between", "mc_zone_above_short",
                            "mc_zone_below_put_long", "mc_zone_in_loss_put", "mc_zone_in_profit",
                            "mc_zone_in_loss_call", "mc_zone_above_call_long",
                            "mc_zone_below_short"):
                    if _zk in _mc:
                        c[_zk] = _mc[_zk]

            # P1-B: evidence volume for this (structure, regime) pair
            _struct_key  = c.get("structure") or ""
            _regime_key  = row.get("market_regime") or ""
            _ev_n        = _count_comparable_trades(_struct_key, _regime_key)
            _conf_tier   = _confidence_tier(_ev_n)

            # P1-D: repair lineage — original_candidate_id is repair_of for max-depth=1
            _repair_of   = c.get("repair_of")
            _orig_cid    = _repair_of  # None for original candidates

            result.append({
                "row":                  row,
                "candidate":            c,
                "ev":                   round(ev, 4),
                "ev_delta_proxy":       ev_delta_proxy,
                "ev_mc":                ev_mc,
                "ev_is_proxy":          ev_is_proxy,
                "meets_both":           (
                    bool(c.get("meets_min_profit"))
                    and c.get("meets_max_loss") is not False
                ),
                "iv_edge_vp":           iv_edge_vp,
                "iv_edge_label":        iv_edge_label,
                "pred_dist":            pred_dist,
                "liquidity_score":      _liq_score,
                "evidence_n":           _ev_n,
                "confidence_tier":      _conf_tier,
                "original_candidate_id": _orig_cid,
            })

    if _rejections:
        from collections import Counter as _Ctr
        _by_gate = _Ctr(r["gate"] for r in _rejections)
        log.info(f"[filter] {len(_rejections)} rejected, {len(result)} survived — "
                 f"gate breakdown: {dict(_by_gate)}")
        _si_structs = _Ctr(
            r["structure"] for r in _rejections if r["gate"] == "strikes_incomplete"
        )
        if _si_structs:
            log.info(f"[filter] strikes_incomplete by structure: "
                     f"{dict(_si_structs.most_common())}")
        log.debug(f"[filter] rejection detail: {_rejections}")

    # P1-D: Capture the complete pre-dedup candidate universe as the decision snapshot.
    # This preserves all repair variants and gate-surviving candidates before the
    # within-group dedup collapses them.  Stored fields are the minimal set needed
    # to reconstruct the decision state at entry: candidate identity, repair lineage,
    # gates passed, EV, POP, composite (not yet computed here), and rejection reason.
    # trend + mc_expiry_p10-p90 added 2026-08-25: previously any analysis of
    # structure eligibility vs. trend/forecast width needed a fuzzy day-level
    # join against training_snapshots (imprecise — trend can change within a
    # day) and had no MC distribution data for rejected candidates at all.
    # trend is now the exact value active at rejection/survival time
    # (closure-captured in _reject() above, or read directly off `row` here).
    # mc_expiry_* now comes from analyze_ticker()'s ticker/expiry-level
    # simulation (computed once, independent of any structure's strikes — see
    # [[project_mc_forecast_structure_eligibility]]), so it's populated for
    # BOTH survived and rejected entries, including strikes_incomplete, which
    # was the whole point. Survived entries prefer the candidate-level value
    # (set later in this pipeline via monte_carlo.run_mc with the candidate's
    # own DTE, which can differ from row's for e.g. diagonal spreads) and fall
    # back to the row-level ticker/expiry simulation if that's unavailable.
    decision_snapshot = [
        {
            "candidate_id":          _r["candidate"].get("candidate_id"),
            "structure":             _r["candidate"].get("structure"),
            "ticker":                _r["row"].get("ticker") or _r["candidate"].get("ticker"),
            "expiry":                _r["row"].get("expiry") or _r["candidate"].get("expiry"),
            "trend":                 _r["row"].get("trend"),
            "repair_of":             _r["candidate"].get("repair_of"),
            "original_candidate_id": _r["original_candidate_id"],
            "ev":                    _r["ev"],
            "ev_mc":                 _r["ev_mc"],
            "ev_is_proxy":           _r["ev_is_proxy"],
            "pop":                   _r["candidate"].get("pop"),
            "evidence_n":            _r["evidence_n"],
            "confidence_tier":       _r["confidence_tier"],
            "disposition":           "survived_filter",
            "rejection_reason":      None,
            "mc_expiry_p10":         _r["candidate"].get("mc_expiry_p10") or _r["row"].get("mc_expiry_p10"),
            "mc_expiry_p25":         _r["candidate"].get("mc_expiry_p25") or _r["row"].get("mc_expiry_p25"),
            "mc_expiry_p50":         _r["candidate"].get("mc_expiry_p50") or _r["row"].get("mc_expiry_p50"),
            "mc_expiry_p75":         _r["candidate"].get("mc_expiry_p75") or _r["row"].get("mc_expiry_p75"),
            "mc_expiry_p90":         _r["candidate"].get("mc_expiry_p90") or _r["row"].get("mc_expiry_p90"),
        }
        for _r in result
    ] + [
        {
            "candidate_id":          _rej.get("candidate_id"),
            "structure":             _rej.get("structure"),
            "ticker":                _rej.get("ticker"),
            "expiry":                _rej.get("expiry"),
            "trend":                 _rej.get("trend"),
            "repair_of":             _rej.get("repair_of"),
            "original_candidate_id": _rej.get("repair_of"),
            "ev":                    None,
            "ev_mc":                 None,
            "ev_is_proxy":           None,
            "pop":                   None,
            "evidence_n":            None,
            "confidence_tier":       None,
            "disposition":           "rejected",
            "rejection_reason":      _rej.get("gate"),
            "mc_expiry_p10":         _rej.get("mc_expiry_p10"),
            "mc_expiry_p25":         _rej.get("mc_expiry_p25"),
            "mc_expiry_p50":         _rej.get("mc_expiry_p50"),
            "mc_expiry_p75":         _rej.get("mc_expiry_p75"),
            "mc_expiry_p90":         _rej.get("mc_expiry_p90"),
        }
        for _rej in _rejections
    ]

    # Within-group dedup: keep best EV per (ticker, structure, expiry).
    # Prevents multiple repair variants of the same thesis from all surviving
    # into the final ranked pool — only the highest-EV feasible variant advances.
    if result:
        _dedup: dict = {}
        for _r in result:
            _rc   = _r["candidate"]
            _key  = (
                _r["row"].get("ticker") or _rc.get("ticker") or "",
                _rc.get("structure") or "",
                _r["row"].get("expiry") or _rc.get("expiry") or "",
            )
            if _key not in _dedup or _r["ev"] > _dedup[_key]["ev"]:
                _dedup[_key] = _r
        _before = len(result)
        result = list(_dedup.values())
        if len(result) < _before:
            log.info(
                "[filter] within-group dedup: %d → %d candidates "
                "(%d duplicate variants removed)",
                _before, len(result), _before - len(result),
            )

    return result, _cap_rejected, decision_snapshot


def _latest_greeks(trade: dict) -> dict:
    """
    Return the most recently remarked Greeks for an open position.
    Prefers the last evening-check snapshot (which contains live-priced Greeks
    from compute_position_greeks), falls back to entry-time values from the
    trade dict itself.  Always returns a dict with net_delta/theta/gamma/vega.
    """
    snaps = trade.get("snapshots") or []
    for snap in reversed(snaps):
        if snap.get("net_delta") is not None:
            return {
                "net_delta": snap["net_delta"],
                "net_theta": snap.get("net_theta", trade.get("net_theta") or 0.0),
                "net_gamma": snap.get("net_gamma", trade.get("net_gamma") or 0.0),
                "net_vega":  snap.get("net_vega",  trade.get("net_vega")  or 0.0),
            }
    # No remarked snapshot yet — use entry-time values
    return {
        "net_delta": trade.get("net_delta") or 0.0,
        "net_theta": trade.get("net_theta") or 0.0,
        "net_gamma": trade.get("net_gamma") or 0.0,
        "net_vega":  trade.get("net_vega")  or 0.0,
    }


def _portfolio_risk_check(best: dict, open_positions: list) -> dict:
    """
    Remove tickers that would violate portfolio concentration rules.
    open_positions: list of trade dicts from paper_trades.json (status='open').
    Rules:
      - No duplicate ticker (already have open exposure in this name)
      - Max 2 open trades per sector ETF
      - Total capital deployed cap: MAX_TOTAL_DEPLOYMENT_PCT of notional
      - Net portfolio delta cap: reject candidates that push |net_delta| beyond threshold
      - Net portfolio theta cap: reject when portfolio theta would exceed limit
      - Net portfolio gamma cap: reject when portfolio gamma would exceed limit
    """
    if not open_positions:
        return best

    _MAX_SECTOR_COUNT    = _g("portfolio_risk", "max_sector_count",    2)
    _MAX_TICKER_TRADES   = _g("portfolio_risk", "max_ticker_trades",   1)
    _MAX_DEPLOYMENT_PCT  = _g("portfolio_risk", "max_deployment_pct", 20.0)
    _MAX_NET_DELTA       = _g("portfolio_risk", "max_net_delta",       3.0)
    _MAX_NET_THETA       = _g("portfolio_risk", "max_net_theta",       0.0)   # 0 = gate disabled
    _MAX_NET_GAMMA       = _g("portfolio_risk", "max_net_gamma",       0.0)   # 0 = gate disabled

    from collections import Counter
    open_tickers = Counter(p.get("ticker", "") for p in open_positions)
    open_sectors = Counter(p.get("sector_etf") or p.get("sector", "")
                           for p in open_positions)
    total_capital = sum(
        (p.get("capital_required") or 0) for p in open_positions
    )

    # Use remarked Greeks from last evening-check snapshot (not entry-time).
    # This is the critical fix: entry-time delta becomes wildly stale after a
    # significant underlying move, causing the gate to underestimate real exposure.
    _pos_greeks = [_latest_greeks(p) for p in open_positions]
    portfolio_delta = sum(g["net_delta"] for g in _pos_greeks)
    portfolio_theta = sum(g["net_theta"] for g in _pos_greeks)
    portfolio_gamma = sum(g["net_gamma"] for g in _pos_greeks)

    _remarked = sum(
        1 for p in open_positions
        if any(s.get("net_delta") is not None for s in (p.get("snapshots") or []))
    )
    log.debug(
        "[risk] portfolio net_delta=%+.3f net_theta=%.4f net_gamma=%.6f "
        "from %d open positions (%d remarked, %d entry-time)",
        portfolio_delta, portfolio_theta, portfolio_gamma,
        len(open_positions), _remarked, len(open_positions) - _remarked,
    )

    result = {}
    for ticker, item in best.items():
        # Ticker concentration
        if open_tickers.get(ticker, 0) >= _MAX_TICKER_TRADES:
            log.info(f"[risk] Skip {ticker} — already have {open_tickers[ticker]} open position(s)")
            continue
        # Sector concentration
        sector = (item["row"].get("sector_etf") or item["row"].get("sector") or "")
        if sector and open_sectors.get(sector, 0) >= _MAX_SECTOR_COUNT:
            log.info(f"[risk] Skip {ticker} — sector {sector} already has {open_sectors[sector]} open trades")
            continue

        c = item["candidate"]

        # Net delta cap — use live-remarked portfolio delta
        candidate_delta = c.get("net_delta") or 0.0
        projected_delta = portfolio_delta + candidate_delta
        if abs(projected_delta) > _MAX_NET_DELTA:
            log.info(
                "[risk] Skip %s — net_delta would reach %+.3f (cap ±%.1f; remarked=%d/total=%d)",
                ticker, projected_delta, _MAX_NET_DELTA, _remarked, len(open_positions),
            )
            continue

        # Net theta cap (positive = we receive theta decay; gate limits total exposure)
        if _MAX_NET_THETA > 0:
            candidate_theta = c.get("net_theta") or 0.0
            projected_theta = portfolio_theta + candidate_theta
            if abs(projected_theta) > _MAX_NET_THETA:
                log.info(
                    "[risk] Skip %s — net_theta would reach %.4f (cap ±%.4f)",
                    ticker, projected_theta, _MAX_NET_THETA,
                )
                continue

        # Net gamma cap (limits convexity/gap risk)
        if _MAX_NET_GAMMA > 0:
            candidate_gamma = c.get("net_gamma") or 0.0
            projected_gamma = portfolio_gamma + candidate_gamma
            if abs(projected_gamma) > _MAX_NET_GAMMA:
                log.info(
                    "[risk] Skip %s — net_gamma would reach %.6f (cap ±%.6f)",
                    ticker, projected_gamma, _MAX_NET_GAMMA,
                )
                continue

        # Rolling 60-day price correlation check
        # Warn (not block) when candidate ticker is highly correlated with any open position.
        _CORR_WARN_THRESHOLD = _g("portfolio_risk", "corr_warn_threshold", 0.75)
        _corr_warnings = _correlation_warnings(ticker, open_positions, _CORR_WARN_THRESHOLD)
        if _corr_warnings:
            item["corr_warnings"] = _corr_warnings
            log.info("[risk] %s corr warning: %s", ticker, "; ".join(_corr_warnings))

        result[ticker] = item

    return result


def _correlation_warnings(candidate_ticker: str, open_positions: list, threshold: float = 0.75) -> list[str]:
    """
    Compute 60-day rolling return correlation between candidate_ticker and every
    open position's ticker. Return a list of warning strings for pairs that
    exceed threshold. Returns [] when price data is unavailable or no open positions.
    """
    open_tickers = list({p.get("ticker", "") for p in open_positions if p.get("ticker")})
    if not open_tickers:
        return []
    try:
        import yfinance as yf
        import pandas as pd
        all_tickers = list({candidate_ticker} | set(open_tickers))
        raw = yf.download(all_tickers, period="90d", auto_adjust=True, progress=False)
        if raw.empty:
            return []
        closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(name=all_tickers[0])
        returns = closes.pct_change().dropna()
        if candidate_ticker not in returns.columns:
            return []
        warnings = []
        cand_ret = returns[candidate_ticker]
        for ot in open_tickers:
            if ot not in returns.columns or ot == candidate_ticker:
                continue
            tail = min(60, len(returns))
            corr = cand_ret.iloc[-tail:].corr(returns[ot].iloc[-tail:])
            if corr is not None and abs(corr) >= threshold:
                warnings.append(
                    f"{candidate_ticker}↔{ot} corr={corr:+.2f} (>{threshold:.2f})"
                )
        return warnings
    except Exception as _e:
        log.debug(f"[corr] correlation check failed: {_e}")
        return []


def _position_size_factor(ml: dict) -> float:
    """
    Dynamic position sizing: return_score × iv_confidence × regime_confidence × (1 - anomaly).
    Returns a factor in [0.05, 1.0] — multiply base position size by this value.
    Missing signals default to neutral (0.5 for probabilities, 0 for anomaly).
    """
    return_score      = (ml.get("return_score") or 50.0) / 100.0
    iv_confidence     = ml.get("iv_confidence") or 0.5
    regime_confidence = (ml.get("composite_score") or 50.0) / 100.0
    anomaly_norm      = (ml.get("anomaly_score") or 0.0) / 100.0
    raw = return_score * iv_confidence * regime_confidence * (1.0 - anomaly_norm)
    return round(min(max(raw, 0.05), 1.0), 4)


def _suggested_allocation(composite: float) -> float:
    """Tiered position sizing: map composite score to portfolio allocation %."""
    if composite >= _g("allocation", "tier1_threshold", 80):
        return _g("allocation", "tier1_pct", 4.0)
    if composite >= _g("allocation", "tier2_threshold", 70):
        return _g("allocation", "tier2_pct", 2.0)
    return _g("allocation", "tier3_pct", 1.0)


def rank_candidates(rows, n=3, score_fn=None, quality_floor=None, open_positions=None, paper_trade: bool = False, buying_power: float | None = None, _cap_rejected_out: list | None = None, _decision_meta_out: list | None = None):  # noqa: score_fn kept for API compat
    """
    Steps 3-5: Score → best per ticker → quality gate → rank tickers → top-n.

    score_fn is accepted but ignored; composite score (config/ranking.toml) is used
    for gates and penalties, but when the return ranker is available its score is the
    primary sort key so the portfolio engine directly optimizes ranking quality.

    quality_floor: override the min_composite gate. Pass 0 (paper trades) to
    always return the top-n by score regardless of quality threshold — every
    outcome, good or bad, is training data.
    """
    _n_evaluated = len(rows)   # candidates entering the full pipeline
    items, _cap_rej, _dec_snapshot = filter_candidates(rows, paper_trade=paper_trade, buying_power=buying_power)
    if _cap_rejected_out is not None:
        _cap_rejected_out.extend(_cap_rej)
    _n_survived_filter = len(items)

    if not items:
        if _decision_meta_out is not None:
            _decision_meta_out.append({
                "status":                       "NO_TRADE",
                "reason":                       "NO_CANDIDATES_SURVIVED_FILTER",
                "best_score":                   None,
                "required_score":               None,
                "candidates_evaluated":         _n_evaluated,
                "candidates_survived_filter":   0,
                "candidates_cleared_quality_gate": 0,
                "portfolio_eligible":           0,
                "market_regime":                None,
                "repair_summary":               [],
                "decision_snapshot":            _dec_snapshot,
            })
        return []

    # Step 3a: Percentile-rank EV (capital-normalized) across surviving candidates.
    #
    # Unit: EVROC = ev / capital_required (EV return on capital).
    # Ranking raw per-share EV favours high-spot tickers regardless of capital
    # efficiency. EVROC makes a $0.42 EV on a $500 IC comparable to a $0.42 EV
    # on a $50 IC (the latter has higher EVROC and should rank higher).
    # Falls back to raw ev when capital_required is None.
    #
    # Source-aware grouping: MC EV and delta-proxy EV are not interchangeable
    # estimators. Group by ev_is_proxy, rank each group independently, then merge.
    # Fall back to combined ranking when either group has fewer than 5 candidates.
    _MIN_GROUP = 5
    _mc_idx    = [i for i, it in enumerate(items) if not it.get("ev_is_proxy")]
    _prx_idx   = [i for i, it in enumerate(items) if it.get("ev_is_proxy")]
    _use_split = len(_mc_idx) >= _MIN_GROUP and len(_prx_idx) >= _MIN_GROUP

    def _evroc(item):
        """EV / capital_required (EVROC). ev is $/share; cap is $/contract → divide by cap/100."""
        ev  = item["ev"]
        cap = item["candidate"].get("capital_required")
        return ev / (cap / 100.0) if cap else ev

    def _pct_rank_group(indices):
        """Return {orig_idx: pct_rank} ranked by EVROC within the group."""
        vals = [(_evroc(items[i]), i) for i in indices]
        vals.sort(key=lambda x: x[0])
        n = len(vals)
        return {orig: round(pos / max(n - 1, 1) * 100, 1)
                for pos, (_, orig) in enumerate(vals)}

    if _use_split:
        _ranks = {**_pct_rank_group(_mc_idx), **_pct_rank_group(_prx_idx)}
    else:
        _all_idx   = list(range(len(items)))
        _ranks     = _pct_rank_group(_all_idx)

    for i, item in enumerate(items):
        item["candidate"]["_ev_pct_rank"] = _ranks[i]

    # Step 3a2: Percentile-rank bid-ask spread across surviving candidates.
    # Lower short_leg_ba_pct = tighter spread = better liquidity = higher rank.
    # Blended into liquidity_score (30% weight) so the tiebreaker sort picks it up
    # automatically without changing the sort key.
    _ba_vals = [item["candidate"].get("short_leg_ba_pct") for item in items]
    _ba_present = [v for v in _ba_vals if v is not None]
    if len(_ba_present) >= 2:
        _sorted_ba = sorted(
            range(len(items)),
            key=lambda i: _ba_vals[i] if _ba_vals[i] is not None else float("inf"),
        )
        # rank_pos 0 = tightest spread (best) → assign highest pct_rank
        _n_ba = len(items)
        for _rank_pos, _orig_idx in enumerate(_sorted_ba):
            _ba_pct = round((_n_ba - 1 - _rank_pos) / max(_n_ba - 1, 1) * 100, 1)
            item = items[_orig_idx]
            item["candidate"]["_ba_liq_pct_rank"] = _ba_pct
            # Only blend when both Gate-8 liquidity_score and spread data exist
            if _ba_vals[_orig_idx] is not None and item["liquidity_score"] is not None:
                item["liquidity_score"] = round(
                    0.70 * item["liquidity_score"] + 0.30 * (_ba_pct / 100.0), 3
                )

    # Step 3b: Per-candidate family POP score (structure-specific model when available).
    # Falls back to pooled pop_score from regime_predictor when no family model exists.
    # Injected into candidate dict as _family_pop_score; _composite_score prefers it.
    try:
        import joblib as _jl, pandas as _pd
        from scripts.train_pop_model import (
            build_feature_matrix as _pop_bfm,
            _family_for_structure as _fam_of,
            _family_model_path as _fam_path,
        )
        _fam_cache: dict = {}
        for _item in items:
            _struct = _item["candidate"].get("structure") or ""
            if not _struct:
                continue
            _fam = _fam_of(_struct)
            if _fam not in _fam_cache:
                _cal = _fam_path(_fam).with_name(f"pop_{_fam}_calibrated.joblib")
                _base = _fam_path(_fam)
                try:
                    _fam_cache[_fam] = _jl.load(_cal) if _cal.exists() else (
                        _jl.load(_base) if _base.exists() else None)
                except Exception:
                    _fam_cache[_fam] = None
            _art = _fam_cache[_fam]
            if _art is None:
                continue
            try:
                _merged = {**_item["row"], **_item["candidate"]}
                _Xf, _ = _pop_bfm(_pd.DataFrame([_merged]),
                                   encoders=_art.get("feature_encoders"), fit=False)
                _drop = _art.get("dropped_cols") or []
                if _drop:
                    _Xf = _Xf.drop(columns=[c for c in _drop if c in _Xf.columns])
                _fp = round(float(_art["model"].predict_proba(_Xf)[0][1]), 4)
                _item["candidate"]["_family_pop_score"] = _fp
            except Exception as _fe:
                log.debug("[pop_family] %s %s → %s", _item["row"].get("ticker"), _struct, _fe)
    except ImportError:
        pass  # train_pop_model not available

    # Step 3c: Composite score (used for quality gate + tie-break) and ranker score
    for item in items:
        item["composite"] = _composite_score(
            item["row"], item["candidate"], item["ev"], item["ev_is_proxy"]
        )
        ml = item["row"].get("ml") or {}
        item["ranker_score"] = ml.get("ranker_score")  # None when ranker not trained
        item["position_size_factor"] = _position_size_factor(ml)

    # Repair lift: for each repair group, record original vs best repair composite.
    # Collected here (after scoring, before dedup) for inclusion in decision_meta.
    # Not optimised on — just accumulated for future diagnostic analysis.
    _repair_summary: list = []
    if _decision_meta_out is not None:
        _orig_by_id   = {it["candidate"].get("candidate_id"): it for it in items
                         if it["candidate"].get("repair_of") is None}
        _repairs_by_orig: dict = {}
        for _it in items:
            _rof = _it["candidate"].get("repair_of")
            if _rof is not None:
                _repairs_by_orig.setdefault(_rof, []).append(_it)
        for _oid, _orig_item in _orig_by_id.items():
            _variants = _repairs_by_orig.get(_oid) or []
            if not _variants:
                continue
            _best_repair = max(_variants, key=lambda x: x["composite"])
            _repair_summary.append({
                "original_candidate_id": _oid,
                "structure":             _orig_item["candidate"].get("structure"),
                "ticker":                _orig_item["row"].get("ticker"),
                "original_composite":    _orig_item["composite"],
                "best_repair_composite": _best_repair["composite"],
                "repair_count":          len(_variants),
                "repair_lift":           round(_best_repair["composite"] - _orig_item["composite"], 2),
            })

    # Step 4: Best candidate per ticker (prefer higher ranker_score; fall back to composite)
    best: dict[str, dict] = {}
    for item in items:
        ticker = item["row"].get("ticker", "")
        if ticker not in best:
            best[ticker] = item
        else:
            prev = best[ticker]
            # Compare on ranker_score when both have it; else composite
            if item["ranker_score"] is not None and prev["ranker_score"] is not None:
                if item["ranker_score"] > prev["ranker_score"]:
                    best[ticker] = item
            elif item["composite"] > prev["composite"]:
                best[ticker] = item

    # Step 4b: Dynamic quality floor (#10)
    # Use rolling percentile (top decile of batch) instead of a fixed threshold.
    # Falls back to the static floor when the batch is too small to be meaningful.
    static_floor = quality_floor if quality_floor is not None else _g("quality", "min_composite", 55)
    if len(best) >= 10:
        _scores     = sorted(v["composite"] for v in best.values())
        _p90_idx    = int(len(_scores) * 0.10)   # bottom of top-decile
        _dynamic_floor = _scores[_p90_idx]
        min_q = max(static_floor, _dynamic_floor)
        log.debug(f"[rank] dynamic quality floor: {min_q:.1f} (static={static_floor}, p90={_dynamic_floor:.1f})")
    else:
        min_q = static_floor

    _n_best_per_ticker  = len(best)
    _best_score         = max((v["composite"] for v in best.values()), default=None)
    _regime             = (items[0]["row"].get("market_regime") if items else None)
    best = {t: v for t, v in best.items() if v["composite"] >= min_q}
    _n_cleared_quality  = len(best)

    # P1-C: NO TRADE — quality gate eliminated all remaining tickers
    if not best:
        if _decision_meta_out is not None:
            _decision_meta_out.append({
                "status":                          "NO_TRADE",
                "reason":                          "NO_QUALIFIED_CANDIDATES",
                "best_score":                      _best_score,
                "required_score":                  min_q,
                "candidates_evaluated":            _n_evaluated,
                "candidates_survived_filter":      _n_survived_filter,
                "candidates_cleared_quality_gate": 0,
                "portfolio_eligible":              0,
                "market_regime":                   _regime,
                "repair_summary":                  _repair_summary,
                "decision_snapshot":               _dec_snapshot,
            })
        log.info(
            "[rank] NO_TRADE(quality): best=%.1f required=%.1f survived_filter=%d regime=%s",
            _best_score if _best_score is not None else -1, min_q,
            _n_survived_filter, _regime,
        )
        return []

    # Step 4c: Portfolio risk check — removes tickers that breach concentration limits
    best = _portfolio_risk_check(best, open_positions or [])
    _n_portfolio_eligible = len(best)

    # P1-C: NO TRADE — portfolio concentration gate eliminated all quality-passing tickers
    if not best:
        if _decision_meta_out is not None:
            _decision_meta_out.append({
                "status":                          "NO_TRADE",
                "reason":                          "PORTFOLIO_RISK_GATE",
                "best_score":                      _best_score,
                "required_score":                  min_q,
                "candidates_evaluated":            _n_evaluated,
                "candidates_survived_filter":      _n_survived_filter,
                "candidates_cleared_quality_gate": _n_cleared_quality,
                "portfolio_eligible":              0,
                "market_regime":                   _regime,
                "repair_summary":                  _repair_summary,
                "decision_snapshot":               _dec_snapshot,
            })
        return []

    # Step 5: Rank by ranker_score (cross-sectional ML signal) when available,
    # fall back to composite score when ranker not trained.
    has_ranker = any(v["ranker_score"] is not None for v in best.values())
    log.info("Ranking: %s", "XGBRanker" if has_ranker else "composite score (XGBRanker not trained)")
    if has_ranker:
        ranked = sorted(
            best.values(),
            key=lambda x: (
                -(x["ranker_score"] if x["ranker_score"] is not None else x["composite"] / 1000.0),
                -(x.get("liquidity_score") or 0.0),   # tiebreaker within near-equal ranker scores
            ),
        )
    else:
        ranked = sorted(
            best.values(),
            key=lambda x: (-x["composite"], -(x.get("liquidity_score") or 0.0)),
        )

    result = ranked[:n]
    for item in result:
        item["suggested_allocation_pct"] = _suggested_allocation(item["composite"])

    if _decision_meta_out is not None:
        _decision_meta_out.append({
            "status":                          "TRADE",
            "reason":                          None,
            "best_score":                      result[0]["composite"] if result else None,
            "required_score":                  min_q,
            "candidates_evaluated":            _n_evaluated,
            "candidates_survived_filter":      _n_survived_filter,
            "candidates_cleared_quality_gate": _n_cleared_quality,
            "portfolio_eligible":              _n_portfolio_eligible,
            "market_regime":                   _regime,
            "repair_summary":                  _repair_summary,
            "decision_snapshot":               _dec_snapshot,
        })

    return result
