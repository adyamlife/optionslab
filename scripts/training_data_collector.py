"""
Training Data Collector

Builds the historical labeled dataset a real ML model (POP classifier,
regime detector) would need to train on — infrastructure the current
rule-based engine doesn't require, but that any future ML path absolutely
needs first, since none exists today. Nothing here trains or runs a model;
this only collects feature snapshots and later labels them with the actual
outcome.

Two halves, triggered independently (same external-cron pattern this
codebase already uses for paper_trade_engine's morning-scan/evening-check —
see app.py's /api/paper-trades/* routes; Flask's dev server has no
built-in scheduler of its own):

  - collect_snapshots(): call several times a day (recommended: every ~2h
    during market hours) via /api/training-data/collect to record every
    WATCHLIST ticker's current state + recommended candidate, PLUS a ±10
    strike options chain snapshot for each ticker (E*TRADE if authenticated,
    yfinance fallback). The chain snapshot enables real per-leg Greek seeding
    for the drift card and richer ML features (IV skew, strike-level Greeks).
  - label_pending_snapshots(): call once a day (e.g. alongside the evening
    check) via /api/training-data/label to fill in the actual outcome for
    snapshots whose candidate's expiry has now passed.

Storage: DuckDB (data/ml_training.duckdb), tables training_snapshots and
option_chain_snapshots. JSON columns store nested fields (candidate, strikes,
outcome). Labeling does in-place SQL UPDATEs — no full-file rewrites.
"""
import logging
from datetime import date, datetime
from pathlib import Path

import numpy as np
import yfinance as yf

log = logging.getLogger(__name__)

from config.watchlist import WATCHLIST
from scripts.analyze import analyze_ticker, days_to_earnings
from scripts.candidate_provider import _payoff_per_share
from scripts.data_fetch import (
    pick_expiry,
    get_otm_pcr, get_beta, get_atr_pct, get_iv_rank_52w,
    get_sector_context, get_index_trends, get_vix_context, get_macro_context,
    get_news_sentiment_score, get_analyst_rec_change,
)
from scripts.market_context import get_market_context
from scripts.paper_trade_engine import fetch_underlying_price

_ROOT = Path(__file__).resolve().parent.parent

# How many strikes either side of ATM to store per expiry
_CHAIN_STRIKE_RADIUS = 10


def _load_all() -> list:
    from scripts.db import load_all_snapshots
    return load_all_snapshots()


def load_chain_index() -> dict:
    """
    Load option_chain_snapshots into a lookup index from DuckDB:
      { ticker: { date_str: { (strike, opt_type): {iv, delta, gamma, theta, vega} } } }
    Used by the POP model to substitute real chain Greeks for candidate estimated Greeks.
    Only E*TRADE-sourced entries carry Greeks; yfinance entries have Greeks=None (still
    useful for IV).
    """
    from scripts.db import load_chain_index_from_db
    return load_chain_index_from_db()


