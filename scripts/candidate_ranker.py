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
    if p_iv_expanding  is not None and float(p_iv_expanding) < 0.30:
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

    return round(score, 2)


def filter_candidates(rows, paper_trade: bool = False):
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
        except Exception:
            pass

    # Structured gate rejection log — written at DEBUG, cheap to collect, useful for tuning
    _rejections: list[dict] = []

    def _reject(ticker, struct, gate, threshold, actual):
        _rejections.append({
            "ticker": ticker, "structure": struct, "gate": gate,
            "threshold": threshold, "actual": actual,
        })
        log.debug(f"[gate:{gate}] {ticker} {struct} → rejected (threshold={threshold}, actual={actual})")

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

            _t = row.get("ticker", "")

            # Gate 1: minimum profit floor (unlimited-profit structures exempt)
            if c.get("max_profit") is not None and c["max_profit"] < _min_profit:
                _reject(_t, struct, "min_profit", _min_profit, round(c["max_profit"], 2))
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

            # Gate 4: EV computable
            ev = c.get("ev")
            ev_is_proxy = False
            if ev is None:
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
                    ev = pop / 100 * (c.get("max_loss") or _MLPT)
                else:
                    ev = pop / 100 * c["max_profit"]
                ev_is_proxy = True

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

            # Gate 9: expected ROI — (max_profit / capital_required) × pop_estimate
            # Uses POP from candidate when available; falls back to 0.50 (coin flip prior).
            if gate_enabled and _min_roi > 0:
                from scripts.candidate_provider import compute_capital_required as _ccr
                _cap = _ccr(c)
                if _cap and _cap > 0:
                    _pop_est  = (c.get("pop") or 50.0) / 100.0
                    _mp       = c.get("max_profit") or 0.0
                    _roi      = (_mp * _pop_est) / (_cap / 100.0)  # _cap is per-contract, _mp is per-share
                    if _roi < _min_roi:
                        _reject(_t, struct, "expected_roi", _min_roi, round(_roi, 3))
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

    if _rejections:
        from collections import Counter as _Ctr
        _by_gate = _Ctr(r["gate"] for r in _rejections)
        log.info(f"[filter] {len(_rejections)} rejected, {len(result)} survived — "
                 f"gate breakdown: {dict(_by_gate)}")
        log.debug(f"[filter] rejection detail: {_rejections}")
    return result


def _portfolio_risk_check(best: dict, open_positions: list) -> dict:
    """
    Remove tickers that would violate portfolio concentration rules.
    open_positions: list of trade dicts from paper_trades.json (status='open').
    Rules:
      - No duplicate ticker (already have open exposure in this name)
      - Max 2 open trades per sector ETF
      - Total capital deployed cap: MAX_TOTAL_DEPLOYMENT_PCT of notional
      - Net portfolio delta cap: reject candidates that push |net_delta| beyond threshold (#7)
    """
    if not open_positions:
        return best

    _MAX_SECTOR_COUNT    = _g("portfolio_risk", "max_sector_count",    2)
    _MAX_TICKER_TRADES   = _g("portfolio_risk", "max_ticker_trades",   1)
    _MAX_DEPLOYMENT_PCT  = _g("portfolio_risk", "max_deployment_pct", 20.0)
    _MAX_NET_DELTA       = _g("portfolio_risk", "max_net_delta",       3.0)  # in underlying-equivalent shares

    from collections import Counter
    open_tickers = Counter(p.get("ticker", "") for p in open_positions)
    open_sectors = Counter(p.get("sector_etf") or p.get("sector", "")
                           for p in open_positions)
    total_capital = sum(
        (p.get("capital_required") or 0) for p in open_positions
    )

    # Current portfolio net delta (sum across all open positions)
    portfolio_delta = sum((p.get("net_delta") or 0.0) for p in open_positions)
    log.debug(f"[risk] portfolio net_delta={portfolio_delta:+.2f} from {len(open_positions)} open positions")

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
        # Net delta cap — would adding this candidate push portfolio delta past limit?
        candidate_delta = item["candidate"].get("net_delta") or 0.0
        projected_delta = portfolio_delta + candidate_delta
        if abs(projected_delta) > _MAX_NET_DELTA:
            log.info(
                f"[risk] Skip {ticker} — net_delta would reach {projected_delta:+.2f} "
                f"(cap ±{_MAX_NET_DELTA:.1f})"
            )
            continue
        result[ticker] = item

    return result


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


def rank_candidates(rows, n=3, score_fn=None, quality_floor=None, open_positions=None, paper_trade: bool = False):  # noqa: score_fn kept for API compat
    """
    Steps 3-5: Score → best per ticker → quality gate → rank tickers → top-n.

    score_fn is accepted but ignored; composite score (config/ranking.toml) is used
    for gates and penalties, but when the return ranker is available its score is the
    primary sort key so the portfolio engine directly optimizes ranking quality.

    quality_floor: override the min_composite gate. Pass 0 (paper trades) to
    always return the top-n by score regardless of quality threshold — every
    outcome, good or bad, is training data.
    """
    items = filter_candidates(rows, paper_trade=paper_trade)
    if not items:
        return []

    # Step 3a: Percentile-rank EV across all surviving candidates (#3)
    # Inject _ev_pct_rank into each candidate dict so _composite_score can use it.
    _ev_vals = [item["ev"] for item in items]
    _n_ev    = len(_ev_vals)
    _sorted_ev = sorted(range(_n_ev), key=lambda i: _ev_vals[i])
    _ev_pct_ranks = [0.0] * _n_ev
    for _rank_pos, _orig_idx in enumerate(_sorted_ev):
        _ev_pct_ranks[_orig_idx] = round(_rank_pos / max(_n_ev - 1, 1) * 100, 1)
    for item, pct in zip(items, _ev_pct_ranks):
        item["candidate"]["_ev_pct_rank"] = pct

    # Step 3b: Composite score (used for quality gate + tie-break) and ranker score
    for item in items:
        item["composite"] = _composite_score(
            item["row"], item["candidate"], item["ev"], item["ev_is_proxy"]
        )
        ml = item["row"].get("ml") or {}
        item["ranker_score"] = ml.get("ranker_score")  # None when ranker not trained
        item["position_size_factor"] = _position_size_factor(ml)

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
    best = {t: v for t, v in best.items() if v["composite"] >= min_q}

    # Step 4c: Portfolio risk check — removes tickers that breach concentration limits
    best = _portfolio_risk_check(best, open_positions or [])

    # Step 5: Rank by ranker_score (cross-sectional ML signal) when available,
    # fall back to composite score when ranker not trained.
    has_ranker = any(v["ranker_score"] is not None for v in best.values())
    if has_ranker:
        ranked = sorted(best.values(),
                        key=lambda x: -(x["ranker_score"] if x["ranker_score"] is not None
                                        else x["composite"] / 1000.0))
    else:
        ranked = sorted(best.values(), key=lambda x: -x["composite"])

    result = ranked[:n]
    for item in result:
        item["suggested_allocation_pct"] = _suggested_allocation(item["composite"])
    return result
