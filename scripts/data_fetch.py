import json
import time
import threading
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm
import yfinance as yf

# Cap concurrent yfinance HTTP calls to avoid 429/rate-limit responses when
# 10 scanner threads all hit Yahoo Finance simultaneously.
_YF_CONCURRENCY = threading.Semaphore(5)

_EARNINGS_CACHE_PATH = Path(__file__).parent.parent / "data" / "earnings_cache.json"
_OI_CACHE_PATH       = Path(__file__).parent.parent / "data" / "oi_cache.json"
_SECRETS_PATH        = Path(__file__).parent.parent / "config" / "secrets.toml"

# ── FRED API ──────────────────────────────────────────────────────────────────

def _fred_api_key() -> str | None:
    """Read FRED API key from config/secrets.toml [api_keys] fred_api_key."""
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        if not _SECRETS_PATH.exists():
            return None
        with open(_SECRETS_PATH, "rb") as f:
            return tomllib.load(f).get("api_keys", {}).get("fred_api_key")
    except Exception:
        return None


def get_fred_yields() -> dict:
    """
    Fetch daily Treasury yields from FRED (St. Louis Fed).

    Series used:
      DGS10   — 10-year constant maturity Treasury yield (daily, %)
      DGS3MO  — 3-month constant maturity Treasury yield (daily, %)

    Returns {yield_10y, yield_3m, yield_curve} or all-None on any failure.
    Falls back gracefully when API key is absent — caller uses yfinance instead.
    """
    result = {"yield_10y": None, "yield_3m": None, "yield_curve": None}
    key = _fred_api_key()
    if not key:
        return result
    try:
        import urllib.request
        base = "https://api.stlouisfed.org/fred/series/observations"

        def _fetch(series_id: str) -> float | None:
            url = (f"{base}?series_id={series_id}&api_key={key}"
                   f"&file_type=json&sort_order=desc&limit=5")
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            for obs in data.get("observations", []):
                val = obs.get("value", ".")
                if val != ".":
                    return round(float(val), 3)
            return None

        tnx = _fetch("DGS10")
        irx = _fetch("DGS3MO")
        result["yield_10y"] = tnx
        result["yield_3m"]  = irx
        if tnx is not None and irx is not None:
            result["yield_curve"] = round(tnx - irx, 3)
    except Exception:
        pass
    return result

# ── Per-thread data-source override ──────────────────────────────────────────
# Set force_yfinance=True on a thread to make _use_etrade() always return False
# for that thread (used to restrict eTrade access to admin role).
_tls = threading.local()

def set_force_yfinance(val: bool) -> None:
    _tls.force_yfinance = val

# ── Live risk-free rate (^IRX = 13-week T-bill annualised yield) ──────────────
_rfr_cache: float | None = None
_rfr_cached_at: float = 0.0
_RFR_TTL = 3600.0   # re-fetch at most once per hour
from config.rules import RISK_FREE_RATE as _RFR_FALLBACK


def get_risk_free_rate() -> float:
    """Return the current annualised risk-free rate from the 13-week T-bill (^IRX).
    ^IRX is quoted as a percent (e.g. 5.25), so divide by 100.
    Cached for 1 hour; falls back to 5 % if fetch fails."""
    global _rfr_cache, _rfr_cached_at
    if _rfr_cache is not None and time.time() - _rfr_cached_at < _RFR_TTL:
        return _rfr_cache
    try:
        irx = yf.Ticker("^IRX").fast_info
        price = getattr(irx, "last_price", None) or getattr(irx, "previous_close", None)
        if price and float(price) > 0:
            _rfr_cache = round(float(price) / 100, 5)
            _rfr_cached_at = time.time()
            return _rfr_cache
    except Exception:
        pass
    return _RFR_FALLBACK


def get_price_history(ticker, period="1y"):
    return yf.Ticker(ticker).history(period=period)


def get_trend(hist, sma_short=20, sma_long=50, band_pct=0.005):
    close = hist["Close"]
    price = close.iloc[-1]
    sma_s = close.rolling(sma_short).mean().iloc[-1]
    sma_l = close.rolling(sma_long).mean().iloc[-1]

    if price > sma_s * (1 + band_pct) and sma_s > sma_l:
        return "Uptrend"
    if price < sma_s * (1 - band_pct) and sma_s < sma_l:
        return "Downtrend"
    return "Range-bound"


def _get_expirations(ticker_obj) -> list[str]:
    """Return available option expirations — E*TRADE or yfinance per config.

    When E*TRADE is active, intersect with yfinance dates before returning.
    E*TRADE sometimes includes phantom/adjusted dates that its own chain
    endpoint rejects with 400 — filtering to dates both sources agree on
    prevents pick_expiry from selecting a date that neither chain can serve.
    """
    ticker_str = getattr(ticker_obj, "ticker", None)
    with _YF_CONCURRENCY:
        yf_exps = list(ticker_obj.options)      # always fetch; cheap + cached
    if ticker_str and _use_etrade("expirations"):
        try:
            et = _et_module()
            exps = et.get_option_expirations(ticker_str)
            if exps and yf_exps:
                yf_set = set(yf_exps)
                valid = [e for e in exps if e in yf_set]
                return valid if valid else yf_exps
            if exps:
                return exps
        except Exception:
            pass
    return yf_exps


def _load_candidate_dte() -> list[int]:
    """Load candidate_dte targets from settings.toml; fall back to [21, 30, 45]."""
    try:
        from pathlib import Path as _P
        try:
            import tomllib as _tl
        except ImportError:
            import tomli as _tl
        _s = _tl.loads((_P(__file__).resolve().parent.parent / "config" / "settings.toml").read_text(encoding="utf-8"))
        v = _s.get("dte", {}).get("candidate_dte")
        if isinstance(v, list) and v:
            return sorted(int(x) for x in v)
    except Exception:
        pass
    return [21, 30, 45]


def pick_expiry(ticker_obj, min_dte, max_dte):
    """Pick the available expiry whose DTE is closest to one of the candidate_dte
    targets (from settings.toml [dte] candidate_dte). Only considers expirations
    within [min_dte, max_dte]. Falls back to midpoint of window if no target fits,
    and to the nearest available expiry if nothing is within the window at all."""
    today      = datetime.now().date()
    targets    = _load_candidate_dte()
    expirations = _get_expirations(ticker_obj)

    # Filter to the DTE window
    candidates = []
    for exp in expirations:
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if min_dte <= dte <= max_dte:
            candidates.append((exp, dte))

    if candidates:
        # Score each available expiry by its distance to the nearest candidate_dte target
        def _target_dist(dte_val):
            return min(abs(dte_val - t) for t in targets)
        best = min(candidates, key=lambda c: _target_dist(c[1]))
        return best

    if not expirations:
        return None, None

    # Fallback: closest expiry to nearest target, ignoring window
    def _score(exp):
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        return min(abs(dte - t) for t in targets)
    best = min(expirations, key=_score)
    dte  = (datetime.strptime(best, "%Y-%m-%d").date() - today).days
    return best, dte


def _bs_price(S, K, T, r, sigma, option_type):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if option_type == "call" else (K - S))
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _implied_vol(price, S, K, T, r, option_type):
    """Solve for IV from a mid price using Brent's method."""
    if price <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if option_type == "call" else (K - S))
    if price <= intrinsic:
        return None
    try:
        return brentq(lambda s: _bs_price(S, K, T, r, s, option_type) - price,
                      1e-6, 20.0, xtol=1e-4, maxiter=100)
    except Exception:
        return None