def enrich_candidate_greeks(record: dict, chain_index: dict) -> dict:
    """
    Replace a snapshot record's candidate Greeks with values from the chain
    snapshot taken on the same date, if available.  Falls back to the
    candidate's own estimated values when the chain has no data for those legs.

    Leg geometry is derived from the candidate's strike fields:
      Iron Condor / Jade Lizard  → put_long_strike, put_short_strike,
                                    call_short_strike, call_long_strike
      Vertical spread            → short_strike, long_strike  (put or call)
      Single-leg                 → short_strike or long_strike
    """
    candidate = dict(record.get("candidate") or {})
    if not candidate:
        return record

    ticker  = record.get("ticker", "")
    day     = (record.get("collected_at") or "")[:10]
    day_idx = (chain_index.get(ticker) or {}).get(day, {})
    if not day_idx:
        return record   # no chain data for this date — use as-is

    def _lookup(strike, opt_type):
        if strike is None:
            return None
        return day_idx.get((round(float(strike), 2), opt_type))

    structure = (candidate.get("structure") or "").lower()
    legs = []  # list of (strike, opt_type, side)

    if candidate.get("put_short_strike") is not None:
        legs.append((candidate["put_short_strike"],  "put",  -1))
    if candidate.get("put_long_strike") is not None:
        legs.append((candidate["put_long_strike"],   "put",  +1))
    if candidate.get("call_short_strike") is not None:
        legs.append((candidate["call_short_strike"], "call", -1))
    if candidate.get("call_long_strike") is not None:
        legs.append((candidate["call_long_strike"],  "call", +1))

    if not legs:
        opt_type = "put" if "put" in structure else "call"
        if candidate.get("short_strike") is not None:
            legs.append((candidate["short_strike"], opt_type, -1))
        if candidate.get("long_strike") is not None:
            legs.append((candidate["long_strike"],  opt_type, +1))

    if not legs:
        return record   # geometry unknown — can't enrich

    net_delta = net_gamma = net_theta = net_vega = 0.0
    ivs = []
    all_found = True
    for strike, opt_type, side in legs:
        g = _lookup(strike, opt_type)
        if g is None:
            all_found = False
            break
        if g.get("iv"):
            ivs.append(g["iv"])
        if g.get("delta") is not None:
            net_delta += side * g["delta"]
        if g.get("gamma") is not None:
            net_gamma += side * g["gamma"]
        if g.get("theta") is not None:
            net_theta += side * g["theta"]
        if g.get("vega") is not None:
            net_vega  += side * g["vega"]

    if not all_found:
        return record   # partial match — don't mix chain + candidate values

    enriched = dict(record)
    enriched["candidate"] = {
        **candidate,
        "net_delta": round(net_delta, 4),
        "net_gamma": round(net_gamma, 6),
        "net_theta": round(net_theta, 4),
        "net_vega":  round(net_vega,  4),
        # Also update atm_iv with the chain's average leg IV (more precise than ticker-level)
        "_chain_avg_iv": round(sum(ivs) / len(ivs), 4) if ivs else None,
        "_chain_enriched": True,
    }
    return enriched


def _append(record: dict):
    from scripts.db import insert_snapshot
    insert_snapshot(record)


def _fetch_garch_vol(ticker: str) -> float | None:
    """Return GARCH(1,1) conditional vol (annualized) from saved model, or None."""
    try:
        from scripts.train_garch_model import get_garch_forecast
        return get_garch_forecast(ticker)
    except Exception:
        return None


