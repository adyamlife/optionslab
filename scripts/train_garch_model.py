"""
GARCH Volatility Model — per-ticker GARCH(1,1) / EGARCH(1,1) on daily log returns.

XGBoost predicts forward HV from lagged features treating each day independently.
GARCH captures volatility clustering — the actual DGP behind short-term realized
vol. GARCH conditional variance is a superior vol feature vs HV20 alone because
it weights recent shocks appropriately (via alpha) and mean-reverts via the
long-run variance (via omega/(1-alpha-beta)).

This module:
  1. Fits GARCH(1,1) with Student-t innovations per ticker on 2yr of daily log returns.
     If EGARCH(1,1) produces a meaningfully lower AIC, saves EGARCH instead.
  2. Applies quality gates before saving:
       - n_obs >= 250
       - optimizer converged
       - GARCH persistence (α+β) < 0.999  (IGARCH is unreliable for forecasting)
  3. Saves a rich artifact per ticker to data/models/garch/{ticker}.joblib.
  4. Exposes:
       get_garch_forecast(ticker) → float | None   — backward-compatible float
       get_garch_features(ticker) → dict | None    — rich feature dict for ML use

Dependency: arch>=5.0  (pip install arch)
  If arch is not installed, all functions return None — the rest of the pipeline
  degrades gracefully to garch_conditional_var = None.

Run standalone: python -m scripts.train_garch_model [--tickers AAPL MSFT ...]
Output: data/models/garch/{TICKER}.joblib
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import yfinance as yf

_ROOT      = Path(__file__).resolve().parent.parent
_GARCH_DIR = _ROOT / "data" / "models" / "garch"

_TRADING_DAYS  = 252
_MIN_OBS       = 250          # reject models with fewer observations
_PERSISTENCE_MAX = 0.999      # α+β above this → IGARCH; forecasts unreliable
_VOL_CLIP_LO   = 0.05         # 5%  — floor on annualised conditional vol
_VOL_CLIP_HI   = 2.50         # 250% — ceiling on annualised conditional vol

log = logging.getLogger(__name__)


def _log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1)).dropna()


def _next_bday(d: date) -> date:
    return (pd.Timestamp(d) + pd.offsets.BDay(1)).date()


def fit_garch(ticker: str, period: str = "2y") -> dict | None:
    """
    Fit GARCH(1,1) and EGARCH(1,1) with Student-t innovations; save the better AIC.

    Quality gates are NOT applied here — callers decide whether to save.
    Returns the artifact dict (always, even when quality is poor), or None on
    hard failure (import error, empty history, both fits crashed).
    """
    try:
        from arch import arch_model
    except ImportError:
        log.warning("arch not installed — skipping GARCH for %s. Run: pip install arch", ticker)
        return None

    hist = yf.Ticker(ticker).history(period=period)
    if hist.empty or len(hist) < 60:
        return None

    ret = _log_returns(hist["Close"]) * 100   # scale to % — arch convention

    def _try_fit(vol_type: str):
        m = arch_model(ret, vol=vol_type, p=1, q=1, dist="t", rescale=False)
        try:
            return m.fit(disp="off", show_warning=False)
        except Exception:
            return None

    res_garch  = _try_fit("Garch")
    res_egarch = _try_fit("EGARCH")

    if res_garch is None and res_egarch is None:
        return None

    # Pick the fit with lower AIC; prefer GARCH when tied (simpler/more interpretable)
    if res_garch is not None and res_egarch is not None:
        if float(res_egarch.aic) < float(res_garch.aic):
            res, model_type = res_egarch, "EGARCH"
        else:
            res, model_type = res_garch, "GARCH"
    elif res_garch is not None:
        res, model_type = res_garch, "GARCH"
    else:
        res, model_type = res_egarch, "EGARCH"

    # ── Convergence ───────────────────────────────────────────────────────────
    try:
        converged = int(res.convergence_flag) == 0
    except Exception:
        converged = True   # assume converged if attribute unavailable

    # ── Parameters ───────────────────────────────────────────────────────────
    omega = float(res.params.get("omega", np.nan))
    alpha = float(res.params.get("alpha[1]", np.nan))
    beta  = float(res.params.get("beta[1]", np.nan))
    nu    = float(res.params.get("nu", np.nan))      # Student-t degrees of freedom

    # ── Persistence / stationarity ────────────────────────────────────────────
    # GARCH(1,1): stationary iff α+β < 1
    # EGARCH(1,1): stationary iff |β| < 1  (different condition, different formula)
    if model_type == "GARCH":
        persistence = alpha + beta if not (np.isnan(alpha) or np.isnan(beta)) else float("nan")
        stationary  = float(persistence) < _PERSISTENCE_MAX if not np.isnan(persistence) else False
    else:
        persistence = abs(beta) if not np.isnan(beta) else float("nan")
        stationary  = float(persistence) < 1.0 if not np.isnan(persistence) else False

    # ── Long-run (unconditional) variance — GARCH(1,1) only ──────────────────
    if (model_type == "GARCH" and stationary
            and not any(np.isnan(v) for v in [omega, alpha, beta])):
        long_run_var_pct_sq = omega / (1.0 - alpha - beta)
        long_run_vol_ann    = float(np.sqrt(long_run_var_pct_sq / 1e4 * _TRADING_DAYS))
    else:
        long_run_var_pct_sq = None
        long_run_vol_ann    = None

    # ── 1-step-ahead conditional vol forecast ────────────────────────────────
    fc = res.forecast(horizon=1, reindex=False)
    next_var_pct_sq = float(fc.variance.iloc[-1, 0])
    cond_vol_ann    = float(np.sqrt(next_var_pct_sq / 1e4 * _TRADING_DAYS))
    cond_vol_ann    = float(np.clip(cond_vol_ann, _VOL_CLIP_LO, _VOL_CLIP_HI))

    last_fit_date = date.today()
    forecast_for  = _next_bday(last_fit_date)

    return {
        "ticker":        ticker,
        "model_type":    model_type,
        "model_params":  {k: float(v) for k, v in res.params.items()},
        "aic":           float(res.aic),
        "bic":           float(res.bic),
        "loglikelihood": float(res.loglikelihood),
        "n_obs":         int(len(ret)),
        "converged":     converged,
        "last_fit_date": str(last_fit_date),
        "forecast_for":  str(forecast_for),
        "cond_vol_ann":  cond_vol_ann,
        # Warm-start params (used by _garch_forecast_from_artifact for GARCH only)
        "last_conditional_variance_pct_sq": float(res.conditional_volatility.iloc[-1] ** 2),
        "last_resid_pct":                   float(res.resid.iloc[-1]),
        "omega":         omega,
        "alpha":         alpha,
        "beta":          beta,
        "nu":            nu,
        "persistence":   round(float(persistence), 6) if not np.isnan(persistence) else None,
        "stationary":    stationary,
        "long_run_vol_ann": long_run_vol_ann,
    }


def _garch_forecast_from_artifact(art: dict) -> float | None:
    """
    Recompute 1-step-ahead conditional vol from stored params (avoids re-fitting).
    For EGARCH, the recursive update formula requires the full log-variance series,
    so we fall back to the cached forecast instead.
    """
    # EGARCH warm-start not implemented — cached value is accurate as of last fit
    if art.get("model_type") == "EGARCH":
        v = art.get("cond_vol_ann")
        return float(np.clip(v, _VOL_CLIP_LO, _VOL_CLIP_HI)) if v is not None else None

    omega = art.get("omega")
    alpha = art.get("alpha")
    beta  = art.get("beta")
    h_t   = art.get("last_conditional_variance_pct_sq")
    e_t   = art.get("last_resid_pct")
    if any(v is None or (isinstance(v, float) and np.isnan(v))
           for v in [omega, alpha, beta, h_t, e_t]):
        v = art.get("cond_vol_ann")
        return float(np.clip(v, _VOL_CLIP_LO, _VOL_CLIP_HI)) if v is not None else None

    h_next = omega + alpha * (e_t ** 2) + beta * h_t
    return float(np.clip(np.sqrt(h_next / 1e4 * _TRADING_DAYS), _VOL_CLIP_LO, _VOL_CLIP_HI))


def get_garch_forecast(ticker: str) -> float | None:
    """
    Return next-day conditional vol (annualized) for ticker from saved model.
    Returns None if arch is not installed or model file is missing.
    Preserved as float-returning for backward compatibility.
    """
    path = _GARCH_DIR / f"{ticker}.joblib"
    if not path.exists():
        return None
    try:
        art = joblib.load(path)
        return _garch_forecast_from_artifact(art)
    except Exception:
        return None


def get_garch_features(ticker: str) -> dict | None:
    """
    Return rich GARCH feature dict for downstream ML models, or None.

    Keys: garch_conditional_vol, garch_long_run_vol, garch_persistence,
          garch_alpha, garch_beta
    """
    path = _GARCH_DIR / f"{ticker}.joblib"
    if not path.exists():
        return None
    try:
        art = joblib.load(path)
        vol = _garch_forecast_from_artifact(art)
        if vol is None:
            return None
        return {
            "garch_conditional_vol": vol,
            "garch_long_run_vol":    art.get("long_run_vol_ann"),
            "garch_persistence":     art.get("persistence"),
            "garch_alpha":           art.get("alpha"),
            "garch_beta":            art.get("beta"),
        }
    except Exception:
        return None


def train(tickers: list[str] | None = None, period: str = "2y") -> dict:
    """Fit and save GARCH / EGARCH for each ticker. Applies quality gates before saving."""
    if tickers is None:
        from scripts.regime_backfill import backfill_tickers
        tickers = backfill_tickers()

    _GARCH_DIR.mkdir(parents=True, exist_ok=True)

    results, errors, skipped = [], [], []
    for ticker in tickers:
        art = fit_garch(ticker, period=period)
        if art is None:
            errors.append(ticker)
            continue

        # ── Quality gates — bad models are worse than no model ────────────────
        if art["n_obs"] < _MIN_OBS:
            reason = f"n_obs={art['n_obs']}<{_MIN_OBS}"
            log.info("[GARCH] %s skipped: %s", ticker, reason)
            skipped.append({"ticker": ticker, "reason": reason})
            continue
        if not art["converged"]:
            reason = "optimizer_did_not_converge"
            log.info("[GARCH] %s skipped: %s", ticker, reason)
            skipped.append({"ticker": ticker, "reason": reason})
            continue
        if not art["stationary"]:
            reason = f"persistence={art['persistence']:.4f}>={_PERSISTENCE_MAX} (IGARCH)"
            log.info("[GARCH] %s skipped: %s", ticker, reason)
            skipped.append({"ticker": ticker, "reason": reason})
            continue

        out = _GARCH_DIR / f"{ticker}.joblib"
        joblib.dump(art, out)
        results.append({
            "ticker":       ticker,
            "model_type":   art["model_type"],
            "aic":          art["aic"],
            "n_obs":        art["n_obs"],
            "cond_vol_ann": round(art["cond_vol_ann"], 4),
            "long_run_vol": round(art["long_run_vol_ann"], 4) if art["long_run_vol_ann"] else None,
            "omega":        round(art["omega"], 6),
            "alpha":        round(art["alpha"], 4),
            "beta":         round(art["beta"],  4),
            "persistence":  art["persistence"],
            "converged":    art["converged"],
            "stationary":   art["stationary"],
        })

    return {
        "ok":      True,
        "fitted":  len(results),
        "errors":  errors,
        "skipped": skipped,
        "results": results,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", help="Tickers to fit (default: all watchlist)")
    parser.add_argument("--period", default="2y", help="yfinance period string (default: 2y)")
    args = parser.parse_args()

    out = train(tickers=args.tickers, period=args.period)
    if not out["ok"]:
        print("Failed")
        sys.exit(1)

    print(f"\nFitted for {out['fitted']} tickers  |  "
          f"errors: {len(out['errors'])}  |  skipped (quality): {len(out['skipped'])}")
    if out["errors"]:
        print(f"  Failed:  {out['errors']}")
    if out["skipped"]:
        for s in out["skipped"]:
            print(f"  Skipped: {s['ticker']} — {s['reason']}")

    hdr = (f"\n{'Ticker':<8} {'Type':>6} {'AIC':>10} {'N':>6} "
           f"{'CondVol':>9} {'LongRunVol':>11} {'alpha':>7} {'beta':>7} {'Persist':>9}")
    print(hdr)
    print("-" * 80)
    for r in sorted(out["results"], key=lambda x: x["ticker"]):
        lr = f"{r['long_run_vol']:.4f}" if r["long_run_vol"] else "    n/a"
        print(f"{r['ticker']:<8} {r['model_type']:>6} {r['aic']:>10.1f} {r['n_obs']:>6} "
              f"{r['cond_vol_ann']:>9.4f} {lr:>11} "
              f"{r['alpha']:>7.4f} {r['beta']:>7.4f} {r['persistence']:>9.4f}")

    print(f"\nModels saved to {_GARCH_DIR}")
