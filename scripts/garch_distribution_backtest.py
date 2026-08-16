"""
P2-A: GARCH MC price-distribution interval-coverage backtest.

Central question: does the GARCH MC p10→p90 interval contain the realized
price ~80% of the time? If not, what vol_adj_factor achieves the target?

Methodology (pseudo-point-in-time):
  For each ticker:
    1. Pull 2yr of daily closes via yfinance.
    2. Fit GARCH(1,1) on the FULL history (scipy MLE, fraction-scale returns).
       "Pseudo-point-in-time" — params are from full history, but the
       conditional variance series h_t is reconstructed at each historical
       date using the GARCH filter. This is fast (O(n) per ticker) and
       sufficient to identify systematic vol bias.
    3. Walk forward from day MIN_HISTORY to N-HORIZON, every STEP_DAYS:
         - h_t from GARCH filter = initialisation variance for MC
         - run N_SIMS MC paths × HORIZON steps (matches _simulate_garch exactly)
         - p10, p50, p90 of terminal price distribution
         - realized = actual close HORIZON trading days later
         - covered  = (p10 <= realized <= p90)
    4. Compute z-scores: z = log(realized/spot) / sqrt(h_t * HORIZON)
       Implied vol_adj_factor = empirical_p90(|z|) / normal_p90(1.0)
       A factor > 1.0 means realized moves are wider than GARCH predicts.

Note: vol_adj_factor in config/settings.toml currently only scales GBM iv,
NOT GARCH paths. If this backtest shows coverage < 80%, a separate GARCH
vol-scaling mechanism would be needed. This script quantifies the gap.

Run:
  python -m scripts.garch_distribution_backtest
  python -m scripts.garch_distribution_backtest --tickers AAPL MSFT SPY QQQ
  python -m scripts.garch_distribution_backtest --step 10 --n-sims 5000
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_TICKERS = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "TSLA",
    "AMZN", "META", "GOOGL", "JPM", "XOM", "INTC", "CRM", "AVGO",
]
MIN_HISTORY  = 252   # days of history before first forecast
HORIZON      = 10    # trading days ahead (matches run_mc default dte)
STEP_DAYS    = 5     # walk-forward step (re-simulate every N trading days)
DEFAULT_SIMS = 2000
P_LO, P_HI  = 10, 90       # interval percentiles → 80% nominal coverage
TARGET_COV   = 0.80
RF_ANNUAL    = 0.045        # risk-free rate assumed for drift correction
_NORMAL_P90  = 1.28155      # qnorm(0.90); for implied vol_adj_factor calc
_PERSIST_MAX = 0.9999


# ── GARCH(1,1) fitting — scipy MLE, fraction-scale returns ───────────────────

def _garch_negll(p, returns, n, h0):
    """Negative log-likelihood for GARCH(1,1) with Normal innovations."""
    omega, alpha, beta = p
    if omega <= 0 or alpha < 0 or beta < 0 or (alpha + beta) >= _PERSIST_MAX:
        return 1e10
    h = np.empty(n)
    h[0] = h0
    for t in range(1, n):
        h[t] = omega + alpha * returns[t - 1] ** 2 + beta * h[t - 1]
        if h[t] <= 0:
            return 1e10
    return 0.5 * float(np.sum(np.log(h) + returns ** 2 / h))


def _fit_garch(returns: np.ndarray) -> dict | None:
    """
    Fit GARCH(1,1) with Normal innovations via scipy SLSQP with multi-start.

    Works in fraction-scale returns (NOT percent). Params in fraction²/day.
    Multi-start over typical (alpha, beta) pairs guards against local minima
    that arise when the likelihood surface has a ridge near the IGARCH boundary.

    Returns {"omega": float, "alpha": float, "beta": float} or None.
    """
    from scipy.optimize import minimize

    n      = len(returns)
    svar   = float(np.var(returns))
    h0     = svar

    bnd = [(1e-12, None), (1e-6, 0.45), (0.01, 0.995)]
    con = [{"type": "ineq", "fun": lambda p: _PERSIST_MAX - p[1] - p[2]}]

    # Grid of starting (alpha, beta) values — covers typical GARCH parameter space
    starts = [
        (0.05, 0.93),   # moderate persistence ~0.98
        (0.10, 0.85),   # lower beta
        (0.08, 0.90),   # canonical
        (0.15, 0.80),   # higher alpha
        (0.03, 0.95),   # very persistent
    ]

    best_ll, best_params = np.inf, None
    for a0, b0 in starts:
        omega0 = svar * max(1 - a0 - b0, 1e-6)
        x0 = [omega0, a0, b0]
        try:
            res = minimize(
                _garch_negll, x0,
                args=(returns, n, h0),
                method="SLSQP",
                bounds=bnd,
                constraints=con,
                options={"maxiter": 1000, "ftol": 1e-10},
            )
            if res.fun < best_ll and res.fun < 1e9:
                best_ll, best_params = res.fun, res.x
        except Exception:
            continue

    if best_params is None:
        return None
    omega, alpha, beta = best_params
    return {"omega": float(omega), "alpha": float(alpha), "beta": float(beta)}


def _garch_filter(returns: np.ndarray, omega: float, alpha: float, beta: float) -> np.ndarray:
    """Reconstruct GARCH(1,1) conditional variance series from fitted params."""
    n  = len(returns)
    h  = np.empty(n)
    h0 = omega / max(1 - alpha - beta, 1e-9)   # long-run variance as seed
    h[0] = h0
    for t in range(1, n):
        h[t] = omega + alpha * returns[t - 1] ** 2 + beta * h[t - 1]
        h[t] = max(h[t], 1e-12)
    return h


# ── Monte Carlo — mirrors _simulate_garch() in monte_carlo.py exactly ────────

def _mc_terminal(
    spot: float, h0: float,
    omega: float, alpha: float, beta: float,
    n_sims: int, steps: int,
    rf: float, rng: np.random.Generator,
) -> np.ndarray:
    """
    GARCH(1,1) MC terminal prices.  Matches monte_carlo._simulate_garch:
      h_{t+1} = ω + α·ε_t² + β·h_t
      r_t     = (rf/252 − ½·h_t) + √h_t · Z_t
    All quantities in fraction²/day.
    """
    rf_daily = rf / 252.0
    Z        = rng.standard_normal((n_sims, steps))

    log_S = np.zeros(n_sims)
    h     = np.full(n_sims, h0)

    for t in range(steps):
        eps_t  = np.sqrt(h) * Z[:, t]
        r_t    = (rf_daily - 0.5 * h) + eps_t
        log_S += r_t
        h      = np.maximum(omega + alpha * eps_t ** 2 + beta * h, 1e-12)

    return spot * np.exp(log_S)


# ── Per-ticker backtest ───────────────────────────────────────────────────────

def _backtest_ticker(
    ticker: str,
    n_sims: int,
    step_days: int,
    rng: np.random.Generator,
) -> dict | None:
    """Walk-forward interval coverage for one ticker. Returns None if insufficient data."""
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="2y")
    if hist.empty or len(hist) < MIN_HISTORY + HORIZON + 10:
        log.warning("%s: not enough price history (%d rows)", ticker, len(hist))
        return None

    closes  = hist["Close"].values.astype(float)
    n       = len(closes)
    log_ret = np.log(closes[1:] / closes[:-1])   # n-1 fraction-scale returns

    params = _fit_garch(log_ret)
    if params is None:
        log.warning("%s: GARCH fit failed", ticker)
        return None

    omega, alpha, beta = params["omega"], params["alpha"], params["beta"]
    h_series = _garch_filter(log_ret, omega, alpha, beta)   # length n-1

    # Walk forward: closes[i] = spot, closes[i+HORIZON] = realized
    # h_series[i-1] = conditional variance at bar i (0-indexed into log_ret)
    covered, widths, z_scores = [], [], []
    realized_sd_ann = np.std(log_ret) * np.sqrt(252)

    for i in range(MIN_HISTORY, n - HORIZON, step_days):
        spot     = closes[i]
        h0       = h_series[i - 1]           # h_{i}; i-1 indexes into log_ret
        realized = closes[i + HORIZON]

        S_T = _mc_terminal(spot, h0, omega, alpha, beta, n_sims, HORIZON, RF_ANNUAL, rng)

        p10, p50, p90 = np.percentile(S_T, [P_LO, 50, P_HI])

        covered.append(int(p10 <= realized <= p90))
        widths.append((p90 - p10) / spot)

        # z-score: how many GARCH standard-deviations is the realized move?
        garch_sd = np.sqrt(h0 * HORIZON)      # fraction, 10-day horizon
        if garch_sd > 0:
            z = np.log(realized / spot) / garch_sd
            z_scores.append(z)

    if not covered:
        return None

    n_obs    = len(covered)
    coverage = float(np.mean(covered))

    # Implied vol_adj_factor: scale GARCH vol until p10-p90 covers 80%
    # z ~ N(0,1) if model correct → empirical p90 of |z| should equal 1.282
    if z_scores:
        empirical_p90_absz = float(np.percentile(np.abs(z_scores), 90))
        implied_vaf        = round(empirical_p90_absz / _NORMAL_P90, 3)
    else:
        implied_vaf = None

    return {
        "ticker":       ticker,
        "n_obs":        n_obs,
        "coverage":     coverage,
        "median_width": float(np.median(widths)),
        "persistence":  round(alpha + beta, 4),
        "implied_vaf":  implied_vaf,
        "realized_vol": round(realized_sd_ann, 4),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def run(
    tickers: list[str] | None = None,
    n_sims: int = DEFAULT_SIMS,
    step_days: int = STEP_DAYS,
) -> None:
    tickers = tickers or DEFAULT_TICKERS
    rng = np.random.default_rng(42)

    print(f"\n-- GARCH Distribution Backtest  "
          f"(p{P_LO}-p{P_HI} interval, horizon={HORIZON} trading days) --\n")
    print(f"{'Ticker':<8}  {'N':>5}  {'Coverage':>9}  {'Width%':>7}  "
          f"{'Persist':>8}  {'Impl VAF':>9}  Status")
    print("-" * 72)

    all_covered: list[int] = []
    all_z:       list[float] = []
    results: list[dict] = []

    for tkr in tickers:
        try:
            r = _backtest_ticker(tkr, n_sims, step_days, rng)
        except Exception as exc:
            print(f"{tkr:<8}  ERROR: {exc}")
            continue
        if r is None:
            print(f"{tkr:<8}  SKIP — insufficient data")
            continue

        gap = r["coverage"] - TARGET_COV
        if abs(gap) < 0.03:
            status = "~ target"
        elif gap < 0:
            status = f"LOW {gap:+.1%}"
        else:
            status = f"HIGH {gap:+.1%}"

        vaf_str = f"{r['implied_vaf']:.3f}" if r["implied_vaf"] is not None else "n/a"
        print(f"{tkr:<8}  {r['n_obs']:>5}  {r['coverage']:>8.1%}  "
              f"{r['median_width']*100:>6.1f}%  "
              f"{r['persistence']:>8.4f}  {vaf_str:>9}  {status}")

        results.append(r)

    if not results:
        print("\nNo results — check yfinance connectivity.")
        return

    # ── Aggregate ────────────────────────────────────────────────────────────
    total_n   = sum(r["n_obs"] for r in results)
    # Weighted coverage by n_obs
    overall_cov = sum(r["coverage"] * r["n_obs"] for r in results) / total_n
    vaf_values  = [r["implied_vaf"] for r in results if r["implied_vaf"] is not None]
    agg_vaf     = float(np.median(vaf_values)) if vaf_values else None
    gap         = overall_cov - TARGET_COV

    print("\n" + "-" * 72)
    print(f"\n-- Aggregate --")
    print(f"  Tickers evaluated  : {len(results)}")
    print(f"  Total observations : {total_n:,}")
    print(f"  Overall coverage   : {overall_cov:.1%}  (target: {TARGET_COV:.0%})")
    print(f"  Coverage gap       : {gap:+.1%}  ({gap*100:+.1f} percentage points)")

    # Implied VAF from z-score tails: how fat are the residuals vs Normal GARCH?
    # VAF > 1 means fatter tails than Normal; VAF < 1 means thinner.
    # NOTE: MC already accounts for GARCH dynamics during the horizon (propagated
    # variance path), so MC coverage is the definitive metric, not z-score VAF.
    # The z-score uses only h0 for the denominator, which understates variance
    # when vol spikes within the horizon — explaining why VAF > 1 coexists with
    # coverage above target.
    if agg_vaf is not None:
        print(f"  Tail z-score VAF   : {agg_vaf:.3f}  "
              f"(>1 = fat residual tails vs Normal; MC coverage is the true metric)")

    print()
    if gap > -0.03:
        # Coverage >= 77% — within acceptable range (either at or above target)
        if gap > 0.05:
            print(f"  NOTE  Coverage {overall_cov:.1%} is {gap:.1%} above target — "
                  f"GARCH intervals are conservative (wide). Safe to leave as-is.")
        else:
            print("  OK    Coverage is within +/-3pp of target -- vol_adj_factor = 1.0 is appropriate.")
        print( "        Recommendation: do NOT change vol_adj_factor from 1.0.")
    else:
        print(f"  WARN  Coverage {overall_cov:.1%} is {gap:.1%} below 80% target.")
        print( "        GARCH MC intervals are too narrow for this universe.")
        print( "        Note: vol_adj_factor in settings.toml scales GBM iv only,")
        print( "        not GARCH paths. A GARCH-specific vol scaling would be needed.")

    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="GARCH distribution interval-coverage backtest")
    ap.add_argument("--tickers", nargs="+", default=None,
                    help="Tickers to test (default: top-15 universe)")
    ap.add_argument("--n-sims", type=int, default=DEFAULT_SIMS,
                    help=f"Monte Carlo simulations per step (default: {DEFAULT_SIMS})")
    ap.add_argument("--step", type=int, default=STEP_DAYS,
                    help=f"Walk-forward step in trading days (default: {STEP_DAYS})")
    args = ap.parse_args()

    run(tickers=args.tickers, n_sims=args.n_sims, step_days=args.step)
