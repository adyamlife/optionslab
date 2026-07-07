"""
Volatility surface fitting via SVI (Stochastic Volatility Inspired, Gatheral 2004).

SVI total-variance parametrization per expiry slice:
    w(k) = a + b * (rho*(k - m) + sqrt((k - m)^2 + sigma^2))
    k    = log(K / F)   — log-moneyness (we use spot as forward proxy)
    T    = DTE / 365
    IV   = sqrt(w / T)

Mispricing = Market_IV - Model_IV
  positive → option is expensive vs the fitted surface (sell candidate)
  negative → option is cheap vs the fitted surface (buy candidate)
"""

from __future__ import annotations

import logging
import math
import warnings
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import minimize

log = logging.getLogger(__name__)

# ── SVI kernel ────────────────────────────────────────────────────────────────

def _svi_w(k: np.ndarray, a: float, b: float, rho: float, m: float, sigma: float) -> np.ndarray:
    """Raw SVI total variance at log-moneyness array k."""
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + sigma ** 2))


def _svi_iv(k: np.ndarray, T: float, a, b, rho, m, sigma) -> np.ndarray:
    """Convert SVI params to annualised IV."""
    w = _svi_w(k, a, b, rho, m, sigma)
    w = np.maximum(w, 1e-10)
    return np.sqrt(w / T)


# ── Single-slice fit ──────────────────────────────────────────────────────────

@dataclass
class SliceFit:
    expiry: str
    dte: int
    T: float
    params: dict          # a, b, rho, m, sigma
    rmse: float           # IV RMSE of fit
    n_points: int
    strikes: pd.DataFrame  # full per-strike result table


def fit_svi_slice(
    strikes_df: pd.DataFrame,
    spot: float,
    expiry: str,
    dte: int,
    min_strikes: int = 4,
    weight_col: Optional[str] = None,
) -> Optional[SliceFit]:
    """
    Fit SVI to one expiry slice.

    strikes_df must have columns: strike, iv (decimal).
    Optional weight_col (e.g. 'open_interest') for weighted least-squares.

    Returns None if there aren't enough usable strikes.
    """
    df = strikes_df.copy()
    df = df[df["iv"] > 0.01].dropna(subset=["strike", "iv"])

    if len(df) < min_strikes:
        return None

    T = max(dte / 365.0, 1 / 365.0)
    F = spot  # no dividend adjustment — good enough for short-dated options
    df["k"] = np.log(df["strike"] / F)
    df["w"]  = df["iv"] ** 2 * T

    k = df["k"].values
    w = df["w"].values
    weights = np.ones(len(df))
    if weight_col and weight_col in df.columns:
        oi = df[weight_col].fillna(0).values.astype(float)
        if oi.sum() > 0:
            weights = oi / oi.sum() * len(oi)

    # ── Objective ─────────────────────────────────────────────────────────────
    def objective(p):
        a, b, rho, m, sigma = p
        w_hat = _svi_w(k, a, b, rho, m, sigma)
        resid = (w_hat - w) * weights
        return np.sum(resid ** 2)

    # ── Initial guess ─────────────────────────────────────────────────────────
    atm_var  = np.interp(0.0, k[np.argsort(k)], w[np.argsort(k)])
    a0       = atm_var * 0.6
    b0       = 0.1
    rho0     = -0.3
    m0       = 0.0
    sigma0   = 0.2

    # ── Bounds ────────────────────────────────────────────────────────────────
    #   a > 0 (positive minimum variance)
    #   b >= 0 (wings must spread, not invert)
    #   |rho| < 1
    #   sigma > 0
    #   a + b*sigma*sqrt(1 - rho^2) >= 0  (no negative variance at wings)
    bounds = [
        (1e-6, atm_var * 2),   # a
        (1e-6, 2.0),            # b
        (-0.99, 0.99),          # rho
        (-0.5, 0.5),            # m
        (1e-4, 1.0),            # sigma
    ]

    best = None
    best_val = np.inf
    # Multiple starting points to avoid local minima
    for rho_init in [-0.5, -0.2, 0.0]:
        for b_init in [0.05, 0.15]:
            x0 = [a0, b_init, rho_init, m0, sigma0]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = minimize(objective, x0, method="L-BFGS-B", bounds=bounds,
                               options={"maxiter": 2000, "ftol": 1e-12})
            if res.success and res.fun < best_val:
                best_val = res.fun
                best = res

    if best is None:
        return None

    a, b, rho, m, sigma = best.x

    # Per-strike model IV and mispricing
    df["model_iv"]   = _svi_iv(k, T, a, b, rho, m, sigma)
    df["mispricing"] = df["iv"] - df["model_iv"]
    df["misprice_pct"] = df["mispricing"] / df["model_iv"].clip(lower=0.001) * 100

    rmse = float(np.sqrt(np.mean((df["iv"] - df["model_iv"]) ** 2)))

    return SliceFit(
        expiry=expiry,
        dte=dte,
        T=T,
        params=dict(a=a, b=b, rho=rho, m=m, sigma=sigma),
        rmse=rmse,
        n_points=len(df),
        strikes=df[["strike", "iv", "k", "model_iv", "mispricing", "misprice_pct"]].reset_index(drop=True),
    )


