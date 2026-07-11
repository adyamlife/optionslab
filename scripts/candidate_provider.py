"""
Candidate Provider — ticker -> recommended structure -> enriched candidate.

This wraps analyze.py's existing ticker analysis (rule-based regime
detection + multi-factor signal scoring + structure-matrix lookup +
per-structure candidate building — all already implemented there, unchanged
by this module) with additional risk/sizing tools that are buildable today
without a historical ML training pipeline:

  - Monte Carlo simulation (GBM price paths) for a candidate's probability
    of touch, 95% worst-case loss, and expected P&L re-estimated from
    simulation rather than the closed-form Greeks alone
  - Kelly Criterion position-sizing SUGGESTION (informational only —
    sizing decisions stay with the user, nothing here auto-executes)
  - Portfolio-level exposure check: does adding this candidate push the
    user's other open positions past a configured net-delta or per-trade
    capital limit

NOT included here (would need a historical labeled-outcomes dataset to
train on, scoped as a separate project): ML-based regime classification
(HMM/Bayesian/Random Forest) and ML-based Probability-of-Profit
(XGBoost/LightGBM/CatBoost). The existing rule-based regime detection
(analyze.py's trend/iv_env/regime) and signal-alignment scoring
(compute_signal_alignment) already serve that role today.
"""
import numpy as np

from config.structures import get_or_none as get_structure
from config.rules import RISK_FREE_RATE, RISK_LIMITS, CAPITAL, MAX_PORTFOLIO_VEGA
from scripts.analyze import analyze_ticker

DEFAULT_N_SIMS = 5000
DEFAULT_N_STEPS = 21  # ~trading days in a month; capped to candidate's own DTE


# ── Monte Carlo simulation ────────────────────────────────────────────────────

def simulate_price_paths(spot, iv, dte, risk_free_rate, n_sims=DEFAULT_N_SIMS, n_steps=None):
    """
    Vectorized GBM price-path simulation under risk-neutral drift.
    Returns (terminal_prices, path_min, path_max) — each a length-n_sims array.
    path_min/path_max let probability-of-touch checks see if a barrier was
    breached at any point, not just at expiry.
    """
    if spot is None or iv is None or dte is None or dte <= 0 or iv <= 0:
        return None, None, None

    T = dte / 365.0
    steps = max(1, min(n_steps or DEFAULT_N_STEPS, dte))
    dt = T / steps

    # Risk-neutral drift; iv is annualized volatility (fraction, e.g. 0.35)
    drift = (risk_free_rate - 0.5 * iv ** 2) * dt
    vol_step = iv * np.sqrt(dt)

    rng = np.random.default_rng()
    z = rng.standard_normal((n_sims, steps))
    log_returns = drift + vol_step * z
    log_paths = np.cumsum(log_returns, axis=1)
    price_paths = spot * np.exp(log_paths)

    terminal_prices = price_paths[:, -1]
    path_min = np.minimum(spot, price_paths.min(axis=1))
    path_max = np.maximum(spot, price_paths.max(axis=1))
    return terminal_prices, path_min, path_max


def _payoff_per_share(structure_name: str, candidate: dict, S_T: np.ndarray):
    """
    Structure-aware terminal payoff (per share, before commissions), mirroring
    the same formulas backtest.py/paper_trade_engine.py use for expiry P&L.
    Returns None for structures with no clean expiry-based payoff (Calendar,
    Diagonal, Jade Lizard, Covered Call — path/ownership-dependent).
    """
    st = get_structure(structure_name)
    if st is None:
        return None

    if st.strike_schema.value == "single_leg" and st.option_type == "put" and st.is_credit:
        # Cash Secured Put
        k = candidate.get("short_strike")
        credit = candidate.get("max_profit")
        if k is None or credit is None:
            return None
        loss = np.maximum(0.0, k - S_T)
        return credit - loss

    if st.strike_schema.value == "two_leg":
        k_short, k_long = candidate.get("short_strike"), candidate.get("long_strike")
        if k_short is None or k_long is None:
            return None
        width = abs(k_short - k_long)
        if st.is_credit:
            credit = candidate.get("max_profit")
            if credit is None:
                return None
            if st.option_type == "put":
                loss = np.clip(k_short - S_T, 0.0, width)
            else:
                loss = np.clip(S_T - k_short, 0.0, width)
            return credit - loss
        else:
            debit = candidate.get("max_loss")
            if debit is None:
                return None
            if st.option_type == "call":
                payoff = np.clip(S_T - k_long, 0.0, width) if k_long < k_short else np.clip(S_T - k_short, 0.0, width)
            else:
                payoff = np.clip(k_long - S_T, 0.0, width) if k_long > k_short else np.clip(k_short - S_T, 0.0, width)
            return payoff - debit

    if st.strike_schema.value == "iron_condor":
        pl, ps = candidate.get("put_long_strike"), candidate.get("put_short_strike")
        cs, cl = candidate.get("call_short_strike"), candidate.get("call_long_strike")
        credit = candidate.get("max_profit")
        if None in (pl, ps, cs, cl, credit):
            return None
        put_width, call_width = abs(ps - pl), abs(cl - cs)
        loss_put = np.clip(ps - S_T, 0.0, put_width)
        loss_call = np.clip(S_T - cs, 0.0, call_width)
        return credit - loss_put - loss_call

    return None  # Calendar / Diagonal / Jade Lizard / Covered Call — not path-clean