def _build_snapshot_record(ticker: str, vix_price, spy_hist=None,
                           index_trends: dict | None = None,
                           vix_ctx: dict | None = None,
                           macro_ctx: dict | None = None) -> dict:
    """One unlabeled feature snapshot for a single ticker."""
    row = analyze_ticker(ticker)
    candidates = row.get("candidates", [])
    recommended = next((c for c in candidates if c.get("recommended")), None)

    try:
        earn_days = days_to_earnings(yf.Ticker(ticker))
    except Exception:
        earn_days = None

    try:
        raw_news = yf.Ticker(ticker).news or []
        news_headlines = [
            (n.get("content") or {}).get("title")
            for n in raw_news[:5]
            if (n.get("content") or {}).get("title")
        ]
    except Exception:
        news_headlines = []

    # ── Tier 1 additional features ────────────────────────────────────────────
    spot    = row.get("spot") or 0
    atm_iv  = row.get("atm_iv") or 0

    # OTM PCR: requires the live option chain that analyze_ticker already fetched.
    # Re-fetch here is avoidable only if analyze_ticker exposed calls/puts — it
    # doesn't, so we derive from the already-stored pcr fields as a proxy and
    # do a targeted re-fetch for the OTM bands.
    otm_pcr_val = None
    try:
        from scripts.data_fetch import get_option_chain, get_atm_iv as _atm_iv
        tkr_obj = yf.Ticker(ticker)
        expiry  = (recommended or {}).get("expiry") or row.get("expiry")
        if expiry and spot:
            calls_df, puts_df = get_option_chain(tkr_obj, expiry, spot=spot)
            if calls_df is not None and not calls_df.empty and puts_df is not None:
                otm = get_otm_pcr(calls_df, puts_df, spot)
                if otm:
                    otm_pcr_val = otm["otm_pcr"]
    except Exception:
        pass

    # Beta vs SPY (60-day)
    beta_val = None
    try:
        hist = yf.Ticker(ticker).history(period="3mo")
        if spy_hist is None:
            spy_hist_local = yf.Ticker("SPY").history(period="3mo")
        else:
            spy_hist_local = spy_hist
        if not hist.empty and not spy_hist_local.empty:
            beta_val = get_beta(hist, spy_hist_local, window=60)
    except Exception:
        pass

    # ATR % of spot (14-day)
    atr_pct_val = None
    try:
        hist_atr = yf.Ticker(ticker).history(period="2mo")
        if not hist_atr.empty and spot:
            atr_pct_val = get_atr_pct(hist_atr, spot, period=14)
    except Exception:
        pass

    # True 52-week IV rank (uses 1y of rolling HV20 as IV proxy distribution)
    iv_rank_52w_val = None
    try:
        if atm_iv:
            iv_rank_52w_val = get_iv_rank_52w(ticker, atm_iv)
    except Exception:
        pass

    # IV term structure slope (ratio form: front_iv / back_iv)
    # front_iv and back_iv are already computed by analyze_ticker
    iv_term_slope = None
    try:
        front_iv = row.get("iv_front_iv")
        back_iv  = row.get("iv_back_iv")
        if front_iv and back_iv and back_iv > 0:
            iv_term_slope = round(front_iv / back_iv, 4)
    except Exception:
        pass

    # ── Tier 2: sector and index context ──────────────────────────────────────
    sector_ctx = {}
    try:
        sector_ctx = get_sector_context(ticker, atm_iv)
    except Exception:
        pass

    idx = index_trends or {}
    vix = vix_ctx or {}

    # ── Tier 4: chain-snapshot-derived features ───────────────────────────────
    tier4 = {"iv_skew_20d": None, "gex_proxy": None, "max_pain_strike": None,
             "oi_concentration": None, "wings_iv_ratio": None}
    try:
        from scripts.data_fetch import compute_chain_features as _ccf
        tier4 = _ccf(ticker, spot=spot if spot else None)
    except Exception:
        pass

    # ── Tier 3: news sentiment, analyst change, short interest, earnings flag ──
    tkr_obj = yf.Ticker(ticker)

    news_sentiment_score = None
    try:
        news_sentiment_score = get_news_sentiment_score(tkr_obj)
    except Exception:
        pass

    analyst_rec_change = None
    try:
        analyst_rec_change = get_analyst_rec_change(tkr_obj, days=5)
    except Exception:
        pass

    short_interest_pct = row.get("short_interest")   # already fetched by analyze_ticker

    # earnings_inside_expiry: does the earnings date fall within the candidate's DTE window?
    candidate_dte = (recommended or {}).get("dte") or row.get("dte")
    earnings_inside_expiry = (
        earn_days is not None
        and candidate_dte is not None
        and 0 < earn_days <= candidate_dte
    )

    return {
        "snapshot_id":           f"{ticker}-{datetime.now().isoformat()}",
        "collected_at":          datetime.now().isoformat(),
        "ticker":                ticker,
        "spot":                  spot or None,
        "iv_env":                row.get("iv_env"),
        "trend":                 row.get("trend"),
        "weekly_trend":          row.get("weekly_trend"),
        "regime":                row.get("regime"),
        "rsi":                   row.get("rsi"),
        "macd_trend":            row.get("macd_trend"),
        "adx":                   row.get("adx"),
        "atm_iv":                row.get("atm_iv"),
        "iv_rank_proxy":         row.get("iv_rank_proxy"),
        "hv20":                  row.get("hv20"),
        "pcr":                   row.get("pcr"),
        "vix":                   vix_price,
        "earnings_days_away":    earn_days,
        "news_headlines":        news_headlines,
        "status":                row.get("status"),
        "recommended_structure": row.get("recommended_structure"),
        "signal_score":          row.get("signal_score"),
        "candidate":             recommended,
        "expiry":                (recommended or {}).get("expiry") or row.get("expiry"),
        "dte":                   (recommended or {}).get("dte") or row.get("dte"),
        # ── Tier 1 fields ──────────────────────────────────────────────────────
        "vol_oi_ratio":          row.get("vol_oi_ratio"),
        "iv_skew":               row.get("vol_skew_pct"),
        "iv_term_slope":         iv_term_slope,
        "otm_pcr":               otm_pcr_val,
        "beta_60d":              beta_val,
        "atr_pct":               atr_pct_val,
        "iv_rank_52w":           iv_rank_52w_val,
        # ── Tier 2 fields ──────────────────────────────────────────────────────
        "sector_etf":            sector_ctx.get("sector_etf"),
        "sector_trend":          sector_ctx.get("sector_trend"),
        "sector_rsi":            sector_ctx.get("sector_rsi"),
        "sector_iv_ratio":       sector_ctx.get("sector_iv_ratio"),
        "spy_trend":             idx.get("spy_trend"),
        "spy_rsi":               idx.get("spy_rsi"),
        "qqq_trend":             idx.get("qqq_trend"),
        "qqq_rsi":               idx.get("qqq_rsi"),
        "iwm_trend":             idx.get("iwm_trend"),
        "iwm_rsi":               idx.get("iwm_rsi"),
        "vvix":                  vix.get("vvix"),
        "vix_3m":                vix.get("vix_3m"),
        "vix_term_slope":        vix.get("vix_term_slope"),
        # ── Tier 3 fields ──────────────────────────────────────────────────────
        "earnings_inside_expiry": earnings_inside_expiry,   # bool: expiry straddles earnings
        "news_sentiment_score":   news_sentiment_score,     # float [-1,+1]: net bullish/bearish
        "analyst_rec_change":     analyst_rec_change,       # int: upgrades - downgrades last 5d
        "short_interest_pct":     short_interest_pct,       # float: % of float short
        # ── GARCH conditional vol at entry ─────────────────────────────────────
        "garch_vol_at_entry": _fetch_garch_vol(ticker),
        # ── Tier 4 fields (chain-snapshot-derived; E*TRADE has delta/gamma) ──
        "iv_skew_20d":      tier4.get("iv_skew_20d"),      # 20d put IV - 20d call IV
        "gex_proxy":        tier4.get("gex_proxy"),         # Σ gamma×OI×100 calls - puts
        "max_pain_strike":  tier4.get("max_pain_strike"),   # strike minimizing holder value
        "oi_concentration": tier4.get("oi_concentration"),  # % OI within ±2 strikes of ATM
        "wings_iv_ratio":   tier4.get("wings_iv_ratio"),    # 10d put IV ÷ ATM IV
        # ── Tier 5 fields (macro context, market-wide, passed from caller) ─────
        "yield_10y":      (macro_ctx or {}).get("yield_10y"),
        "yield_3m":       (macro_ctx or {}).get("yield_3m"),
        "yield_curve":    (macro_ctx or {}).get("yield_curve"),
        "dollar_index":   (macro_ctx or {}).get("dollar_index"),
        "fed_within_dte": (macro_ctx or {}).get("fed_within_dte"),
        "cpi_within_dte": (macro_ctx or {}).get("cpi_within_dte"),
        # ───────────────────────────────────────────────────────────────────────
        "labeled":               False,
        "outcome":               None,
        "labeled_at":            None,
    }