def _fill_bid_ask(df, spot, dte, option_type, r=_RFR_FALLBACK):
    """Synthesise bid/ask and IV when missing.

    Two passes:
    1. Closed market (bid=ask=0, lastPrice>0): fill bid/ask from lastPrice.
    2. IV=0 on any row: back-solve IV from mid-price (bid/ask available but
       E*TRADE OptionGreeks were absent or zero).
    """
    df = df.copy()
    T = max(dte, 1) / 365.0

    # Pass 1 — closed market: fill bid/ask from lastPrice
    closed_mask = (df["bid"] == 0) & (df["ask"] == 0) & (df["lastPrice"] > 0)
    for idx in df[closed_mask].index:
        last = df.at[idx, "lastPrice"]
        df.at[idx, "bid"] = round(last * 0.98, 2)
        df.at[idx, "ask"] = round(last * 1.02, 2)

    # Pass 2 — fill IV from mid-price wherever it is 0 or missing
    zero_iv_mask = df["impliedVolatility"].fillna(0) < 1e-4
    for idx in df[zero_iv_mask].index:
        bid = df.at[idx, "bid"]
        ask = df.at[idx, "ask"]
        last = df.at[idx, "lastPrice"]
        mid = (bid + ask) / 2 if (bid > 0 or ask > 0) else last
        if mid and mid > 0:
            K = df.at[idx, "strike"]
            iv = _implied_vol(mid, spot, K, T, r, option_type)
            if iv is not None:
                df.at[idx, "impliedVolatility"] = iv

    return df