def monte_carlo_outcome(row: dict, candidate: dict, n_sims=DEFAULT_N_SIMS, ticker: str = None):
    """
    Run a Monte Carlo simulation for one candidate.
    Delegates to scripts.monte_carlo.run_mc which uses GARCH(1,1) paths when
    a fitted model exists for the ticker, falling back to flat-vol GBM otherwise.
    Returns {prob_of_touch, worst_loss_95, expected_pnl, prob_profit_sim,
             cvar_loss, vol_source, n_sims} or None.
    """
    try:
        from scripts.monte_carlo import run_mc
        return run_mc(ticker or row.get("ticker"), row, candidate, n_sims=n_sims)
    except Exception:
        pass

    # Hard fallback: legacy flat-GBM (should never be reached after monte_carlo.py is present)
    spot = row.get("spot")
    iv = candidate.get("atm_iv") or row.get("atm_iv") or row.get("hv20")
    dte = candidate.get("dte") or row.get("dte")
    risk_free_rate = row.get("risk_free_rate") or RISK_FREE_RATE

    S_T, path_min, path_max = simulate_price_paths(spot, iv, dte, risk_free_rate, n_sims=n_sims)
    if S_T is None:
        return None
    pnl = _payoff_per_share(candidate.get("structure"), candidate, S_T)
    if pnl is None:
        return None
    short_strikes = [
        s for s in (candidate.get("short_strike"), candidate.get("put_short_strike"), candidate.get("call_short_strike"))
        if s is not None
    ]
    prob_of_touch = None
    if short_strikes and spot:
        nearest = min(short_strikes, key=lambda k: abs(k - spot))
        touched = (path_min <= nearest) if nearest <= spot else (path_max >= nearest)
        prob_of_touch = round(float(np.mean(touched)) * 100, 1)
    return {
        "prob_of_touch":   prob_of_touch,
        "worst_loss_95":   round(float(np.percentile(pnl, 5)), 3),
        "expected_pnl":    round(float(np.mean(pnl)), 3),
        "prob_profit_sim": round(float(np.mean(pnl > 0)) * 100, 1),
        "vol_source":      "gbm",
        "n_sims":          n_sims,
    }


# ── Kelly Criterion sizing (informational only — nothing here executes) ──────

