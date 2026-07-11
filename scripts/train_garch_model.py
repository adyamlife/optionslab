"""
GARCH Volatility Model — per-ticker GARCH(1,1) on daily log returns.

XGBoost predicts forward HV from lagged features treating each day independently.
GARCH captures volatility clustering — the actual DGP behind short-term realized
vol. GARCH conditional variance is a superior vol feature vs HV20 alone because
it weights recent shocks appropriately (via alpha) and mean-reverts via the
long-run variance (via omega/(1-alpha-beta)).

This module:
  1. Fits GARCH(1,1) per ticker on 2yr of yfinance daily log returns.
  2. Saves {model, last_fit_date, n_obs, aic, bic} to data/models/garch/{ticker}.joblib.
  3. Exposes get_garch_forecast(ticker) → float (next-day conditional vol, annualized).
     Returns None gracefully when arch is not installed or model file missing.

Dependency: arch>=5.0  (pip install arch)
  If arch is not installed, all functions return None — the rest of the pipeline
  degrades gracefully to garch_conditional_var = None.

Run standalone: python -m scripts.train_garch_model [--tickers AAPL MSFT ...]
Output: data/models/garch/{TICKER}.joblib
"""
import argparse
import sys
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf
import joblib

_ROOT       = Path(__file__).resolve().parent.parent
_GARCH_DIR  = _ROOT / "data" / "models" / "garch"

# Annualization factor: daily variance → annualized vol
_TRADING_DAYS = 252


def _log_returns(close: pd.Series) -> pd.Series:
    """Daily log returns, dropping the first NaN."""
    return np.log(close / close.shift(1)).dropna()


def fit_garch(ticker: str, period: str = "2y") -> dict | None:
    """
    Fit GARCH(1,1) on daily log returns for ticker.
    Returns artifact dict on success, None on failure.
    """
    try:
        from arch import arch_model
    except ImportError:
        print("arch not installed — skipping GARCH. Run: pip install arch")
        return None

    hist = yf.Ticker(ticker).history(period=period)
    if hist.empty or len(hist) < 60:
        return None

    ret = _log_returns(hist["Close"]) * 100  # scale to % — arch prefers this

    model = arch_model(ret, vol="Garch", p=1, q=1, dist="normal", rescale=False)
    try:
        res = model.fit(disp="off", show_warning=False)
    except Exception:
        return None

    # 1-step ahead forecast (h.1 = next-day conditional variance in %-squared units)
    fc = res.forecast(horizon=1, reindex=False)
    next_var_pct_sq = float(fc.variance.iloc[-1, 0])  # (% units)^2
    # Convert: (% units)^2 → fractional variance → annualized vol
    next_var_frac = next_var_pct_sq / 1e4              # from % to fraction
    cond_vol_ann  = float(np.sqrt(next_var_frac * _TRADING_DAYS))

    artifact = {
        "ticker":       ticker,
        "model_params": {k: float(v) for k, v in res.params.items()},
        "aic":          float(res.aic),
        "bic":          float(res.bic),
        "n_obs":        int(len(ret)),
        "last_fit_date": str(date.today()),
        "cond_vol_ann":  cond_vol_ann,  # cached last forecast for quick access
        # Store the last few conditional variances for warm-start inference
        "last_conditional_variance_pct_sq": float(res.conditional_volatility.iloc[-1] ** 2),
        "last_resid_pct":                   float(res.resid.iloc[-1]),
        "omega": float(res.params.get("omega", np.nan)),
        "alpha": float(res.params.get("alpha[1]", np.nan)),
        "beta":  float(res.params.get("beta[1]", np.nan)),
    }
    return artifact


def _garch_forecast_from_artifact(art: dict) -> float | None:
    """Recompute 1-step ahead from stored GARCH params (avoids re-fitting)."""
    omega = art.get("omega")
    alpha = art.get("alpha")
    beta  = art.get("beta")
    h_t   = art.get("last_conditional_variance_pct_sq")
    e_t   = art.get("last_resid_pct")
    if any(v is None or np.isnan(v) for v in [omega, alpha, beta, h_t, e_t]):
        return art.get("cond_vol_ann")
    h_next = omega + alpha * (e_t ** 2) + beta * h_t
    return float(np.sqrt(h_next / 1e4 * _TRADING_DAYS))


def get_garch_forecast(ticker: str) -> float | None:
    """
    Return next-day conditional vol (annualized) for ticker from saved model.
    Returns None if arch is not installed or model file is missing.
    """
    path = _GARCH_DIR / f"{ticker}.joblib"
    if not path.exists():
        return None
    try:
        art = joblib.load(path)
        return _garch_forecast_from_artifact(art)
    except Exception:
        return None


def train(tickers: list[str] | None = None, period: str = "2y") -> dict:
    """Fit and save GARCH(1,1) for each ticker. Returns summary dict."""
    if tickers is None:
        from scripts.regime_backfill import backfill_tickers
        tickers = backfill_tickers()

    _GARCH_DIR.mkdir(parents=True, exist_ok=True)

    results, errors = [], []
    for ticker in tickers:
        art = fit_garch(ticker, period=period)
        if art is None:
            errors.append(ticker)
            continue
        out = _GARCH_DIR / f"{ticker}.joblib"
        joblib.dump(art, out)
        results.append({
            "ticker":       ticker,
            "aic":          art["aic"],
            "n_obs":        art["n_obs"],
            "cond_vol_ann": round(art["cond_vol_ann"], 4),
            "omega":        round(art["omega"], 6),
            "alpha":        round(art["alpha"], 4),
            "beta":         round(art["beta"],  4),
        })

    return {"ok": True, "fitted": len(results), "errors": errors, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", help="Tickers to fit (default: all watchlist)")
    parser.add_argument("--period", default="2y", help="yfinance period string (default: 2y)")
    args = parser.parse_args()

    out = train(tickers=args.tickers, period=args.period)
    if not out["ok"]:
        print("Failed")
        sys.exit(1)

    print(f"\nFitted GARCH(1,1) for {out['fitted']} tickers  |  errors: {len(out['errors'])}")
    if out["errors"]:
        print(f"  Failed: {out['errors']}")
    print(f"\n{'Ticker':<8} {'AIC':>10} {'N':>6} {'CondVol':>9} {'omega':>10} {'alpha':>7} {'beta':>7}")
    print("-" * 60)
    for r in sorted(out["results"], key=lambda x: x["ticker"]):
        print(f"{r['ticker']:<8} {r['aic']:>10.1f} {r['n_obs']:>6} "
              f"{r['cond_vol_ann']:>9.4f} {r['omega']:>10.6f} {r['alpha']:>7.4f} {r['beta']:>7.4f}")
    print(f"\nModels saved to {_GARCH_DIR}")
