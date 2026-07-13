"""
Monte Carlo price-path simulation — GARCH(1,1) + GBM fallback.

Why GARCH beats flat GBM for options:
  GBM assumes constant daily volatility, which mis-prices options around
  earnings, regime shifts, and Vol spikes — precisely the environments where
  this system is most exposed. GARCH(1,1) propagates conditional variance
  forward step-by-step (h_{t+1} = ω + α·ε_t² + β·h_t), so the simulated
  paths reflect actual vol clustering: quiet periods stay quiet, shocked
  periods stay elevated. This leads to wider tails in high-vol regimes and
  tighter tails in low-vol regimes — a more honest probability-of-touch and
  worst-case loss estimate than flat IV.

Engine hierarchy (automatic, no config needed):
  1. GARCH(1,1) paths — when a fitted model exists in data/models/garch/
  2. Flat-vol GBM    — fallback using atm_iv or hv20 (existing behavior)

Public API:
  simulate_paths(ticker, spot, iv, dte, risk_free_rate, n_sims, n_steps)
    → (terminal, path_min, path_max)  — identical return shape as the old
    candidate_provider.simulate_price_paths; drop-in replacement.

  run_mc(ticker, row, candidate, n_sims)
    → dict | None  — full MC result dict (same keys as old monte_carlo_outcome
    + "vol_source" tag showing which engine ran).

Standalone:
  python -m scripts.monte_carlo AAPL --spot 213 --iv 0.28 --dte 14
"""
import logging
import math
from pathlib import Path

import numpy as np

_ROOT      = Path(__file__).resolve().parent.parent
_GARCH_DIR = _ROOT / "data" / "models" / "garch"

logger = logging.getLogger(__name__)

DEFAULT_N_SIMS  = 5_000
DEFAULT_N_STEPS = 21   # ~1 calendar month; capped to candidate DTE in practice


# ── GARCH simulation engine ────────────────────────────────────────────────────

def _load_garch_art(ticker: str) -> dict | None:
    """Load saved GARCH artifact, or None if unavailable."""
    try:
        import joblib
        path = _GARCH_DIR / f"{ticker}.joblib"
        return joblib.load(path) if path.exists() else None
    except Exception:
        return None


def _garch_steps(dte: int) -> int:
    """
    Convert calendar DTE to trading-day steps using numpy.busday_count so
    holidays are accounted for, not just the 5/7 approximation.
    Falls back to the approximation if today's date is unavailable.
    """
    try:
        from datetime import date, timedelta
        today = date.today()
        end   = today + timedelta(days=dte)
        steps = int(np.busday_count(today.isoformat(), end.isoformat()))
        return max(1, steps)
    except Exception:
        return max(1, round(dte * 5 / 7))


def _validate_garch_params(omega: float, alpha: float, beta: float) -> None:
    """Raise ValueError for degenerate GARCH parameters before simulation."""
    if omega <= 0:
        raise ValueError(f"GARCH omega must be > 0, got {omega}")
    if alpha < 0:
        raise ValueError(f"GARCH alpha must be >= 0, got {alpha}")
    if beta < 0:
        raise ValueError(f"GARCH beta must be >= 0, got {beta}")
    if alpha + beta >= 1.0:
        raise ValueError(
            f"GARCH alpha+beta={alpha+beta:.4f} >= 1 — variance non-stationary"
        )


def _simulate_garch(
    spot: float, art: dict, dte: int,
    risk_free_rate: float, n_sims: int,
    rng: np.random.Generator | None = None,
) -> tuple:
    """
    GARCH(1,1) price paths. Each simulated step = 1 trading day.

    GARCH was fitted on daily %-returns (×100), so stored params are in
    %-squared units (% vol)^2 per day. We convert to fractional units once
    at the top so all internal arithmetic is in natural return space.

    h_{t+1} = ω + α·ε_t² + β·h_t   (all in fraction²/day)
    r_t     = (rf/252 − ½·h_t) + √h_t · Z_t    Z ~ N(0,1)
    S_{t+1} = S_t · exp(r_t)

    Returns (terminal_prices, path_min, path_max), same shape as GBM fallback.
    """
    omega = art["omega"] / 1e4  # (% units)²/day  →  fraction²/day
    alpha = art["alpha"]
    beta  = art["beta"]
    h0    = art["last_conditional_variance_pct_sq"] / 1e4  # initial daily variance

    _validate_garch_params(omega, alpha, beta)

    rf_daily = risk_free_rate / 252.0
    steps    = _garch_steps(dte)

    _rng = rng or np.random.default_rng()
    Z    = _rng.standard_normal((n_sims, steps))

    log_S     = np.zeros(n_sims)
    h         = np.full(n_sims, h0)
    log_paths = np.empty((n_sims, steps))

    for t in range(steps):
        eps_t  = np.sqrt(h) * Z[:, t]
        r_t    = (rf_daily - 0.5 * h) + eps_t
        log_S += r_t
        log_paths[:, t] = log_S
        # Clamp to a small positive floor before next sqrt — floating-point
        # rounding can produce tiny negative values that make sqrt(h) = NaN.
        h = np.maximum(omega + alpha * eps_t ** 2 + beta * h, 1e-12)

    price_paths = spot * np.exp(log_paths)
    terminal    = price_paths[:, -1]
    path_min    = np.minimum(spot, price_paths.min(axis=1))
    path_max    = np.maximum(spot, price_paths.max(axis=1))
    return terminal, path_min, path_max