def _et_module():
    """Return the etrade_client module (lazy import)."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scripts import etrade_client as et
    return et


def _use_etrade(category: str) -> bool:
    """True if the config says to try E*TRADE for this data category.
    Returns False when the current thread has force_yfinance set (role-based gating)."""
    if getattr(_tls, "force_yfinance", False):
        return False
    try:
        et = _et_module()
        pref = et.ds_pref(category)
        if pref == "yfinance":
            return False
        if pref == "etrade":
            return True          # caller must handle auth error gracefully
        # "auto": use E*TRADE only when authenticated
        return et.is_authenticated()
    except Exception:
        return False


def _try_et_chain(ticker_str: str, expiry: str, spot=None, dte=None):
    """Fetch option chain from E*TRADE (real-time). Returns (calls, puts) or (None, None)."""
    try:
        et = _et_module()
        calls, puts = et.get_option_chain(ticker_str, expiry)
        if calls is None or puts is None or calls.empty or puts.empty:
            return None, None
        if spot is not None and dte is not None:
            calls = _fill_bid_ask(calls, spot, dte, "call")
            puts  = _fill_bid_ask(puts,  spot, dte, "put")
        return calls, puts
    except Exception:
        return None, None


def get_live_spot(ticker_str: str) -> dict | None:
    """Live spot price from E*TRADE (real-time). Returns {last, change_pct} or None."""
    if not _use_etrade("quotes"):
        return None
    try:
        et = _et_module()
        q = et.get_quote(ticker_str)
        if q and q.get("last") and float(q["last"]) > 0:
            return {"last": float(q["last"]), "change_pct": q.get("change_pct")}
    except Exception:
        pass
    return None


def get_option_chain(ticker_obj, expiry, spot=None, dte=None):
    ticker_str = getattr(ticker_obj, "ticker", None)
    if ticker_str and _use_etrade("option_chain"):
        et_calls, et_puts = _try_et_chain(ticker_str, expiry, spot, dte)
        if et_calls is not None:
            return et_calls, et_puts
    # Fallback: yfinance (15–20 min delayed)
    with _YF_CONCURRENCY:
        chain = ticker_obj.option_chain(expiry)
    calls, puts = chain.calls, chain.puts
    if spot is not None and dte is not None:
        calls = _fill_bid_ask(calls, spot, dte, "call")
        puts = _fill_bid_ask(puts, spot, dte, "put")
    return calls, puts


def pick_back_expiry(ticker_obj, front_expiry, min_gap_days=14, max_gap_days=45):
    """Pick a later expiry at least min_gap_days after front_expiry, for the
    long leg of a calendar/diagonal spread. Returns (expiry, dte) or (None, None)
    if no expiry falls within the gap window."""
    front_date = datetime.strptime(front_expiry, "%Y-%m-%d").date()
    today = datetime.now().date()
    expirations = _get_expirations(ticker_obj)
    candidates = []
    for exp in expirations:
        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        gap = (exp_date - front_date).days
        if min_gap_days <= gap <= max_gap_days:
            candidates.append((exp, (exp_date - today).days, gap))
    if not candidates:
        return None, None
    mid = (min_gap_days + max_gap_days) / 2
    best = min(candidates, key=lambda c: abs(c[2] - mid))
    return best[0], best[1]


_VIX_INDEX_TICKERS = {"SPY", "QQQ", "TQQQ", "IWM", "DIA", "VXX", "SQQQ", "SPXU"}

def get_iv_rank_proxy(hist, atm_iv, window=30, ticker=None):
    """IV rank proxy. For major index ETFs uses VIX percentile (cleaner signal).
    For individual stocks uses the IV/HV ratio: if ATM IV is elevated relative to
    the stock's own 30-day realized vol, options are 'rich' (High IV env).
    Falls back to the original realized-vol percentile if VIX is unavailable."""
    log_ret = np.log(hist["Close"] / hist["Close"].shift(1))
    hv_30 = log_ret.rolling(window).std().iloc[-1] * np.sqrt(252)

    # Index ETFs: use VIX percentile as the IV rank
    if ticker and ticker.upper() in _VIX_INDEX_TICKERS:
        try:
            vix_hist = yf.Ticker("^VIX").history(period="1y")
            if not vix_hist.empty:
                vix_now = vix_hist["Close"].iloc[-1]
                rank = float((vix_hist["Close"] < vix_now).mean() * 100)
                return round(rank, 1)
        except Exception:
            pass

    # Individual stocks: IV/HV ratio then map to 0-100 scale
    if hv_30 and hv_30 > 0:
        iv_hv_ratio = atm_iv / hv_30
        # ratio > 1.3 → richly priced (maps toward 100); ratio < 0.8 → cheap (maps toward 0)
        rank = min(100.0, max(0.0, (iv_hv_ratio - 0.5) / 1.5 * 100))
        return round(rank, 1)

    # Original fallback: ATM IV vs realized vol distribution
    realized_vol = log_ret.rolling(window).std() * np.sqrt(252)
    realized_vol = realized_vol.dropna()
    if realized_vol.empty:
        return None
    return round(float((realized_vol < atm_iv).mean() * 100), 1)


def get_hv_and_iv_premium(hist, atm_iv, window=20):
    """Compute HV(window) and IV premium over HV.

    Returns dict:
      hv20         – 20-day realised volatility (annualised, fraction)
      iv_premium   – atm_iv - hv20  (positive = IV rich → selling edge)
      iv_discount  – True when IV < HV (buying edge)
      iv_hv_ratio  – atm_iv / hv20
    """
    try:
        log_ret = np.log(hist["Close"] / hist["Close"].shift(1))
        hv = float(log_ret.rolling(window).std().dropna().iloc[-1]) * np.sqrt(252)
        if hv <= 0:
            return None
        premium = round(atm_iv - hv, 4)
        ratio   = round(atm_iv / hv, 3)
        return {
            "hv20":        round(hv, 4),
            "iv_premium":  premium,
            "iv_discount": premium < 0,
            "iv_hv_ratio": ratio,
        }
    except Exception:
        return None


def get_analyst_sentiment(ticker_obj):
    """Parse yfinance recommendations into a Buy/Hold/Sell count and net score.

    Returns dict:
      buy, hold, sell  – raw counts from the last 3 months of analyst ratings
      net_score        – (buy - sell) / total  in [-1, +1]; positive = net bullish
      label            – "Bullish" | "Bearish" | "Neutral" | "N/A"
    """
    try:
        rec = ticker_obj.recommendations
        if rec is None or rec.empty:
            return _neutral_analyst()
        # Keep only the last 3 months
        cutoff = pd.Timestamp.now(tz="UTC") - pd.DateOffset(months=3)
        if rec.index.tz is None:
            rec = rec[rec.index >= cutoff.tz_localize(None)]
        else:
            rec = rec[rec.index >= cutoff]
        if rec.empty:
            return _neutral_analyst()

        # yfinance columns vary by version; normalise to lowercase
        rec.columns = [c.lower() for c in rec.columns]

        # Aggregate Buy/Hold/Sell across "strongBuy","buy","hold","sell","strongSell"
        buy  = int(rec.get("strongbuy", rec.get("strong buy",  pd.Series(0))).sum()
                 + rec.get("buy",       pd.Series(0)).sum())
        hold = int(rec.get("hold",      pd.Series(0)).sum())
        sell = int(rec.get("sell",      pd.Series(0)).sum()
                 + rec.get("strongsell", rec.get("strong sell", pd.Series(0))).sum())
        total = buy + hold + sell
        if total == 0:
            return _neutral_analyst()

        net = round((buy - sell) / total, 3)
        if net > 0.2:
            label = "Bullish"
        elif net < -0.2:
            label = "Bearish"
        else:
            label = "Neutral"
        return {"buy": buy, "hold": hold, "sell": sell, "net_score": net, "label": label}
    except Exception:
        return _neutral_analyst()


def _neutral_analyst():
    return {"buy": 0, "hold": 0, "sell": 0, "net_score": 0.0, "label": "N/A"}


def get_news_sentiment(ticker_obj):
    """Fetch recent headlines from yfinance and classify them as
    Bullish / Bearish / Neutral using keyword matching."""
    BULLISH_WORDS = {
        "upgrade", "beat", "beats", "surges", "surge", "rally", "rallies",
        "buy", "raised", "raises", "record", "growth", "strong", "outperform",
        "bullish", "breakout", "positive", "profit", "gains", "gain",
        "exceeds", "jumps", "soars",
    }
    BEARISH_WORDS = {
        "downgrade", "miss", "misses", "falls", "fall", "cut", "cuts",
        "warning", "lawsuit", "layoff", "layoffs", "loss", "losses",
        "decline", "declines", "weak", "underperform", "bearish", "concern",
        "concerns", "negative", "recall", "slump", "drops", "drop",
        "disappoints", "disappointing",
    }
    try:
        news = ticker_obj.news or []
    except Exception:
        news = []

    bullish, bearish, headlines = 0, 0, []
    for article in news[:15]:
        # yfinance ≥0.2.x nests title under content{}
        title = (article.get("content") or {}).get("title") or article.get("title", "")
        if not title:
            continue
        headlines.append(title)
        words = set(title.lower().replace(",", "").replace("'", "").split())
        if words & BULLISH_WORDS:
            bullish += 1
        elif words & BEARISH_WORDS:
            bearish += 1

    total = bullish + bearish
    if total == 0:
        sentiment = "Neutral"
    elif bullish > bearish * 1.5:
        sentiment = "Bullish"
    elif bearish > bullish * 1.5:
        sentiment = "Bearish"
    else:
        sentiment = "Mixed"

    return {
        "sentiment": sentiment,
        "bullish_count": bullish,
        "bearish_count": bearish,
        "article_count": len(headlines),
        "headlines": headlines[:5],
    }


def get_news_sentiment_score(ticker_obj) -> float | None:
    """
    Numeric news sentiment score in [-1, +1] for ML features.

    Uses the same financial keyword lists as get_news_sentiment() but returns
    (bullish_hits - bearish_hits) / total_articles as a continuous signal.
    Positive = net bullish headlines, negative = net bearish, 0 = neutral/no news.
    Returns None if no headlines are available.

    No external API or paid service — pure yfinance headlines + keyword match.
    Intentionally domain-specific (financial vocabulary) rather than a generic
    sentiment model — more accurate for options trading signals.
    """
    BULLISH = {
        "upgrade", "beat", "beats", "surges", "surge", "rally", "rallies",
        "buy", "raised", "raises", "record", "growth", "strong", "outperform",
        "bullish", "breakout", "positive", "profit", "gains", "gain",
        "exceeds", "jumps", "soars",
    }
    BEARISH = {
        "downgrade", "miss", "misses", "falls", "fall", "cut", "cuts",
        "warning", "lawsuit", "layoff", "layoffs", "loss", "losses",
        "decline", "declines", "weak", "underperform", "bearish", "concern",
        "concerns", "negative", "recall", "slump", "drops", "drop",
        "disappoints", "disappointing",
    }
    try:
        news = ticker_obj.news or []
    except Exception:
        return None

    bullish = bearish = total = 0
    for article in news[:15]:
        title = (article.get("content") or {}).get("title") or article.get("title", "")
        if not title:
            continue
        total += 1
        words = set(title.lower().replace(",", "").replace("'", "").split())
        if words & BULLISH:
            bullish += 1
        elif words & BEARISH:
            bearish += 1

    if total == 0:
        return None
    return round((bullish - bearish) / total, 3)


def get_analyst_rec_change(ticker_obj, days: int = 5) -> int | None:
    """
    Net analyst recommendation change over the last `days` calendar days.

    Counts upgrades (+1 each) minus downgrades (-1 each) from
    yfinance Ticker.upgrades_downgrades — a direct measure of recent
    analyst sentiment shift, not just the standing consensus.

    Returns:
        positive int  = net upgrades (bullish shift)
        negative int  = net downgrades (bearish shift)
        0             = no change or equal up/down
        None          = data unavailable
    """
    try:
        ud = ticker_obj.upgrades_downgrades
        if ud is None or ud.empty:
            return None
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
        if ud.index.tz is None:
            recent = ud[ud.index >= cutoff.tz_localize(None)]
        else:
            recent = ud[ud.index >= cutoff]
        if recent.empty:
            return 0
        action_col = "Action" if "Action" in recent.columns else None
        if action_col is None:
            return None
        actions = recent[action_col].str.lower()
        upgrades   = int((actions == "up").sum())
        downgrades = int((actions == "down").sum())
        return upgrades - downgrades
    except Exception:
        return None


def get_weekly_trend(ticker, sma_short, sma_long, band_pct):
    """Same trend classification as get_trend(), but on weekly bars -
    used as a higher-timeframe confirmation signal."""
    hist = yf.Ticker(ticker).history(period="3y", interval="1wk")
    if hist.empty or len(hist) < sma_long:
        return "N/A"
    return get_trend(hist, sma_short, sma_long, band_pct)


def get_rsi(hist, period=14):
    close = hist["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    if rsi.empty or pd.isna(rsi.iloc[-1]):
        return None
    return round(rsi.iloc[-1], 1)


def get_macd(hist, fast=12, slow=26, signal=9):
    close = hist["Close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist_val = macd_line - signal_line
    if hist_val.empty or pd.isna(hist_val.iloc[-1]):
        return {"macd": None, "signal": None, "hist": None, "trend": "N/A"}
    return {
        "macd": round(macd_line.iloc[-1], 3),
        "signal": round(signal_line.iloc[-1], 3),
        "hist": round(hist_val.iloc[-1], 3),
        "trend": "Bullish" if hist_val.iloc[-1] > 0 else "Bearish",
    }


def _load_earnings_cache():
    if _EARNINGS_CACHE_PATH.exists():
        with open(_EARNINGS_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_earnings_cache(cache):
    _EARNINGS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_EARNINGS_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def get_next_earnings_date(ticker_obj):
    """Look up the next earnings date, using a local cache to avoid repeat
    lookups. If a cached date is still in the future, use it directly;
    otherwise (missing, in the past, or no earnings) look it up live via
    yfinance and cache the result. Returns a date or None if unavailable
    (e.g. ETFs have no earnings)."""
    symbol = ticker_obj.ticker
    today = datetime.now().date()
    cache = _load_earnings_cache()

    cached = cache.get(symbol)
    if cached:
        cached_date = datetime.strptime(cached, "%Y-%m-%d").date()
        if cached_date >= today:
            return cached_date

    try:
        cal = ticker_obj.calendar
    except Exception:
        cal = None
    earn = cal.get("Earnings Date") if cal else None
    if not earn:
        return None

    earn_date = earn[0]
    cache[symbol] = earn_date.strftime("%Y-%m-%d")
    _save_earnings_cache(cache)
    return earn_date


def get_adx(hist, period=14):
    """Average Directional Index. >25 = strong trend, <20 = choppy/range-bound."""
    high = hist["High"]
    low = hist["Low"]
    close = hist["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=hist.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=hist.index
    )
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    denom = plus_di + minus_di
    dx = pd.Series(
        np.where(denom > 0, 100 * (plus_di - minus_di).abs() / denom, 0.0), index=hist.index
    )
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    if adx.empty or pd.isna(adx.iloc[-1]):
        return None
    return round(float(adx.iloc[-1]), 1)


def get_relative_volume(hist, period=20):
    """Today's volume relative to the N-day average. >1.5 = elevated, <0.5 = thin."""
    vol = hist["Volume"]
    avg = vol.rolling(period).mean()
    if avg.empty or pd.isna(avg.iloc[-1]) or avg.iloc[-1] == 0:
        return None
    return round(float(vol.iloc[-1] / avg.iloc[-1]), 2)


