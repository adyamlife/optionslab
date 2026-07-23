"""
trade_filters.py — Three validation layers applied in analyze_ticker().

  #14  validate_price_data(hist)          data quality before indicator calcs
  #14  validate_chain(calls, puts, spot)  chain quality before analysis
  #8   leg_liquid(row)                    per-leg liquidity gate
  #15  check_risk_gates(candidate, ...)   hard post-optimization rejection rules

All thresholds live in scoring.toml [gates] so they can be tuned without code changes.
"""
from __future__ import annotations

import math

import pandas as pd

import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config import scoring as sc


# ── #14 — Data quality ────────────────────────────────────────────────────────

def validate_price_data(hist: pd.DataFrame) -> dict:
    """
    Sanity-check raw OHLCV history before any indicator is computed.

    Returns {"ok": bool, "issues": [str]}.
    "ok" is False only for data so broken that indicators would be meaningless.
    Soft warnings (e.g. a few NaN rows) still return ok=True with populated issues[].
    """
    issues: list[str] = []

    if hist.empty:
        return {"ok": False, "issues": ["price history is empty"]}

    close = hist["Close"].dropna()

    # Not enough bars for the shortest indicator (RSI needs 14, MACD needs 26)
    if len(close) < 30:
        issues.append(f"only {len(close)} bars — indicators need ≥ 30")
        return {"ok": False, "issues": issues}

    # Zero or negative close prices
    if (close <= 0).any():
        n = int((close <= 0).sum())
        issues.append(f"{n} zero/negative close price(s) — possible data error or delisted")

    # NaN rows in OHLCV (beyond the close series we already checked)
    nan_rows = hist[["Open", "High", "Low", "Close", "Volume"]].isna().any(axis=1).sum()
    if nan_rows > len(hist) * 0.10:
        issues.append(f"{nan_rows}/{len(hist)} OHLCV rows contain NaN (>{10}%) — data may be incomplete")

    # Zero-volume sessions (excluding genuine halts — flag if > 20% of rows)
    if "Volume" in hist.columns:
        zero_vol = int((hist["Volume"].fillna(0) == 0).sum())
        if zero_vol > len(hist) * 0.20:
            issues.append(f"{zero_vol}/{len(hist)} zero-volume sessions — possible stale or non-trading data")

    # High < Low sanity check
    if "High" in hist.columns and "Low" in hist.columns:
        inverted = int((hist["High"] < hist["Low"]).sum())
        if inverted:
            issues.append(f"{inverted} bar(s) where High < Low — OHLC data may be mis-labeled")

    return {"ok": True, "issues": issues}