def _fetch_chain_yfinance(ticker: str, expiry: str, spot: float) -> list[dict]:
    """
    Fetch ±RADIUS strikes around ATM from yfinance for one expiry.
    yfinance does not provide per-strike Greeks — those fields are None.
    Returns [] on any failure.
    """
    try:
        tkr = yf.Ticker(ticker)
        chain = tkr.option_chain(expiry)
        rows = []
        for opt_type, df in (("call", chain.calls), ("put", chain.puts)):
            if df is None or df.empty:
                continue
            df = df.copy()
            df["_dist"] = (df["strike"] - spot).abs()
            nearest_idx = df["_dist"].argsort().iloc[:_CHAIN_STRIKE_RADIUS]
            for _, row in df.iloc[nearest_idx].iterrows():
                rows.append({
                    "strike":        float(row["strike"]),
                    "opt_type":      opt_type,
                    "bid":           float(row.get("bid") or 0),
                    "ask":           float(row.get("ask") or 0),
                    "mid":           round((float(row.get("bid") or 0) + float(row.get("ask") or 0)) / 2, 4),
                    "iv":            float(row.get("impliedVolatility") or 0),
                    "delta":         None,
                    "gamma":         None,
                    "theta":         None,
                    "vega":          None,
                    "volume":        int(row.get("volume") or 0),
                    "open_interest": int(row.get("openInterest") or 0),
                })
        return rows
    except Exception:
        return []


