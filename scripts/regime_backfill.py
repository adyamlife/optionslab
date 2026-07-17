"""
Regime/Trend Classifier — 2-year historical backfill.

Unlike training_data_collector.py (which needs LIVE option chain data and
can therefore only accumulate going forward from now), this model only needs
price history — Yahoo Finance gives 2 years of that for free today. So this
backfill can be built in one shot rather than drip-fed.

Scope (confirmed): WATCHLIST + major indices (SPY, QQQ, ^VIX) + sector ETFs,
all free via yfinance. News/sentiment and political/geopolitical signals are
explicitly NOT included here:
  - News: yfinance's Ticker.news only returns ~10 CURRENT headlines, not a
    historical feed by date — there is no free way to backfill 2 years of
    news. It's captured going forward instead, in training_data_collector.py.
  - Political/geopolitical/sentiment scoring: no free, structured, daily-
    updated source exists; deferred per prior discussion.

Features are computed vectorized (RSI/MACD/ADX/realized-vol over the whole
series at once), not via per-day function calls — same reasoning backtest.py
already uses for its own historical loop. The label is a forward N-day
return bucketed into the SAME trend vocabulary analyze.py/backtest.py use
(Uptrend/Downtrend/Range-bound), via the SAME thresholds (config.rules
SMA_SHORT/SMA_LONG/TREND_BAND_PCT) — so a model trained on this matches the
categories the live rulebook already reasons about.

Output: data/regime_training.csv (one row per ticker per trading day).
"""
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date as _date

from config.watchlist import WATCHLIST
from config import rules
from scripts.market_context import SECTOR_TO_ETF

# Historical FOMC decision dates (last day of each 2-day meeting, ET close)
_FOMC_DATES = {
    _date(2022,1,26), _date(2022,3,16), _date(2022,5,4),  _date(2022,6,15),
    _date(2022,7,27), _date(2022,9,21), _date(2022,11,2), _date(2022,12,14),
    _date(2023,2,1),  _date(2023,3,22), _date(2023,5,3),  _date(2023,6,14),
    _date(2023,7,26), _date(2023,9,20), _date(2023,11,1), _date(2023,12,13),
    _date(2024,1,31), _date(2024,3,20), _date(2024,5,1),  _date(2024,6,12),
    _date(2024,7,31), _date(2024,9,18), _date(2024,11,7), _date(2024,12,18),
    _date(2025,1,29), _date(2025,3,19), _date(2025,5,7),  _date(2025,6,18),
    _date(2025,7,30), _date(2025,9,17), _date(2025,11,5), _date(2025,12,10),
    _date(2026,1,28), _date(2026,3,18), _date(2026,5,6),  _date(2026,6,17),
    _date(2026,7,29), _date(2026,9,16),
}

# BLS CPI release dates (scheduled announcement dates, ET 8:30am)
_CPI_DATES = {
    _date(2022,1,12),  _date(2022,2,10),  _date(2022,3,10),  _date(2022,4,12),
    _date(2022,5,11),  _date(2022,6,10),  _date(2022,7,13),  _date(2022,8,10),
    _date(2022,9,13),  _date(2022,10,13), _date(2022,11,10), _date(2022,12,13),
    _date(2023,1,12),  _date(2023,2,14),  _date(2023,3,14),  _date(2023,4,12),
    _date(2023,5,10),  _date(2023,6,13),  _date(2023,7,12),  _date(2023,8,10),
    _date(2023,9,13),  _date(2023,10,12), _date(2023,11,14), _date(2023,12,12),
    _date(2024,1,11),  _date(2024,2,13),  _date(2024,3,12),  _date(2024,4,10),
    _date(2024,5,15),  _date(2024,6,12),  _date(2024,7,11),  _date(2024,8,14),
    _date(2024,9,11),  _date(2024,10,10), _date(2024,11,13), _date(2024,12,11),
    _date(2025,1,15),  _date(2025,2,12),  _date(2025,3,12),  _date(2025,4,10),
    _date(2025,5,13),  _date(2025,6,11),  _date(2025,7,15),  _date(2025,8,12),
    _date(2025,9,10),  _date(2025,10,9),  _date(2025,11,13), _date(2025,12,10),
    _date(2026,1,14),  _date(2026,2,11),  _date(2026,3,11),  _date(2026,4,14),
    _date(2026,5,12),  _date(2026,6,11),  _date(2026,7,14),
}