def validate_chain(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> dict:
    """
    Sanity-check the option chain before scoring begins.

    Returns {"ok": bool, "issues": [str]}.
    ok=False means the chain is too thin/stale to produce reliable candidates.
    """
    issues: list[str] = []

    if calls.empty or puts.empty:
        return {"ok": False, "issues": ["option chain is empty"]}

    # Stale-quote detection: all bids and asks are 0
    call_has_market = ((calls.get("bid", 0) > 0) | (calls.get("ask", 0) > 0)).any()
    put_has_market  = ((puts.get("bid", 0) > 0)  | (puts.get("ask", 0) > 0)).any()
    if not call_has_market:
        issues.append("all call bids/asks are 0 — stale or after-hours chain")
    if not put_has_market:
        issues.append("all put bids/asks are 0 — stale or after-hours chain")
    if not call_has_market or not put_has_market:
        return {"ok": False, "issues": issues}

    # IV availability — zero IV means BS/binomial pricing will be unreliable
    iv_col = "impliedVolatility"
    if iv_col in calls.columns:
        zero_iv_calls = int((calls[iv_col].fillna(0) == 0).sum())
        if zero_iv_calls == len(calls):
            issues.append("all call IVs are 0 — greeks and EV estimates will be unreliable")
    if iv_col in puts.columns:
        zero_iv_puts = int((puts[iv_col].fillna(0) == 0).sum())
        if zero_iv_puts == len(puts):
            issues.append("all put IVs are 0 — greeks and EV estimates will be unreliable")

    # Minimum number of usable strikes (need at least 3 near spot for delta searches)
    if spot > 0:
        near_calls = calls[(calls["strike"] >= spot * 0.90) & (calls["strike"] <= spot * 1.10)]
        near_puts  = puts[(puts["strike"]  >= spot * 0.90) & (puts["strike"]  <= spot * 1.10)]
        if len(near_calls) < 3:
            issues.append(f"only {len(near_calls)} call strike(s) within ±10% of spot — delta targeting may fail")
        if len(near_puts) < 3:
            issues.append(f"only {len(near_puts)} put strike(s) within ±10% of spot — delta targeting may fail")

    return {"ok": True, "issues": issues}


# ── #8 — Per-leg liquidity gate ───────────────────────────────────────────────

def leg_liquid(row) -> tuple[bool, list[str]]:
    """
    Check whether a single option leg meets all liquidity requirements.

    Replaces the inline `_ba_ok` closure in analyze.py and the ad-hoc
    `put_leg_liquid` / `call_leg_liquid` checks that only tested bid > 0.

    Returns (passed: bool, issues: list[str]).
    Issues list is populated when passed=False so callers can log the reason.
    """
    issues: list[str] = []

    try:
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        oi  = int(row.get("openInterest") or 0)
        vol = int(row.get("volume") or 0)
    except (TypeError, ValueError):
        issues.append("could not parse bid/ask/OI/volume — treating as illiquid")
        return False, issues

    mid = (bid + ask) / 2

    # 1. Both sides of the market must exist
    if bid <= 0 or ask <= 0:
        issues.append(f"no market (bid={bid:.2f}, ask={ask:.2f})")
        return False, issues

    # 2. Bid-ask percentage spread
    ba_pct_gate = sc.gate("bid_ask_max_pct") or 0.30
    if mid > 0 and (ask - bid) / mid > ba_pct_gate:
        spread_pct = (ask - bid) / mid * 100
        issues.append(f"spread {spread_pct:.0f}% > {ba_pct_gate * 100:.0f}% limit")
        return False, issues

    # 3. Absolute dollar spread (catches wide markets on cheap OTM options)
    max_spread_d = sc.gate("max_spread_dollars") or 5.00
    if (ask - bid) > max_spread_d:
        issues.append(f"spread ${ask - bid:.2f} > ${max_spread_d:.2f} limit")
        return False, issues

    # 4. Open interest
    min_oi = sc.gate("min_open_interest") or 50
    if oi < min_oi:
        issues.append(f"OI {oi} < {min_oi} minimum")
        return False, issues

    # 5. Daily volume
    min_vol = sc.gate("min_leg_volume") or 10
    if vol < min_vol:
        issues.append(f"volume {vol} < {min_vol} minimum")
        return False, issues

    return True, []


# ── #15 — Post-optimization hard risk gates ───────────────────────────────────

def check_risk_gates(
    candidate: dict,
    spot: float,
    atm_iv: float,
    dte: int,
    div_in_window: bool = False,
) -> tuple[bool, list[str]]:
    """
    Hard rejection rules applied after strike/EV optimization.

    These catch trade-level risks that per-leg liquidity checks miss:

    Gate 1 — Expected move vs spread width
        1-std-dev expected move = atm_iv × spot × √(dte/365).
        If this exceeds the spread width the short strike is statistically
        likely to be breached by expiry. Gate threshold in scoring.toml:
        expected_move_max_ratio (default 1.0 = reject when EM > width).

    Gate 2 — Ex-dividend inside assignment window
        Short put structures face early-assignment risk when the stock
        goes ex-dividend before expiry. div_in_window is already computed
        in analyze_ticker; this gate converts the flag into a hard rejection
        rather than just a penalty.

    Returns (rejected: bool, issues: list[str]).
    """
    issues: list[str] = []
    max_profit = candidate.get("max_profit")
    max_loss   = candidate.get("max_loss")

    # Gate 1 — expected move
    if (
        max_profit is not None and max_loss is not None
        and atm_iv and atm_iv > 0
        and dte and dte > 0
        and spot and spot > 0
    ):
        width         = float(max_profit) + float(max_loss)
        expected_move = atm_iv * spot * math.sqrt(dte / 365.0)
        ratio         = sc.gate("expected_move_max_ratio") or 1.0
        if width > 0 and expected_move > width * ratio:
            issues.append(
                f"Expected move ${expected_move:.2f} > spread width ${width:.2f} "
                f"— short strike is within the 1-std-dev range at expiry"
            )

    # Gate 2 — ex-dividend assignment risk
    is_short_put_structure = (
        candidate.get("is_credit") is True
        and candidate.get("short_strike") is not None
        and candidate.get("structure", "").lower() not in ("call credit spread", "covered call")
    )
    if div_in_window and is_short_put_structure:
        issues.append(
            "Ex-dividend falls within the trade window — short put carries "
            "early-assignment risk before ex-date"
        )

    return len(issues) > 0, issues