# ── Flat-vol GBM fallback ─────────────────────────────────────────────────────

def _simulate_gbm(
    spot: float, iv: float, dte: int,
    risk_free_rate: float, n_sims: int, n_steps: int,
    rng: np.random.Generator | None = None,
) -> tuple:
    """Standard GBM with constant vol — identical to the legacy implementation."""
    T      = dte / 365.0
    steps  = max(1, min(n_steps, dte))
    dt     = T / steps
    drift  = (risk_free_rate - 0.5 * iv ** 2) * dt
    vol_dt = iv * math.sqrt(dt)

    _rng         = rng or np.random.default_rng()
    Z            = _rng.standard_normal((n_sims, steps))
    log_returns  = drift + vol_dt * Z
    log_paths    = np.cumsum(log_returns, axis=1)
    price_paths  = spot * np.exp(log_paths)

    terminal = price_paths[:, -1]
    path_min = np.minimum(spot, price_paths.min(axis=1))
    path_max = np.maximum(spot, price_paths.max(axis=1))
    return terminal, path_min, path_max


# ── Public interface ──────────────────────────────────────────────────────────

def simulate_paths(
    ticker: str, spot: float, iv: float, dte: int,
    risk_free_rate: float,
    n_sims: int = DEFAULT_N_SIMS,
    n_steps: int = DEFAULT_N_STEPS,
    rng: np.random.Generator | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, str]:
    """
    Route to GARCH or GBM engine. Returns (terminal, path_min, path_max, vol_source).
    vol_source is 'garch' or 'gbm' — callers can attach it to result metadata.
    rng — optional seeded generator for deterministic unit tests.
    """
    if spot is None or dte is None or dte <= 0:
        return None, None, None, "error"

    art = _load_garch_art(ticker) if ticker else None
    if art is not None:
        try:
            T, mn, mx = _simulate_garch(spot, art, dte, risk_free_rate, n_sims, rng=rng)
            return T, mn, mx, "garch"
        except Exception as e:
            logger.warning("GARCH simulation failed for %s (%s) — falling back to GBM", ticker, e)

    if iv is None or iv <= 0:
        return None, None, None, "error"
    T, mn, mx = _simulate_gbm(spot, iv, dte, risk_free_rate, n_sims, n_steps, rng=rng)
    return T, mn, mx, "gbm"


def run_mc(ticker: str, row: dict, candidate: dict,
           n_sims: int = DEFAULT_N_SIMS) -> dict | None:
    """
    Full MC result for a candidate. Drop-in replacement for
    candidate_provider.monte_carlo_outcome, with identical output keys
    plus 'vol_source' ('garch' | 'gbm').
    """
    from config.rules import RISK_FREE_RATE
    from config.structures import get_or_none as get_structure

    spot           = row.get("spot")
    iv             = candidate.get("atm_iv") or row.get("atm_iv") or row.get("hv20")
    dte            = candidate.get("dte") if candidate.get("dte") is not None else row.get("dte")
    risk_free_rate = row.get("risk_free_rate") or RISK_FREE_RATE

    S_T, path_min, path_max, vol_source = simulate_paths(
        ticker, spot, iv, dte, risk_free_rate, n_sims=n_sims
    )
    if S_T is None:
        return None

    pnl = _payoff(candidate.get("structure"), candidate, S_T)
    if pnl is None:
        return None

    # Probability-of-touch: any short strike ever breached intra-path.
    # For iron condors both legs are evaluated independently and combined with OR,
    # so rallying past the call short OR falling through the put short both count.
    # Using "nearest only" underestimates touch probability for multi-leg structures.
    put_short  = candidate.get("put_short_strike")
    call_short = candidate.get("call_short_strike")
    short_strike = candidate.get("short_strike")  # single-leg / two-leg

    prob_of_touch = None
    if spot is not None:
        if put_short is not None and call_short is not None:
            # Iron condor: touch if price falls through put short OR rallies past call short
            touched = (path_min <= put_short) | (path_max >= call_short)
            prob_of_touch = round(float(np.mean(touched)) * 100, 1)
        elif short_strike is not None:
            touched = (path_min <= short_strike) if short_strike <= spot else (path_max >= short_strike)
            prob_of_touch = round(float(np.mean(touched)) * 100, 1)

    # CVaR (Expected Shortfall): mean loss in the worst 5% of outcomes.
    # Not "conditional on any loss" — that would be conditional expected loss.
    var_5 = float(np.percentile(pnl, 5))
    tail  = pnl[pnl <= var_5]
    cvar_loss = round(float(tail.mean()), 3) if len(tail) > 0 else 0.0

    return {
        "prob_of_touch":   prob_of_touch,
        "worst_loss_95":   round(float(np.percentile(pnl, 5)), 3),
        "expected_pnl":    round(float(np.mean(pnl)), 3),
        "prob_profit_sim": round(float(np.mean(pnl > 0)) * 100, 1),
        "p10_pnl":         round(float(np.percentile(pnl, 10)), 3),
        "p90_pnl":         round(float(np.percentile(pnl, 90)), 3),
        "cvar_loss":       cvar_loss,
        "vol_source":      vol_source,
        "n_sims":          n_sims,
    }