_ROOT_INDICES = ["SPY", "QQQ", "^VIX"]
_SECTOR_ETFS = ["XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]

# Long-term thesis watchlist (user-supplied, 50 names). BRK.B rewritten to
# BRK-B — yfinance/Yahoo uses a hyphen for share-class tickers, not a dot.
_THESIS_TICKERS = [
    "NVDA", "MSFT", "AMZN", "GOOGL", "META", "AVGO", "AMD", "TSM", "MU", "ARM",
    "ANET", "PANW", "CRWD", "FTNT", "PLTR", "SNOW", "MDB", "ORCL", "CRM", "NOW",
    "AAPL", "NFLX", "UBER", "SHOP", "MELI", "V", "MA", "BRK-B", "LLY", "NVO",
    "ISRG", "ABBV", "BSX", "GE", "ETN", "PH", "TT", "CAT", "DE", "VRT",
    "CEG", "VST", "NEE", "XOM", "COP", "KLAC", "ASML", "LRCX", "CDNS", "SNPS",
]

FORWARD_DAYS = 10  # label horizon — matches analyze.py's ~weekly-DTE trade horizon


def backfill_tickers() -> list:
    seen, out = set(), []
    for t in list(WATCHLIST) + _ROOT_INDICES + _SECTOR_ETFS + _THESIS_TICKERS:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _rsi_series(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd_trend_series(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist_val = macd_line - signal_line
    return np.where(hist_val > 0, "Bullish", "Bearish")


def _adx_series(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(period).mean()


def _trend_series(close):
    sma_short = close.rolling(rules.SMA_SHORT).mean()
    sma_long = close.rolling(rules.SMA_LONG).mean()
    band = rules.TREND_BAND_PCT
    up = (close > sma_short * (1 + band)) & (sma_short > sma_long)
    down = (close < sma_short * (1 - band)) & (sma_short < sma_long)
    return np.select([up, down], ["Uptrend", "Downtrend"], default="Range-bound")


def _realized_vol_series(close, window=20):
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window).std() * np.sqrt(252)


def _forward_realized_vol_series(close, window=FORWARD_DAYS):
    """Annualized realized vol computed from the NEXT `window` days' returns
    (t+1..t+window), not the trailing window _realized_vol_series() uses.
    Implemented via reverse-rolling-reverse + shift(-1); validated against a
    manual std() on a synthetic series with known per-day returns."""
    log_ret = np.log(close / close.shift(1))
    rev_std = log_ret.iloc[::-1].rolling(window).std().iloc[::-1]
    return rev_std.shift(-1) * np.sqrt(252)


def _fetch_market_context(period="2y"):
    """VIX + SPY + QQQ + IWM close series, fetched once and reused across every
    ticker's feature build. Returns (vix, spy, qqq, iwm) all indexed by date."""
    vix = yf.Ticker("^VIX").history(period=period)["Close"]
    spy = yf.Ticker("SPY").history(period=period)["Close"]
    qqq = yf.Ticker("QQQ").history(period=period)["Close"]
    iwm = yf.Ticker("IWM").history(period=period)["Close"]
    for s in (vix, spy, qqq, iwm):
        s.index = s.index.date
    return vix, spy, qqq, iwm


def _fetch_macro_series(period="2y") -> dict[str, pd.Series]:
    """Backfillable macro series: yields, dollar index, VVIX, VIX3M.
    Returns dict of {field_name: pd.Series indexed by date}.
    FOMC/CPI event flags are backfilled via hardcoded date lists in
    build_ticker_features(); vix_term_slope is computed there from VIX+VIX3M."""
    result = {
        "yield_10y": None, "yield_3m": None, "yield_curve": None, "dollar_index": None,
        "vvix": None, "vix_3m": None,
    }
    try:
        data = yf.download(["^TNX", "^IRX", "DX-Y.NYB", "^VVIX", "^VIX3M"],
                           period=period, auto_adjust=True, progress=False)
        close = data["Close"] if "Close" in data.columns else data

        def _series(sym):
            try:
                s = close[sym].dropna()
                s.index = s.index.date
                return s
            except Exception:
                return None

        tnx  = _series("^TNX")
        irx  = _series("^IRX")
        dxy  = _series("DX-Y.NYB")
        vvix = _series("^VVIX")
        v3m  = _series("^VIX3M")

        result["yield_10y"]    = tnx
        result["yield_3m"]     = irx
        result["dollar_index"] = dxy
        result["vvix"]         = vvix
        result["vix_3m"]       = v3m
        if tnx is not None and irx is not None:
            aligned_irx = irx.reindex(tnx.index)
            result["yield_curve"] = (tnx - aligned_irx).round(3)
    except Exception:
        pass
    return result


def _fetch_cross_asset_series(period: str = "2y") -> dict:
    """
    Cross-asset context series fetched once per full backfill.
    Returns dict with date-indexed pd.Series:
      credit_spread_proxy  — HYG / LQD price ratio (higher = tighter spreads = bullish)
      spx_above_200        — 1 if SPY > 200-day SMA, else 0
      spy_above_50ma       — 1 if SPY > 50-day SMA, else 0  (shorter-term breadth)
      bond_vol_proxy       — TLT 20-day realized vol annualized (MOVE index proxy)
      qqq_rs               — QQQ / SPY ratio (growth vs. value relative strength)
    """
    result: dict = {
        "credit_spread_proxy": None,
        "spx_above_200":       None,
        "spy_above_50ma":      None,
        "bond_vol_proxy":      None,
        "qqq_rs":              None,
    }
    try:
        data = yf.download(["HYG", "LQD", "SPY", "TLT", "QQQ"], period=period,
                           auto_adjust=True, progress=False)
        close = data["Close"] if "Close" in data.columns else data

        def _s(sym):
            try:
                s = close[sym].dropna()
                s.index = s.index.date
                return s
            except Exception:
                return None

        hyg = _s("HYG")
        lqd = _s("LQD")
        spy = _s("SPY")
        tlt = _s("TLT")
        qqq = _s("QQQ")

        if hyg is not None and lqd is not None:
            lqd_al = lqd.reindex(hyg.index).ffill()
            result["credit_spread_proxy"] = (hyg / lqd_al.replace(0, np.nan)).round(4)

        if spy is not None:
            ma200 = spy.rolling(200, min_periods=100).mean()
            result["spx_above_200"] = (spy > ma200).astype(float)
            result["spx_above_200"].index = spy.index
            ma50 = spy.rolling(50, min_periods=25).mean()
            result["spy_above_50ma"] = (spy > ma50).astype(float)

        if tlt is not None:
            log_rets = np.log(tlt / tlt.shift(1))
            result["bond_vol_proxy"] = log_rets.rolling(20, min_periods=10).std() * np.sqrt(252)

        if qqq is not None and spy is not None:
            spy_al = spy.reindex(qqq.index).ffill()
            result["qqq_rs"] = (qqq / spy_al.replace(0, np.nan)).round(4)
    except Exception:
        pass
    return result


def _earnings_series(ticker: str, dates) -> np.ndarray:
    """1.0 if a known earnings date falls within the next FORWARD_DAYS trading
    days (~14 calendar days) from each row's date. Uses yfinance earnings_dates
    which typically covers ~2 years of history — enough for the backfill window."""
    try:
        earn_df = yf.Ticker(ticker).earnings_dates
        if earn_df is None or earn_df.empty:
            return np.zeros(len(dates), dtype=float)
        earn_date_set = set(earn_df.index.date)
        cal_window = FORWARD_DAYS + 4   # 10 trading days ≈ 14 calendar days
        return np.array([
            1.0 if any(0 < (e - d).days <= cal_window for e in earn_date_set) else 0.0
            for d in dates
        ], dtype=float)
    except Exception:
        return np.full(len(dates), np.nan)


def _event_within_window_series(event_dates: set, dates, cal_window: int = 14) -> np.ndarray:
    """1.0 if any event_date falls strictly within the next cal_window calendar
    days from each row date. Used for FOMC and CPI flags in the backfill."""
    return np.array([
        1.0 if any(0 < (e - d).days <= cal_window for e in event_dates) else 0.0
        for d in dates
    ], dtype=float)


def _rel_strength_series(close, spy_close: pd.Series, window=FORWARD_DAYS):
    """Ticker's own N-day return minus SPY's same-period return, aligned by
    date — positive means outperforming the broad market, not just 'up'."""
    own_ret = close.pct_change(window)
    own_ret.index = close.index.date if hasattr(close.index, "date") else close.index
    spy_ret = spy_close.pct_change(window)
    aligned_spy_ret = spy_ret.reindex(own_ret.index)
    return (own_ret.values - aligned_spy_ret.values)


def _beta_series(close, spy_close: pd.Series, window=60) -> np.ndarray:
    """Rolling 60-day beta vs SPY (covariance / SPY variance of log returns)."""
    own_log = np.log(close / close.shift(1))
    own_log.index = close.index.date if hasattr(close.index, "date") else close.index
    spy_log = np.log(spy_close / spy_close.shift(1))
    spy_aligned = spy_log.reindex(own_log.index)
    cov = own_log.rolling(window).cov(spy_aligned)
    var = spy_aligned.rolling(window).var()
    beta = (cov / var.replace(0, np.nan)).round(3)
    return beta.values


def _atr_pct_series(high, low, close, period=14) -> np.ndarray:
    """ATR(14) as % of close price — daily strike-sizing signal."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    atr_pct = (atr / close.replace(0, np.nan) * 100).round(3)
    return atr_pct.values


def _iv_rank_52w_series(hv20: pd.Series, window=252) -> np.ndarray:
    """Rolling 52-week IV rank: percentile of each day's HV20 vs its trailing
    `window`-day distribution. Returns 0–100 (higher = IV historically elevated).
    Same formula as data_fetch.get_iv_rank_52w(), but vectorized over the full
    price series so it can be backfilled without per-day API calls."""
    def _rank(x):
        if len(x) < 2:
            return np.nan
        return float((x[:-1] < x[-1]).mean() * 100)
    return hv20.rolling(window, min_periods=60).apply(_rank, raw=True).values


def _vix_rank_series(vix_close: pd.Series, window: int = 252) -> np.ndarray:
    """Rolling 52-week percentile rank of VIX close — market-wide IV rank.
    High rank = vol elevated (mean-revert lower / contraction likely).
    Low rank = vol suppressed (expansion risk / complacency)."""
    def _rank(x):
        if len(x) < 2:
            return np.nan
        return float((x[:-1] < x[-1]).mean() * 100)
    return vix_close.rolling(window, min_periods=60).apply(_rank, raw=True).values


def _forward_iv_rank_series(hv20: pd.Series, window: int = 252, forward: int = FORWARD_DAYS) -> np.ndarray:
    """HV20-based IV rank shifted FORWARD_DAYS into the future — target for the
    IV Direction Model. forward_iv_rank[t] = iv_rank_52w[t + forward]."""
    iv_rank = pd.Series(_iv_rank_52w_series(hv20), index=hv20.index)
    return iv_rank.shift(-forward).values


def _index_rsi_aligned(index_close: pd.Series, dates) -> np.ndarray:
    """RSI(14) of an index series aligned to ticker dates."""
    delta = index_close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, float("nan"))
    rsi   = 100 - 100 / (1 + rs)
    rsi.index = index_close.index
    return pd.Series(dates).map(rsi.to_dict()).values


def _index_trend_aligned(index_close: pd.Series, dates) -> np.ndarray:
    """SMA-based trend label for an index aligned to ticker dates."""
    sma_s = index_close.rolling(rules.SMA_SHORT).mean()
    sma_l = index_close.rolling(rules.SMA_LONG).mean()
    band  = rules.TREND_BAND_PCT
    pct   = (sma_s - sma_l) / sma_l.replace(0, float("nan"))
    trend = np.where(pct > band, "Uptrend", np.where(pct < -band, "Downtrend", "Range-bound"))
    trend_series = pd.Series(trend, index=index_close.index)
    trend_series.index = index_close.index
    return pd.Series(dates).map(trend_series.to_dict()).values


def _fetch_short_interest(ticker: str) -> float | None:
    """Current short interest as % of float. Slow-moving (updated bi-monthly)
    so applying today's value to historical rows is a reasonable approximation."""
    try:
        from scripts.data_fetch import get_short_interest as _gsi
        return _gsi(yf.Ticker(ticker))
    except Exception:
        return None


def _sector_etf_for(ticker: str) -> str | None:
    """Return the sector ETF symbol for a ticker, or None if unavailable."""
    try:
        sector = yf.Ticker(ticker).info.get("sector", "")
        return SECTOR_TO_ETF.get(sector)
    except Exception:
        return None


def build_ticker_features(ticker: str, period="2y", vix_close: pd.Series = None, spy_close: pd.Series = None,
                           qqq_close: pd.Series = None, iwm_close: pd.Series = None,
                           macro_series: dict | None = None,
                           cross_asset: dict | None = None) -> pd.DataFrame:
    """One row per trading day: features as of that day + the forward-looking
    regime label (requires FORWARD_DAYS of future data, so the most recent
    FORWARD_DAYS rows are unlabeled and dropped)."""
    hist = yf.Ticker(ticker).history(period=period)
    if hist.empty or len(hist) < rules.SMA_LONG + FORWARD_DAYS + 5:
        return pd.DataFrame()

    close, high, low = hist["Close"], hist["High"], hist["Low"]
    dates = hist.index.date

    if vix_close is None or spy_close is None:
        vix_close, spy_close, qqq_close, iwm_close = _fetch_market_context(period)

    vix_aligned = pd.Series(dates).map(vix_close.to_dict()).values
    rel_strength = _rel_strength_series(close, spy_close)

    # VIX rank (52-week percentile of VIX) — market-wide IV rank feature
    _vix_rank_arr = _vix_rank_series(vix_close)
    _vix_rank_indexed = pd.Series(_vix_rank_arr, index=vix_close.index)
    vix_rank_aligned = pd.Series(dates).map(_vix_rank_indexed.to_dict()).values

    # VIX term structure from macro_series (fetched once per build, not per ticker)
    _vix_3m_s = macro_series.get("vix_3m") if macro_series else None
    _vvix_s   = macro_series.get("vvix")   if macro_series else None
    if _vix_3m_s is not None:
        vix_3m_aligned = pd.Series(dates).map(_vix_3m_s.to_dict()).values.astype(float)
        vix_term_slope_aligned = np.where(
            vix_3m_aligned > 0,
            np.array(vix_aligned, dtype=float) / vix_3m_aligned,
            np.nan,
        ).round(4)
    else:
        vix_3m_aligned        = np.full(len(dates), np.nan)
        vix_term_slope_aligned = np.full(len(dates), np.nan)
    vvix_aligned = (
        pd.Series(dates).map(_vvix_s.to_dict()).values.astype(float)
        if _vvix_s is not None else np.full(len(dates), np.nan)
    )

    # Earnings flag (backfillable via yfinance earnings_dates ~2yr history)
    earnings_flag = _earnings_series(ticker, dates)

    # FOMC / CPI event flags (backfillable via hardcoded date lists)
    _cal_window = FORWARD_DAYS + 4   # 10 trading days ≈ 14 calendar days
    fed_flag = _event_within_window_series(_FOMC_DATES, dates, _cal_window)
    cpi_flag = _event_within_window_series(_CPI_DATES,  dates, _cal_window)

    # Tier 2 sector context — price-derived fields are backfillable
    sector_etf_sym = _sector_etf_for(ticker)
    sector_close = None
    if sector_etf_sym:
        try:
            sector_hist = yf.Ticker(sector_etf_sym).history(period=period)
            if not sector_hist.empty:
                sector_close = sector_hist["Close"]
                sector_close.index = sector_close.index.date
        except Exception:
            pass

    _hv20_series = _realized_vol_series(close, 20)
    _iv_rank_arr = _iv_rank_52w_series(_hv20_series)
    _fwd_iv_rank_arr = _forward_iv_rank_series(_hv20_series)
    _iv_expanding_arr = np.where(
        ~np.isnan(_iv_rank_arr) & ~np.isnan(_fwd_iv_rank_arr),
        (_fwd_iv_rank_arr > _iv_rank_arr).astype(float),
        np.nan,
    )

    # GARCH(1,1) per-day conditional variance (annualized vol) — computed inline
    # from the close series without requiring a saved model file, since we have
    # the full historical series here. Uses arch if available; falls back to NaN.
    _garch_series = np.full(len(dates), np.nan)
    try:
        from arch import arch_model as _arch_model
        _ret_pct = np.log(close / close.shift(1)).dropna() * 100
        _garch_res = _arch_model(_ret_pct, vol="Garch", p=1, q=1, dist="normal", rescale=False).fit(
            disp="off", show_warning=False
        )
        _cond_var_pct_sq = _garch_res.conditional_volatility ** 2  # (% units)^2
        _cond_vol_ann = np.sqrt(_cond_var_pct_sq / 1e4 * 252)
        # Align to dates (conditional_volatility index matches ret, which is one shorter)
        _cv_dict = dict(zip(_cond_vol_ann.index.date, _cond_vol_ann.values))
        _garch_series = np.array([_cv_dict.get(d, np.nan) for d in dates])
    except Exception:
        pass

    df = pd.DataFrame({
        "ticker": ticker,
        "date": dates,
        "close": close.values,
        "rsi": _rsi_series(close).values,
        "macd_trend": _macd_trend_series(close),
        "adx": _adx_series(high, low, close).values,
        "trend": _trend_series(close),
        "hv20": _hv20_series.values,
        "vix_close": vix_aligned,
        "vix_rank": vix_rank_aligned,
        "rel_strength_spy": rel_strength,
        "beta_60d":    _beta_series(close, spy_close, window=60),
        "atr_pct":     _atr_pct_series(high, low, close, period=14),
        # Tier 1 — price-derived (backfillable)
        "iv_rank_52w": _iv_rank_arr,
        # Tier 1 — options-derived (NOT backfillable); filled live by _build_today_row()
        "vol_oi_ratio":  np.nan,
        "iv_skew":       np.nan,
        "iv_term_slope": np.nan,
        "otm_pcr":       np.nan,
        # Tier 2 — index trends and RSI (price-derived → backfillable)
        "spy_rsi":   _index_rsi_aligned(spy_close, dates),
        "spy_trend": _index_trend_aligned(spy_close, dates),
        "qqq_rsi":   _index_rsi_aligned(qqq_close, dates) if qqq_close is not None else np.nan,
        "qqq_trend": _index_trend_aligned(qqq_close, dates) if qqq_close is not None else np.nan,
        "iwm_rsi":   _index_rsi_aligned(iwm_close, dates) if iwm_close is not None else np.nan,
        "iwm_trend": _index_trend_aligned(iwm_close, dates) if iwm_close is not None else np.nan,
        # Tier 2 — sector (trend/RSI backfillable; IV ratio requires live chain → NaN)
        "sector_etf":      sector_etf_sym or np.nan,
        "sector_trend":    _index_trend_aligned(sector_close, dates) if sector_close is not None else np.nan,
        "sector_rsi":      _index_rsi_aligned(sector_close, dates)   if sector_close is not None else np.nan,
        "sector_iv_ratio": np.nan,
        # Tier 2 — VIX context (backfilled from ^VVIX and ^VIX3M via yfinance)
        "vvix":           vvix_aligned,
        "vix_3m":         vix_3m_aligned,
        "vix_term_slope": vix_term_slope_aligned,
        # Tier 3 — structural stock characteristics (slow-moving; use current value)
        "short_interest_pct": _fetch_short_interest(ticker),
        # Tier 3 — earnings flag backfilled via yfinance; news/analyst not backfillable
        "earnings_inside_expiry": earnings_flag,
        "news_sentiment_score":   np.nan,
        "analyst_rec_change":     np.nan,
        # Tier 5 — macro context
        "yield_10y":     pd.Series(dates).map(macro_series["yield_10y"].to_dict()).values
                         if macro_series and macro_series.get("yield_10y") is not None else np.nan,
        "yield_3m":      pd.Series(dates).map(macro_series["yield_3m"].to_dict()).values
                         if macro_series and macro_series.get("yield_3m") is not None else np.nan,
        "yield_curve":   pd.Series(dates).map(macro_series["yield_curve"].to_dict()).values
                         if macro_series and macro_series.get("yield_curve") is not None else np.nan,
        "dollar_index":  pd.Series(dates).map(macro_series["dollar_index"].to_dict()).values
                         if macro_series and macro_series.get("dollar_index") is not None else np.nan,
        "fed_within_dte": fed_flag,   # backfilled via hardcoded FOMC date list
        "cpi_within_dte": cpi_flag,   # backfilled via hardcoded CPI date list
        # GARCH(1,1) per-day conditional vol (annualized) — superior to HV20 as vol feature
        "garch_conditional_var": _garch_series,
        # Tier 4 — chain-snapshot-derived (NOT backfillable; chain collection started ~Jun 26)
        "iv_skew_20d":      np.nan,
        "gex_proxy":        np.nan,
        "max_pain_strike":  np.nan,
        "oi_concentration": np.nan,
        "wings_iv_ratio":   np.nan,
        # Cross-asset context (fetched once per full backfill run)
        "credit_spread_proxy": (
            pd.Series(dates).map(cross_asset["credit_spread_proxy"].to_dict()).values
            if cross_asset and cross_asset.get("credit_spread_proxy") is not None else np.nan
        ),
        "spx_above_200": (
            pd.Series(dates).map(cross_asset["spx_above_200"].to_dict()).values
            if cross_asset and cross_asset.get("spx_above_200") is not None else np.nan
        ),
        "spy_above_50ma": (
            pd.Series(dates).map(cross_asset["spy_above_50ma"].to_dict()).values
            if cross_asset and cross_asset.get("spy_above_50ma") is not None else np.nan
        ),
        "bond_vol_proxy": (
            pd.Series(dates).map(cross_asset["bond_vol_proxy"].to_dict()).values
            if cross_asset and cross_asset.get("bond_vol_proxy") is not None else np.nan
        ),
        "qqq_rs": (
            pd.Series(dates).map(cross_asset["qqq_rs"].to_dict()).values
            if cross_asset and cross_asset.get("qqq_rs") is not None else np.nan
        ),
    })

    future_close = close.shift(-FORWARD_DAYS)
    fwd_return = (future_close - close) / close
    # 4-class regime labels for options strategy selection.
    # NOTE: TREND_BAND_PCT (0.5%) is the SMA trend detection threshold — far too
    # small for 10-day forward return classification (93% of rows would be Trending).
    # Use a separate 5% threshold calibrated to 10-day price movement distributions.
    _TREND_RETURN_THRESHOLD = 0.05   # |10-day return| > 5% → Trending (~35-45% of rows)
    #   Trending        — large directional move (|return| > 5%)
    #   High-vol-breakout — non-trending but chaotic (forward HV > 28% annualized)
    #   Low-vol-squeeze   — non-trending and calm (forward HV < 15% annualized)
    #   Mean-reverting    — oscillating, moderate vol
    _fhv = _forward_realized_vol_series(close).values
    _abs_ret = np.abs(fwd_return.values)
    label = np.where(
        _abs_ret > _TREND_RETURN_THRESHOLD, "Trending",
        np.where(_fhv > 0.28,              "High-vol-breakout",
        np.where(_fhv < 0.15,              "Low-vol-squeeze",
                                           "Mean-reverting"))
    )
    df["forward_return"] = fwd_return.values
    df["regime_label"] = label
    df["forward_hv"] = _fhv
    df["forward_iv_rank"] = _fwd_iv_rank_arr
    df["iv_expanding"] = _iv_expanding_arr
    df["labeled"] = True  # full backfill only ever keeps rows with a real forward label

    # Drop rows with no indicator history yet, or no forward label yet
    df = df.iloc[max(rules.SMA_LONG, 26, 14):]
    df = df.iloc[: -FORWARD_DAYS] if FORWARD_DAYS else df
    return df.dropna(subset=["rsi", "adx", "hv20", "forward_return", "vix_close", "rel_strength_spy", "forward_hv"])


def build_regime_dataset(period="2y", out_path=None) -> dict:
    from scripts.db import connect, TABLE, table_exists
    from datetime import date, timedelta

    # Extend period so we never drop history that's already in the table.
    # Each month we wait, a "2y" window shifts forward and would silently lose
    # the oldest month.  Check the earliest date present and stretch period if needed.
    try:
        with connect() as _con:
            if table_exists(_con, TABLE):
                _row = _con.execute(f"SELECT MIN(date) FROM {TABLE}").fetchone()
                if _row and _row[0]:
                    earliest = _row[0]  # datetime.date or string
                    if isinstance(earliest, str):
                        from datetime import datetime
                        earliest = datetime.strptime(earliest, "%Y-%m-%d").date()
                    days_needed = (date.today() - earliest).days + 30  # 30-day buffer
                    needed_years = days_needed / 365.25
                    # Parse current period (e.g. "2y", "3y") and use the larger
                    current_years = float(period.rstrip("ymd")) if period.endswith("y") else 2.0
                    if needed_years > current_years:
                        period = f"{int(needed_years) + 1}y"
                        import logging as _log
                        _log.getLogger(__name__).info(
                            "build_regime_dataset: extending period to %s to preserve "
                            "existing history back to %s", period, earliest
                        )
    except Exception:
        pass  # if DB doesn't exist yet, use the default period as-is

    vix_close, spy_close, qqq_close, iwm_close = _fetch_market_context(period)
    macro_series  = _fetch_macro_series(period)
    cross_asset   = _fetch_cross_asset_series(period)
    frames, errors = [], []
    for ticker in backfill_tickers():
        try:
            df = build_ticker_features(ticker, period=period, vix_close=vix_close, spy_close=spy_close,
                                       qqq_close=qqq_close, iwm_close=iwm_close,
                                       macro_series=macro_series, cross_asset=cross_asset)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

    if not frames:
        return {"ok": False, "rows": 0, "errors": errors}

    full = pd.concat(frames, ignore_index=True)
    with connect() as con:
        con.execute(f"DROP TABLE IF EXISTS {TABLE}_new")
        con.register("backfill_df", full)
        con.execute(f"CREATE TABLE {TABLE}_new AS SELECT * FROM backfill_df")
        con.execute(f"DROP TABLE IF EXISTS {TABLE}")
        con.execute(f"ALTER TABLE {TABLE}_new RENAME TO {TABLE}")
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_ticker_date ON {TABLE} (ticker, date)")
        con.commit()
    return {
        "ok": True,
        "rows": len(full),
        "tickers": full["ticker"].nunique(),
        "label_counts": full["regime_label"].value_counts().to_dict(),
        "errors": errors,
        "out_path": "data/ml_training.duckdb",
    }


# ── Daily incremental maintenance (EOD) ───────────────────────────────────────
#
# The one-shot build_regime_dataset() above re-pulls and recomputes the whole
# 2yr history — fine once, wasteful daily. These two functions keep the CSV
# current after that:
#   - update_regime_dataset(): each EOD, append ONE new feature row per
#     ticker for today (regime_label/forward_return unknown yet — needs
#     FORWARD_DAYS of future price that doesn't exist yet, same constraint
#     training_data_collector.py's snapshots have).
#   - label_pending_regime_rows(): each EOD, fill in the label for rows from
#     FORWARD_DAYS+ trading days ago, now that the future price they need
#     actually exists.

def _garch_live_vol(ticker: str, hist: pd.DataFrame) -> float | None:
    """Compute GARCH(1,1) 1-step-ahead conditional vol (annualized) from hist.
    First checks for a saved model artifact; falls back to quick in-place fit.
    Returns None if arch is not installed."""
    try:
        from scripts.train_garch_model import get_garch_forecast as _saved_fc
        saved = _saved_fc(ticker)
        if saved is not None:
            return saved
    except Exception:
        pass
    try:
        from arch import arch_model as _arch_model
        ret_pct = np.log(hist["Close"] / hist["Close"].shift(1)).dropna() * 100
        if len(ret_pct) < 30:
            return None
        res = _arch_model(ret_pct, vol="Garch", p=1, q=1, dist="normal", rescale=False).fit(
            disp="off", show_warning=False
        )
        fc = res.forecast(horizon=1, reindex=False)
        next_var_pct_sq = float(fc.variance.iloc[-1, 0])
        return float(np.sqrt(next_var_pct_sq / 1e4 * 252))
    except Exception:
        return None


def _build_today_row(ticker: str, lookback="6mo", vix_close: pd.Series = None, spy_close: pd.Series = None,
                     qqq_close: pd.Series = None, iwm_close: pd.Series = None,
                     vix_ctx: dict | None = None, macro_ctx: dict | None = None,
                     cross_asset: dict | None = None) -> dict | None:
    """One unlabeled feature row for today. lookback=6mo gives enough
    history for SMA_LONG/ADX warmup without re-pulling the full 2yr."""
    hist = yf.Ticker(ticker).history(period=lookback)
    if hist.empty or len(hist) < rules.SMA_LONG + 5:
        return None
    close, high, low = hist["Close"], hist["High"], hist["Low"]
    rsi = _rsi_series(close)
    adx = _adx_series(high, low, close)
    hv20 = _realized_vol_series(close, 20)
    trend = _trend_series(close)
    macd_trend = _macd_trend_series(close)
    if pd.isna(rsi.iloc[-1]) or pd.isna(adx.iloc[-1]) or pd.isna(hv20.iloc[-1]):
        return None

    if vix_close is None or spy_close is None:
        vix_close, spy_close, qqq_close, iwm_close = _fetch_market_context(lookback)
    today_date = hist.index[-1].date()
    vix_today = vix_close.get(today_date)
    rel_strength_today = _rel_strength_series(close, spy_close)[-1]
    if vix_today is None or pd.isna(rel_strength_today):
        return None

    beta_arr    = _beta_series(close, spy_close, window=60)
    atr_arr     = _atr_pct_series(high, low, close, period=14)
    iv_rank_arr = _iv_rank_52w_series(hv20)
    beta_today     = float(beta_arr[-1])    if not np.isnan(beta_arr[-1])    else None
    atr_today      = float(atr_arr[-1])     if not np.isnan(atr_arr[-1])     else None
    iv_rank_today  = float(iv_rank_arr[-1]) if not np.isnan(iv_rank_arr[-1]) else None

    # VIX rank — market-wide IV rank (live-computable from the vix_close series)
    _vix_rank_live = _vix_rank_series(vix_close)
    vix_rank_today = float(_vix_rank_live[-1]) if len(_vix_rank_live) and not np.isnan(_vix_rank_live[-1]) else None

    dates = [d.date() for d in hist.index]

    spy_rsi_arr   = _index_rsi_aligned(spy_close,  dates) if spy_close  is not None else [None] * len(dates)
    spy_trend_arr = _index_trend_aligned(spy_close, dates) if spy_close  is not None else [None] * len(dates)
    qqq_rsi_arr   = _index_rsi_aligned(qqq_close,  dates) if qqq_close  is not None else [None] * len(dates)
    qqq_trend_arr = _index_trend_aligned(qqq_close, dates) if qqq_close  is not None else [None] * len(dates)
    iwm_rsi_arr   = _index_rsi_aligned(iwm_close,  dates) if iwm_close  is not None else [None] * len(dates)
    iwm_trend_arr = _index_trend_aligned(iwm_close, dates) if iwm_close  is not None else [None] * len(dates)

    def _last(arr):
        v = arr[-1]
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else v

    # ── Live Tier 2: sector context (per-ticker) ──────────────────────────────
    sector_etf_today = sector_trend_today = sector_rsi_today = sector_iv_ratio_today = None
    try:
        from scripts.data_fetch import get_sector_context as _get_sector_ctx
        spot_for_sector = float(close.iloc[-1])
        atm_iv_approx = float(hv20.iloc[-1])   # HV20 as rough IV proxy for sector_iv_ratio
        sctx = _get_sector_ctx(ticker, atm_iv_approx)
        sector_etf_today    = sctx.get("sector_etf")
        sector_trend_today  = sctx.get("sector_trend")
        sector_rsi_today    = sctx.get("sector_rsi")
        sector_iv_ratio_today = sctx.get("sector_iv_ratio")
    except Exception:
        pass

    # ── Live Tier 3: news sentiment, analyst change, short interest ──────────
    news_sentiment_today = analyst_rec_change_today = short_interest_today = None
    try:
        from scripts.data_fetch import get_news_sentiment_score as _gns, get_analyst_rec_change as _garc
        _tkr = yf.Ticker(ticker)
        news_sentiment_today      = _gns(_tkr)
        analyst_rec_change_today  = _garc(_tkr, days=5)
        short_interest_today      = _fetch_short_interest(ticker)
    except Exception:
        pass

    # earnings_inside_expiry for regime: does earnings fall in the next FORWARD_DAYS window?
    earnings_inside_today = None
    try:
        from scripts.analyze import days_to_earnings as _dte
        earn_d = _dte(yf.Ticker(ticker))
        if earn_d is not None:
            earnings_inside_today = int(0 < earn_d <= FORWARD_DAYS)
    except Exception:
        pass

    # ── Live Tier 2: VIX context (once per run, passed in) ───────────────────
    if vix_ctx is None:
        try:
            from scripts.data_fetch import get_vix_context as _get_vix_ctx
            vix_ctx = _get_vix_ctx()
        except Exception:
            vix_ctx = {}

    # ── Live Tier 5: macro context (fetched once per run, passed in) ─────────
    if macro_ctx is None:
        try:
            from scripts.data_fetch import get_macro_context as _get_macro
            _dte = FORWARD_DAYS  # use label horizon as event window
            macro_ctx = _get_macro(dte=_dte)
        except Exception:
            macro_ctx = {}

    # ── Live Tier 4: chain-snapshot-derived features ─────────────────────────
    chain_features = {"iv_skew_20d": None, "gex_proxy": None, "max_pain_strike": None,
                      "oi_concentration": None, "wings_iv_ratio": None}
    try:
        from scripts.data_fetch import compute_chain_features as _ccf
        _spot = float(close.iloc[-1])
        chain_features = _ccf(ticker, spot=_spot)
    except Exception:
        pass

    # ── Live options-derived Tier 1 (NOT backfillable — fetch today's chain) ──
    vol_oi_ratio_today = iv_skew_today = iv_term_slope_today = otm_pcr_today = None
    try:
        from scripts.data_fetch import (
            get_option_chain, get_vol_skew, get_otm_pcr as _get_otm_pcr,
            pick_expiry, pick_back_expiry,
        )
        import yfinance as _yf
        tkr_obj = _yf.Ticker(ticker)
        spot    = float(close.iloc[-1])
        front_exp_result = pick_expiry(tkr_obj, min_dte=7, max_dte=21)
        front_exp = front_exp_result[0] if front_exp_result else None
        if front_exp:
            calls, puts = get_option_chain(tkr_obj, front_exp, spot=spot)
            if calls is not None and not calls.empty and puts is not None and not puts.empty:
                total_oi  = float(calls["openInterest"].fillna(0).sum() + puts["openInterest"].fillna(0).sum())
                total_vol = float(calls["volume"].fillna(0).sum()       + puts["volume"].fillna(0).sum())
                vol_oi_ratio_today = round(total_vol / total_oi, 2) if total_oi > 0 else None
                skew = get_vol_skew(calls, puts, spot)
                if skew:
                    iv_skew_today = skew["skew_pct"]
                otm = _get_otm_pcr(calls, puts, spot)
                if otm:
                    otm_pcr_today = otm["otm_pcr"]
                back_exp, _ = pick_back_expiry(tkr_obj, front_exp)
                if back_exp:
                    back_calls, back_puts = get_option_chain(tkr_obj, back_exp, spot=spot)
                    if back_calls is not None and not back_calls.empty:
                        front_iv = float(calls["impliedVolatility"].dropna().median() or 0)
                        back_iv  = float(back_calls["impliedVolatility"].dropna().median() or 0)
                        if front_iv > 0 and back_iv > 0:
                            iv_term_slope_today = round(front_iv / back_iv, 4)
    except Exception:
        pass

    return {
        "ticker": ticker,
        "date": today_date,
        "close": float(close.iloc[-1]),
        "rsi": float(rsi.iloc[-1]),
        "macd_trend": str(macd_trend[-1]),
        "adx": float(adx.iloc[-1]),
        "trend": str(trend[-1]),
        "hv20": float(hv20.iloc[-1]),
        "vix_close": float(vix_today),
        "rel_strength_spy": float(rel_strength_today),
        "beta_60d":    beta_today,
        "atr_pct":     atr_today,
        # Tier 1 — price-derived (backfillable) or live options-derived
        "iv_rank_52w":   iv_rank_today,
        "vix_rank":      vix_rank_today,
        "vol_oi_ratio":  vol_oi_ratio_today,
        "iv_skew":       iv_skew_today,
        "iv_term_slope": iv_term_slope_today,
        "otm_pcr":       otm_pcr_today,
        # Tier 2 — index trends
        "spy_rsi":   _last(spy_rsi_arr),
        "spy_trend": _last(spy_trend_arr),
        "qqq_rsi":   _last(qqq_rsi_arr),
        "qqq_trend": _last(qqq_trend_arr),
        "iwm_rsi":   _last(iwm_rsi_arr),
        "iwm_trend": _last(iwm_trend_arr),
        # Tier 2 — sector context (live fetch per ticker)
        "sector_etf":      sector_etf_today,
        "sector_trend":    sector_trend_today,
        "sector_rsi":      sector_rsi_today,
        "sector_iv_ratio": sector_iv_ratio_today,
        # Tier 2 — VIX context (live, passed from caller or fetched once)
        "vvix":           vix_ctx.get("vvix"),
        "vix_3m":         vix_ctx.get("vix_3m"),
        "vix_term_slope": vix_ctx.get("vix_term_slope"),
        # Tier 3 — event / sentiment (live per ticker)
        "earnings_inside_expiry": earnings_inside_today,
        "news_sentiment_score":   news_sentiment_today,
        "analyst_rec_change":     analyst_rec_change_today,
        "short_interest_pct":     short_interest_today,
        # Tier 4 — chain-snapshot-derived (read from stored option_chain_snapshots.jsonl)
        "iv_skew_20d":      chain_features.get("iv_skew_20d"),
        "gex_proxy":        chain_features.get("gex_proxy"),
        "max_pain_strike":  chain_features.get("max_pain_strike"),
        "oi_concentration": chain_features.get("oi_concentration"),
        "wings_iv_ratio":   chain_features.get("wings_iv_ratio"),
        # Cross-asset context (passed from caller; look up today's value)
        "credit_spread_proxy": (
            cross_asset["credit_spread_proxy"].get(today_date)
            if cross_asset and cross_asset.get("credit_spread_proxy") is not None else None
        ),
        "spx_above_200": (
            cross_asset["spx_above_200"].get(today_date)
            if cross_asset and cross_asset.get("spx_above_200") is not None else None
        ),
        "spy_above_50ma": (
            cross_asset["spy_above_50ma"].get(today_date)
            if cross_asset and cross_asset.get("spy_above_50ma") is not None else None
        ),
        "bond_vol_proxy": (
            cross_asset["bond_vol_proxy"].get(today_date)
            if cross_asset and cross_asset.get("bond_vol_proxy") is not None else None
        ),
        "qqq_rs": (
            cross_asset["qqq_rs"].get(today_date)
            if cross_asset and cross_asset.get("qqq_rs") is not None else None
        ),
        # Tier 5 — macro context (market-wide, passed from caller)
        "yield_10y":      macro_ctx.get("yield_10y"),
        "yield_3m":       macro_ctx.get("yield_3m"),
        "yield_curve":    macro_ctx.get("yield_curve"),
        "dollar_index":   macro_ctx.get("dollar_index"),
        "fed_within_dte": macro_ctx.get("fed_within_dte"),
        "cpi_within_dte": macro_ctx.get("cpi_within_dte"),
        # GARCH(1,1) live conditional vol for today — used as feature in models
        "garch_conditional_var": _garch_live_vol(ticker, hist),
        "forward_return":  None,
        "regime_label":    None,
        "forward_hv":      None,
        "forward_iv_rank": None,
        "iv_expanding":    None,
        "labeled":         False,
    }


def update_regime_dataset(out_path=None) -> dict:
    """Append today's unlabeled feature row per ticker, skipping tickers
    that already have a row for today (e.g. if run more than once)."""
    from scripts.db import read_df, append_df, table_exists, TABLE

    if table_exists():
        existing_pairs = read_df(f"SELECT ticker, CAST(date AS VARCHAR) AS date FROM {TABLE}")
        existing_dates = set(zip(existing_pairs["ticker"], existing_pairs["date"]))
    else:
        existing_dates = set()

    vix_close, spy_close, qqq_close, iwm_close = _fetch_market_context("6mo")
    cross_asset = _fetch_cross_asset_series("6mo")
    try:
        from scripts.data_fetch import get_vix_context as _get_vix_ctx
        vix_ctx = _get_vix_ctx()
    except Exception:
        vix_ctx = {}
    try:
        from scripts.data_fetch import get_macro_context as _get_macro
        macro_ctx = _get_macro(dte=FORWARD_DAYS)
    except Exception:
        macro_ctx = {}
    new_rows, errors = [], []
    for ticker in backfill_tickers():
        try:
            row = _build_today_row(ticker, vix_close=vix_close, spy_close=spy_close,
                                   qqq_close=qqq_close, iwm_close=iwm_close,
                                   vix_ctx=vix_ctx, macro_ctx=macro_ctx,
                                   cross_asset=cross_asset)
            if row is None:
                continue
            if (ticker, str(row["date"])) in existing_dates:
                continue
            new_rows.append(row)
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

    if not new_rows:
        return {"ok": True, "appended": 0, "errors": errors}

    append_df(pd.DataFrame(new_rows))
    return {"ok": True, "appended": len(new_rows), "errors": errors}


def label_pending_regime_rows(out_path=None) -> dict:
    """Fill in regime_label/forward_return for rows old enough that the
    future price they need now exists. Re-fetches a short recent history per
    ticker (cheap) rather than the full backfill."""
    from datetime import date as _date
    from scripts.db import read_df, upsert_df, table_exists, TABLE

    if not table_exists():
        return {"ok": True, "labeled": 0, "total_rows": 0}

    df = read_df()
    if "labeled" not in df.columns:
        return {"ok": False, "error": "DB missing 'labeled' column — rebuild via build_regime_dataset"}

    df["labeled"] = df["labeled"].astype(bool)
    # Force object/float dtype up front — if these columns are all-NaN on
    # load, pandas infers float64, and assigning a string label later raises
    # LossySetitemError.
    df["regime_label"] = df["regime_label"].astype(object)
    df["forward_return"] = df["forward_return"].astype(float)
    if "forward_hv" not in df.columns:
        df["forward_hv"] = np.nan
    df["forward_hv"] = df["forward_hv"].astype(float)
    if "forward_iv_rank" not in df.columns:
        df["forward_iv_rank"] = np.nan
    if "iv_expanding" not in df.columns:
        df["iv_expanding"] = np.nan
    pending = df[~df["labeled"]]
    if pending.empty:
        return {"ok": True, "labeled": 0, "total_rows": len(df)}

    today = _date.today()
    band = rules.TREND_BAND_PCT
    labeled_count = 0
    price_cache = {}

    for ticker in pending["ticker"].unique():
        try:
            hist = yf.Ticker(ticker).history(period="3mo")
        except Exception:
            continue
        if hist.empty:
            continue
        price_cache[ticker] = hist["Close"]

    for idx, row in pending.iterrows():
        ticker = row["ticker"]
        if ticker not in price_cache:
            continue
        try:
            row_date = pd.to_datetime(row["date"]).date()
        except Exception:
            continue
        trading_days_elapsed = (price_cache[ticker].index.tz_localize(None).date >= row_date).sum()
        # need at least FORWARD_DAYS trading days strictly after row_date
        future_closes = price_cache[ticker][price_cache[ticker].index.tz_localize(None).date > row_date]
        if len(future_closes) < FORWARD_DAYS:
            if (today - row_date).days < FORWARD_DAYS * 2:
                continue  # not enough calendar time has passed yet — try again later
            continue  # stale/illiquid ticker gap — leave pending rather than mislabel
        future_close = float(future_closes.iloc[FORWARD_DAYS - 1])
        fwd_return = (future_close - row["close"]) / row["close"]
        price_seq = np.array([row["close"]] + future_closes.iloc[:FORWARD_DAYS].tolist())
        log_rets = np.diff(np.log(price_seq))
        forward_hv = float(np.std(log_rets) * np.sqrt(252))
        # 4-class labels (mirrors build_ticker_features label logic)
        # Use 5% threshold — same as _TREND_RETURN_THRESHOLD in build_ticker_features
        if abs(fwd_return) > 0.05:
            regime_label = "Trending"
        elif forward_hv > 0.28:
            regime_label = "High-vol-breakout"
        elif forward_hv < 0.15:
            regime_label = "Low-vol-squeeze"
        else:
            regime_label = "Mean-reverting"

        df.loc[idx, "forward_return"] = fwd_return
        df.loc[idx, "regime_label"] = regime_label
        df.loc[idx, "forward_hv"] = forward_hv
        df.loc[idx, "labeled"] = True
        labeled_count += 1

    if labeled_count > 0:
        upsert_df(df[df["labeled"].astype(bool)].copy())
    return {"ok": True, "labeled": labeled_count, "total_rows": len(df)}


def build_hmm_labels(df: pd.DataFrame, n_states: int = 3) -> pd.Series:
    """
    Fit GaussianHMM on (log_return, hv20, vix_close) and map hidden states to
    Bull / Bear / Sideways based on each state's mean forward return.

    Alternative to the SMA-based regime_label — HMM learns data-driven market
    regimes without hand-crafted SMA thresholds. Call this after build_ticker_features()
    on a combined DataFrame; the resulting series can be stored as `hmm_label`.

    Returns pd.Series of strings ("Bull"/"Bear"/"Sideways") indexed by df.index.
    Falls back to regime_label if hmmlearn is not installed.
    """
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError:
        import warnings
        warnings.warn("hmmlearn not installed — pip install hmmlearn. Falling back to regime_label.")
        return df.get("regime_label", pd.Series(["unknown"] * len(df), index=df.index))

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    has_return = "forward_return" in df.columns and df["forward_return"].notna().any()
    cols_needed = ["hv20", "vix_close"]
    if has_return:
        df["_log_ret"] = np.log1p(
            pd.to_numeric(df["forward_return"], errors="coerce").clip(-0.5, 0.5)
        )
        cols_needed = ["_log_ret"] + cols_needed

    X_raw = pd.DataFrame({
        c: pd.to_numeric(df[c], errors="coerce") for c in cols_needed if c in df.columns
    }).fillna(method="ffill").fillna(0).values

    X_scaled = (X_raw - X_raw.mean(axis=0)) / (X_raw.std(axis=0) + 1e-8)

    try:
        hmm = GaussianHMM(n_components=n_states, covariance_type="full",
                          n_iter=100, random_state=42)
        hmm.fit(X_scaled)
        states = hmm.predict(X_scaled)
    except Exception as e:
        import warnings
        warnings.warn(f"GaussianHMM fit failed: {e}. Returning regime_label.")
        return df.get("regime_label", pd.Series(["unknown"] * len(df), index=df.index))

    if has_return:
        fwd = pd.to_numeric(df["forward_return"], errors="coerce").values
        mean_returns = {s: float(fwd[states == s].mean()) for s in range(n_states) if (states == s).any()}
    else:
        # Without returns, use HV20 proxy: lowest vol = Bull, highest = Bear
        hv20 = pd.to_numeric(df["hv20"], errors="coerce").values
        mean_returns = {s: -float(hv20[states == s].mean()) for s in range(n_states) if (states == s).any()}

    sorted_states = sorted(mean_returns.keys(), key=lambda s: mean_returns[s])
    names = ["Bear", "Sideways", "Bull"] if n_states == 3 else \
            ["Bear"] + [f"State{i}" for i in range(1, n_states - 1)] + ["Bull"]
    state_map = {s: names[i] for i, s in enumerate(sorted_states)}

    result = pd.Series([state_map.get(s, "unknown") for s in states], index=df.index)
    if "_log_ret" in df.columns:
        pass  # temp column on local copy, no cleanup needed
    return result


if __name__ == "__main__":
    import sys
    print("Building regime dataset (2-year backfill)...")
    result = build_regime_dataset()
    if not result.get("ok"):
        print(f"FAILED: {result.get('error')}")
        sys.exit(1)
    print(f"Done. {result['rows']} rows, {result['tickers']} tickers -> {result['out_path']}")
    print(f"Label distribution: {result['label_counts']}")
    if result.get("errors"):
        print(f"Ticker errors ({len(result['errors'])}): {[e['ticker'] for e in result['errors']]}")