def kelly_from_pred_dist(pred_dist: dict, max_profit: float, max_loss: float,
                         p_profit: float | None = None) -> dict | None:
    """
    Compute Kelly-sized position using the trade's actual probability of profit
    (p_profit), scaled down by pred_dist.confidence (model agreement).

    p_profit — probability the trade expires profitably, in [0,1]. Use
               MC prob_profit_sim/100 when available, else candidate.pop/100.
               Falls back to pred_dist.p_win only when nothing else is present
               (directional proxy; less accurate for options).
    pred_dist — from predict_ticker; provides confidence scalar.

    Returns {kelly_f, kelly_pct, kelly_capital, kelly_contracts,
             confidence_scalar, p_profit, confidence} or None.
    This is informational only — nothing auto-executes.
    """
    if pred_dist is None or max_profit is None or max_loss is None or max_loss <= 0:
        return None

    confidence = pred_dist.get("confidence")
    if confidence is None:
        return None

    # Resolve p_profit: MC sim > candidate POP > directional p_win (last resort).
    # p_win is a directional signal (stock direction), NOT a trade POP estimate.
    # Using it as p_profit is only correct for bullish structures; for iron condors
    # or bearish structures it can be wrong. Log a warning so callers know.
    if p_profit is None:
        import logging as _log
        _log.getLogger(__name__).warning(
            "kelly_from_pred_dist: p_profit not supplied — falling back to p_win "
            "(directional proxy). Pass candidate pop/100 or MC prob_profit_sim/100 "
            "for accurate Kelly sizing."
        )
        p_profit = pred_dist.get("p_win")
    if p_profit is None:
        return None
    p_profit = float(p_profit)

    # Read Kelly config
    try:
        from pathlib import Path
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        _cfg = tomllib.loads((Path(__file__).resolve().parent.parent / "config" / "settings.toml").read_text(encoding="utf-8"))
        _g = _cfg.get("ml_gate", {})
        half_k  = float(_g.get("kelly_fraction", 0.5))
        max_pct = float(_g.get("kelly_max_pct",  0.05))
        min_pct = float(_g.get("kelly_min_pct",  0.01))
        capital = float(_cfg.get("capital", {}).get("amount", 1000))
    except Exception:
        half_k, max_pct, min_pct, capital = 0.5, 0.05, 0.01, 1000.0

    p = p_profit
    q = 1.0 - p
    b = max_profit / max_loss   # reward/risk ratio
    if b <= 0:
        return None

    # Kelly formula: f* = (b·p - q) / b
    raw_kelly = (b * p - q) / b
    if raw_kelly <= 0:
        # Negative Kelly = negative edge: don't size up this trade
        return None

    # Scale: half-Kelly setting × confidence attenuation
    # confidence=1.0 → full half-Kelly; confidence=0.6 → 60% of that
    conf_scalar = confidence
    kelly_f     = raw_kelly * half_k * conf_scalar

    # Hard cap and floor
    kelly_pct     = min(max(kelly_f, min_pct), max_pct)
    kelly_capital = capital * kelly_pct
    # Contracts: how many fit the Kelly capital at max_loss per contract (×100 multiplier)
    contracts = max(1, int(kelly_capital / (max_loss * 100))) if max_loss > 0 else 1

    return {
        "kelly_f":           round(kelly_f, 4),
        "kelly_pct":         round(kelly_pct, 4),
        "kelly_capital":     round(kelly_capital, 2),
        "kelly_contracts":   contracts,
        "confidence_scalar": round(conf_scalar, 3),
        "p_profit":          round(p_profit, 3),
        "confidence":        round(confidence, 3),
    }


def kelly_fraction(pop_pct: float, max_profit: float, max_loss: float, half_kelly: bool = True):
    """
    f = (bp - q) / b   where b = reward/risk, p = win prob, q = loss prob.
    Returns a SUGGESTED fraction of account capital to risk on this trade, or
    None if inputs don't support a meaningful estimate. This is informational
    only — position sizing remains the user's decision; nothing auto-executes.
    """
    if not pop_pct or not max_profit or not max_loss or max_loss <= 0:
        return None

    p = pop_pct / 100.0
    q = 1 - p
    b = max_profit / max_loss
    if b <= 0:
        return None

    f = (b * p - q) / b
    f = max(0.0, f)  # never suggest a negative/short-the-edge size
    if half_kelly:
        f /= 2
    return round(f, 4)


# ── Portfolio-level exposure check ────────────────────────────────────────────

