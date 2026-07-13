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
from config.rules import MIN_PROFIT_AMOUNT, IV_EDGE_SKIP_VP

log = logging.getLogger(__name__)


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
    return c.get("short_strike") is not None and c.get("long_strike") is not None


def _composite_score(row, c, ev) -> float:
    """
    Compute a 0-100 composite score for one (row, candidate) pair.

    Each component is normalized to [0, 100] before weighting.
    Bonuses and penalties are added/subtracted from the weighted total.
    All constants come from config/ranking.toml.
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

    # ── Component 1: ML confidence (0-1 → 0-100) ─────────────────────────────
    confidence = pred_dist.get("confidence")
    s_conf = (confidence * 100) if confidence is not None else 50.0

    # ── Component 2: Expected Value — normalized per capital at risk ──────────
    # Priority: max_loss (per-share, defined-risk) → capital_req/100 (per-share equiv)
    #           → fallback constant
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

    # ── Component 3: ML meta_score (already 0-100) ────────────────────────────
    meta = ml.get("meta_score")
    s_meta = float(meta) if meta is not None else 50.0

    # ── Component 4: POP — probability of profit ──────────────────────────────
    pop_c = c.get("pop")           # candidate pop: 0-100
    pop_m = ml.get("pop_score")    # ML POP model: 0-1
    if pop_c is not None:
        s_pop = float(pop_c)
    elif pop_m is not None:
        s_pop = float(pop_m) * 100
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

    return round(score, 2)


def filter_candidates(rows):
    """
    Steps 1-2: Build the candidate universe and apply hard gates.

    Returns a flat list of enriched dicts — one per candidate that survived —
    ready for composite scoring.
    """
    min_conf     = _g("gate", "min_confidence",         0.70)
    min_rvol     = _g("gate", "min_rel_volume",         0.40)
    conv_meta    = _g("gate", "ml_conviction_meta",    10)
    conv_conf    = _g("gate", "ml_conviction_conf",     0.80)
    exp_move_thr = _g("gate", "expected_move_threshold", 0.12)

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

    result = []
    for row in rows:
        if (row.get("status") or "").startswith("SKIP"):
            continue

        ml         = row.get("ml") or {}
        pred_dist  = ml.get("pred_dist") or {}
        confidence = pred_dist.get("confidence")
        meta_score = ml.get("meta_score")

        for c in row.get("candidates", []):
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

            # Gate 1: minimum profit floor (unlimited-profit structures exempt)
            if c.get("max_profit") is not None and c["max_profit"] < MIN_PROFIT_AMOUNT:
                log.info(
                    f"Skip {row['ticker']} {struct} — "
                    f"max_profit ${c['max_profit']:.2f} < ${MIN_PROFIT_AMOUNT:.2f} min"
                )
                continue

            # Gate 2: all required strikes present
            if not _strikes_complete(c):
                log.info(f"Skip {row['ticker']} {struct} — required strikes incomplete")
                continue

            # Gate 3: IV edge hard skip
            iv_edge_vp    = c.get("iv_edge_vp")
            iv_edge_label = c.get("iv_edge_label", "fair")
            if (iv_edge_vp is not None
                    and iv_edge_label in ("overpay", "undersell")
                    and abs(iv_edge_vp) > IV_EDGE_SKIP_VP):
                log.info(f"Skip {row['ticker']} {struct} — IV edge {iv_edge_vp:+.1f}vp ({iv_edge_label})")
                continue

            # Gate 4: EV computable
            ev = c.get("ev")
            ev_is_proxy = False
            if ev is None:
                pop = c.get("pop")
                if pop is None:
                    log.info(f"Skip {row['ticker']} {struct} — no ev or pop to compute EV proxy")
                    continue
                if c.get("max_profit") is None:
                    from config.rules import MAX_LOSS_PER_TRADE as _MLPT
                    ev = pop / 100 * _MLPT
                else:
                    ev = pop / 100 * c["max_profit"]
                ev_is_proxy = True

            # Gate 5: minimum ML confidence
            if gate_enabled and confidence is not None and confidence < min_conf:
                log.info(
                    f"Skip {row['ticker']} {struct} — "
                    f"confidence {confidence:.3f} < {min_conf:.2f}"
                )
                continue

            # Gate 6: minimum relative volume (hard floor; 0.40-0.80 incurs penalty in scoring)
            rel_vol = row.get("rel_volume")
            if gate_enabled and rel_vol is not None and rel_vol < min_rvol:
                log.info(
                    f"Skip {row['ticker']} {struct} — "
                    f"rel_volume {rel_vol:.2f} < {min_rvol:.2f}"
                )
                continue

            result.append({
                "row":           row,
                "candidate":     c,
                "ev":            round(ev, 4),
                "ev_is_proxy":   ev_is_proxy,
                "meets_both":    (
                    bool(c.get("meets_min_profit"))
                    and c.get("meets_max_loss") is not False
                ),
                "iv_edge_vp":    iv_edge_vp,
                "iv_edge_label": iv_edge_label,
                "pred_dist":     pred_dist,
            })
    return result


def rank_candidates(rows, n=3, score_fn=None, quality_floor=None):  # noqa: score_fn kept for API compat, not used
    """
    Steps 3-5: Score → best per ticker → quality gate → rank tickers → top-n.

    score_fn is accepted but ignored; the composite score (config/ranking.toml)
    is the sole ranking criterion.

    quality_floor: override the min_composite gate. Pass 0 (paper trades) to
    always return the top-n by score regardless of quality threshold — every
    outcome, good or bad, is training data.
    """
    items = filter_candidates(rows)

    # Step 3: Composite score
    for item in items:
        item["composite"] = _composite_score(item["row"], item["candidate"], item["ev"])

    # Step 4: Best candidate per ticker
    best: dict[str, dict] = {}
    for item in items:
        ticker = item["row"].get("ticker", "")
        if ticker not in best or item["composite"] > best[ticker]["composite"]:
            best[ticker] = item

    # Step 4b: Minimum quality gate — skipped when quality_floor=0 (paper trades)
    min_q = quality_floor if quality_floor is not None else _g("quality", "min_composite", 55)
    best = {t: v for t, v in best.items() if v["composite"] >= min_q}

    # Step 5: Rank tickers by composite score, return top-n
    ranked = sorted(best.values(), key=lambda x: -x["composite"])
    return ranked[:n]