def _fetch_chain_etrade(ticker: str, expiry: str, spot: float) -> list[dict]:
    """
    Fetch ±RADIUS strikes around ATM from E*TRADE for one expiry.
    E*TRADE provides full Greeks per strike.
    Returns [] on any failure or when not authenticated.
    """
    try:
        from scripts import etrade_client as et
        calls_df, puts_df = et.get_option_chain(ticker, expiry)
        rows = []
        for opt_type, df in (("call", calls_df), ("put", puts_df)):
            if df is None or df.empty:
                continue
            df = df.copy()
            df["_dist"] = (df["strike"] - spot).abs()
            nearest_idx = df["_dist"].argsort().iloc[:_CHAIN_STRIKE_RADIUS]
            for _, row in df.iloc[nearest_idx].iterrows():
                bid = float(row.get("bid") or 0)
                ask = float(row.get("ask") or 0)
                rows.append({
                    "strike":        float(row["strike"]),
                    "opt_type":      opt_type,
                    "bid":           bid,
                    "ask":           ask,
                    "mid":           round((bid + ask) / 2, 4),
                    "iv":            float(row.get("impliedVolatility") or 0),
                    "delta":         float(row.get("delta") or 0) or None,
                    "gamma":         float(row.get("gamma") or 0) or None,
                    "theta":         float(row.get("theta") or 0) or None,
                    "vega":          float(row.get("vega") or 0) or None,
                    "volume":        int(row.get("volume") or 0),
                    "open_interest": int(row.get("openInterest") or 0),
                })
        return rows
    except Exception:
        return []


def _collect_chain_snapshot(ticker: str, spot: float) -> dict | None:
    """
    Collect a ±10-strike chain snapshot for one ticker.
    Tries E*TRADE first (full Greeks); falls back to yfinance (IV only).
    Uses the front expiry in the 7-28 DTE window (same range as analyze).
    Returns None if no chain data could be fetched.
    """
    try:
        tkr_obj = yf.Ticker(ticker)
        expiry, dte = pick_expiry(tkr_obj, min_dte=7, max_dte=28)
        if not expiry:
            return None

        source = "yfinance"
        strikes = []

        try:
            from scripts import etrade_client as et
            pref = et.ds_pref("option_chain")
            use_et = (pref == "etrade") or (pref == "auto" and et.is_authenticated())
            if use_et:
                strikes = _fetch_chain_etrade(ticker, expiry, spot)
                if strikes:
                    source = "etrade"
        except Exception:
            pass

        if not strikes:
            strikes = _fetch_chain_yfinance(ticker, expiry, spot)

        if not strikes:
            return None

        return {
            "snapshot_id": f"{ticker}-{datetime.now().isoformat()}",
            "collected_at": datetime.now().isoformat(),
            "ticker":       ticker,
            "spot":         spot,
            "expiry":       expiry,
            "dte":          dte,
            "source":       source,
            "strikes":      strikes,
        }
    except Exception:
        return None


def _row_to_strike_dict(row, opt_type: str) -> dict:
    """Convert a chain DataFrame row to the standard strike dict."""
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    return {
        "strike":        float(row["strike"]),
        "opt_type":      opt_type,
        "bid":           bid,
        "ask":           ask,
        "mid":           round((bid + ask) / 2, 4),
        "iv":            float(row.get("impliedVolatility") or 0),
        "delta":         float(row.get("delta") or 0) or None,
        "gamma":         float(row.get("gamma") or 0) or None,
        "theta":         float(row.get("theta") or 0) or None,
        "vega":          float(row.get("vega") or 0) or None,
        "volume":        int(row.get("volume") or 0),
        "open_interest": int(row.get("openInterest") or 0),
    }


