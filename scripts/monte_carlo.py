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
import math
from pathlib import Path

import numpy as np

_ROOT      = Path(__file__).resolve().parent.parent
_GARCH_DIR = _ROOT / "data" / "models" / "garch"

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


def _simulate_garch(spot: float, art: dict, dte: int,
                    risk_free_rate: float, n_sims: int) -> tuple:
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

    rf_daily = risk_free_rate / 252.0
    steps    = max(1, round(dte * 5 / 7))  # calendar DTE → approx trading days

    rng = np.random.default_rng()
    Z   = rng.standard_normal((n_sims, steps))  # (n_sims, steps)

    # Vectorized over paths; iterate over time (steps typically 7–45)
    log_S     = np.zeros(n_sims)
    h         = np.full(n_sims, h0)
    log_paths = np.empty((n_sims, steps))

    for t in range(steps):
        eps_t   = np.sqrt(h) * Z[:, t]          # innovation (fraction)
        r_t     = (rf_daily - 0.5 * h) + eps_t  # log-return this day
        log_S  += r_t
        log_paths[:, t] = log_S
        h = omega + alpha * eps_t ** 2 + beta * h  # propagate variance

    price_paths   = spot * np.exp(log_paths)
    terminal      = price_paths[:, -1]
    path_min      = np.minimum(spot, price_paths.min(axis=1))
    path_max      = np.maximum(spot, price_paths.max(axis=1))
    return terminal, path_min, path_max


# ── Flat-vol GBM fallback ─────────────────────────────────────────────────────

def _simulate_gbm(spot: float, iv: float, dte: int,
                  risk_free_rate: float, n_sims: int, n_steps: int) -> tuple:
    """Standard GBM with constant vol — identical to the legacy implementation."""
    T      = dte / 365.0
    steps  = max(1, min(n_steps, dte))
    dt     = T / steps
    drift  = (risk_free_rate - 0.5 * iv ** 2) * dt
    vol_dt = iv * math.sqrt(dt)

    rng          = np.random.default_rng()
    Z            = rng.standard_normal((n_sims, steps))
    log_returns  = drift + vol_dt * Z
    log_paths    = np.cumsum(log_returns, axis=1)
    price_paths  = spot * np.exp(log_paths)

    terminal = price_paths[:, -1]
    path_min = np.minimum(spot, price_paths.min(axis=1))
    path_max = np.maximum(spot, price_paths.max(axis=1))
    return terminal, path_min, path_max


# ── Public interface ──────────────────────────────────────────────────────────

def simulate_paths(ticker: str, spot: float, iv: float, dte: int,
                   risk_free_rate: float,
                   n_sims: int = DEFAULT_N_SIMS,
                   n_steps: int = DEFAULT_N_STEPS) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """
    Route to GARCH or GBM engine. Returns (terminal, path_min, path_max, vol_source).
    vol_source is 'garch' or 'gbm' — callers can attach it to result metadata.
    """
    if spot is None or dte is None or dte <= 0:
        return None, None, None, "error"

    art = _load_garch_art(ticker) if ticker else None
    if art is not None:
        try:
            T, mn, mx = _simulate_garch(spot, art, dte, risk_free_rate, n_sims)
            return T, mn, mx, "garch"
        except Exception:
            pass  # fall through to GBM

    if iv is None or iv <= 0:
        return None, None, None, "error"
    T, mn, mx = _simulate_gbm(spot, iv, dte, risk_free_rate, n_sims, n_steps)
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

    # Probability-of-touch: nearest short strike ever breached intra-path
    short_strikes = [
        s for s in (
            candidate.get("short_strike"),
            candidate.get("put_short_strike"),
            candidate.get("call_short_strike"),
        ) if s is not None
    ]
    prob_of_touch = None
    if short_strikes and spot is not None:
        nearest  = min(short_strikes, key=lambda k: abs(k - spot))
        touched  = (path_min <= nearest) if nearest <= spot else (path_max >= nearest)
        prob_of_touch = round(float(np.mean(touched)) * 100, 1)

    # Conditional expected loss: E[loss | loss > 0]  — CVaR-flavored insight
    losses = pnl[pnl < 0]
    cvar_loss = round(float(losses.mean()), 3) if len(losses) > 0 else 0.0

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
    args = p.parse_args()

    T, mn, mx, src = simulate_paths(args.ticker, args.spot, args.iv, args.dte, args.rf, args.sims)
    if T is None:
        print("Simulation failed.")
        sys.exit(1)

    print(f"\nMonte Carlo — {args.ticker}  |  engine: {src}  |  n={args.sims}")
    print(f"  Spot:         {args.spot:.2f}")
    print(f"  DTE:          {args.dte}")
    print(f"  Terminal      mean={T.mean():.2f}  std={T.std():.2f}")
    print(f"  5th pct:      {np.percentile(T,5):.2f}")
    print(f"  95th pct:     {np.percentile(T,95):.2f}")
    print(f"  Path min avg: {mn.mean():.2f}")
    print(f"  Path max avg: {mx.mean():.2f}")
    implied_vol = T.std() / args.spot / (args.dte / 365.0) ** 0.5
    print(f"  Implied ann vol from simulation: {implied_vol:.1%}")