# ── Full surface from DuckDB chain snapshots ──────────────────────────────────

def compute_mispricing(
    ticker: str,
    source_filter: Optional[str] = None,
    min_dte: int = 1,
    max_dte: int = 90,
    opt_type: str = "both",
) -> dict:
    """
    Load the latest option chain snapshot for ticker, fit SVI per expiry,
    and return per-strike mispricing.

    opt_type: "call", "put", or "both" (default: both, averaged per strike)

    Returns a dict with:
      ticker, spot, as_of, slices: list[dict]
      Each slice: expiry, dte, rmse, params, strikes: list[dict]
    """
    from scripts.db import read_df, CHAIN_TABLE

    try:
        exists = read_df(
            f"SELECT count(*) AS n FROM information_schema.tables "
            f"WHERE table_name = '{CHAIN_TABLE}'"
        ).iloc[0]["n"] > 0
    except Exception:
        exists = False
    if not exists:
        return {"ok": False, "error": "option_chain_snapshots table not found"}

    # Most recent snapshot per ticker
    query = f"""
        SELECT snapshot_id, collected_at, ticker, spot, expiry, dte, source, strikes
        FROM {CHAIN_TABLE}
        WHERE ticker = '{ticker}'
          AND dte BETWEEN {min_dte} AND {max_dte}
          AND collected_at = (
              SELECT MAX(collected_at) FROM {CHAIN_TABLE}
              WHERE ticker = '{ticker}'
          )
        ORDER BY dte ASC
    """
    if source_filter:
        query = query.replace(f"ticker = '{ticker}'",
                              f"ticker = '{ticker}' AND source = '{source_filter}'")

    try:
        rows = read_df(query)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if rows.empty:
        return {"ok": False, "error": f"No chain snapshots found for {ticker}"}

    spot      = float(rows.iloc[0]["spot"])
    as_of     = rows.iloc[0]["collected_at"]
    source    = rows.iloc[0]["source"]
    slices    = []

    for _, row in rows.iterrows():
        expiry = row["expiry"]
        dte    = int(row["dte"])
        raw    = row["strikes"]

        # Parse JSON if still a string
        if isinstance(raw, str):
            import json
            raw = json.loads(raw)

        if not raw:
            continue

        strike_df = pd.DataFrame(raw)

        # Filter by opt_type
        if opt_type in ("call", "put"):
            strike_df = strike_df[strike_df["opt_type"] == opt_type]
        elif opt_type == "both":
            # Average IV across calls & puts for same strike (reduces bid-ask noise)
            strike_df = (
                strike_df.groupby("strike", as_index=False)
                .agg(iv=("iv", "mean"), open_interest=("open_interest", "sum"),
                     volume=("volume", "sum"))
            )

        fit = fit_svi_slice(
            strike_df,
            spot=spot,
            expiry=expiry,
            dte=dte,
            weight_col="open_interest",
        )
        if fit is None:
            continue

        slices.append({
            "expiry":   fit.expiry,
            "dte":      fit.dte,
            "rmse":     round(fit.rmse * 100, 3),   # in vol points (%)
            "n_points": fit.n_points,
            "params":   {k: round(v, 6) for k, v in fit.params.items()},
            "strikes":  _format_strikes(fit.strikes, spot),
        })

    if not slices:
        return {"ok": False, "error": f"Not enough strikes to fit SVI for {ticker}"}

    return {
        "ok":     True,
        "ticker": ticker,
        "spot":   spot,
        "as_of":  as_of,
        "source": source,
        "slices": slices,
    }


def _format_strikes(df: pd.DataFrame, spot: float) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "strike":        round(float(r["strike"]), 2),
            "moneyness":     round(float(r["strike"]) / spot, 4),
            "market_iv":     round(float(r["iv"]) * 100, 2),       # %
            "model_iv":      round(float(r["model_iv"]) * 100, 2),  # %
            "mispricing":    round(float(r["mispricing"]) * 100, 2), # vol points
            "misprice_pct":  round(float(r["misprice_pct"]), 1),    # % of model IV
        })
    return sorted(rows, key=lambda x: x["strike"])


# ── Quick summary: most mispriced strikes across all slices ───────────────────

def top_mispriced(result: dict, n: int = 5, min_abs_vp: float = 1.0) -> list[dict]:
    """
    Return top-n most mispriced strikes from a compute_mispricing() result.
    min_abs_vp: minimum |mispricing| in vol points to include.
    """
    if not result.get("ok"):
        return []

    rows = []
    for sl in result["slices"]:
        for s in sl["strikes"]:
            if abs(s["mispricing"]) >= min_abs_vp:
                rows.append({**s, "expiry": sl["expiry"], "dte": sl["dte"],
                             "fit_rmse": sl["rmse"]})

    rows.sort(key=lambda x: abs(x["mispricing"]), reverse=True)
    return rows[:n]