def _collect_open_position_legs() -> list[dict]:
    """
    If E*TRADE is authenticated, fetch Greeks for every currently open option
    leg. Returns a list of chain snapshot dicts (caller inserts into DuckDB).
    Guarantees exact held strikes are captured regardless of ±10-strike sweep.
    """
    try:
        from scripts import etrade_client as et
        if not et.is_authenticated():
            return []
        positions = et.get_positions()
        if not positions:
            return []
    except Exception:
        return []

    now = datetime.now().isoformat()
    from collections import defaultdict
    leg_groups: dict[tuple, list[dict]] = defaultdict(list)
    for pos in positions:
        if pos.get("security_type") != "OPTN":
            continue
        ticker = pos.get("underlying", "")
        expiry = pos.get("expiry", "")
        if not ticker or not expiry or pos.get("strike") is None:
            continue
        leg_groups[(ticker, expiry)].append(pos)

    snaps = []
    try:
        from scripts import etrade_client as et
        for (ticker, expiry), legs in leg_groups.items():
            try:
                spot = fetch_underlying_price(ticker)
                calls_df, puts_df = et.get_option_chain(ticker, expiry)
                if calls_df is None and puts_df is None:
                    continue
                strike_rows = []
                for pos in legs:
                    opt_type = "call" if (pos.get("call_put") or "").upper() == "CALL" else "put"
                    df = calls_df if opt_type == "call" else puts_df
                    if df is None or df.empty:
                        continue
                    df = df.copy()
                    df["_dist"] = (df["strike"] - float(pos["strike"])).abs()
                    best = df.sort_values("_dist").iloc[0]
                    strike_rows.append(_row_to_strike_dict(best, opt_type))
                if not strike_rows:
                    continue
                snaps.append({
                    "snapshot_id":      f"pos-{ticker}-{expiry}-{now}",
                    "collected_at":     now,
                    "ticker":           ticker,
                    "spot":             spot,
                    "expiry":           expiry,
                    "dte":              (date.fromisoformat(expiry) - date.today()).days,
                    "source":           "etrade",
                    "is_position_legs": True,
                    "strikes":          strike_rows,
                })
            except Exception:
                continue
    except Exception:
        pass
    return snaps


def collect_option_chain_snapshots(tickers: list[str] | None = None) -> dict:
    """
    Collect ±10-strike chain snapshots for each ticker, PLUS exact-strike
    Greeks for every currently open E*TRADE option leg (if authenticated).
    Called automatically by collect_snapshots() each run; can also be called
    standalone. Stored in DuckDB option_chain_snapshots table.
    """
    from scripts.db import insert_chain_snapshot
    targets = tickers or WATCHLIST
    collected, errors = [], []
    for ticker in targets:
        try:
            spot = fetch_underlying_price(ticker)
            if spot is None:
                errors.append({"ticker": ticker, "error": "no spot price"})
                continue
            snap = _collect_chain_snapshot(ticker, spot)
            if snap is None:
                errors.append({"ticker": ticker, "error": "no chain data"})
                continue
            insert_chain_snapshot(snap)
            collected.append(ticker)
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

    # Capture exact open position legs regardless of ±10-strike sweep
    for snap in _collect_open_position_legs():
        try:
            insert_chain_snapshot(snap)
        except Exception:
            pass

    return {"collected": len(collected), "tickers": collected, "errors": errors}


def collect_snapshots() -> dict:
    """
    Snapshot every WATCHLIST ticker's current state + recommendation.
    Call this multiple times a day (recommended: every ~2h during market
    hours) via an external cron hitting /api/training-data/collect.
    """
    try:
        mkt_ctx = get_market_context()
        vix_price = (mkt_ctx.get("vix") or {}).get("price")
    except Exception:
        vix_price = None

    # Fetch shared market data once — reused across all 80 tickers
    spy_hist = qqq_hist = iwm_hist = None
    try:
        spy_hist = yf.Ticker("SPY").history(period="3mo")
        qqq_hist = yf.Ticker("QQQ").history(period="3mo")
        iwm_hist = yf.Ticker("IWM").history(period="3mo")
    except Exception:
        pass

    index_trends = get_index_trends(spy_hist=spy_hist, qqq_hist=qqq_hist, iwm_hist=iwm_hist)
    vix_ctx      = get_vix_context()
    macro_ctx    = get_macro_context(dte=14)   # 14d window matches default trade DTE

    collected, errors = [], []
    for ticker in WATCHLIST:
        try:
            _append(_build_snapshot_record(
                ticker, vix_price,
                spy_hist=spy_hist,
                index_trends=index_trends,
                vix_ctx=vix_ctx,
                macro_ctx=macro_ctx,
            ))
            collected.append(ticker)
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

    chain_result = collect_option_chain_snapshots()

    return {
        "collected":       len(collected),
        "tickers":         collected,
        "errors":          errors,
        "chain_collected": chain_result["collected"],
        "chain_errors":    chain_result["errors"],
    }


