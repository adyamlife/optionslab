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
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np

from config.structures import get_or_none as get_structure
from config.rules import RISK_FREE_RATE, RISK_LIMITS, CAPITAL, MAX_PORTFOLIO_VEGA
from scripts.analyze import analyze_ticker

logger = logging.getLogger(__name__)

DEFAULT_N_SIMS  = 5000
DEFAULT_N_STEPS = 21  # ~trading days in a month; capped to candidate's own DTE

# Directional-bias sets — more precise than is_credit for correlation warnings.
# Bull Put Spread and Bear Call Spread are both credit but opposite direction.
_BULLISH_STRUCTURES = frozenset({
    "Bull Put Spread", "Cash Secured Put", "Bull Call Spread",
    "Long Call", "Covered Call",
})
_BEARISH_STRUCTURES = frozenset({
    "Bear Call Spread", "Bear Put Spread", "Long Put",
})
_NEUTRAL_STRUCTURES = frozenset({
    "Iron Condor", "Iron Butterfly", "Short Strangle", "Short Straddle",
    "Long Strangle", "Long Straddle",
})


@lru_cache(maxsize=1)
def _kelly_config() -> dict:
    """Read Kelly / capital settings once per process; cached for all calls."""
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        _path = Path(__file__).resolve().parent.parent / "config" / "settings.toml"
        cfg = tomllib.loads(_path.read_text(encoding="utf-8"))
        g = cfg.get("ml_gate", {})
        return {
            "half_k":  float(g.get("kelly_fraction", 0.5)),
            "max_pct": float(g.get("kelly_max_pct",  0.05)),
            "min_pct": float(g.get("kelly_min_pct",  0.01)),
            "capital": float(cfg.get("capital", {}).get("amount", 1000)),
        }
    except Exception:
        return {"half_k": 0.5, "max_pct": 0.05, "min_pct": 0.01, "capital": 1000.0}


# ── Monte Carlo simulation ────────────────────────────────────────────────────

def simulate_price_paths(
    spot, iv, dte, risk_free_rate,
    n_sims=DEFAULT_N_SIMS, n_steps=None,
    rng: np.random.Generator | None = None,
):
    """
    Vectorized GBM price-path simulation under risk-neutral drift.
    Returns (terminal_prices, path_min, path_max) — each a length-n_sims array.
    path_min/path_max let probability-of-touch checks see if a barrier was
    breached at any point, not just at expiry.

    rng — optional seeded generator for deterministic unit tests.
          If None, a fresh default_rng() is created each call.
    """
    if spot is None or iv is None or dte is None or dte <= 0 or iv <= 0:
        return None, None, None

    T = dte / 365.0
    steps = max(1, min(n_steps or DEFAULT_N_STEPS, dte))
    dt = T / steps

    drift    = (risk_free_rate - 0.5 * iv ** 2) * dt
    vol_step = iv * np.sqrt(dt)

    _rng = rng or np.random.default_rng()
    z = _rng.standard_normal((n_sims, steps))
    log_returns = drift + vol_step * z
    log_paths   = np.cumsum(log_returns, axis=1)
    price_paths = spot * np.exp(log_paths)

    terminal_prices = price_paths[:, -1]
    path_min = np.minimum(spot, price_paths.min(axis=1))
    path_max = np.maximum(spot, price_paths.max(axis=1))
    return terminal_prices, path_min, path_max


# ── Payoff functions (one per payoff shape) ──────────────────────────────────

def _payoff_csp(candidate: dict, S_T: np.ndarray) -> np.ndarray | None:
    k, credit = candidate.get("short_strike"), candidate.get("max_profit")
    if k is None or credit is None:
        return None
    return credit - np.maximum(0.0, k - S_T)