def portfolio_exposure_check(candidate: dict, open_positions: list, limits: dict = None):
    """
    Does adding `candidate` push the book past configured limits, given the
    user's other `open_positions` (list of dicts with at least net_delta and
    capital_required/max_loss)? Returns {ok, net_delta_after, capital_after,
    breaches: [str]}. Purely a check — does not block or execute anything.
    """
    limits = limits or {
        "max_net_delta":            RISK_LIMITS.get("max_net_delta", 0.15),
        "max_position_capital_pct": RISK_LIMITS.get("max_position_capital_pct", 0.05),
    }

    current_delta = sum(p.get("net_delta", 0) or 0 for p in (open_positions or []))
    current_capital = sum(p.get("capital_required", 0) or p.get("max_loss", 0) or 0 for p in (open_positions or []))

    candidate_delta = candidate.get("net_delta", 0) or 0
    candidate_capital = candidate.get("capital_required", 0) or 0

    net_delta_after = round(current_delta + candidate_delta, 4)
    capital_after = round(current_capital + candidate_capital, 2)

    breaches = []

    # Net delta limit
    if abs(net_delta_after) > limits["max_net_delta"]:
        breaches.append(f"Portfolio net delta would be {net_delta_after:+.3f}, outside ±{limits['max_net_delta']}")

    # Max open positions limit
    max_positions = RISK_LIMITS.get("max_open_positions", 5)
    current_count = len(open_positions or [])
    if current_count >= max_positions:
        breaches.append(f"Already at max open positions ({current_count}/{max_positions})")

    # Sector concentration limit
    candidate_sector = candidate.get("sector_etf")
    if candidate_sector and CAPITAL > 0:
        max_sector_pct = RISK_LIMITS.get("max_sector_pct", 0.20)
        sector_capital = sum(
            p.get("capital_required", 0) or p.get("max_loss", 0) or 0
            for p in (open_positions or [])
            if p.get("sector_etf") == candidate_sector
        )
        sector_pct_after = (sector_capital + candidate_capital) / CAPITAL
        if sector_pct_after > max_sector_pct:
            breaches.append(
                f"Sector {candidate_sector} exposure would reach {sector_pct_after:.0%} of capital "
                f"(limit: {max_sector_pct:.0%})"
            )

    # Portfolio vega cap
    current_vega = sum(p.get("net_vega", 0) or 0 for p in (open_positions or []))
    candidate_vega = candidate.get("net_vega", 0) or 0
    net_vega_after = round(current_vega + candidate_vega, 4)
    max_vega = RISK_LIMITS.get("max_portfolio_vega", MAX_PORTFOLIO_VEGA)
    if abs(net_vega_after) > max_vega:
        breaches.append(
            f"Portfolio net vega would be {net_vega_after:+.3f}, outside ±{max_vega} cap"
        )

    # Directional correlation warning: flag if the new trade is structurally
    # similar (same is_credit direction) to the majority of open positions.
    # This warns about correlated risk even when individual delta/capital limits pass.
    if open_positions:
        candidate_credit = candidate.get("is_credit", True)
        same_dir = sum(1 for p in open_positions if p.get("is_credit", True) == candidate_credit)
        if same_dir >= max(2, len(open_positions) * 0.6):
            direction = "short-vol (credit)" if candidate_credit else "long-vol (debit)"
            breaches.append(
                f"Correlated: {same_dir}/{len(open_positions)} open positions are also {direction} — concentrated directional exposure"
            )

    return {
        "ok": not breaches,
        "net_delta_after": net_delta_after,
        "capital_after": capital_after,
        "net_vega_after": net_vega_after,
        "open_positions_after": current_count + 1,
        "breaches": breaches,
    }


# ── Orchestration ──────────────────────────────────────────────────────────────

def get_enriched_candidate(ticker, candidate_structure=None, open_positions=None, params=None, regime="chop"):
    """
    Run the existing rule-based analysis pipeline (unchanged) for `ticker`,
    then enrich either the recommended candidate or `candidate_structure` (by
    name, if you want a specific alternative rather than the recommended one)
    with Monte Carlo outcome stats, a Kelly sizing suggestion, and a
    portfolio exposure check against `open_positions` if supplied.
    """
    row = analyze_ticker(ticker, params=params, regime=regime)
    candidates = row.get("candidates", [])

    candidate = None
    if candidate_structure:
        candidate = next((c for c in candidates if c.get("structure") == candidate_structure), None)
    else:
        candidate = next((c for c in candidates if c.get("recommended")), None)

    if candidate is None:
        return {"row": row, "candidate": None, "monte_carlo": None, "kelly": None, "portfolio": None}

    mc = monte_carlo_outcome(row, candidate, ticker=ticker)
    kelly = kelly_fraction(candidate.get("pop"), candidate.get("max_profit"), candidate.get("max_loss"))
    portfolio = portfolio_exposure_check(candidate, open_positions) if open_positions else None

    return {
        "row": row,
        "candidate": candidate,
        "monte_carlo": mc,
        "kelly_fraction": kelly,
        "portfolio": portfolio,
    }