def label_pending_snapshots() -> dict:
    """
    Fill in the actual outcome for snapshots whose candidate's expiry has
    now passed. Call this once a day (e.g. alongside the evening check) via
    /api/training-data/label.
    """
    records = _load_all()
    today = date.today()
    labeled_count = 0
    price_cache = {}

    # Lazy-load paper trades once for all paper_trade_entry snapshots
    _paper_trades_cache = None

    for r in records:
        if r.get("labeled") or not r.get("expiry") or not r.get("candidate"):
            continue

        # ── Paper trade entry snapshots: label from managed-exit outcome ──────
        if r.get("source") == "paper_trade_entry" and r.get("paper_trade_id"):
            if _paper_trades_cache is None:
                try:
                    from scripts.paper_trade_engine import load_trades
                    _paper_trades_cache = {t["id"]: t for t in load_trades()}
                except Exception:
                    _paper_trades_cache = {}
            trade = _paper_trades_cache.get(r["paper_trade_id"])
            if trade is None or trade.get("status") == "open":
                continue  # trade still open — try again on a later run
            exit_data = trade.get("exit") or {}
            pnl = exit_data.get("pnl_per_share")
            win = exit_data.get("win")
            if pnl is None:
                continue
            r["labeled"] = True
            r["outcome"] = {
                "pnl_per_share": round(pnl, 4),
                "win": bool(win),
                "exit_reason": exit_data.get("reason"),
                "pnl_pct_of_max": exit_data.get("pnl_pct_of_max"),
            }
            r["labeled_at"] = datetime.now().isoformat()
            labeled_count += 1
            continue

        # ── Regular snapshots: label from hold-to-expiry payoff ──────────────
        try:
            exp_date = date.fromisoformat(str(r["expiry"])[:10])
        except Exception:
            continue
        if exp_date > today:
            continue  # not expired yet — try again on a later run

        ticker = r["ticker"]
        if ticker not in price_cache:
            price_cache[ticker] = fetch_underlying_price(ticker)
        s_t = price_cache[ticker]
        if s_t is None:
            continue  # couldn't fetch right now — try again next run

        pnl_arr = _payoff_per_share(r["candidate"].get("structure"), r["candidate"], np.array([s_t]))
        if pnl_arr is None:
            # Path-dependent structure — can't be labeled this way
            r["labeled"] = True
            r["outcome"] = {"unlabelable": True}
            r["labeled_at"] = datetime.now().isoformat()
            labeled_count += 1
            continue

        pnl = float(pnl_arr[0])
        r["labeled"] = True
        r["outcome"] = {"pnl_per_share": round(pnl, 4), "win": pnl > 0, "spot_at_expiry": round(s_t, 2)}
        r["labeled_at"] = datetime.now().isoformat()
        labeled_count += 1

    from scripts.db import update_snapshot_labels
    labeled_records = [r for r in records if r.get("labeled") and r.get("snapshot_id")]
    update_snapshot_labels(labeled_records)
    return {"labeled": labeled_count, "total_records": len(records)}