# ── Payoff functions (mirrored from candidate_provider) ───────────────────────

def _payoff(structure_name: str, candidate: dict, S_T: np.ndarray) -> np.ndarray | None:
    """Terminal payoff per share for supported structures."""
    from config.structures import get_or_none as get_structure
    st = get_structure(structure_name)
    if st is None:
        return None

    if st.strike_schema.value == "single_leg" and st.option_type == "put" and st.is_credit:
        k      = candidate.get("short_strike")
        credit = candidate.get("max_profit")
        if k is None or credit is None:
            return None
        return credit - np.maximum(0.0, k - S_T)

    if st.strike_schema.value == "two_leg":
        k_short, k_long = candidate.get("short_strike"), candidate.get("long_strike")
        if k_short is None or k_long is None:
            return None
        width = abs(k_short - k_long)
        # Normalize once so payoff formulas don't need conditional strike ordering.
        lower, upper = min(k_short, k_long), max(k_short, k_long)
        if st.is_credit:
            credit = candidate.get("max_profit")
            if credit is None:
                return None
            if st.option_type == "put":
                # Credit put spread: short the higher strike, long the lower
                loss = np.clip(upper - S_T, 0.0, width)
            else:
                # Credit call spread: short the lower strike, long the higher
                loss = np.clip(S_T - lower, 0.0, width)
            return credit - loss
        else:
            debit = candidate.get("max_loss")
            if debit is None:
                return None
            if st.option_type == "call":
                # Debit call spread: long the lower strike, short the higher
                payoff = np.clip(S_T - lower, 0.0, width)
            else:
                # Debit put spread: long the higher strike, short the lower
                payoff = np.clip(upper - S_T, 0.0, width)
            return payoff - debit

    if st.strike_schema.value == "iron_condor":
        pl, ps = candidate.get("put_long_strike"), candidate.get("put_short_strike")
        cs, cl = candidate.get("call_short_strike"), candidate.get("call_long_strike")
        credit = candidate.get("max_profit")
        if None in (pl, ps, cs, cl, credit):
            return None
        put_width  = abs(ps - pl)
        call_width = abs(cl - cs)
        loss_put   = np.clip(ps - S_T, 0.0, put_width)
        loss_call  = np.clip(S_T - cs, 0.0, call_width)
        return credit - loss_put - loss_call

    return None


# ── CLI smoke-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    p = argparse.ArgumentParser()
    p.add_argument("ticker")
    p.add_argument("--spot",  type=float, default=100.0)
    p.add_argument("--iv",    type=float, default=0.25, help="Annualized IV fraction (fallback only)")
    p.add_argument("--dte",   type=int,   default=14)
    p.add_argument("--sims",  type=int,   default=DEFAULT_N_SIMS)
    p.add_argument("--rf",    type=float, default=0.04)
    p.add_argument("--seed",  type=int,   default=None, help="RNG seed for reproducible output")
    args = p.parse_args()

    _rng = np.random.default_rng(args.seed)
    T, mn, mx, src = simulate_paths(
        args.ticker, args.spot, args.iv, args.dte, args.rf, args.sims, rng=_rng,
    )
    if T is None:
        print("Simulation failed.")
        sys.exit(1)

    # Realised vol from simulated log-returns (standard estimator)
    log_ret     = np.log(T / args.spot)
    realised_vol = log_ret.std() * math.sqrt(365.0 / args.dte)

    print(f"\nMonte Carlo — {args.ticker}  |  engine: {src}  |  n={args.sims}")
    print(f"  Spot:         {args.spot:.2f}")
    print(f"  DTE:          {args.dte}")
    print(f"  Terminal      mean={T.mean():.2f}  std={T.std():.2f}")
    print(f"  5th pct:      {np.percentile(T,5):.2f}")
    print(f"  95th pct:     {np.percentile(T,95):.2f}")
    print(f"  Path min avg: {mn.mean():.2f}")
    print(f"  Path max avg: {mx.mean():.2f}")
    print(f"  Realised ann vol from simulation: {realised_vol:.1%}")
