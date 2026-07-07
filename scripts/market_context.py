"""
scripts/market_context.py
Fetch index futures and sector ETF data for the Market Context panel.

All data comes from yfinance in two fast batch calls:
  1. yf.download() for all tickers in one HTTP request (sectors + VIX)
  2. yf.Ticker(future).fast_info for each index future (< 1s each, 4 total)

Results are cached in-process for `cache_seconds` (default 300).
"""

from __future__ import annotations

import time
import logging
from pathlib import Path
from typing import Any

from config import scoring as sc

log = logging.getLogger(__name__)

# ── Sector → ETF mapping ──────────────────────────────────────────────────────
# yfinance sector string (from Ticker.info["sector"]) → SPDR ETF ticker
SECTOR_TO_ETF: dict[str, str] = {
    "Technology":              "XLK",
    "Financial Services":      "XLF",
    "Energy":                  "XLE",
    "Healthcare":              "XLV",
    "Utilities":               "XLU",
    "Industrials":             "XLI",
    "Consumer Cyclical":       "XLY",
    "Consumer Defensive":      "XLP",
    "Basic Materials":         "XLB",
    "Real Estate":             "XLRE",
    "Communication Services":  "XLC",
    # Aliases yfinance sometimes returns
    "Financial":               "XLF",
    "Consumer Discretionary":  "XLY",
    "Consumer Staples":        "XLP",
    "Materials":               "XLB",
    "Health Care":             "XLV",
}

# ETF display label
ETF_LABEL: dict[str, str] = {
    "XLK":  "Tech",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Health",
    "XLU":  "Utilities",
    "XLI":  "Industrial",
    "XLY":  "Cons. Disc",
    "XLP":  "Cons. Staples",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLC":  "Comm.",
}

# ── Module-level caches ───────────────────────────────────────────────────────
_ctx_cache:       dict[str, Any] | None = None
_ctx_cached_at:   float = 0.0

# ticker → sector ETF string, persists for the process lifetime
_ticker_sector_cache: dict[str, str | None] = {}


# ── Main function ─────────────────────────────────────────────────────────────

def get_market_context(
    future_tickers: list[str] | None = None,
    future_labels:  list[str] | None = None,
    sector_tickers: list[str] | None = None,
    vix_ticker:     str             = "^VIX",
    cache_seconds:  int             = 300,
) -> dict[str, Any]:
    """
    Return a dict with:
      futures  – list of {ticker, label, price, change_pct}
      sectors  – list of {ticker, label, change_pct}
      vix      – {price, change_pct} or None
      fetched_at – unix timestamp
    """
    global _ctx_cache, _ctx_cached_at

    if _ctx_cache is not None and time.time() - _ctx_cached_at < cache_seconds:
        return _ctx_cache

    import yfinance as yf

    f_tickers = future_tickers or ["ES=F", "NQ=F", "YM=F", "RTY=F"]
    f_labels  = future_labels  or ["S&P 500", "Nasdaq 100", "Dow Jones", "Russell 2000"]
    s_tickers = sector_tickers or list(ETF_LABEL.keys())

    result: dict[str, Any] = {
        "futures":    [],
        "sectors":    [],
        "vix":        None,
        "risk_regime": None,
        "fetched_at": time.time(),
    }

    _RISK_TICKERS = ["HYG", "LQD", "TLT"]
    all_quote_tickers = s_tickers + ([vix_ticker] if vix_ticker else []) + _RISK_TICKERS + f_tickers

    # ── Step 1: try E*TRADE batch quotes (real-time) if configured ───────────
    et_quotes: dict[str, dict] = {}
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from scripts import etrade_client as et
        pref = et.ds_pref("market_context")
        use_et = (pref == "etrade") or (pref == "auto" and et.is_authenticated())
        if use_et:
            q = et.get_quotes(all_quote_tickers)
            if q:
                et_quotes = q
    except Exception as e:
        log.debug(f"market_context: E*TRADE batch quotes failed: {e}")

    # ── Step 2: yfinance fallback for anything E*TRADE didn't cover ───────────
    yf_needed = [t for t in (s_tickers + ([vix_ticker] if vix_ticker else []) + _RISK_TICKERS)
                 if t not in et_quotes or et_quotes[t].get("last") is None]
    close = None
    if yf_needed:
        try:
            raw   = yf.download(yf_needed, period="5d", interval="1d",
                                auto_adjust=True, progress=False)
            close = raw["Close"]
        except Exception as e:
            log.warning(f"market_context: yfinance batch fallback failed: {e}")

    def _change_pct(ticker):
        q = et_quotes.get(ticker)
        if q and q.get("change_pct") is not None:
            return q["change_pct"]
        return _day_change_pct(close, ticker) if close is not None else None

    def _price(ticker):
        q = et_quotes.get(ticker)
        if q and q.get("last"):
            return q["last"]
        return _latest_price(close, ticker) if close is not None else None

    # ── Sector ETFs ───────────────────────────────────────────────────────────
    for etf in s_tickers:
        result["sectors"].append({
            "ticker":     etf,
            "label":      ETF_LABEL.get(etf, etf),
            "change_pct": _change_pct(etf),
            "source":     "etrade" if etf in et_quotes else "yfinance",
        })

    # ── VIX ───────────────────────────────────────────────────────────────────
    if vix_ticker:
        vix_price = _price(vix_ticker)
        if vix_price is not None:
            result["vix"] = {
                "price":      round(vix_price, 2),
                "change_pct": _change_pct(vix_ticker),
                "source":     "etrade" if vix_ticker in et_quotes else "yfinance",
            }

    # ── Risk-on / risk-off (HYG / LQD / TLT) ─────────────────────────────────
    hyg_pct = _change_pct("HYG")
    lqd_pct = _change_pct("LQD")
    tlt_pct = _change_pct("TLT")
    result["risk_regime"] = _compute_risk_regime(hyg_pct, lqd_pct, tlt_pct)

    # ── Index futures: E*TRADE first, fast_info fallback ─────────────────────
    for ticker, label in zip(f_tickers, f_labels):
        q = et_quotes.get(ticker)
        if q and q.get("last"):
            result["futures"].append({
                "ticker":     ticker,
                "label":      label,
                "price":      round(float(q["last"]), 2),
                "change_pct": q.get("change_pct"),
                "source":     "etrade",
            })
        else:
            try:
                fi    = yf.Ticker(ticker).fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
                prev  = getattr(fi, "previous_close", None)
                pct   = round((price - prev) / prev * 100, 2) if price and prev and prev != 0 else None
                result["futures"].append({
                    "ticker":     ticker,
                    "label":      label,
                    "price":      round(float(price), 2) if price else None,
                    "change_pct": pct,
                    "source":     "yfinance",
                })
            except Exception as e:
                log.debug(f"market_context: future {ticker} yfinance fallback failed: {e}")
                result["futures"].append({"ticker": ticker, "label": label,
                                          "price": None, "change_pct": None, "source": "error"})

    _ctx_cache    = result
    _ctx_cached_at = time.time()
    return result


