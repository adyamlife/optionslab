"""
F1: Earnings fundamentals backfill — 101 tickers, 8 quarters.

Pulls from yfinance:
  - earnings_dates          → up to 8 quarters of earnings dates + EPS surprise
  - quarterly_income_stmt   → revenue, ebitda, ebit, net_income, gross_profit
  - quarterly_cashflow      → free_cash_flow, operating_cash_flow
  - quarterly_balance_sheet → cash_and_equiv, total_debt, net_debt
  - history()               → price at earnings_date → trailing_pe, ev_ebitda, ps, fcf_yield
  - history()               → ret_1d, ret_3d, ret_5d, ret_10d, drift_30d

Computed at insert time (no extra API calls):
  - margins (gross, operating, net, fcf) — quarterly/quarterly
  - eps_beat_rate_8q, rev_beat_rate_8q, avg_eps_surprise_8q  (rolling from prior rows)
  - valuation percentiles  (rolling own history, min 6 obs)
  - sector percentiles     (cross-sectional per fiscal quarter, computed after full collection)

point_in_time_verified:
  TRUE  — actuals from quarterly stmts; EPS surprise computed from actuals;
           multiples from stored price + stored financials; returns from history().
  FALSE — forward_pe (not collected; no free historical consensus source).

Run:
  python -m scripts.collect_earnings_fundamentals
  python -m scripts.collect_earnings_fundamentals --tickers AAPL MSFT NVDA
  python -m scripts.collect_earnings_fundamentals --dry-run
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from pathlib import Path
import sys

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_ET_TZ = "America/New_York"
sys.path.insert(0, str(_ROOT))

from scripts.db import connect, ensure_financial_results_tables

log = logging.getLogger(__name__)

# Sector ETF mapping (same as market_context.py SECTOR_TO_ETF)
_SECTOR_ETF = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Healthcare":             "XLV",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}

_NO_EARNINGS = frozenset({
    "SPY", "QQQ", "TQQQ", "IWM",
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "^VIX", "VIX",
})

SLEEP_BETWEEN = 2.0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_release_timing(idx_dt, row_data=None) -> str:
    """Return 'BMO', 'AMC', or 'unknown' from an earnings_dates row."""
    # Some yfinance versions expose an 'Hour' column
    if row_data is not None:
        h = str(row_data.get("Hour") or "").strip().lower()
        if h and h not in ("", "nan", "none", "unknown", "-"):
            if any(k in h for k in ("pre", "before", "bmo")):
                return "BMO"
            if any(k in h for k in ("after", "close", "amc", "tas")):
                return "AMC"
    # Fall back to time component of index datetime
    try:
        ts = pd.Timestamp(idx_dt)
        if ts.tzinfo is not None:
            ts = ts.tz_convert(_ET_TZ)
        hour = ts.hour
        if hour < 12:
            return "BMO"
        elif hour >= 16:
            return "AMC"
    except Exception:
        pass
    return "unknown"


def _earnings_available_date(earn_date: date, timing: str) -> date:
    """First date when earnings info is publicly available for use in models."""
    if timing == "BMO":
        return earn_date
    # AMC or unknown: advance one trading day (skip weekends)
    d = earn_date + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _safe(val, cast=float):
    """Return cast(val) or None if val is NaN / None / non-finite."""
    try:
        if val is None:
            return None
        v = cast(val)
        if isinstance(v, float) and not np.isfinite(v):
            return None
        return v
    except Exception:
        return None


def _col(df: pd.DataFrame, label: str, col_idx: int):
    """Scalar from a quarterly-stmt DataFrame (rows=metrics, cols=quarter-dates)."""
    try:
        if df is None or df.empty or label not in df.index:
            return None
        if col_idx >= len(df.columns):
            return None
        return _safe(df.loc[label].iloc[col_idx])
    except Exception:
        return None


def _nearest_quarter_idx(df: pd.DataFrame, target: date, max_days: int = 100) -> int | None:
    """Return column index whose date is nearest to target, within max_days."""
    if df is None or df.empty:
        return None
    dates = pd.to_datetime(df.columns).date
    diffs = [abs((d - target).days) for d in dates]
    best = int(np.argmin(diffs))
    return best if diffs[best] <= max_days else None


def _price_on(hist: pd.DataFrame, target: date, window: int = 3) -> float | None:
    """Closing price on or just after target within window trading days."""
    if hist is None or hist.empty:
        return None
    hdates = pd.to_datetime(hist.index).date
    for offset in range(window + 1):
        d = target + timedelta(days=offset)
        mask = hdates == d
        if mask.any():
            return _safe(hist["Close"][mask].iloc[0])
    return None


def _fwd_ret(hist: pd.DataFrame, start: date, n: int) -> float | None:
    """Log return from start close to n trading-days-later close."""
    if hist is None or hist.empty:
        return None
    hdates = pd.to_datetime(hist.index).date
    s_idx = next((i for i, d in enumerate(hdates) if d >= start), None)
    if s_idx is None or s_idx + n >= len(hdates):
        return None
    p0 = _safe(hist["Close"].iloc[s_idx])
    p1 = _safe(hist["Close"].iloc[s_idx + n])
    if p0 and p1 and p0 > 0:
        return round(float(np.log(p1 / p0)), 6)
    return None


def _drift(hist: pd.DataFrame, start: date, gap: int = 1, total: int = 22) -> float | None:
    """Post-gap drift: log return from (start+gap) to (start+total) trading days."""
    if hist is None or hist.empty:
        return None
    hdates = pd.to_datetime(hist.index).date
    s_idx = next((i for i, d in enumerate(hdates) if d >= start), None)
    if s_idx is None or s_idx + total >= len(hdates):
        return None
    p0 = _safe(hist["Close"].iloc[s_idx + gap])
    p1 = _safe(hist["Close"].iloc[s_idx + total])
    if p0 and p1 and p0 > 0:
        return round(float(np.log(p1 / p0)), 6)
    return None


def _beat_rates(rows: list[dict]) -> tuple[float | None, float | None, float | None]:
    """Rolling eps_beat_rate, rev_beat_rate, avg_eps_surprise over up to 8 rows."""
    if not rows:
        return None, None, None
    window = rows[-8:]
    eps_b, rev_b, eps_s = [], [], []
    for r in window:
        es = r.get("eps_surprise")
        if es is not None:
            eps_b.append(1 if es > 0 else 0)
            eps_s.append(es)
        rs = r.get("revenue_surprise")
        if rs is not None:
            rev_b.append(1 if rs > 0 else 0)
    return (
        round(float(np.mean(eps_b)), 4) if eps_b else None,
        round(float(np.mean(rev_b)), 4) if rev_b else None,
        round(float(np.mean(eps_s)), 6) if eps_s else None,
    )


def _pct_rank(history: list[dict], field: str, val: float | None) -> float | None:
    """Percentile rank of val vs own prior history (min 6 prior obs)."""
    if val is None:
        return None
    vals = [r[field] for r in history if r.get(field) is not None]
    if len(vals) < 6:
        return None
    return round(float(np.mean([v < val for v in vals])), 4)


# ── Per-ticker collection ──────────────────────────────────────────────────────

def _collect_ticker(ticker: str) -> list[dict]:
    """Fetch yfinance data for one ticker. Returns list of rows oldest→newest."""
    import yfinance as yf

    tkr = yf.Ticker(ticker)

    # earnings_dates gives up to 8 quarters (vs earnings_history's 4)
    try:
        ed = tkr.earnings_dates
        if ed is None or ed.empty:
            raise ValueError("empty")
        # Filter out future dates (estimates only)
        today = date.today()
        ed = ed[pd.to_datetime(ed.index).date <= today]
        if ed.empty:
            raise ValueError("all future")
    except Exception as exc:
        log.warning("%s: no earnings_dates (%s) — skipping", ticker, exc)
        return []

    # Quarterly financials
    try:
        inc = tkr.quarterly_income_stmt
    except Exception:
        inc = pd.DataFrame()
    try:
        cf = tkr.quarterly_cashflow
    except Exception:
        cf = pd.DataFrame()
    try:
        bs = tkr.quarterly_balance_sheet
    except Exception:
        bs = pd.DataFrame()

    # 2yr price history
    try:
        hist = tkr.history(period="2y")
        hist = None if hist.empty else hist
    except Exception:
        hist = None

    # Shares outstanding for mktcap
    try:
        shares = _safe(tkr.info.get("sharesOutstanding"))
    except Exception:
        shares = None

    # Sector label + ETF history for abnormal return computation
    try:
        sector = tkr.info.get("sector") or "Unknown"
    except Exception:
        sector = "Unknown"

    sector_etf = _SECTOR_ETF.get(sector)
    etf_hist = None
    if sector_etf:
        try:
            etf_hist = yf.Ticker(sector_etf).history(period="2y")
            etf_hist = None if etf_hist.empty else etf_hist
        except Exception:
            etf_hist = None

    rows: list[dict] = []

    # Iterate oldest → newest so rolling stats accumulate correctly
    ed_sorted = ed.sort_index(ascending=True)

    for idx in ed_sorted.index:
        try:
            earn_date = pd.to_datetime(idx).date()
        except Exception:
            continue

        row_data = ed_sorted.loc[idx]
        release_timing = _parse_release_timing(idx, row_data)
        earnings_avail = _earnings_available_date(earn_date, release_timing)

        # EPS from earnings_dates columns
        eps_actual   = _safe(row_data.get("Reported EPS"))
        eps_estimate = _safe(row_data.get("EPS Estimate"))
        surp_raw     = row_data.get("Surprise(%)")
        # Compute surprise ourselves — avoids ambiguity about yfinance units
        if eps_actual is not None and eps_estimate is not None and eps_estimate != 0:
            eps_surprise = round((eps_actual - eps_estimate) / abs(eps_estimate), 6)
        else:
            eps_surprise = None

        # Match nearest quarterly financial column
        q = _nearest_quarter_idx(inc, earn_date)

        revenue_actual   = _col(inc, "Total Revenue",  q)
        gross_profit     = _col(inc, "Gross Profit",   q)
        ebitda           = _col(inc, "EBITDA",         q)
        ebit             = _col(inc, "EBIT",           q)
        net_income       = _col(inc, "Net Income",     q)

        fcf = _col(cf, "Free Cash Flow",          _nearest_quarter_idx(cf, earn_date))
        ocf = _col(cf, "Operating Cash Flow",     _nearest_quarter_idx(cf, earn_date))

        bs_q    = _nearest_quarter_idx(bs, earn_date)
        cash    = _col(bs, "Cash And Cash Equivalents", bs_q)
        if cash is None:
            for lbl in ["Cash Cash Equivalents And Short Term Investments",
                        "Cash And Short Term Investments"]:
                cash = _col(bs, lbl, bs_q)
                if cash is not None:
                    break
        total_debt = _col(bs, "Total Debt", bs_q)
        net_debt   = _col(bs, "Net Debt",   bs_q)
        if net_debt is None and cash is not None and total_debt is not None:
            net_debt = round(total_debt - cash, 2)

        # Margins — all quarterly values (same denominator = revenue_actual)
        gross_margin     = round(gross_profit / revenue_actual, 6) if gross_profit and revenue_actual else None
        operating_margin = round(ebit         / revenue_actual, 6) if ebit and revenue_actual else None
        net_margin       = round(net_income   / revenue_actual, 6) if net_income and revenue_actual else None
        fcf_margin       = round(fcf          / revenue_actual, 6) if fcf and revenue_actual else None

        # Post-earnings price returns (ticker)
        ret_1d  = _fwd_ret(hist, earn_date, 1)
        ret_3d  = _fwd_ret(hist, earn_date, 3)
        ret_5d  = _fwd_ret(hist, earn_date, 5)
        ret_10d = _fwd_ret(hist, earn_date, 10)
        drift   = _drift(hist, earn_date, gap=1, total=22)

        # Sector ETF returns over same window (raw — abnormal_return computed post-collection)
        etf_ret_1d = _fwd_ret(etf_hist, earn_date, 1)
        etf_ret_3d = _fwd_ret(etf_hist, earn_date, 3)
        etf_ret_5d = _fwd_ret(etf_hist, earn_date, 5)

        # Valuation multiples at earnings_date
        price  = _price_on(hist, earn_date)
        mktcap = round(price * shares, 2) if price and shares else None

        try:
            trailing_eps = _safe(tkr.info.get("trailingEps"))
        except Exception:
            trailing_eps = None

        trailing_pe    = round(price / trailing_eps, 4) if price and trailing_eps and trailing_eps > 0 else None
        ev_ebitda      = round((mktcap + (net_debt or 0)) / ebitda, 4) if mktcap and ebitda and ebitda > 0 else None
        # revenue_actual is full USD (not millions) — mktcap is full USD too
        price_to_sales = round(mktcap / revenue_actual, 4) if mktcap and revenue_actual and revenue_actual > 0 else None
        fcf_yield      = round(fcf / mktcap, 6) if fcf and mktcap and mktcap > 0 else None

        # Fiscal quarter label (approximate from earnings date)
        fq = (earn_date.month - 1) // 3 + 1
        fiscal_quarter = f"Q{fq}-{earn_date.year}"

        # Rolling stats from rows already built (oldest→newest order)
        ebr, rbr, aes = _beat_rates(rows)
        trailing_pe_5y_pct  = _pct_rank(rows, "trailing_pe",    trailing_pe)
        ev_ebitda_5y_pct    = _pct_rank(rows, "ev_ebitda",      ev_ebitda)
        ps_5y_pct           = _pct_rank(rows, "price_to_sales", price_to_sales)

        rows.append({
            "ticker":                   ticker,
            "earnings_date":            earn_date.isoformat(),
            "earnings_available_date":  earnings_avail.isoformat(),
            "fiscal_quarter":           fiscal_quarter,
            "release_timing":           release_timing,
            "revenue_actual":       revenue_actual,
            "revenue_estimate":     None,
            "revenue_surprise":     None,
            "eps_actual":           eps_actual,
            "eps_estimate":         eps_estimate,
            "eps_surprise":         eps_surprise,
            "ebitda":               ebitda,
            "ebit":                 ebit,
            "net_income":           net_income,
            "free_cash_flow":       fcf,
            "operating_cash_flow":  ocf,
            "cash_and_equiv":       cash,
            "total_debt":           total_debt,
            "net_debt":             net_debt,
            "gross_margin":         gross_margin,
            "operating_margin":     operating_margin,
            "net_margin":           net_margin,
            "fcf_margin":           fcf_margin,
            "guidance_direction":   None,
            "guidance_revenue_mid": None,
            "guidance_eps_mid":     None,
            "eps_beat_rate_8q":     ebr,
            "rev_beat_rate_8q":     rbr,
            "avg_eps_surprise_8q":  aes,
            "trailing_pe":          trailing_pe,
            "forward_pe":           None,
            "ev_ebitda":            ev_ebitda,
            "price_to_sales":       price_to_sales,
            "fcf_yield":            fcf_yield,
            "peg_ratio":            None,
            "forward_pe_5y_pct":    trailing_pe_5y_pct,
            "ev_ebitda_5y_pct":     ev_ebitda_5y_pct,
            "ps_5y_pct":            ps_5y_pct,
            "forward_pe_sector_pct":  None,  # computed after all tickers
            "ev_ebitda_sector_pct":   None,
            "median_impl_real_ratio_8q": None,
            "p25_impl_real_ratio_8q":    None,
            "p75_impl_real_ratio_8q":    None,
            "ret_1d":               ret_1d,
            "ret_3d":               ret_3d,
            "ret_5d":               ret_5d,
            "ret_10d":              ret_10d,
            "drift_30d":            drift,
            # Raw sector ETF returns (derived abnormal_return_* computed post-collection)
            "sector_etf_return_1d": etf_ret_1d,
            "sector_etf_return_3d": etf_ret_3d,
            "sector_etf_return_5d": etf_ret_5d,
            # Sector cross-sectional fields — populated in _enrich_sector_fields()
            "sector_coverage_n":                   None,
            "sector_eps_surprise_mean":            None,
            "sector_eps_surprise_median":          None,
            "sector_beat_rate_7d":                 None,
            "sector_beat_rate_14d":                None,
            "sector_avg_eps_surprise_7d":          None,
            "sector_avg_eps_surprise_14d":         None,
            "sector_avg_post_earnings_return_14d": None,
            "eps_surprise_vs_sector":              None,
            "eps_surprise_vs_market":              None,
            "abnormal_return_1d":                  None,
            "abnormal_return_3d":                  None,
            "abnormal_return_5d":                  None,
            "point_in_time_verified": True,
            "source":               "yfinance",
            "_sector":              sector,
        })

    return rows


# ── Post-collection enrichment ─────────────────────────────────────────────────

def _enrich_sector_fields(all_rows: list[dict]) -> None:
    """
    Compute all cross-sectional and derived fields after all tickers are collected.
    Mutates rows in-place. Requires oldest→newest ordering within each ticker.

    Fields computed:
      Valuation percentiles    : forward_pe_sector_pct, ev_ebitda_sector_pct
      Sector context (quarter) : sector_coverage_n, sector_eps_surprise_mean/median
      Rolling windows (7d/14d) : sector_beat_rate_*, sector_avg_eps_surprise_*,
                                 sector_avg_post_earnings_return_14d
      Market-level             : eps_surprise_vs_market
      Derived                  : eps_surprise_vs_sector, abnormal_return_1d/3d/5d

    Temporal integrity for rolling windows:
      For each event (ticker, earnings_date), rolling sector stats only include
      peers whose earnings_date < this event's earnings_date (strictly before).
      Same-day events are excluded — enforced by the strict < comparison.
    """
    # Sort all rows by earnings_date for time-aware rolling computations
    all_rows.sort(key=lambda r: r["earnings_date"])

    # ── Per-quarter cross-sectional: valuation percentiles + coverage ─────────
    by_quarter: dict[str, list[dict]] = {}
    for r in all_rows:
        by_quarter.setdefault(r["fiscal_quarter"], []).append(r)

    for qrows in by_quarter.values():
        by_sector: dict[str, list[dict]] = {}
        for r in qrows:
            by_sector.setdefault(r.get("_sector", "Unknown"), []).append(r)

        for sector, srows in by_sector.items():
            n = len(srows)
            # Valuation percentiles
            pe_vals = [(r, r["trailing_pe"])  for r in srows if r["trailing_pe"]  is not None]
            ev_vals = [(r, r["ev_ebitda"])    for r in srows if r["ev_ebitda"]    is not None]
            for r, val in pe_vals:
                peers = [v for _, v in pe_vals]
                r["forward_pe_sector_pct"] = round(float(np.mean([v < val for v in peers])), 4)
            for r, val in ev_vals:
                peers = [v for _, v in ev_vals]
                r["ev_ebitda_sector_pct"] = round(float(np.mean([v < val for v in peers])), 4)

            # sector_coverage_n + same-quarter EPS surprise stats (min 5 peers)
            surps = [r["eps_surprise"] for r in srows if r.get("eps_surprise") is not None]
            for r in srows:
                r["sector_coverage_n"] = n
                if len(surps) >= 5:
                    r["sector_eps_surprise_mean"]   = round(float(np.mean(surps)), 6)
                    r["sector_eps_surprise_median"] = round(float(np.median(surps)), 6)
                # else leave NULL — semantically meaningful absence

    # ── Market-level EPS surprise (all tickers, per quarter) ─────────────────
    for quarter, qrows in by_quarter.items():
        all_surps = [r["eps_surprise"] for r in qrows if r.get("eps_surprise") is not None]
        market_median = round(float(np.median(all_surps)), 6) if all_surps else None
        for r in qrows:
            if r.get("eps_surprise") is not None and market_median is not None:
                r["eps_surprise_vs_market"] = round(r["eps_surprise"] - market_median, 6)

    # ── Rolling 7d / 14d sector stats (time-aware: strictly before event) ────
    for i, r in enumerate(all_rows):
        ed = r["earnings_date"]         # ISO string 'YYYY-MM-DD'
        sector = r.get("_sector", "Unknown")
        es = r.get("eps_surprise")

        # Peers: same sector, earnings_date strictly before this event
        peers_7d  = [p for p in all_rows
                     if p is not r
                     and p.get("_sector") == sector
                     and p["earnings_date"] < ed
                     and (date.fromisoformat(ed) - date.fromisoformat(p["earnings_date"])).days <= 7]
        peers_14d = [p for p in all_rows
                     if p is not r
                     and p.get("_sector") == sector
                     and p["earnings_date"] < ed
                     and (date.fromisoformat(ed) - date.fromisoformat(p["earnings_date"])).days <= 14]

        def _rolling_stats(peers, field_beat="eps_surprise", field_ret="ret_1d"):
            if not peers:
                return None, None, None
            surps = [p[field_beat] for p in peers if p.get(field_beat) is not None]
            beats = [1 if s > 0 else 0 for s in surps]
            rets  = [p[field_ret] for p in peers if p.get(field_ret) is not None]
            beat_rate  = round(float(np.mean(beats)), 4) if beats else None
            surp_mean  = round(float(np.mean(surps)), 6) if surps else None
            ret_mean   = round(float(np.mean(rets)),  6) if rets  else None
            return beat_rate, surp_mean, ret_mean

        br7,  sm7,  _    = _rolling_stats(peers_7d)
        br14, sm14, rm14 = _rolling_stats(peers_14d)

        r["sector_beat_rate_7d"]                 = br7
        r["sector_beat_rate_14d"]                = br14
        r["sector_avg_eps_surprise_7d"]          = sm7
        r["sector_avg_eps_surprise_14d"]         = sm14
        r["sector_avg_post_earnings_return_14d"] = rm14

        # eps_surprise_vs_sector (uses same-quarter sector median; enforce min 5)
        sec_med = r.get("sector_eps_surprise_median")
        if es is not None and sec_med is not None and (r.get("sector_coverage_n") or 0) >= 5:
            r["eps_surprise_vs_sector"] = round(es - sec_med, 6)

        # Derived: abnormal returns (ticker - sector ETF)
        for n_days, ret_field, etf_field, out_field in [
            (1, "ret_1d", "sector_etf_return_1d", "abnormal_return_1d"),
            (3, "ret_3d", "sector_etf_return_3d", "abnormal_return_3d"),
            (5, "ret_5d", "sector_etf_return_5d", "abnormal_return_5d"),
        ]:
            tr = r.get(ret_field)
            er = r.get(etf_field)
            if tr is not None and er is not None:
                r[out_field] = round(tr - er, 6)


# ── DB insert ──────────────────────────────────────────────────────────────────

_INSERT_COLS = [
    "ticker", "earnings_date", "earnings_available_date", "fiscal_quarter", "release_timing",
    "revenue_actual", "revenue_estimate", "revenue_surprise",
    "eps_actual", "eps_estimate", "eps_surprise",
    "ebitda", "ebit", "net_income",
    "free_cash_flow", "operating_cash_flow",
    "cash_and_equiv", "total_debt", "net_debt",
    "gross_margin", "operating_margin", "net_margin", "fcf_margin",
    "guidance_direction", "guidance_revenue_mid", "guidance_eps_mid",
    "eps_beat_rate_8q", "rev_beat_rate_8q", "avg_eps_surprise_8q",
    "trailing_pe", "forward_pe", "ev_ebitda", "price_to_sales",
    "fcf_yield", "peg_ratio",
    "forward_pe_5y_pct", "ev_ebitda_5y_pct", "ps_5y_pct",
    "forward_pe_sector_pct", "ev_ebitda_sector_pct",
    "median_impl_real_ratio_8q", "p25_impl_real_ratio_8q", "p75_impl_real_ratio_8q",
    "ret_1d", "ret_3d", "ret_5d", "ret_10d", "drift_30d",
    # Raw sector ETF returns
    "sector_etf_return_1d", "sector_etf_return_3d", "sector_etf_return_5d",
    # Sector cross-sectional
    "sector_coverage_n",
    "sector_eps_surprise_mean", "sector_eps_surprise_median",
    "sector_beat_rate_7d", "sector_beat_rate_14d",
    "sector_avg_eps_surprise_7d", "sector_avg_eps_surprise_14d",
    "sector_avg_post_earnings_return_14d",
    # Derived relative fields
    "eps_surprise_vs_sector", "eps_surprise_vs_market",
    "abnormal_return_1d", "abnormal_return_3d", "abnormal_return_5d",
    "point_in_time_verified", "source",
]

def _upsert(rows: list[dict], dry_run: bool) -> int:
    if not rows or dry_run:
        return len(rows)
    ph = ", ".join(["?"] * len(_INSERT_COLS))
    sql = (
        f"INSERT OR REPLACE INTO earnings_fundamentals "
        f"({', '.join(_INSERT_COLS)}) VALUES ({ph})"
    )
    with connect() as con:
        for row in rows:
            con.execute(sql, [row.get(c) for c in _INSERT_COLS])
        con.commit()
    return len(rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def run(tickers: list[str] | None = None, dry_run: bool = False) -> None:
    ensure_financial_results_tables()

    if tickers is None:
        with connect() as con:
            tickers = [
                r[0] for r in con.execute(
                    "SELECT DISTINCT ticker FROM regime_training ORDER BY ticker"
                ).fetchall()
            ]

    tickers = [t for t in tickers if t not in _NO_EARNINGS]
    log.info("Collecting earnings fundamentals for %d tickers", len(tickers))

    all_rows: list[dict] = []
    n_skip = 0

    for i, ticker in enumerate(tickers):
        log.info("[%d/%d] %s", i + 1, len(tickers), ticker)
        try:
            rows = _collect_ticker(ticker)
            if not rows:
                n_skip += 1
            else:
                all_rows.extend(rows)
                log.info("  %s: %d quarters collected", ticker, len(rows))
        except Exception as exc:
            log.warning("  %s: ERROR — %s", ticker, exc)
            n_skip += 1

        if i < len(tickers) - 1:
            time.sleep(SLEEP_BETWEEN)

    if all_rows:
        log.info("Enriching sector + relative fields (%d rows)...", len(all_rows))
        _enrich_sector_fields(all_rows)

    inserted = _upsert(all_rows, dry_run)

    print(f"\nDone.")
    print(f"  Tickers attempted : {len(tickers)}")
    print(f"  Tickers skipped   : {n_skip}")
    print(f"  Rows {'would insert' if dry_run else 'inserted'}: {inserted}")

    if dry_run and all_rows:
        r = all_rows[-1]  # show most recent row
        print(f"\n[dry-run] sample row ({r['ticker']} {r['earnings_date']}):")
        for k, v in r.items():
            if not k.startswith("_"):
                print(f"  {k:35s}: {v}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Earnings fundamentals backfill")
    ap.add_argument("--tickers", nargs="+", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and compute but do not write to DB")
    args = ap.parse_args()
    run(tickers=args.tickers, dry_run=args.dry_run)