def _payoff_two_leg(candidate: dict, S_T: np.ndarray, is_credit: bool, option_type: str) -> np.ndarray | None:
    k_short = candidate.get("short_strike")
    k_long  = candidate.get("long_strike")
    if k_short is None or k_long is None:
        return None
    width = abs(k_short - k_long)
    if is_credit:
        credit = candidate.get("max_profit")
        if credit is None:
            return None
        if option_type == "put":
            loss = np.clip(k_short - S_T, 0.0, width)
        else:
            loss = np.clip(S_T - k_short, 0.0, width)
        return credit - loss
    else:
        debit = candidate.get("max_loss")
        if debit is None:
            return None
        if option_type == "call":
            payoff = (np.clip(S_T - k_long, 0.0, width) if k_long < k_short
                      else np.clip(S_T - k_short, 0.0, width))
        else:
            payoff = (np.clip(k_long - S_T, 0.0, width) if k_long > k_short
                      else np.clip(k_short - S_T, 0.0, width))
        return payoff - debit


def _payoff_iron_condor(candidate: dict, S_T: np.ndarray) -> np.ndarray | None:
    pl  = candidate.get("put_long_strike")
    ps  = candidate.get("put_short_strike")
    cs  = candidate.get("call_short_strike")
    cl  = candidate.get("call_long_strike")
    credit = candidate.get("max_profit")
    if None in (pl, ps, cs, cl, credit):
        return None
    put_width  = abs(ps - pl)
    call_width = abs(cl - cs)
    loss_put  = np.clip(ps - S_T, 0.0, put_width)
    loss_call = np.clip(S_T - cs, 0.0, call_width)
    return credit - loss_put - loss_call


# Payoff registry: keyed by (strike_schema, is_credit, option_type).
# Add entries here as new payoff shapes are introduced; _payoff_per_share
# dispatches through this registry instead of growing its own if/elif chain.
_PAYOFF_REGISTRY: dict[tuple, callable] = {
    ("single_leg", True,  "put"):  lambda c, S: _payoff_csp(c, S),
    ("two_leg",    True,  "put"):  lambda c, S: _payoff_two_leg(c, S, True,  "put"),
    ("two_leg",    True,  "call"): lambda c, S: _payoff_two_leg(c, S, True,  "call"),
    ("two_leg",    False, "put"):  lambda c, S: _payoff_two_leg(c, S, False, "put"),
    ("two_leg",    False, "call"): lambda c, S: _payoff_two_leg(c, S, False, "call"),
    ("iron_condor", True, None):   lambda c, S: _payoff_iron_condor(c, S),
}