# ── Per-ticker sector tag ─────────────────────────────────────────────────────

def get_sector_tag(ticker: str, ctx: dict[str, Any]) -> str | None:
    """
    Return a short tag string like "XLK −1.2% ↓" for the ticker's sector,
    or None if the sector is unknown or context is missing.
    Uses a process-lifetime cache so each ticker is only looked up once.
    """
    if not ctx:
        return None

    etf = _resolve_sector_etf(ticker)
    if not etf:
        return None

    sector_data = next((s for s in ctx.get("sectors", []) if s["ticker"] == etf), None)
    if not sector_data:
        return None

    pct = sector_data.get("change_pct")
    if pct is None:
        return None

    arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
    sign  = "+" if pct > 0 else ""
    return f"{etf} {sign}{pct:.1f}% {arrow}"


def _resolve_sector_etf(ticker: str) -> str | None:
    """Look up which sector ETF maps to this ticker. Cached per process."""
    if ticker in _ticker_sector_cache:
        return _ticker_sector_cache[ticker]

    etf = None
    try:
        import yfinance as yf
        sector = yf.Ticker(ticker).info.get("sector", "")
        etf    = SECTOR_TO_ETF.get(sector)
    except Exception:
        pass

    _ticker_sector_cache[ticker] = etf
    return etf


# ── Helpers ───────────────────────────────────────────────────────────────────

def _latest_price(close, ticker: str) -> float | None:
    try:
        col = close[ticker]
        val = col.dropna().iloc[-1]
        return float(val)
    except Exception:
        return None


def _day_change_pct(close, ticker: str) -> float | None:
    try:
        col = close[ticker].dropna()
        if len(col) < 2:
            return None
        prev, last = float(col.iloc[-2]), float(col.iloc[-1])
        if prev == 0:
            return None
        return round((last - prev) / prev * 100, 2)
    except Exception:
        return None


# ── Market-bias scoring ───────────────────────────────────────────────────────

# Which structures are directionally bullish / bearish / neutral
_BULLISH_STRUCTURES = {"Call Credit Spread", "Put Debit Spread",
                       "Call Debit Spread", "Jade Lizard"}
_BEARISH_STRUCTURES = {"Put Credit Spread", "Call Debit Spread"}
_NEUTRAL_STRUCTURES = {"Iron Condor", "Calendar Spread", "Diagonal Spread"}
# Note: "Call Debit Spread" appears in both because context matters;
# a bearish market might still suit a CDS on a diverging stock.
# We resolve ambiguity by net-delta sign when available.