def _fetch_vix_now() -> float | None:
    """Quick VIX spot fetch at paper trade entry time. Returns None on any failure."""
    try:
        hist = yf.Ticker("^VIX").history(period="2d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return None


def _ml_scores_at_entry(ticker: str) -> dict:
    """
    Read the cached ML predictions for ticker at the moment of trade entry.
    Returns {ml_meta_score, ml_p_win, ml_confidence} — all None if cache miss.
    """
    try:
        from scripts.ml_cache import ml_cache
        pred = ml_cache.get(ticker)
        if not pred:
            return {"ml_meta_score": None, "ml_p_win": None, "ml_confidence": None}
        return {
            "ml_meta_score": pred.get("meta_score"),
            "ml_p_win":      pred.get("pop_score"),
            "ml_confidence": pred.get("analogues_win_rate"),
        }
    except Exception:
        return {"ml_meta_score": None, "ml_p_win": None, "ml_confidence": None}


def write_paper_trade_snapshot(trade: dict, analyze_row: dict) -> None:
    """
    Write a training snapshot at the moment a paper trade is opened.

    Uses the already-fetched analyze_row (no re-fetch) so the morning scan
    doesn't pay an extra round-trip per trade. Tier 1-5 supplemental fields
    (beta, atr_pct, etc.) are None — they aren't in analyze_ticker output.
    Labeling is done by label_pending_snapshots() once the trade closes, using
    the actual managed-exit P&L from paper_trades.json instead of the
    hold-to-expiry payoff function.
    """
    from scripts.db import insert_snapshot
    ticker = trade["ticker"]
    structure = trade["structure"]

    # Find the candidate in the row that matches the opened trade
    candidates = analyze_row.get("candidates", [])
    candidate = next((c for c in candidates if c.get("structure") == structure), None)

    record = {
        "snapshot_id":      f"paper-{trade['id']}",
        "collected_at":     trade["entered_at"],
        "ticker":           ticker,
        "spot":             analyze_row.get("spot"),
        "iv_env":           analyze_row.get("iv_env"),
        "trend":            analyze_row.get("trend"),
        "weekly_trend":     analyze_row.get("weekly_trend"),
        "regime":           analyze_row.get("regime"),
        "rsi":              analyze_row.get("rsi"),
        "macd_trend":       analyze_row.get("macd_trend"),
        "adx":              analyze_row.get("adx"),
        "atm_iv":           analyze_row.get("atm_iv"),
        "iv_rank_proxy":    analyze_row.get("iv_rank_proxy"),
        "hv20":             analyze_row.get("hv20"),
        "pcr":              analyze_row.get("pcr"),
        "vix":              _fetch_vix_now(),
        "earnings_days_away": None,
        "news_headlines":   [],
        "status":           analyze_row.get("status"),
        "recommended_structure": analyze_row.get("recommended_structure"),
        "signal_score":     analyze_row.get("signal_score"),
        "candidate":        candidate,
        "expiry":           trade.get("expiry"),
        "dte":              trade.get("dte_at_entry"),
        # Tier 1-5 fields not available from analyze_ticker — left None
        "vol_oi_ratio": None, "iv_skew": None, "iv_term_slope": None,
        "otm_pcr": None, "beta_60d": None, "atr_pct": None, "iv_rank_52w": None,
        "sector_etf": None, "sector_trend": None, "sector_rsi": None, "sector_iv_ratio": None,
        "spy_trend": None, "spy_rsi": None, "qqq_trend": None, "qqq_rsi": None,
        "iwm_trend": None, "iwm_rsi": None, "vvix": None, "vix_3m": None,
        "vix_term_slope": None, "earnings_inside_expiry": None,
        "news_sentiment_score": None, "analyst_rec_change": None, "short_interest_pct": None,
        "iv_skew_20d": None, "gex_proxy": None, "max_pain_strike": None,
        "oi_concentration": None, "wings_iv_ratio": None,
        "yield_10y": None, "yield_3m": None, "yield_curve": None,
        "dollar_index": None, "fed_within_dte": None, "cpi_within_dte": None,
        # Paper trade tracking fields
        "source":           "paper_trade_entry",
        "paper_trade_id":   trade["id"],
        # ML model state at entry — for post-hoc audit of signal accuracy
        **_ml_scores_at_entry(ticker),
        "labeled":          False,
        "outcome":          None,
        "labeled_at":       None,
    }
    try:
        insert_snapshot(record)
        log.info(f"Training snapshot written for paper trade {trade['id']}")
    except Exception as e:
        log.warning(f"Failed to write training snapshot for {trade['id']}: {e}")


def get_dataset_summary() -> dict:
    """Quick health check on how much labeled data exists so far."""
    records = _load_all()
    labeled = [r for r in records if r.get("labeled") and r.get("outcome") and "win" in (r["outcome"] or {})]
    wins = sum(1 for r in labeled if r["outcome"]["win"])
    return {
        "total_snapshots": len(records),
        "labeled":         len(labeled),
        "unlabeled":       len(records) - len(labeled),
        "win_rate_pct":    round(wins / len(labeled) * 100, 1) if labeled else None,
    }