def _payoff_per_share(structure_name: str, candidate: dict, S_T: np.ndarray) -> np.ndarray | None:
    """
    Structure-aware terminal payoff (per share, before commissions), mirroring
    the same formulas backtest.py/paper_trade_engine.py use for expiry P&L.
    Returns None for structures with no clean expiry-based payoff (Calendar,
    Diagonal, Jade Lizard, Covered Call — path/ownership-dependent).
    """
    st = get_structure(structure_name)
    if st is None:
        return None

    schema = st.strike_schema.value
    is_credit  = st.is_credit
    option_type = getattr(st, "option_type", None)

    # iron_condor is always credit; option_type not meaningful for dispatch
    key = (schema, is_credit, None if schema == "iron_condor" else option_type)
    handler = _PAYOFF_REGISTRY.get(key)
    if handler is not None:
        return handler(candidate, S_T)

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
        logger.exception(
            "monte_carlo.run_mc failed for %s — falling back to flat-GBM",
            ticker or row.get("ticker"),
        )

    # Hard fallback: legacy flat-GBM (should never be reached after monte_carlo.py is present)
    spot = row.get("spot")
    iv   = candidate.get("atm_iv") or row.get("atm_iv") or row.get("hv20")
    dte  = candidate.get("dte") or row.get("dte")
    risk_free_rate = row.get("risk_free_rate") or RISK_FREE_RATE

    S_T, path_min, path_max = simulate_price_paths(spot, iv, dte, risk_free_rate, n_sims=n_sims)
    if S_T is None:
        return None
    pnl = _payoff_per_share(candidate.get("structure"), candidate, S_T)
    if pnl is None:
        return None

    short_strikes = [
        s for s in (
            candidate.get("short_strike"),
            candidate.get("put_short_strike"),
            candidate.get("call_short_strike"),
        )
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

def kelly_from_pred_dist(
    pred_dist: dict,
    max_profit: float,
    max_loss: float,
    p_profit: float | None = None,
) -> dict | None:
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
        logger.warning(
            "kelly_from_pred_dist: p_profit not supplied — falling back to p_win "
            "(directional proxy). Pass candidate pop/100 or MC prob_profit_sim/100 "
            "for accurate Kelly sizing."
        )
        p_profit = pred_dist.get("p_win")
    if p_profit is None:
        return None
    p_profit = float(p_profit)

    cfg     = _kelly_config()
    half_k  = cfg["half_k"]
    max_pct = cfg["max_pct"]
    min_pct = cfg["min_pct"]
    capital = cfg["capital"]

    p = p_profit
    q = 1.0 - p
    b = max_profit / max_loss
    if b <= 0:
        return None

    raw_kelly = (b * p - q) / b
    if raw_kelly <= 0:
        return None

    # Scale: half-Kelly setting × confidence attenuation
    # confidence=1.0 → full half-Kelly; confidence=0.6 → 60% of that
    conf_scalar   = confidence
    kelly_f       = raw_kelly * half_k * conf_scalar
    kelly_pct     = min(max(kelly_f, min_pct), max_pct)
    kelly_capital = capital * kelly_pct
    contracts     = max(1, int(kelly_capital / (max_loss * 100))) if max_loss > 0 else 1

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
    Simple Kelly estimate from a POP percentage alone (no ML confidence weighting).
    f = (bp - q) / b   where b = reward/risk, p = win prob, q = loss prob.
    Returns a SUGGESTED fraction of account capital, or None on bad inputs.
    This is informational only — position sizing remains the user's decision.

    Prefer kelly_from_pred_dist() when a pred_dist is available — it incorporates
    model confidence and is the primary sizing function used in get_enriched_candidate.
    """
    if not pop_pct or not max_profit or not max_loss or max_loss <= 0:
        return None

    p = pop_pct / 100.0
    q = 1 - p
    b = max_profit / max_loss
    if b <= 0:
        return None

    f = max(0.0, (b * p - q) / b)
    if half_kelly:
        f /= 2
    return round(f, 4)


# ── Portfolio-level exposure check ────────────────────────────────────────────

def _directional_bias(candidate: dict) -> str:
    """
    Return the directional exposure category for a candidate.
    More precise than is_credit — Bull Put Spread and Bear Call Spread are both
    credit but carry opposite directional risk.
    """
    structure = candidate.get("structure", "")
    if structure in _BULLISH_STRUCTURES:
        return "bullish"
    if structure in _BEARISH_STRUCTURES:
        return "bearish"
    if structure in _NEUTRAL_STRUCTURES:
        return "neutral"
    # Fallback: infer from is_credit when structure isn't in the registry yet
    return "short_vol" if candidate.get("is_credit") else "long_vol"


def compute_capital_required(candidate: dict) -> float | None:
    """
    Return the cash/margin required to enter `candidate` for 1 contract (×100 shares).

    Formula depends on capital_type from the structure definition:
      debit        — premium paid × 100  (long options, debit spreads, LEAPS)
      spread_width — (width − net_credit) × 100  (credit spreads, iron condor)
      margin       — ~20% of notional + premium × 100  (naked put, jade lizard)
      cash_secured — short_strike × 100  (cash secured put)
      shares       — None  (covered call — caller must verify share ownership)

    Returns None when the requirement cannot be computed from available fields.
    """
    st = get_structure(candidate.get("structure", ""))
    if st is None:
        return None

    ctype        = st.capital_type
    spot         = candidate.get("spot_at_entry") or candidate.get("spot") or 0.0
    max_profit   = candidate.get("max_profit") or 0.0
    max_loss     = candidate.get("max_loss")   or 0.0
    short_strike = candidate.get("short_strike") or candidate.get("put_short_strike") or 0.0

    if ctype == "debit":
        # For debit trades the debit paid = max_loss (what you can lose = what you paid)
        return round(max_loss * 100, 2)

    if ctype == "spread_width":
        # Max loss on a credit spread = width − credit received
        return round(max_loss * 100, 2)

    if ctype == "margin":
        # Standard broker formula: 20% of notional + premium collected − OTM amount
        # Approximated here as 20% of (spot × 100) — actual broker varies
        if spot <= 0:
            return None
        notional       = spot * 100
        premium_credit = max_profit * 100
        otm_amount     = max(0.0, (spot - short_strike) * 100) if short_strike else 0.0
        return round(notional * 0.20 + premium_credit - otm_amount, 2)

    if ctype == "cash_secured":
        # Full strike value must be in cash
        strike = short_strike or spot
        return round(strike * 100, 2)

    if ctype == "shares":
        return None   # caller must verify 100 shares are held

    return None


def check_balance_for_candidate(candidate: dict, available_cash: float) -> dict:
    """
    Check whether `available_cash` covers the capital required for `candidate`.

    Returns:
      {
        "ok":               bool,
        "capital_required": float | None,
        "available_cash":   float,
        "shortfall":        float,          # 0 when ok
        "requires_margin":  bool,
        "capital_type":     str,
        "note":             str,
      }
    """
    st = get_structure(candidate.get("structure", ""))
    capital_type    = st.capital_type    if st else "unknown"
    requires_margin = st.requires_margin if st else False

    if capital_type == "shares":
        return {
            "ok": True, "capital_required": None,
            "available_cash": available_cash, "shortfall": 0.0,
            "requires_margin": False, "capital_type": "shares",
            "note": "Covered Call — verify 100 shares are held; no cash requirement.",
        }

    cap = compute_capital_required(candidate)
    if cap is None:
        return {
            "ok": False, "capital_required": None,
            "available_cash": available_cash, "shortfall": 0.0,
            "requires_margin": requires_margin, "capital_type": capital_type,
            "note": "Capital requirement could not be computed — missing price data.",
        }

    ok       = available_cash >= cap
    shortfall = round(max(0.0, cap - available_cash), 2)
    if requires_margin:
        note = f"Margin account required. Estimated margin: ${cap:,.2f}."
    elif ok:
        note = f"Sufficient balance. Capital needed: ${cap:,.2f}."
    else:
        note = f"Insufficient balance. Need ${cap:,.2f}, have ${available_cash:,.2f} (short ${shortfall:,.2f})."

    return {
        "ok":               ok,
        "capital_required": cap,
        "available_cash":   available_cash,
        "shortfall":        shortfall,
        "requires_margin":  requires_margin,
        "capital_type":     capital_type,
        "note":             note,
    }


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

    current_delta   = sum(p.get("net_delta", 0) or 0 for p in (open_positions or []))
    current_capital = sum(
        p.get("capital_required", 0) or p.get("max_loss", 0) or 0
        for p in (open_positions or [])
    )

    candidate_delta   = candidate.get("net_delta", 0) or 0
    # Mirror how open_positions are measured: capital_required falls back to max_loss
    candidate_capital = (
        candidate.get("capital_required")
        or candidate.get("max_loss")
        or 0
    )

    net_delta_after = round(current_delta + candidate_delta, 4)
    capital_after   = round(current_capital + candidate_capital, 2)

    breaches = []

    if abs(net_delta_after) > limits["max_net_delta"]:
        breaches.append(
            f"Portfolio net delta would be {net_delta_after:+.3f}, outside ±{limits['max_net_delta']}"
        )

    max_positions = RISK_LIMITS.get("max_open_positions", 5)
    current_count = len(open_positions or [])
    if current_count >= max_positions:
        breaches.append(f"Already at max open positions ({current_count}/{max_positions})")

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

    current_vega  = sum(p.get("net_vega", 0) or 0 for p in (open_positions or []))
    candidate_vega = candidate.get("net_vega", 0) or 0
    net_vega_after = round(current_vega + candidate_vega, 4)
    max_vega = RISK_LIMITS.get("max_portfolio_vega", MAX_PORTFOLIO_VEGA)
    if abs(net_vega_after) > max_vega:
        breaches.append(
            f"Portfolio net vega would be {net_vega_after:+.3f}, outside ±{max_vega} cap"
        )

    # Directional correlation warning: flag if the new trade is the same
    # directional bias as the majority of open positions.
    # Uses _directional_bias() rather than is_credit — Bull Put Spread and
    # Bear Call Spread are both credit but carry opposite directional risk.
    if open_positions:
        bias = _directional_bias(candidate)
        same_bias = sum(1 for p in open_positions if _directional_bias(p) == bias)
        if same_bias >= max(2, len(open_positions) * 0.6):
            breaches.append(
                f"Correlated: {same_bias}/{len(open_positions)} open positions share "
                f"the same directional bias ({bias}) — concentrated exposure"
            )

    return {
        "ok":                   not breaches,
        "net_delta_after":      net_delta_after,
        "capital_after":        capital_after,
        "net_vega_after":       net_vega_after,
        "open_positions_after": current_count + 1,
        "breaches":             breaches,
    }


# ── Orchestration ──────────────────────────────────────────────────────────────

def get_enriched_candidate(ticker, candidate_structure=None, open_positions=None, params=None, regime="chop"):
    """
    Run the existing rule-based analysis pipeline (unchanged) for `ticker`,
    then enrich either the recommended candidate or `candidate_structure` (by
    name, if you want a specific alternative rather than the recommended one)
    with Monte Carlo outcome stats, a Kelly sizing suggestion, and a
    portfolio exposure check against `open_positions` if supplied.

    Kelly uses kelly_from_pred_dist() when ML predictions are present in the
    analysis row (pred_dist + model confidence), falling back to the simple
    kelly_fraction() when no ML context is available.
    """
    row        = analyze_ticker(ticker, params=params, regime=regime)
    candidates = row.get("candidates", [])

    if candidate_structure:
        candidate = next((c for c in candidates if c.get("structure") == candidate_structure), None)
    else:
        candidate = next((c for c in candidates if c.get("recommended")), None)

    if candidate is None:
        return {"row": row, "candidate": None, "monte_carlo": None, "kelly": None, "portfolio": None}

    mc = monte_carlo_outcome(row, candidate, ticker=ticker)

    # Kelly: prefer the richer model-confidence-aware version.
    # p_profit from Monte Carlo sim is the most accurate source; fall back to candidate POP.
    pred_dist = row.get("prediction", {}).get("pred_dist") if row.get("prediction") else None
    if pred_dist is not None:
        p_profit = (
            mc["prob_profit_sim"] / 100.0
            if mc and mc.get("prob_profit_sim") is not None
            else (candidate.get("pop") or 0) / 100.0
        )
        kelly = kelly_from_pred_dist(
            pred_dist,
            candidate.get("max_profit"),
            candidate.get("max_loss"),
            p_profit=p_profit,
        )
    else:
        # No ML predictions available — use simple Kelly based on POP alone
        kelly = kelly_fraction(
            candidate.get("pop"),
            candidate.get("max_profit"),
            candidate.get("max_loss"),
        )

    portfolio = portfolio_exposure_check(candidate, open_positions) if open_positions else None

    return {
        "row":           row,
        "candidate":     candidate,
        "monte_carlo":   mc,
        "kelly":         kelly,
        "portfolio":     portfolio,
    }