_CREDIT_STRUCTURES  = {"Put Credit Spread", "Call Credit Spread",
                       "Iron Condor", "Jade Lizard"}
_DEBIT_STRUCTURES   = {"Put Debit Spread", "Call Debit Spread",
                       "Calendar Spread", "Diagonal Spread"}


def compute_market_bias(
    candidate: dict[str, Any],
    ticker:    str,
    ctx:       dict[str, Any],
    regime:    str = "chop",
) -> dict[str, Any]:
    """
    Score how well a trade candidate aligns with current market conditions.

    Returns:
      {
        "score":  float,          # −3 to +3 (added to signal_score × 0.5)
        "label":  str,            # "Confirmed" | "Opposed" | "Mixed" | "Neutral"
        "notes":  list[str],      # human-readable reasons
        "vix_regime": str,        # "High" | "Low" | "Normal"
        "futures_bias": str,      # "Bullish" | "Bearish" | "Neutral"
        "sector_bias":  str,      # "Aligned" | "Opposed" | "Neutral" | "Unknown"
      }
    """
    if not ctx:
        return _neutral_bias()

    structure  = candidate.get("structure", "")
    net_delta  = candidate.get("net_delta")

    if net_delta is not None:
        struct_dir = "bullish" if net_delta > 0.05 else "bearish" if net_delta < -0.05 else "neutral"
    elif structure in _BULLISH_STRUCTURES:
        struct_dir = "bullish"
    elif structure in _BEARISH_STRUCTURES:
        struct_dir = "bearish"
    else:
        struct_dir = "neutral"

    is_credit = structure in _CREDIT_STRUCTURES
    is_debit  = structure in _DEBIT_STRUCTURES

    score = 0.0
    notes: list[str] = []

    def w(sub: str) -> float:
        return sc.get_sub_weight("market_ctx", sub, regime)

    # ── 1. VIX regime ─────────────────────────────────────────────────────────
    vix        = ctx.get("vix") or {}
    vix_price  = vix.get("price")
    vix_regime = "Normal"
    wt_vix     = w("vix_regime")

    if vix_price is not None:
        if vix_price > 25:
            vix_regime = "High"
            if is_credit:
                score += wt_vix
                notes.append(f"VIX {vix_price:.1f} (fear) — high premium favors selling")
            elif is_debit:
                score -= wt_vix
                notes.append(f"VIX {vix_price:.1f} (fear) — expensive premium for buyers")
        elif vix_price < 18:
            vix_regime = "Low"
            if is_debit:
                score += wt_vix
                notes.append(f"VIX {vix_price:.1f} (calm) — cheap options favor buying")
            elif is_credit:
                score -= wt_vix
                notes.append(f"VIX {vix_price:.1f} (calm) — low premium reduces credit income")

    # ── 2. Futures consensus direction ────────────────────────────────────────
    futures      = ctx.get("futures", [])
    valid_pcts   = [f["change_pct"] for f in futures if f.get("change_pct") is not None]
    futures_bias = "Neutral"
    wt_fut       = w("futures_dir")

    if valid_pcts:
        avg_pct = sum(valid_pcts) / len(valid_pcts)
        if avg_pct > 0.3:
            futures_bias = "Bullish"
            if struct_dir == "bullish":
                score += wt_fut
                notes.append(f"Futures up avg {avg_pct:+.2f}% — confirms bullish structure")
            elif struct_dir == "bearish":
                score -= wt_fut
                notes.append(f"Futures up avg {avg_pct:+.2f}% — opposes bearish structure")
        elif avg_pct < -0.3:
            futures_bias = "Bearish"
            if struct_dir == "bearish":
                score += wt_fut
                notes.append(f"Futures down avg {avg_pct:+.2f}% — confirms bearish structure")
            elif struct_dir == "bullish":
                score -= wt_fut
                notes.append(f"Futures down avg {avg_pct:+.2f}% — opposes bullish structure")

    # ── 3. Sector ETF alignment ───────────────────────────────────────────────
    sector_bias = "Unknown"
    etf         = _resolve_sector_etf(ticker)
    wt_sector   = w("sector_etf")

    if etf:
        sector_data = next((s for s in ctx.get("sectors", []) if s["ticker"] == etf), None)
        if sector_data and sector_data.get("change_pct") is not None:
            spct       = sector_data["change_pct"]
            sector_dir = "bullish" if spct > 0.5 else "bearish" if spct < -0.5 else "neutral"
            if sector_dir == "neutral":
                sector_bias = "Neutral"
            elif sector_dir == struct_dir:
                sector_bias = "Aligned"
                score += wt_sector
                notes.append(f"{etf} {spct:+.2f}% — sector trend confirms structure")
            elif struct_dir != "neutral":
                sector_bias = "Opposed"
                score -= wt_sector
                notes.append(f"{etf} {spct:+.2f}% — sector trend opposes structure")
            else:
                sector_bias = "Neutral"

    # ── 4. Risk-on / Risk-off (HYG/LQD credit spread + TLT bond signal) ─────────
    risk_regime = ctx.get("risk_regime") or {}
    risk_label  = risk_regime.get("label", "Neutral")
    risk_score  = risk_regime.get("score", 0.0)
    wt_risk     = w("risk_regime")   # uses scoring.toml market_ctx sub-weight

    if risk_label == "Risk-On":
        if struct_dir == "bullish":
            score += wt_risk
            notes.append(f"Risk-on (HYG/TLT) — credit/bond signals confirm bullish structure")
        elif struct_dir == "bearish":
            score -= wt_risk
            notes.append(f"Risk-on (HYG/TLT) — credit/bond signals oppose bearish structure")
        elif structure in ("Iron Condor",):
            score -= wt_risk * 0.5
            notes.append(f"Risk-on environment — elevated directionality adds breakout risk to neutral trade")
    elif risk_label == "Risk-Off":
        if struct_dir == "bearish" or is_credit:
            score += wt_risk
            notes.append(f"Risk-off (HYG/TLT) — flight-to-safety confirms defensive/credit-selling stance")
        elif struct_dir == "bullish" and is_debit:
            score -= wt_risk
            notes.append(f"Risk-off (HYG/TLT) — flight-to-safety opposes bullish debit trade")

    # ── Label (scaled to the budget, not fixed ±2 thresholds) ────────────────
    budget = sc.get_factor_budget("market_ctx", regime)

    if score >= budget * 0.65:
        label = "Confirmed"
    elif score <= -(budget * 0.65):
        label = "Opposed"
    elif score > 0:
        label = "Favorable"
    elif score < 0:
        label = "Caution"
    else:
        label = "Neutral"

    return {
        "score":        round(score, 3),
        "label":        label,
        "notes":        notes,
        "vix_regime":   vix_regime,
        "futures_bias": futures_bias,
        "sector_bias":  sector_bias,
        "risk_regime":  risk_label,
        "regime":       regime,
    }