def _load_oi_cache():
    if _OI_CACHE_PATH.exists():
        try:
            with open(_OI_CACHE_PATH) as f:
                content = f.read()
            # Tolerate truncated-write corruption: take the first valid JSON object
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(content)
            return obj
        except Exception:
            return {}
    return {}


def _save_oi_cache(cache):
    _OI_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then rename for atomicity (prevents partial-write corruption)
    tmp = _OI_CACHE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    tmp.replace(_OI_CACHE_PATH)


def get_options_flow(ticker, expiry, calls, puts):
    """Compute PCR, unusual-activity flag, and OI change vs yesterday's cache.

    PCR = total put OI / total call OI for the chosen expiry.
    Unusual activity = total volume > 3× total OI (smart-money positioning signal).
    OI delta = change in call/put OI since the last cached run.
    """
    today_str = datetime.now().date().isoformat()

    call_oi = float(calls["openInterest"].fillna(0).sum())
    put_oi  = float(puts["openInterest"].fillna(0).sum())
    call_vol = float(calls["volume"].fillna(0).sum())
    put_vol  = float(puts["volume"].fillna(0).sum())
    total_oi  = call_oi + put_oi
    total_vol = call_vol + put_vol

    # Put/Call Ratio
    pcr = round(put_oi / call_oi, 2) if call_oi > 0 else None
    if pcr is None:
        pcr_sentiment = "N/A"
    elif pcr > 1.2:
        pcr_sentiment = "Bearish"
    elif pcr < 0.7:
        pcr_sentiment = "Bullish"
    else:
        pcr_sentiment = "Neutral"

    # Unusual activity
    vol_oi_ratio = round(total_vol / total_oi, 2) if total_oi > 0 else 0.0
    unusual = bool(total_oi > 0 and total_vol > 3 * total_oi)

    # OI delta vs previous run
    cache = _load_oi_cache()
    key = f"{ticker}:{expiry}"
    prev = cache.get(key)
    if prev:
        oi_delta_calls = int(call_oi - prev.get("call_oi", 0))
        oi_delta_puts  = int(put_oi  - prev.get("put_oi",  0))
    else:
        oi_delta_calls = None
        oi_delta_puts  = None

    cache[key] = {
        "date": today_str,
        "call_oi": call_oi,
        "put_oi":  put_oi,
        "call_vol": call_vol,
        "put_vol":  put_vol,
    }
    _save_oi_cache(cache)

    return {
        "pcr": pcr,
        "pcr_sentiment": pcr_sentiment,
        "unusual_activity": unusual,
        "vol_oi_ratio": vol_oi_ratio,
        "call_oi": int(call_oi),
        "put_oi":  int(put_oi),
        "call_vol": int(call_vol),
        "put_vol":  int(put_vol),
        "oi_delta_calls": oi_delta_calls,
        "oi_delta_puts":  oi_delta_puts,
    }


def get_ema200(hist):
    """EMA(200) institutional trend filter.
    Returns (ema200_value, position) where position is 'above' | 'below' | None."""
    close = hist["Close"]
    if len(close) < 200:
        return None, None
    ema = close.ewm(span=200, adjust=False).mean()
    val = float(ema.iloc[-1])
    price = float(close.iloc[-1])
    return round(val, 2), ("above" if price > val else "below")


def get_iv_term_structure(ticker_obj, front_expiry, back_expiry, spot, front_dte, back_dte):
    """Compare ATM IV between front and back expiry to detect term structure shape.

    Contango  (normal):      front IV < back IV  — calm near-term, uncertainty rises with time
    Backwardation (elevated): front IV > back IV  — near-term fear/event premium; ideal for calendar/diagonal

    Returns a dict with keys: front_iv, back_iv, slope, shape, note, edge_pct
    """
    try:
        front_calls, front_puts = get_option_chain(ticker_obj, front_expiry, spot=spot, dte=front_dte)
        back_calls,  back_puts  = get_option_chain(ticker_obj, back_expiry,  spot=spot, dte=back_dte)

        front_iv = get_atm_iv(front_calls, front_puts, spot)
        back_iv  = get_atm_iv(back_calls,  back_puts,  spot)

        if front_iv <= 0 or back_iv <= 0:
            return None

        slope    = round(front_iv - back_iv, 4)   # positive = backwardation
        edge_pct = round(slope / back_iv * 100, 1) if back_iv > 0 else 0.0

        if slope > 0.02:
            shape = "Backwardation"
            note  = (f"Front IV {front_iv*100:.0f}% > Back IV {back_iv*100:.0f}% "
                     f"(+{edge_pct:.1f}%) — near-term vol premium, ideal for calendar/diagonal: "
                     f"selling richer front-month vol")
        elif slope < -0.02:
            shape = "Contango"
            note  = (f"Front IV {front_iv*100:.0f}% < Back IV {back_iv*100:.0f}% "
                     f"({edge_pct:.1f}%) — normal term structure; calendar spread has no vol-edge advantage")
        else:
            shape = "Flat"
            note  = (f"Front IV {front_iv*100:.0f}% ≈ Back IV {back_iv*100:.0f}% "
                     f"— flat term structure, calendar spread is neutral on IV")

        return {
            "front_iv":  round(front_iv, 4),
            "back_iv":   round(back_iv,  4),
            "slope":     slope,
            "shape":     shape,
            "note":      note,
            "edge_pct":  edge_pct,
        }
    except Exception:
        return None


def get_atm_iv(calls, puts, spot):
    calls = calls.copy()
    puts = puts.copy()
    calls["dist"] = (calls["strike"] - spot).abs()
    puts["dist"] = (puts["strike"] - spot).abs()
    atm_call_iv = calls.sort_values("dist").iloc[0]["impliedVolatility"]
    atm_put_iv = puts.sort_values("dist").iloc[0]["impliedVolatility"]
    return (atm_call_iv + atm_put_iv) / 2