def _compute_risk_regime(hyg_pct, lqd_pct, tlt_pct) -> dict[str, Any]:
    """Classify market risk appetite from credit-spread and bond-equity signals.

    Returns:
      label        – "Risk-On" | "Risk-Off" | "Neutral"
      score        – float in [-1, +1]; positive = risk-on
      notes        – list[str] of contributing signals
      hyg_pct, lqd_pct, tlt_pct – raw day-change % for display
    """
    score = 0.0
    notes: list[str] = []

    # HYG vs LQD: credit-spread proxy
    # If HYG outperforms LQD → credit spreads tightening → risk-on
    if hyg_pct is not None and lqd_pct is not None:
        spread_move = hyg_pct - lqd_pct
        if spread_move > 0.3:
            score += 0.5
            notes.append(f"HYG {hyg_pct:+.2f}% vs LQD {lqd_pct:+.2f}% — credit spreads tightening (risk-on)")
        elif spread_move < -0.3:
            score -= 0.5
            notes.append(f"HYG {hyg_pct:+.2f}% vs LQD {lqd_pct:+.2f}% — credit spreads widening (risk-off)")

    # TLT: flight-to-safety signal
    # TLT rising means money flowing into long Treasuries → risk-off
    if tlt_pct is not None:
        if tlt_pct > 0.5:
            score -= 0.5
            notes.append(f"TLT {tlt_pct:+.2f}% — bond rally signals flight to safety (risk-off)")
        elif tlt_pct < -0.5:
            score += 0.5
            notes.append(f"TLT {tlt_pct:+.2f}% — bond selloff supports equities (risk-on)")

    # Clamp to [-1, +1]
    score = max(-1.0, min(1.0, round(score, 3)))

    if score >= 0.4:
        label = "Risk-On"
    elif score <= -0.4:
        label = "Risk-Off"
    else:
        label = "Neutral"

    return {
        "label":    label,
        "score":    score,
        "notes":    notes,
        "hyg_pct":  hyg_pct,
        "lqd_pct":  lqd_pct,
        "tlt_pct":  tlt_pct,
    }


def _neutral_bias() -> dict[str, Any]:
    return {"score": 0, "label": "Neutral", "notes": [],
            "vix_regime": "Normal", "futures_bias": "Neutral", "sector_bias": "Unknown"}