def _ticker_info(ticker_obj, timeout=6):
    """Fetch ticker_obj.info with a hard timeout to prevent hanging on Yahoo API stalls."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(lambda: ticker_obj.info)
        try:
            return fut.result(timeout=timeout)
        except (FuturesTimeout, Exception):
            return {}


def get_dividend_info(ticker_obj):
    """Return ex-dividend date and days until it (None if no upcoming dividend).

    Returns dict with keys: ex_date (str|None), days_to_ex (int|None), annual_yield (float|None)
    """
    try:
        info = _ticker_info(ticker_obj)
        ex_ts = info.get("exDividendDate")
        annual_yield = info.get("dividendYield")
        if annual_yield is not None:
            annual_yield = round(float(annual_yield) * 100, 2)

        if ex_ts:
            ex_date = datetime.utcfromtimestamp(int(ex_ts)).date()
            days_to_ex = (ex_date - datetime.now().date()).days
            return {
                "ex_date":     ex_date.isoformat(),
                "days_to_ex":  days_to_ex,
                "annual_yield": annual_yield,
            }
    except Exception:
        pass
    return {"ex_date": None, "days_to_ex": None, "annual_yield": None}


def get_short_interest(ticker_obj):
    """Return short interest as a percent of float (e.g. 8.5 for 8.5%). None if unavailable."""
    try:
        si = _ticker_info(ticker_obj).get("shortPercentOfFloat")
        if si is not None:
            return round(float(si) * 100, 2)
    except Exception:
        pass
    return None


def get_sector_context(ticker_str: str, atm_iv: float, hist_period: str = "3mo") -> dict:
    """
    Fetch the ticker's sector ETF and compute:
      - sector_etf:      ETF symbol (e.g. "XLK")
      - sector_trend:    "Uptrend" | "Downtrend" | "Range-bound" (same vocabulary as analyze.py)
      - sector_rsi:      RSI(14) of sector ETF
      - sector_iv_ratio: stock ATM IV / sector ATM IV  (>1.5 = stock IV elevated vs sector)

    Falls back gracefully — any field can be None if data is unavailable.
    """
    from scripts.market_context import SECTOR_TO_ETF
    result = {
        "sector_etf":      None,
        "sector_trend":    None,
        "sector_rsi":      None,
        "sector_iv_ratio": None,
    }
    try:
        sector = yf.Ticker(ticker_str).info.get("sector", "")
        etf = SECTOR_TO_ETF.get(sector)
        if not etf:
            return result
        result["sector_etf"] = etf
    except Exception:
        return result

    try:
        from config import rules
        etf_hist = yf.Ticker(etf).history(period=hist_period)
        if etf_hist.empty or len(etf_hist) < rules.SMA_LONG + 2:
            return result
        etf_close = etf_hist["Close"].squeeze()
        # RSI
        delta = etf_close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi_val = (100 - 100 / (1 + rs)).iloc[-1]
        result["sector_rsi"] = round(float(rsi_val), 1) if not pd.isna(rsi_val) else None

        # Trend (same SMA logic as analyze.py get_trend)
        if len(etf_close) >= rules.SMA_LONG:
            sma_s = etf_close.rolling(rules.SMA_SHORT).mean().iloc[-1]
            sma_l = etf_close.rolling(rules.SMA_LONG).mean().iloc[-1]
            band  = rules.TREND_BAND_PCT
            pct   = (sma_s - sma_l) / sma_l if sma_l else 0
            if pct > band:
                result["sector_trend"] = "Uptrend"
            elif pct < -band:
                result["sector_trend"] = "Downtrend"
            else:
                result["sector_trend"] = "Range-bound"
    except Exception:
        pass

    # Sector IV ratio — try yfinance option chain for the sector ETF
    try:
        if atm_iv and atm_iv > 0:
            etf_tkr = yf.Ticker(etf)
            if etf_tkr.options:
                etf_expiry = etf_tkr.options[0]
                etf_spot   = float(yf.Ticker(etf).fast_info.last_price or 0)
                etf_calls, etf_puts = get_option_chain(etf_tkr, etf_expiry, spot=etf_spot)
                if etf_calls is not None and not etf_calls.empty and etf_spot > 0:
                    sector_iv = get_atm_iv(etf_calls, etf_puts, etf_spot)
                    if sector_iv > 0:
                        result["sector_iv_ratio"] = round(atm_iv / sector_iv, 3)
    except Exception:
        pass

    return result


def get_index_trends(spy_hist=None, qqq_hist=None, iwm_hist=None, period: str = "3mo") -> dict:
    """
    RSI(14) and trend label for SPY, QQQ, IWM.
    Pass pre-fetched history DataFrames to avoid redundant API calls.
    Returns: {spy_trend, spy_rsi, qqq_trend, qqq_rsi, iwm_trend, iwm_rsi}
    """
    from config import rules

    def _trend_and_rsi(hist):
        if hist is None or hist.empty:
            return None, None
        close = hist["Close"].squeeze()
        if len(close) < rules.SMA_LONG + 2:
            return None, None
        # RSI
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi_val = (100 - 100 / (1 + rs)).iloc[-1]
        rsi_out = round(float(rsi_val), 1) if not pd.isna(rsi_val) else None
        # Trend
        if len(close) >= rules.SMA_LONG:
            sma_s = close.rolling(rules.SMA_SHORT).mean().iloc[-1]
            sma_l = close.rolling(rules.SMA_LONG).mean().iloc[-1]
            pct   = (sma_s - sma_l) / sma_l if sma_l else 0
            band  = rules.TREND_BAND_PCT
            if pct > band:
                trend = "Uptrend"
            elif pct < -band:
                trend = "Downtrend"
            else:
                trend = "Range-bound"
        else:
            trend = None
        return trend, rsi_out

    try:
        if spy_hist is None:
            spy_hist = yf.Ticker("SPY").history(period=period)
        if qqq_hist is None:
            qqq_hist = yf.Ticker("QQQ").history(period=period)
        if iwm_hist is None:
            iwm_hist = yf.Ticker("IWM").history(period=period)
    except Exception:
        pass

    spy_trend, spy_rsi = _trend_and_rsi(spy_hist)
    qqq_trend, qqq_rsi = _trend_and_rsi(qqq_hist)
    iwm_trend, iwm_rsi = _trend_and_rsi(iwm_hist)

    return {
        "spy_trend": spy_trend, "spy_rsi": spy_rsi,
        "qqq_trend": qqq_trend, "qqq_rsi": qqq_rsi,
        "iwm_trend": iwm_trend, "iwm_rsi": iwm_rsi,
    }


_FOMC_DATES_2025 = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
]
_FOMC_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
# CPI/PPI release dates (BLS schedule — update each January)
_CPI_DATES_2025 = [
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10",
    "2025-05-13", "2025-06-11", "2025-07-15", "2025-08-12",
    "2025-09-10", "2025-10-15", "2025-11-13", "2025-12-10",
]
_CPI_DATES_2026 = [
    "2026-01-14", "2026-02-11", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-09", "2026-10-13", "2026-11-12", "2026-12-09",
]
_ALL_FOMC = sorted(_FOMC_DATES_2025 + _FOMC_DATES_2026)
_ALL_CPI  = sorted(_CPI_DATES_2025  + _CPI_DATES_2026)

# PPI release dates (BLS schedule — typically released ~1 week after CPI)
_PPI_DATES_2025 = [
    "2025-01-14", "2025-02-13", "2025-03-13", "2025-04-11",
    "2025-05-15", "2025-06-12", "2025-07-15", "2025-08-14",
    "2025-09-11", "2025-10-16", "2025-11-13", "2025-12-11",
]
_PPI_DATES_2026 = [
    "2026-01-15", "2026-02-12", "2026-03-12", "2026-04-14",
    "2026-05-14", "2026-06-11", "2026-07-15", "2026-08-13",
    "2026-09-10", "2026-10-15", "2026-11-12", "2026-12-10",
]
# BLS Employment Situation (Jobs Report) — first Friday each month
_JOBS_DATES_2025 = [
    "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04",
    "2025-05-02", "2025-06-06", "2025-07-03", "2025-08-01",
    "2025-09-05", "2025-10-03", "2025-11-07", "2025-12-05",
]
_JOBS_DATES_2026 = [
    "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
    "2026-05-01", "2026-06-05", "2026-07-02", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]
_ALL_PPI  = sorted(_PPI_DATES_2025  + _PPI_DATES_2026)
_ALL_JOBS = sorted(_JOBS_DATES_2025 + _JOBS_DATES_2026)

# Quarterly OPEX months (standard equity options expiry = 3rd Friday each month;
# quarterly = March, June, September, December also expire index/futures)
_OPEX_QUARTERLY_MONTHS = {3, 6, 9, 12}


def _third_friday(year: int, month: int):
    """Return the third Friday of the given month (standard equity OPEX date)."""
    from datetime import date as _date, timedelta as _td
    first_day = _date(year, month, 1)
    days_to_first_friday = (4 - first_day.weekday()) % 7
    first_friday = first_day + _td(days=days_to_first_friday)
    return first_friday + _td(weeks=2)


def _opex_info(from_date=None) -> dict:
    """
    Return context about the next standard equity OPEX (3rd Friday).
    days_to_opex     : calendar days to next OPEX
    is_opex_week     : 1 if OPEX falls within 5 calendar days
    is_monthly_opex  : always 1 (every 3rd Friday is monthly OPEX)
    is_quarterly_opex: 1 if the OPEX month is March / June / Sep / Dec
    """
    from datetime import date as _date
    today = from_date or _date.today()
    for month_offset in range(3):
        y, m = today.year, today.month + month_offset
        if m > 12:
            y, m = y + 1, m - 12
        opex = _third_friday(y, m)
        days = (opex - today).days
        if days >= 0:
            return {
                "days_to_opex":      days,
                "is_opex_week":      int(days <= 5),
                "is_monthly_opex":   1,
                "is_quarterly_opex": int(m in _OPEX_QUARTERLY_MONTHS),
            }
    return {"days_to_opex": None, "is_opex_week": 0, "is_monthly_opex": 0, "is_quarterly_opex": 0}


def _days_to_next_event(date_strings: list[str], from_date=None) -> int | None:
    """Return calendar days from `from_date` (default today) to the nearest
    upcoming event date in `date_strings`.  Returns None if none upcoming."""
    from datetime import date as _date
    today = from_date or _date.today()
    upcoming = [
        (_date.fromisoformat(d) - today).days
        for d in date_strings
        if (_date.fromisoformat(d) - today).days >= 0
    ]
    return min(upcoming) if upcoming else None


def get_macro_context(dte: int | None = None) -> dict:
    """
    Tier 5 macro features — fetched once per collection run and shared across
    all tickers (these signals are market-wide, not ticker-specific).

    Returns dict with keys:
      fed_days_away      : calendar days to next FOMC meeting
      fed_within_dte     : 1 if FOMC falls within `dte` days, else 0 (None if dte not given)
      cpi_days_away      : calendar days to next CPI release
      cpi_within_dte     : 1 if CPI falls within `dte` days, else 0
      ppi_days_away      : calendar days to next PPI release
      ppi_within_dte     : 1 if PPI falls within `dte` days, else 0
      jobs_days_away     : calendar days to next Jobs Report (first Friday each month)
      jobs_within_dte    : 1 if Jobs Report falls within `dte` days, else 0
      days_to_opex       : calendar days to next standard equity OPEX (3rd Friday)
      opex_within_dte    : 1 if OPEX falls within `dte` days, else 0
      is_opex_week       : 1 if OPEX is within 5 calendar days
      is_monthly_opex    : always 1 (every 3rd Friday is monthly OPEX)
      is_quarterly_opex  : 1 if OPEX month is March, June, September, or December
      yield_10y          : ^TNX last close (10-year Treasury yield, %)
      yield_3m           : ^IRX last close (3-month T-bill yield, %)
      yield_curve        : yield_10y − yield_3m (positive = normal, negative = inverted)
      dollar_index       : DX-Y.NYB last close
    All price fields can be None if fetch fails; event fields can be None if
    dte is None or days_away is None.
    """
    result = {
        "fed_days_away":       None,
        "fed_within_dte":      None,
        "cpi_days_away":       None,
        "cpi_within_dte":      None,
        "ppi_days_away":       None,
        "ppi_within_dte":      None,
        "jobs_days_away":      None,
        "jobs_within_dte":     None,
        "days_to_opex":        None,
        "opex_within_dte":     None,
        "is_opex_week":        None,
        "is_monthly_opex":     None,
        "is_quarterly_opex":   None,
        "yield_10y":           None,
        "yield_3m":            None,
        "yield_curve":         None,
        "dollar_index":        None,
    }

    result["fed_days_away"]  = _days_to_next_event(_ALL_FOMC)
    result["cpi_days_away"]  = _days_to_next_event(_ALL_CPI)
    result["ppi_days_away"]  = _days_to_next_event(_ALL_PPI)
    result["jobs_days_away"] = _days_to_next_event(_ALL_JOBS)

    opex = _opex_info()
    result["days_to_opex"]      = opex["days_to_opex"]
    result["is_opex_week"]      = opex["is_opex_week"]
    result["is_monthly_opex"]   = opex["is_monthly_opex"]
    result["is_quarterly_opex"] = opex["is_quarterly_opex"]

    if dte is not None:
        fd = result["fed_days_away"]
        cd = result["cpi_days_away"]
        pd_ = result["ppi_days_away"]
        jd  = result["jobs_days_away"]
        od  = result["days_to_opex"]
        result["fed_within_dte"]  = int(fd  is not None and fd  <= dte)
        result["cpi_within_dte"]  = int(cd  is not None and cd  <= dte)
        result["ppi_within_dte"]  = int(pd_ is not None and pd_ <= dte)
        result["jobs_within_dte"] = int(jd  is not None and jd  <= dte)
        result["opex_within_dte"] = int(od  is not None and od  <= dte)

    # Yields — FRED primary (reliable daily data), yfinance fallback
    fred = get_fred_yields()
    if fred["yield_10y"] is not None:
        result["yield_10y"]  = fred["yield_10y"]
        result["yield_3m"]   = fred["yield_3m"]
        result["yield_curve"] = fred["yield_curve"]
    else:
        try:
            data  = yf.download(["^TNX", "^IRX", "DX-Y.NYB"], period="5d",
                                 auto_adjust=True, progress=False)
            close = data["Close"] if "Close" in data.columns else data

            def _last(sym):
                try:
                    col = close[sym].dropna()
                    return float(col.iloc[-1]) if not col.empty else None
                except Exception:
                    return None

            tnx = _last("^TNX")
            irx = _last("^IRX")
            result["yield_10y"]  = round(tnx, 3) if tnx is not None else None
            result["yield_3m"]   = round(irx, 3) if irx is not None else None
            if tnx is not None and irx is not None:
                result["yield_curve"] = round(tnx - irx, 3)
        except Exception:
            pass

    # Dollar index always from yfinance (not on FRED)
    try:
        dxy = yf.Ticker("DX-Y.NYB").fast_info.last_price
        result["dollar_index"] = round(float(dxy), 3) if dxy else None
    except Exception:
        pass

    return result


def get_vix_context() -> dict:
    """
    VIX level, VVIX (vol-of-vol), and VIX term structure slope.

    vvix:           ^VVIX last price. >120 = vol regime unstable, avoid selling premium
    vix_3m:         ^VIX3M last price (3-month expected vol)
    vix_term_slope: ^VIX / ^VIX3M. >1 = backwardation (near-term fear), <1 = contango (calm)

    Returns dict — any field can be None if fetch fails.
    """
    result = {"vvix": None, "vix_3m": None, "vix_term_slope": None}
    try:
        data = yf.download(["^VVIX", "^VIX3M", "^VIX"], period="2d",
                           auto_adjust=True, progress=False)
        close = data["Close"] if "Close" in data.columns else data
        def _last(sym):
            try:
                col = close[sym].dropna()
                return float(col.iloc[-1]) if not col.empty else None
            except Exception:
                return None
        vvix   = _last("^VVIX")
        vix_3m = _last("^VIX3M")
        vix    = _last("^VIX")
        result["vvix"]  = round(vvix,  1) if vvix  else None
        result["vix_3m"] = round(vix_3m, 2) if vix_3m else None
        if vix and vix_3m and vix_3m > 0:
            result["vix_term_slope"] = round(vix / vix_3m, 4)
    except Exception:
        pass
    return result


def get_otm_pcr(calls, puts, spot):
    """
    OTM Put/Call ratio — a more sensitive fear gauge than the standard PCR.

    Standard PCR uses ALL strikes equally weighted. This version weights only
    high-delta puts (0.8–0.9 delta, i.e. deep ITM puts or equivalently the
    OTM puts with the most protection value at ~20% OTM) vs low-delta calls
    (0.1–0.2 delta, the cheap OTM speculative calls). A ratio > 1 means
    institutions are paying for downside protection much more than upside
    speculation — a genuine fear signal independent of ATM noise.

    Approximates 0.8-delta puts as strikes 5–15% below spot and
    0.1–0.2 delta calls as strikes 5–15% above spot — reasonable for
    2–6 week expiries without needing live Greeks from every broker.

    Returns dict: {otm_put_oi, otm_call_oi, otm_pcr} or None on failure.
    """
    try:
        put_lo, put_hi   = spot * 0.85, spot * 0.95   # 5–15% OTM puts
        call_lo, call_hi = spot * 1.05, spot * 1.15   # 5–15% OTM calls
        otm_puts  = puts[(puts["strike"]  >= put_lo)  & (puts["strike"]  <= put_hi)]
        otm_calls = calls[(calls["strike"] >= call_lo) & (calls["strike"] <= call_hi)]
        put_oi  = float(otm_puts["openInterest"].fillna(0).sum())
        call_oi = float(otm_calls["openInterest"].fillna(0).sum())
        if call_oi <= 0:
            return None
        return {
            "otm_put_oi":  int(put_oi),
            "otm_call_oi": int(call_oi),
            "otm_pcr":     round(put_oi / call_oi, 3),
        }
    except Exception:
        return None


def get_beta(hist, spy_hist, window=60):
    """
    Rolling beta vs SPY over `window` trading days.

    Beta > 1 → stock amplifies index moves (wider spreads needed).
    Beta < 1 → stock is more stable than the index.
    Beta < 0 → inverse relationship (rare for WATCHLIST names).

    Returns float or None if insufficient data.
    """
    try:
        close     = hist["Close"].squeeze()
        spy_close = spy_hist["Close"].squeeze()
        # Align on common dates
        combined = pd.DataFrame({"stock": close, "spy": spy_close}).dropna()
        if len(combined) < window + 2:
            return None
        stock_ret = np.log(combined["stock"] / combined["stock"].shift(1)).dropna()
        spy_ret   = np.log(combined["spy"]   / combined["spy"].shift(1)).dropna()
        combined_ret = pd.concat([stock_ret, spy_ret], axis=1).dropna()
        combined_ret.columns = ["stock", "spy"]
        tail = combined_ret.tail(window)
        cov  = tail["stock"].cov(tail["spy"])
        var  = tail["spy"].var()
        if var <= 0:
            return None
        return round(float(cov / var), 3)
    except Exception:
        return None


def get_atr_pct(hist, spot, period=14):
    """
    Average True Range as a % of current spot price.

    ATR captures actual daily range better than HV for strike-width sizing:
    a stock with 2% ATR needs strikes at least 2× ATR away to avoid intraday
    touches. Returns float (e.g. 1.85 meaning 1.85% ATR) or None.
    """
    try:
        high  = hist["High"].squeeze()
        low   = hist["Low"].squeeze()
        close = hist["Close"].squeeze()
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
        if spot <= 0:
            return None
        return round(float(atr / spot * 100), 3)
    except Exception:
        return None


def get_iv_rank_52w(ticker_str, atm_iv):
    """
    True 52-week IV rank: what percentile is today's ATM IV vs the past 252
    trading days of realized volatility (HV20 rolling series)?

    Unlike iv_rank_proxy (which uses a 30-day window and IV/HV ratio), this
    uses the full year of daily HV readings as a proxy for the IV distribution
    — the best approximation available without a paid IV history feed.

    Returns float 0–100 (e.g. 78.5 = ATM IV is above 78.5% of the past year's
    HV readings, meaning IV is currently elevated vs historical norms).
    Returns None on failure.
    """
    try:
        hist = yf.Ticker(ticker_str).history(period="1y")
        if hist.empty or len(hist) < 60:
            return None
        close    = hist["Close"].squeeze()
        log_ret  = np.log(close / close.shift(1))
        hv_daily = log_ret.rolling(20).std() * np.sqrt(252)
        hv_daily = hv_daily.dropna()
        if hv_daily.empty:
            return None
        rank = float((hv_daily < atm_iv).mean() * 100)
        return round(rank, 1)
    except Exception:
        return None


def get_max_pain(calls, puts) -> float | None:
    """
    Max pain strike: the strike where total option buyer losses are maximised
    (i.e. where the most open interest expires worthless). Acts as a price magnet
    into expiry — useful for strike selection near expiry and as an OI signal.

    Returns the strike price (float) or None on failure.
    """
    try:
        strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
        if not strikes:
            return None
        call_oi = calls.set_index("strike")["openInterest"].fillna(0)
        put_oi  = puts.set_index("strike")["openInterest"].fillna(0)
        min_pain, max_pain_strike = None, None
        for s in strikes:
            call_pain = sum(max(s - k, 0) * call_oi.get(k, 0) for k in strikes)
            put_pain  = sum(max(k - s, 0) * put_oi.get(k, 0)  for k in strikes)
            total = call_pain + put_pain
            if min_pain is None or total < min_pain:
                min_pain, max_pain_strike = total, s
        return float(max_pain_strike) if max_pain_strike is not None else None
    except Exception:
        return None


def get_oi_concentration(calls, puts, spot, window_pct=0.10) -> float | None:
    """
    OI concentration: fraction of total OI sitting within ±window_pct of spot.

    High concentration (>0.5) means most open interest is clustered near ATM —
    suggests pinning risk and tighter expected move. Low concentration means OI
    is spread across many strikes — less pinning, larger tail risk.

    Returns float 0–1 or None on failure.
    """
    try:
        lo, hi = spot * (1 - window_pct), spot * (1 + window_pct)
        all_oi    = pd.concat([calls[["strike", "openInterest"]],
                               puts[["strike",  "openInterest"]]]).fillna(0)
        total_oi  = all_oi["openInterest"].sum()
        if total_oi <= 0:
            return None
        near_oi   = all_oi[all_oi["strike"].between(lo, hi)]["openInterest"].sum()
        return round(float(near_oi / total_oi), 4)
    except Exception:
        return None


def get_vol_skew(calls, puts, spot):
    """Compare ~25-delta OTM put IV vs ~25-delta OTM call IV to detect skew direction.

    Approximates 25-delta strikes as ±5% away from spot (reasonable for 2–6 week expiries).
    Returns dict: put_iv, call_iv, skew_pct (put_iv - call_iv as a %, positive = put-skew / fear).
    """
    try:
        put_target  = spot * 0.95
        call_target = spot * 1.05
        puts_c  = puts.copy();   puts_c["dist"]  = (puts_c["strike"]  - put_target).abs()
        calls_c = calls.copy();  calls_c["dist"] = (calls_c["strike"] - call_target).abs()
        put_row  = puts_c.sort_values("dist").iloc[0]
        call_row = calls_c.sort_values("dist").iloc[0]
        put_iv   = float(put_row["impliedVolatility"]  or 0)
        call_iv  = float(call_row["impliedVolatility"] or 0)
        if put_iv <= 0 or call_iv <= 0:
            return None
        skew_pct = round((put_iv - call_iv) * 100, 2)   # positive = puts richer (fear/downside skew)
        return {
            "put_iv":   round(put_iv, 4),
            "call_iv":  round(call_iv, 4),
            "skew_pct": skew_pct,
        }
    except Exception:
        return None


_CHAIN_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "option_chain_snapshots.jsonl"


def compute_chain_features(ticker: str, spot: float | None = None) -> dict:
    """
    Load the most recent option chain snapshot for `ticker` from
    data/option_chain_snapshots.jsonl and compute Tier 4 features.

    Returns dict with keys:
      iv_skew_20d      : put IV at ~20-delta minus call IV at ~20-delta (None if no delta)
      gex_proxy        : sum(gamma × OI × 100) calls minus puts (None if no gamma)
      max_pain_strike  : expiry price where aggregate holder intrinsic value is minimized
      oi_concentration : % of total OI at ±2 strikes from ATM
      wings_iv_ratio   : 10-delta put IV ÷ ATM IV (None if no delta)
    All values None when no snapshot exists or data is insufficient.

    delta/gamma-dependent features require E*TRADE-sourced snapshots; OI-based
    features (max_pain_strike, oi_concentration) work with yfinance data too.
    """
    _null = {
        "iv_skew_20d": None, "gex_proxy": None, "max_pain_strike": None,
        "oi_concentration": None, "wings_iv_ratio": None,
        # Option-specific features (available when chain data has sufficient detail)
        "atm_iv": None,       # IV of the ATM call (or nearest to 50-delta)
        "atm_delta": None,    # Delta of ATM call (E*TRADE snapshots only)
        "atm_gamma": None,    # Gamma of ATM call (E*TRADE snapshots only)
        "front_dte": None,    # Days to expiry of the front expiry in the snapshot
    }
    if not _CHAIN_SNAPSHOT_PATH.exists():
        return _null

    # Walk the JSONL to find the most recent record for this ticker
    latest = None
    try:
        with _CHAIN_SNAPSHOT_PATH.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("ticker") == ticker and rec.get("strikes"):
                        if latest is None or rec["collected_at"] > latest["collected_at"]:
                            latest = rec
                except Exception:
                    pass
    except Exception:
        return _null

    if latest is None:
        return _null

    strikes  = latest["strikes"]
    snap_spot = spot if spot is not None else latest.get("spot")
    calls = [s for s in strikes if s.get("opt_type") == "call"]
    puts  = [s for s in strikes if s.get("opt_type") == "put"]

    result = dict(_null)

    # ── Feature 1: iv_skew_20d ────────────────────────────────────────────────
    # Put IV at ~20-delta minus call IV at ~20-delta (E*TRADE only; delta absent for yfinance)
    try:
        pd_list = [s for s in puts  if s.get("delta") is not None and (s.get("iv") or 0) > 0]
        cd_list = [s for s in calls if s.get("delta") is not None and (s.get("iv") or 0) > 0]
        if pd_list and cd_list:
            p20 = min(pd_list, key=lambda s: abs(abs(s["delta"]) - 0.20))
            c20 = min(cd_list, key=lambda s: abs(abs(s["delta"]) - 0.20))
            result["iv_skew_20d"] = round(float(p20["iv"]) - float(c20["iv"]), 4)
    except Exception:
        pass

    # ── Feature 2: gex_proxy ──────────────────────────────────────────────────
    # Σ(gamma × OI × 100) for calls minus puts — market-maker hedging flow proxy
    try:
        cg = [s for s in calls if s.get("gamma") is not None]
        pg = [s for s in puts  if s.get("gamma") is not None]
        if cg or pg:
            gex_c = sum(s["gamma"] * s.get("open_interest", 0) * 100 for s in cg)
            gex_p = sum(s["gamma"] * s.get("open_interest", 0) * 100 for s in pg)
            result["gex_proxy"] = round(gex_c - gex_p, 0)
    except Exception:
        pass

    # ── Feature 3: max_pain_strike ────────────────────────────────────────────
    # Expiry price where aggregate intrinsic value to holders is minimized;
    # requires only OI per strike (works with yfinance data).
    try:
        call_oi = {s["strike"]: s.get("open_interest", 0) for s in calls}
        put_oi  = {s["strike"]: s.get("open_interest", 0) for s in puts}
        test_prices = sorted(set(call_oi) | set(put_oi))
        if len(test_prices) >= 3:
            min_pain, min_val = None, float("inf")
            for p in test_prices:
                cv = sum(max(0.0, p - K) * oi for K, oi in call_oi.items())
                pv = sum(max(0.0, K - p)  * oi for K, oi in put_oi.items())
                total = cv + pv
                if total < min_val:
                    min_val = total
                    min_pain = p
            result["max_pain_strike"] = round(min_pain, 2)
    except Exception:
        pass

    # ── Feature 4: oi_concentration ───────────────────────────────────────────
    # % of total OI within ±2 strikes of ATM; requires OI + spot.
    try:
        if snap_spot:
            all_k = sorted({s["strike"] for s in strikes})
            if len(all_k) >= 3:
                atm_i  = min(range(len(all_k)), key=lambda i: abs(all_k[i] - snap_spot))
                lo, hi = max(0, atm_i - 2), min(len(all_k) - 1, atm_i + 2)
                band   = set(all_k[lo:hi + 1])
                tot_oi  = sum(s.get("open_interest", 0) for s in strikes)
                band_oi = sum(s.get("open_interest", 0) for s in strikes if s["strike"] in band)
                if tot_oi > 0:
                    result["oi_concentration"] = round(band_oi / tot_oi * 100, 2)
    except Exception:
        pass

    # ── Feature 5: wings_iv_ratio ─────────────────────────────────────────────
    # 10-delta put IV ÷ ATM IV (≈50-delta); E*TRADE only.
    try:
        all_d = [s for s in strikes if s.get("delta") is not None and (s.get("iv") or 0) > 0]
        pd_list = [s for s in puts if s.get("delta") is not None and (s.get("iv") or 0) > 0]
        if all_d and pd_list:
            p10    = min(pd_list, key=lambda s: abs(abs(s["delta"]) - 0.10))
            atm_s  = min(all_d,   key=lambda s: abs(abs(s["delta"]) - 0.50))
            atm_iv = float(atm_s["iv"])
            if atm_iv > 0:
                result["wings_iv_ratio"] = round(float(p10["iv"]) / atm_iv, 4)
    except Exception:
        pass

    # ── Feature 6: atm_iv, atm_delta, atm_gamma ─────────────────────────────
    # ATM call: nearest to delta=0.50 (E*TRADE) or nearest strike to spot
    try:
        atm_call = None
        cd_with_iv = [s for s in calls if (s.get("iv") or 0) > 0]
        if cd_with_iv:
            if any(s.get("delta") is not None for s in cd_with_iv):
                atm_call = min(
                    [s for s in cd_with_iv if s.get("delta") is not None],
                    key=lambda s: abs(abs(s["delta"]) - 0.50),
                )
            elif snap_spot:
                atm_call = min(cd_with_iv, key=lambda s: abs(s["strike"] - snap_spot))
        if atm_call is not None:
            result["atm_iv"] = round(float(atm_call["iv"]), 4)
            if atm_call.get("delta") is not None:
                result["atm_delta"] = round(float(atm_call["delta"]), 4)
            if atm_call.get("gamma") is not None:
                result["atm_gamma"] = round(float(atm_call["gamma"]), 6)
    except Exception:
        pass

    # ── Feature 7: front_dte ─────────────────────────────────────────────────
    # Days from snapshot collection date to the front expiry stored in record
    try:
        expiry_str = latest.get("expiry") or latest.get("front_expiry")
        collected  = (latest.get("collected_at") or "")[:10]
        if expiry_str and collected:
            from datetime import date as _date
            exp_d  = _date.fromisoformat(str(expiry_str)[:10])
            coll_d = _date.fromisoformat(collected)
            result["front_dte"] = max(0, (exp_d - coll_d).days)
    except Exception:
        pass

    return result


def warmup_data_sources(log=None) -> None:
    """
    Prime yfinance crumb and verify E*TRADE session on the calling thread.

    Call this once at the start of any scheduler job that fetches live market
    data, BEFORE spawning worker threads.  yfinance's crumb is not thread-safe
    under concurrent load — priming it here lets all workers reuse a valid
    crumb.  The E*TRADE call confirms the OAuth session is live so auth
    failures surface immediately rather than mid-scan.
    """
    import logging as _logging
    _log = log or _logging.getLogger(__name__)

    # yfinance crumb warmup
    try:
        import yfinance as _yf
        _yf.Ticker("SPY").fast_info
    except Exception as e:
        _log.warning(f"[warmup] yfinance crumb prime failed: {e}")

    # E*TRADE session warmup
    try:
        et = _et_module()
        if et.is_authenticated():
            et.get_quote("SPY")
            _log.info("[warmup] E*TRADE session confirmed live")
        else:
            _log.warning("[warmup] E*TRADE not authenticated — option chains will fall back to yfinance")
    except Exception as e:
        _log.warning(f"[warmup] E*TRADE session check failed: {e}")
