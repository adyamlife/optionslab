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
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import yfinance as yf

log = logging.getLogger(__name__)

from config.watchlist import WATCHLIST
from scripts.analyze import analyze_ticker, days_to_earnings
from scripts.black_scholes import bs_price
from scripts.candidate_provider import _payoff_per_share
from scripts.data_fetch import (
    pick_expiry, _refresh_yf_crumb,
    get_otm_pcr, get_beta, get_atr_pct, get_iv_rank_52w,
    get_sector_context, get_index_trends, get_vix_context, get_macro_context,
    get_news_sentiment_score, get_analyst_rec_change,
    _days_to_next_event, _opex_info, _ALL_FOMC, _ALL_CPI, _ALL_PPI, _ALL_JOBS,
)
from scripts.market_context import get_market_context
from scripts.paper_trade_engine import fetch_underlying_price

_ROOT = Path(__file__).resolve().parent.parent

# How many strikes either side of ATM to store per expiry
_CHAIN_STRIKE_RADIUS = 10

# Labeling schema version — bump when any labeling logic changes semantics
LABEL_SCHEMA_VERSION = 1

# Fingerprint of the current quality rule set (update when thresholds change)
QUALITY_RULESET_FINGERPRINT = "ruleset_v1_2026-07-28"

# Structures where a directional win is defined
_DIRECTION_WIN_MAP = {
    "Call Debit Spread":  lambda fwd, em: fwd > 0,
    "Put Debit Spread":   lambda fwd, em: fwd < 0,
    "Call Credit Spread": lambda fwd, em: fwd < 0,
    "Put Credit Spread":  lambda fwd, em: fwd > 0,
    "Iron Condor":        lambda fwd, em: abs(fwd) < em,
    "Jade Lizard":        lambda fwd, em: fwd >= 0,
    "Long Strangle":      lambda fwd, em: abs(fwd) > em,
    "Long Straddle":      lambda fwd, em: abs(fwd) > em,
    # Calendar Spread profits when underlying stays near short strike at short-leg expiry
    "Calendar Spread":    lambda fwd, em: abs(fwd) < em,
}


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
        if g.get("iv") is not None:
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


def write_scan_decision(
    meta: dict,
    scan_decision_id: str,
    scan_date: str,
    scan_time: str,
    trade_ids: list | None = None,
) -> None:
    """
    Persist one scan decision to the scan_decisions table in DuckDB.

    Called once per morning/afternoon scan regardless of TRADE or NO_TRADE
    outcome.  This is the single write path for Phase 2 observational data —
    no separate JSONL files, no side channels.

    meta: the dict appended to _decision_meta_out by rank_candidates (P1-C).
    trade_ids: list of paper trade IDs entered this scan (empty for NO_TRADE).
    """
    from scripts.db import insert_scan_decision as _ins
    _ins({
        "scan_decision_id":               scan_decision_id,
        "scan_date":                      scan_date,
        "scan_time":                      scan_time,
        "status":                         meta.get("status"),
        "reason":                         meta.get("reason"),
        "best_score":                     meta.get("best_score"),
        "required_score":                 meta.get("required_score"),
        "candidates_evaluated":           meta.get("candidates_evaluated"),
        "candidates_survived_filter":     meta.get("candidates_survived_filter"),
        "candidates_cleared_quality_gate": meta.get("candidates_cleared_quality_gate"),
        "portfolio_eligible":             meta.get("portfolio_eligible"),
        "market_regime":                  meta.get("market_regime"),
        "n_trades_entered":               len(trade_ids or []),
        "trade_ids":                      trade_ids or [],
        "decision_snapshot":              meta.get("decision_snapshot", []),
        "repair_summary":                 meta.get("repair_summary", []),
        "created_at":                     datetime.now().isoformat(),
    })


def _fetch_garch_vol(ticker: str) -> float | None:
    """Return GARCH(1,1) conditional vol (annualized) from saved model, or None."""
    try:
        from scripts.train_garch_model import get_garch_forecast
        return get_garch_forecast(ticker)
    except Exception:
        return None


def _build_gate_summary(candidates: list, row: dict) -> dict:
    """
    Build a compact record of every candidate's gate outcome for this snapshot.

    Stored as gate_summary JSON so we can later answer:
      - How often did the optimizer produce trades that failed liquidity?
      - Which risk gate rejects the most candidates?
      - Would relaxing expected_move_max_ratio increase trade availability?

    Schema:
      {
        "n_evaluated":         int,   total candidates optimizer found
        "n_risk_rejected":     int,   failed check_risk_gates()
        "n_liquidity_rejected":int,   had no bid/ask (no pop/ev)
        "data_quality_ok":     bool,  from validate_price_data
        "chain_quality_ok":    bool,  from validate_chain
        "candidates": [
          {
            "structure":         str,
            "recommended":       bool,
            "risk_rejected":     bool,
            "risk_notes":        [str],
            "liquidity_ok":      bool,   False when pop is None (illiquid legs)
          }, ...
        ]
      }
    """
    data_ok  = (row.get("data_quality")  or {}).get("ok")
    chain_ok = (row.get("chain_quality") or {}).get("ok")

    cand_records = []
    n_risk = 0
    n_liq  = 0
    for c in candidates:
        risk_rej  = bool(c.get("risk_rejected"))
        liq_ok    = c.get("pop") is not None   # pop=None means illiquid / no valid combo
        if risk_rej:
            n_risk += 1
        if not liq_ok:
            n_liq += 1
        cand_records.append({
            "structure":     c.get("structure"),
            "recommended":   bool(c.get("recommended")),
            "risk_rejected": risk_rej,
            "risk_notes":    c.get("risk_notes") or [],
            "liquidity_ok":  liq_ok,
        })

    return {
        "n_evaluated":          len(candidates),
        "n_risk_rejected":      n_risk,
        "n_liquidity_rejected": n_liq,
        "data_quality_ok":      data_ok,
        "chain_quality_ok":     chain_ok,
        "candidates":           cand_records,
    }


def _partial_label_candidate(candidate: dict) -> dict:
    """
    Compute trade_feasible and trade_quality immediately at collection time.
    direction_correct / realized_pnl / win are left None — filled by the
    daily label_rejected_candidates() pass once forward returns are available.
    """
    details = (candidate.get("details") or "").strip()

    # Feasibility: option chain had no usable strikes or was illiquid
    not_feasible_phrases = (
        "No strikes found",
        "Illiquid",
        "wide bid",
        "net credit <=0",
        "cannot price",
    )
    trade_feasible = not any(p.lower() in details.lower() for p in not_feasible_phrases)

    # Quality: trade passed all risk/reward filters (no "[Best EV]" fallback label)
    if not trade_feasible:
        trade_quality = None
    else:
        meets_min  = candidate.get("meets_min_profit")
        meets_loss = candidate.get("meets_max_loss")
        if meets_min is None and meets_loss is None:
            # Older candidates without explicit filter flags — infer from details
            trade_quality = "best ev" not in details.lower() and "below" not in details.lower()
        else:
            trade_quality = bool(meets_min) and bool(meets_loss)

    return {
        "trade_feasible":        trade_feasible,
        "trade_quality":         trade_quality,
        "direction_correct":     None,   # filled by label_rejected_candidates()
        "realized_pnl":          None,   # filled at expiry (CCS only)
        "win":                   None,   # filled at expiry
        "unlabelable":           False,
        "method":                "partial_at_collection",
        "label_schema_version":  LABEL_SCHEMA_VERSION,
        "quality_rule_set":      QUALITY_RULESET_FINGERPRINT,
        "settlement_source":     None,
        "label_confidence":      None,
        # eligibility masks — train_outcome requires win to be filled first
        "train_feasibility":     True,
        "train_quality":         trade_feasible is True,
        "train_direction":       False,  # flipped True once direction_correct is set
        "train_outcome":         False,  # flipped True once win is set
    }


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

    _now = datetime.now().isoformat()
    return {
        "snapshot_id":           f"{ticker}-{_now}",
        "collected_at":          _now,
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
        "signal_pct":            row.get("signal_pct"),
        "candidate":             recommended,
        "expiry":                (recommended or {}).get("expiry") or row.get("expiry"),
        "dte":                   (recommended or {}).get("dte") or row.get("dte"),
        # ── Tier 1 fields ──────────────────────────────────────────────────────
        "vol_oi_ratio":          row.get("vol_oi_ratio"),
        "call_vol":              row.get("call_vol"),
        "put_vol":               row.get("put_vol"),
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
        "sector_return_1d":      sector_ctx.get("sector_return_1d"),
        "spy_trend":             idx.get("spy_trend"),
        "spy_rsi":               idx.get("spy_rsi"),
        "qqq_trend":             idx.get("qqq_trend"),
        "qqq_rsi":               idx.get("qqq_rsi"),
        "iwm_trend":             idx.get("iwm_trend"),
        "iwm_rsi":               idx.get("iwm_rsi"),
        "vvix":                  vix.get("vvix"),
        "vix_3m":                vix.get("vix_3m"),
        "vix_term_slope":        vix.get("vix_term_slope"),
        "move_index":            vix.get("move_index"),
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
        "iv_change_5d":     row.get("iv_change_5d"),        # ATM IV Δ vs 5 trading days ago
        "unusual_activity": row.get("unusual_activity"),    # max per-strike OI spike ratio vs rolling avg
        "iv_hv_ratio":      row.get("iv_hv_ratio"),
        "expected_move_pct": row.get("expected_move_pct"),
        "term_slope":       row.get("term_slope"),
        "vol_pcr":          row.get("vol_pcr"),
        "pcr_diverge":      row.get("pcr_diverge"),
        "hy_oas":           row.get("hy_oas"),
        # ── Tier 5 fields (macro context, market-wide, passed from caller) ─────
        "yield_10y":           (macro_ctx or {}).get("yield_10y"),
        "yield_3m":            (macro_ctx or {}).get("yield_3m"),
        "yield_curve":         (macro_ctx or {}).get("yield_curve"),
        "dollar_index":        (macro_ctx or {}).get("dollar_index"),
        "fed_within_dte":      (macro_ctx or {}).get("fed_within_dte"),
        "cpi_within_dte":      (macro_ctx or {}).get("cpi_within_dte"),
        "ppi_days_away":       (macro_ctx or {}).get("ppi_days_away"),
        "ppi_within_dte":      (macro_ctx or {}).get("ppi_within_dte"),
        "jobs_days_away":      (macro_ctx or {}).get("jobs_days_away"),
        "jobs_within_dte":     (macro_ctx or {}).get("jobs_within_dte"),
        "days_to_opex":        (macro_ctx or {}).get("days_to_opex"),
        "opex_within_dte":     (macro_ctx or {}).get("opex_within_dte"),
        "is_opex_week":        (macro_ctx or {}).get("is_opex_week"),
        "is_monthly_opex":     (macro_ctx or {}).get("is_monthly_opex"),
        "is_quarterly_opex":   (macro_ctx or {}).get("is_quarterly_opex"),
        # ── Tier 6: cross-sectional percentile ranks (filled by collect_snapshots) ──
        "iv_pct_rank":         None,
        "gamma_pct_rank":      None,
        "volume_pct_rank":     None,
        "momentum_pct_rank":   None,
        "oi_pct_rank":         None,
        # ── Forward return labels (filled by label_snapshots_with_forward_returns) ──
        "forward_1d":          None,
        "forward_3d":          None,
        "forward_5d":          None,
        "future_hv5d":         None,
        # ── Gate rejection summary (#8/#14/#15) ──────────────────────────────
        # Captures every candidate's rejection status at collection time so we
        # can answer post-hoc: which gate fires most? what's trade availability?
        "gate_summary":          _build_gate_summary(candidates, row),
        # ───────────────────────────────────────────────────────────────────────
        "labeled":               False,
        "outcome":               _partial_label_candidate(recommended) if recommended else None,
        "labeled_at":            None,
    }


def _fetch_chain_yfinance(ticker: str, expiry: str, spot: float) -> list[dict]:
    """
    Fetch ±RADIUS strikes around ATM from yfinance for one expiry.
    yfinance does not provide per-strike Greeks — those fields are None.
    Returns [] on any failure.  Retries once after crumb refresh on error.
    """
    def _do_fetch() -> list[dict]:
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
                bid = float(row.get("bid") or 0)
                ask = float(row.get("ask") or 0)
                mid = round((bid + ask) / 2, 4)
                rows.append({
                    "strike":        float(row["strike"]),
                    "opt_type":      opt_type,
                    "bid":           bid,
                    "ask":           ask,
                    "mid":           mid,
                    "nbbo_width":    round(ask - bid, 4),
                    "bid_ask_pct":   round((ask - bid) / mid, 4) if mid else None,
                    "bid_size":      None,
                    "ask_size":      None,
                    "iv":            float(row.get("impliedVolatility") or 0),
                    "delta":         None,
                    "gamma":         None,
                    "theta":         None,
                    "vega":          None,
                    "volume":        int(row.get("volume") or 0),
                    "open_interest": int(row.get("openInterest") or 0),
                })
        return rows

    try:
        return _do_fetch()
    except Exception:
        _refresh_yf_crumb()
        try:
            return _do_fetch()
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
                mid = round((bid + ask) / 2, 4)
                def _greek(v):
                    return None if v is None else float(v)
                rows.append({
                    "strike":        float(row["strike"]),
                    "opt_type":      opt_type,
                    "bid":           bid,
                    "ask":           ask,
                    "mid":           mid,
                    "nbbo_width":    round(ask - bid, 4),
                    "bid_ask_pct":   round((ask - bid) / mid, 4) if mid else None,
                    "bid_size":      _greek(row.get("bidSize") or row.get("bid_size")),
                    "ask_size":      _greek(row.get("askSize") or row.get("ask_size")),
                    "iv":            float(row.get("impliedVolatility") or 0),
                    "delta":         _greek(row.get("delta")),
                    "gamma":         _greek(row.get("gamma")),
                    "theta":         _greek(row.get("theta")),
                    "vega":          _greek(row.get("vega")),
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

        _now = datetime.now().isoformat()
        return {
            "snapshot_id": f"{ticker}-{_now}",
            "collected_at": _now,
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
    mid = round((bid + ask) / 2, 4)
    def _greek(v):
        return None if v is None else float(v)
    return {
        "strike":        float(row["strike"]),
        "opt_type":      opt_type,
        "bid":           bid,
        "ask":           ask,
        "mid":           mid,
        "nbbo_width":    round(ask - bid, 4),
        "bid_ask_pct":   round((ask - bid) / mid, 4) if mid else None,
        "bid_size":      _greek(row.get("bidSize") or row.get("bid_size")),
        "ask_size":      _greek(row.get("askSize") or row.get("ask_size")),
        "iv":            float(row.get("impliedVolatility") or 0),
        "delta":         _greek(row.get("delta")),
        "gamma":         _greek(row.get("gamma")),
        "theta":         _greek(row.get("theta")),
        "vega":          _greek(row.get("vega")),
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


def _pct_rank(values: list) -> list[float | None]:
    """
    Return percentile ranks (0–100) for a list of values.
    None values get rank None; ties share the average rank.
    """
    indexed = [(v, i) for i, v in enumerate(values) if v is not None]
    result = [None] * len(values)
    if not indexed:
        return result
    indexed.sort(key=lambda x: x[0])
    n = len(indexed)
    for rank_idx, (_, orig_idx) in enumerate(indexed):
        result[orig_idx] = round(rank_idx / max(n - 1, 1) * 100, 1)
    return result


def _add_xsec_ranks(rows: list[dict]) -> None:
    """
    Compute cross-sectional percentile ranks across all rows in the batch and
    set rank fields in-place. Operates on the in-memory list before DB insert.

    Fields ranked:
      atm_iv            → iv_pct_rank        (high IV relative to watchlist)
      gex_proxy         → gamma_pct_rank     (gamma exposure concentration)
      call_vol+put_vol  → volume_pct_rank    (total option volume vs peers)
      rsi               → momentum_pct_rank  (14-day RSI as momentum proxy)
      vol_oi_ratio      → oi_pct_rank        (option activity relative to OI)
    """
    if not rows:
        return
    fields = [
        ("atm_iv",       "iv_pct_rank"),
        ("gex_proxy",    "gamma_pct_rank"),
        (None,           "volume_pct_rank"),   # derived: call_vol + put_vol
        ("rsi",          "momentum_pct_rank"),
        ("vol_oi_ratio", "oi_pct_rank"),
    ]
    # Volume is a derived column not stored directly — build it
    vol_vals = [
        ((r.get("call_vol") or 0) + (r.get("put_vol") or 0)) or None
        for r in rows
    ]
    for src_field, dst_field in fields:
        if src_field is None:
            vals = vol_vals
        else:
            vals = [r.get(src_field) for r in rows]
        ranks = _pct_rank(vals)
        for row, rank in zip(rows, ranks):
            row[dst_field] = rank


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
    from scripts.data_fetch import warmup_data_sources
    warmup_data_sources(log)

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

    # Build all records in parallel — tickers are independent; shared market
    # data (spy_hist, macro_ctx, etc.) is read-only so safe to share across threads.
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    _MAX_WORKERS = 10

    def _fetch_one(ticker):
        return _build_snapshot_record(
            ticker, vix_price,
            spy_hist=spy_hist,
            index_trends=index_trends,
            vix_ctx=vix_ctx,
            macro_ctx=macro_ctx,
        )

    pending_rows: list[dict] = []
    collected, errors = [], []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as _pool:
        _futures = {_pool.submit(_fetch_one, t): t for t in WATCHLIST}
        for fut in _as_completed(_futures):
            ticker = _futures[fut]
            try:
                pending_rows.append(fut.result())
                collected.append(ticker)
            except Exception as e:
                errors.append({"ticker": ticker, "error": str(e)})

    # Compute cross-sectional percentile ranks across the collected batch
    _add_xsec_ranks(pending_rows)
    for row in pending_rows:
        _append(row)

    chain_result = collect_option_chain_snapshots()

    return {
        "collected":       len(collected),
        "tickers":         collected,
        "errors":          errors,
        "chain_collected": chain_result["collected"],
        "chain_errors":    chain_result["errors"],
    }


_CAL_BACK_RE    = re.compile(r"BUY [\d.]+([CP]) \((\d{4}-\d{2}-\d{2}),")
_CAL_BACK_IV_RE = re.compile(r"back IV (\d+)%")
_CAL_STRIKE_RE  = re.compile(r"SELL ([\d.]+)[CP]")  # "back IV 77%" in details

_RISK_FREE_RATE = 0.03  # constant used for BS approximations


def _label_calendar_bs(r: dict, front_exp_date: date, s_front: float) -> dict | None:
    """
    Approximate P&L for a Calendar or Diagonal Spread at front-leg expiry.

    Method (3B): value the surviving back leg with Black-Scholes using the
    best available IV estimate and the remaining time to back_expiry.  Net out
    the short-leg intrinsic and the original net debit.

    IV resolution order:
      1. Row-level atm_iv (entry-time IV stored at scan time)
      2. Back-leg IV parsed from the details string ("back IV 77%")
      3. Row-level hv20 (historical vol as rough proxy)
      4. 0.30 (30% default — better than refusing to label)

    Caveats stored in the outcome dict so downstream code can filter:
    - iv_source: which fallback was used
    - approx: True — marks this label as model-generated, not market price

    Returns None when required structural fields are missing or un-parseable.
    """
    candidate = r.get("candidate") or {}
    details   = candidate.get("details", "") or ""

    m = _CAL_BACK_RE.search(details)
    if not m:
        return None
    opt_type    = "call" if m.group(1) == "C" else "put"
    back_expiry = date.fromisoformat(m.group(2))

    tau_back = (back_expiry - front_exp_date).days / 365.0
    if tau_back <= 0:
        return None

    debit = candidate.get("max_loss")
    try:
        if debit is None or not (debit >= 0):  # catches None and NaN
            return None
    except TypeError:
        return None

    # IV fallback chain.  pandas returns NaN (not None) for SQL NULLs, so we
    # must use `not (iv > 0)` rather than `iv <= 0` to catch NaN correctly.
    def _valid_iv(v) -> bool:
        try:
            return v is not None and v > 0
        except TypeError:
            return False  # NaN or other non-comparable type

    iv_source = "entry_atm_iv"
    iv = r.get("atm_iv")
    if not _valid_iv(iv):
        m_iv = _CAL_BACK_IV_RE.search(details)
        if m_iv:
            iv = float(m_iv.group(1)) / 100.0
            iv_source = "details_back_iv"
    if not _valid_iv(iv):
        hv = r.get("hv20")
        if _valid_iv(hv):
            iv = hv
            iv_source = "hv20_fallback"
    if not _valid_iv(iv):
        iv = 0.30
        iv_source = "default_30pct"

    structure = candidate.get("structure", "")
    if structure == "Calendar Spread":
        # Both legs share the same ATM strike
        k_long  = candidate.get("short_strike") or candidate.get("long_strike")
        if k_long is None:
            # Old rows don't have strike fields — parse from details string
            # e.g. "SELL 165.0C (2026-07-17, 17d) / BUY 165.0C (2026-08-07, 38d)"
            m_k = _CAL_STRIKE_RE.search(details)
            if m_k:
                k_long = float(m_k.group(1))
        k_short = k_long
    else:
        # Diagonal: long_strike on the back leg, short_strike on the front leg
        k_long  = candidate.get("long_strike")
        k_short = candidate.get("short_strike")

    if None in (k_long, k_short):
        return None

    back_leg_value    = bs_price(s_front, k_long, tau_back, _RISK_FREE_RATE, iv, opt_type)
    if opt_type == "call":
        short_leg_assign  = max(s_front - k_short, 0.0)
    else:
        short_leg_assign  = max(k_short - s_front, 0.0)

    pnl = round(float(back_leg_value - short_leg_assign - debit), 4)
    return {
        "pnl_per_share":        pnl,
        "win":                  bool(pnl > 0),
        "spot_at_front_expiry": round(float(s_front), 2),
        "back_leg_bs_value":    round(float(back_leg_value), 4),
        "short_leg_assign":     round(float(short_leg_assign), 4),
        "iv_source":            iv_source,
        "approx":               True,
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
            try:
                dte = float(r.get("dte") or 0)
            except (TypeError, ValueError):
                dte = 0.0
            core = {
                "pnl_per_share":     round(pnl, 4),
                "win":               bool(win),
                "realized_pnl":      round(pnl, 4),
                "trade_feasible":    True,
                "trade_quality":     True,
                "direction_correct": None,   # filled by label_rejected_candidates() backfill pass once forward_1d/3d/5d are available
                "exit_reason":       exit_data.get("reason"),
                "pnl_pct_of_max":    exit_data.get("pnl_pct_of_max"),
                "hit_tp":            exit_data.get("hit_tp"),
                "hit_sl":            exit_data.get("hit_sl"),
                "mae_pct":           exit_data.get("mae_pct"),
                "mfe_pct":           exit_data.get("mfe_pct"),
                "days_held":         exit_data.get("days_held"),
            }
            r["labeled"] = True
            r["outcome"] = {**core, **_eligibility_masks(core),
                            **_provenance("paper_trade_exit", "paper_trade_engine", dte)}
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
        cache_key = (ticker, str(exp_date))
        if cache_key not in price_cache:
            try:
                from scripts.db import connect as _dbconnect, lookup_expiry_settlement
                with _dbconnect(read_only=True) as _con:
                    _s, _ = lookup_expiry_settlement(_con, ticker, str(exp_date))
                if _s is not None:
                    price_cache[cache_key] = _s
                else:
                    import yfinance as _yf
                    _h = _yf.Ticker(ticker).history(start=str(exp_date), end=str(exp_date + timedelta(days=3)))
                    price_cache[cache_key] = float(_h["Close"].iloc[0]) if not _h.empty else None
            except Exception:
                price_cache[cache_key] = None
        s_t = price_cache[cache_key]
        if s_t is None:
            continue  # couldn't fetch — try again next run

        pnl_arr = _payoff_per_share(r["candidate"].get("structure"), r["candidate"], np.array([s_t]))
        if pnl_arr is None:
            structure = (r.get("candidate") or {}).get("structure", "")
            if structure in ("Calendar Spread", "Diagonal Spread"):
                bs_outcome = _label_calendar_bs(r, exp_date, s_t)
                if bs_outcome is not None:
                    try:
                        dte = float(r.get("dte") or 0)
                    except (TypeError, ValueError):
                        dte = 0.0
                    bs_outcome.update({
                        "trade_feasible": True, "trade_quality": True,
                        "direction_correct": None, "realized_pnl": bs_outcome.get("pnl_per_share"),
                        **_eligibility_masks({**bs_outcome, "trade_feasible": True, "trade_quality": True}),
                        **_provenance("calendar_bs_approx", "yfinance", dte),
                    })
                    r["labeled"] = True
                    r["outcome"] = bs_outcome
                    r["labeled_at"] = datetime.now().isoformat()
                    labeled_count += 1
                    continue
            # Genuinely unlabelable (path-dependent, missing fields)
            r["labeled"] = True
            r["outcome"] = {
                "unlabelable": True, "trade_feasible": True, "trade_quality": True,
                "win": None, "realized_pnl": None, "direction_correct": None,
                **_eligibility_masks({"trade_feasible": True, "trade_quality": True, "win": None}),
                **_provenance("unlabelable", "none", 0.0),
            }
            r["labeled_at"] = datetime.now().isoformat()
            labeled_count += 1
            continue

        try:
            dte = float(r.get("dte") or 0)
        except (TypeError, ValueError):
            dte = 0.0
        pnl = float(pnl_arr[0])
        core = {
            "pnl_per_share":     round(pnl, 4),
            "realized_pnl":      round(pnl, 4),
            "win":               pnl > 0,
            "spot_at_expiry":    round(s_t, 2),
            "trade_feasible":    True,
            "trade_quality":     True,
            "direction_correct": None,
        }
        r["labeled"] = True
        r["outcome"] = {**core, **_eligibility_masks(core),
                        **_provenance("hold_to_expiry", "yfinance", dte)}
        r["labeled_at"] = datetime.now().isoformat()
        r["spot_at_expiry"] = round(s_t, 2)
        r["zone_at_expiry"] = _zone_at_expiry(r.get("candidate"), s_t)
        labeled_count += 1

    from scripts.db import update_snapshot_labels
    labeled_records = [r for r in records if r.get("labeled") and r.get("snapshot_id")]
    update_snapshot_labels(labeled_records)
    return {"labeled": labeled_count, "total_records": len(records)}


def backfill_historical_settlements() -> dict:
    """
    One-time (or periodic) backfill of canonical settlement prices for all
    historical expiry dates found in training_snapshots.

    yfinance provides daily closing prices going back years, so this can
    recover exact settlement for every past expiry — not just tonight's.
    Prices are stored in expiry_settlements; all downstream labelers
    (relabel_previously_unlabelable, relabel_low_confidence_labels,
    label_rejected_candidates) pick them up automatically via _lookup_settlement.

    Groups by ticker to minimise API calls (one history() fetch per ticker,
    reads all needed expiry dates from the returned DataFrame).

    Returns {"stored": N, "tickers_fetched": K, "already_stored": M, "errors": [...]}.
    """
    from scripts.db import connect as _dbconnect, upsert_expiry_settlement, ensure_archive_tables

    ensure_archive_tables()
    records = _load_all()
    today_str = date.today().isoformat()

    # Collect all (ticker, expiry_date) pairs from snapshots with past expiry
    all_pairs: dict[str, set[str]] = {}   # ticker → {date_str, ...}
    for r in records:
        raw_expiry = r.get("expiry")
        try:
            expiry = str(raw_expiry)[:10] if raw_expiry and str(raw_expiry) != "nan" else ""
        except Exception:
            expiry = ""
        ticker = r.get("ticker") or ""
        if not expiry or not ticker or expiry >= today_str:
            continue
        all_pairs.setdefault(ticker, set()).add(expiry)

    total_pairs = sum(len(v) for v in all_pairs.values())

    # Remove already-stored pairs (idempotent — skip what's done)
    already_stored = 0
    needed = {tkr: set(dates) for tkr, dates in all_pairs.items()}
    try:
        with _dbconnect(read_only=True) as con:
            existing = con.execute(
                "SELECT ticker, date FROM expiry_settlements"
            ).fetchall()
        for tkr, dt in existing:
            if tkr in needed and dt in needed[tkr]:
                needed[tkr].discard(dt)
                already_stored += 1
                if not needed[tkr]:
                    del needed[tkr]
    except Exception:
        pass  # table may not exist yet — ensure_archive_tables() creates it below

    # Phase 1: fetch all prices from yfinance (no DB writes yet)
    to_write: list[tuple[str, str, float]] = []   # (ticker, date, price)
    tickers_fetched, errors = [], []

    for ticker, expiry_dates in sorted(needed.items()):
        try:
            hist = yf.Ticker(ticker).history(period="max")
            if hist.empty:
                errors.append({"ticker": ticker, "error": "no history"})
                continue
            hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
            date_to_close = {str(d.date()): float(c) for d, c in zip(hist.index, hist["Close"])}
            tickers_fetched.append(ticker)

            for exp_str in sorted(expiry_dates):
                price = date_to_close.get(exp_str)
                if price is None:
                    exp_d = date.fromisoformat(exp_str)
                    for delta in range(1, 6):
                        prev = str(exp_d - timedelta(days=delta))
                        if prev in date_to_close:
                            price = date_to_close[prev]
                            break
                if price is None:
                    errors.append({"ticker": ticker, "expiry": exp_str, "error": "no close found"})
                    continue
                to_write.append((ticker, exp_str, price))
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

    # Phase 2: write all settlements in a single DB connection
    stored = 0
    if to_write:
        from datetime import datetime as _dt
        now_iso = _dt.now().isoformat()
        try:
            with _dbconnect() as con:
                for ticker, exp_str, price in to_write:
                    con.execute(
                        "INSERT OR REPLACE INTO expiry_settlements "
                        "(ticker, date, settlement_price, source, retrieved_at, confidence) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        [ticker, exp_str, price, "yfinance_history", now_iso, 1.0],
                    )
                con.commit()
                stored = len(to_write)
        except Exception as e:
            errors.append({"batch_write": str(e)})

    log.info(f"backfill_historical_settlements: stored={stored}/{total_pairs} tickers={len(tickers_fetched)} already={already_stored} errors={len(errors)}")
    return {
        "stored":          stored,
        "total_pairs":     total_pairs,
        "already_stored":  already_stored,
        "tickers_fetched": len(tickers_fetched),
        "errors":          errors,
    }


def relabel_previously_unlabelable() -> dict:
    """
    Re-process snapshots that were previously marked unlabelable=True.

    Call this once after adding new payoff handlers — it finds every row that
    the old labeler gave up on and retries with the current _payoff_per_share
    and _label_calendar_bs implementations.  Already-correct real labels are
    never touched.

    Uses _lookup_settlement so it benefits from backfill_historical_settlements()
    without any extra yfinance calls when settlements are already stored.

    Returns {"relabeled": N, "still_unlabelable": M, "skipped_no_expiry": K}.
    """
    from scripts.db import connect as _dbconnect
    records = _load_all()
    today = date.today()
    price_cache: dict = {}
    relabeled = 0
    still_unlabelable = 0
    skipped = 0

    with _dbconnect() as con:
        for r in records:
            try:
                outcome = r.get("outcome") or {}
                if not (r.get("labeled") and outcome.get("unlabelable")):
                    continue
            except Exception:
                continue

            if not r.get("expiry") or not r.get("candidate"):
                skipped += 1
                continue

            try:
                exp_date = date.fromisoformat(str(r["expiry"])[:10])
            except Exception:
                skipped += 1
                continue

            if exp_date > today:
                skipped += 1
                continue

            ticker = r["ticker"]
            exp_str = str(exp_date)
            cache_key = (ticker, exp_str)

            if cache_key not in price_cache:
                # 1. Canonical settlement store (populated by backfill or evening check)
                s_t, settle_src = _lookup_settlement(con, ticker, exp_str)
                if s_t is None:
                    # 2. yfinance fallback for dates not yet in expiry_settlements
                    try:
                        _h = yf.Ticker(ticker).history(
                            start=exp_str, end=str(exp_date + timedelta(days=3))
                        )
                        s_t = float(_h["Close"].iloc[0]) if not _h.empty else None
                        settle_src = "yfinance_history" if s_t is not None else ""
                    except Exception:
                        s_t = None
                        settle_src = ""
                price_cache[cache_key] = (s_t, settle_src)

            s_t, settle_src = price_cache[cache_key]
            if s_t is None:
                skipped += 1
                continue

            structure = r["candidate"].get("structure", "")
            try:
                dte = float(r.get("dte") or 0)
            except (TypeError, ValueError):
                dte = 0.0

            pnl_arr = _payoff_per_share(structure, r["candidate"], np.array([s_t]))
            if pnl_arr is not None:
                pnl = float(pnl_arr[0])
                core = {
                    "pnl_per_share": round(pnl, 4), "win": pnl > 0,
                    "spot_at_expiry": round(s_t, 2),
                    "trade_feasible": True, "trade_quality": True,
                    "direction_correct": None, "realized_pnl": round(pnl, 4),
                }
                r["outcome"] = {**core, **_eligibility_masks(core),
                                **_provenance("hold_to_expiry", settle_src, dte)}
                r["labeled_at"] = datetime.now().isoformat()
                r["spot_at_expiry"] = round(s_t, 2)
                r["zone_at_expiry"] = _zone_at_expiry(r.get("candidate"), s_t)
                relabeled += 1
                continue

            if structure in ("Calendar Spread", "Diagonal Spread"):
                bs_outcome = _label_calendar_bs(r, exp_date, s_t)
                if bs_outcome is not None:
                    r["outcome"] = bs_outcome
                    r["labeled_at"] = datetime.now().isoformat()
                    relabeled += 1
                    continue

            still_unlabelable += 1

    from scripts.db import update_snapshot_labels
    changed = [r for r in records if r.get("snapshot_id")
               and r.get("labeled")
               and not (r.get("outcome") or {}).get("unlabelable")
               and r.get("labeled_at")]
    update_snapshot_labels(changed)
    return {"relabeled": relabeled, "still_unlabelable": still_unlabelable, "skipped_no_expiry": skipped}


def relabel_low_confidence_labels() -> dict:
    """
    Upgrade labels previously computed from forward-return estimates to exact
    settlement prices now available in expiry_settlements.

    Targets rows where:
      - labeled=True
      - settlement_source is forward_Nd (not intraday_bars or expiry_settlement)
      - expiry date is in the past
      - expiry_settlements now has an exact price for that (ticker, date)

    Re-runs the full labeling logic (direction_correct, realized_pnl, win,
    eligibility masks, provenance) and overwrites the outcome.

    Returns {"upgraded": N, "no_settlement": M, "skipped": K}.
    """
    from scripts.db import connect as _dbconnect
    records = _load_all()
    today = date.today()
    upgraded, no_settlement, skipped = 0, 0, 0
    changed = []

    _estimated_sources = {"forward_1d", "forward_3d", "forward_5d"}

    with _dbconnect() as con:
        for r in records:
            outcome = r.get("outcome") or {}
            src = outcome.get("settlement_source", "")

            # Only upgrade rows with estimated (forward-return) settlement
            if src not in _estimated_sources:
                continue
            if not r.get("labeled") or outcome.get("unlabelable"):
                continue

            expiry = (r.get("expiry") or "")[:10]
            ticker = r.get("ticker", "")
            if not expiry or not ticker:
                skipped += 1
                continue

            try:
                exp_date = date.fromisoformat(expiry)
            except Exception:
                skipped += 1
                continue

            if exp_date > today:
                skipped += 1
                continue

            # Check if exact settlement is now available
            s_t, new_src = _lookup_settlement(con, ticker, expiry)
            if s_t is None or new_src not in ("expiry_settlement", "intraday_bars"):
                no_settlement += 1
                continue

            candidate = r.get("candidate") or {}
            structure = candidate.get("structure", "")
            spot = r.get("spot")
            try:
                dte = float(r.get("dte") or 0)
            except (TypeError, ValueError):
                dte = 0.0

            # Recompute direction_correct with exact settlement
            fwd = (s_t / float(spot) - 1.0) if spot else None
            direction_fn = _DIRECTION_WIN_MAP.get(structure)
            direction_correct = None
            if fwd is not None and direction_fn is not None:
                try:
                    em = float(r.get("expected_move_pct") or 0)
                    direction_correct = bool(direction_fn(fwd, em))
                except Exception:
                    pass

            # Recompute realized_pnl if feasible
            trade_feasible = outcome.get("trade_feasible")
            trade_quality  = outcome.get("trade_quality")
            realized_pnl   = outcome.get("realized_pnl")
            win            = outcome.get("win")

            if trade_feasible:
                pnl_arr = _payoff_per_share(structure, candidate, np.array([s_t]))
                if pnl_arr is not None:
                    realized_pnl = round(float(pnl_arr[0]), 4)
                    win = realized_pnl > 0

            core = {
                "direction_correct": direction_correct,
                "trade_feasible":    trade_feasible,
                "trade_quality":     trade_quality,
                "realized_pnl":      realized_pnl,
                "win":               win,
                "spot_at_expiry":    round(s_t, 2),
                # Preserve any original fields not being upgraded
                **{k: v for k, v in outcome.items()
                   if k not in ("direction_correct", "trade_feasible", "trade_quality",
                                "realized_pnl", "win", "spot_at_expiry",
                                "settlement_source", "label_confidence", "method",
                                "train_feasibility", "train_quality", "train_direction", "train_outcome")},
            }
            r["outcome"] = {**core, **_eligibility_masks(core),
                            **_provenance("upgraded_from_estimate", new_src, dte)}
            r["labeled_at"] = datetime.now().isoformat()
            r["spot_at_expiry"] = round(s_t, 2)
            r["zone_at_expiry"] = _zone_at_expiry(candidate, s_t)
            changed.append(r)
            upgraded += 1

    from scripts.db import update_snapshot_labels
    if changed:
        update_snapshot_labels(changed)
    log.info(f"relabel_low_confidence_labels: upgraded={upgraded} no_settlement={no_settlement} skipped={skipped}")
    return {"upgraded": upgraded, "no_settlement": no_settlement, "skipped": skipped}


# ── Labeling helpers shared across all labeling functions ────────────────────

# Regex to parse CCS strikes from details: "SELL 1080.0C / BUY 1100.0C"
_CCS_STRIKES_RE = re.compile(r"SELL ([\d.]+)C\s*/\s*BUY ([\d.]+)C")


def _label_confidence(settlement_source: str, dte: float) -> float:
    """
    Deterministic confidence score in [0, 1] based on settlement source
    and the gap between the forward-return horizon and the option DTE.

    Confidence function version: 1 (bump LABEL_SCHEMA_VERSION when this changes).
      intraday_bars         → 1.00  (exact settlement session)
      intraday_bars_prev    → 0.85  (one session before expiry)
      forward_Nd proxy      → 1 - |N - DTE| / max(DTE, 1), clipped to [0, 1]
    """
    if settlement_source in ("intraday_bars", "expiry_settlement", "yfinance_history", "yfinance_eod"):
        return 1.0
    if settlement_source == "intraday_bars_prev":
        return 0.85
    # forward_1d / forward_3d / forward_5d — penalise horizon mismatch
    try:
        horizon = int(settlement_source.split("_")[-1].replace("d", ""))
    except Exception:
        horizon = 5
    return round(max(0.0, 1.0 - abs(horizon - dte) / max(dte, 1.0)), 2)


def _zone_at_expiry(candidate: dict, s_t: float) -> str | None:
    """Classify where s_t landed relative to the trade's strikes at expiry."""
    if s_t is None or candidate is None:
        return None
    structure = candidate.get("structure", "")

    if structure == "Iron Condor":
        pl = candidate.get("put_long_strike")
        ps = candidate.get("put_short_strike")
        cs = candidate.get("call_short_strike")
        cl = candidate.get("call_long_strike")
        if None in (pl, ps, cs, cl):
            return None
        if ps <= s_t <= cs:
            return "full_win"
        if (pl <= s_t < ps) or (cs < s_t <= cl):
            return "partial_win"
        return "loss"

    if structure in ("Call Debit Spread", "Call Credit Spread",
                     "Ratio Call Backspread"):
        k_long  = candidate.get("long_strike")
        k_short = candidate.get("short_strike")
        if None in (k_long, k_short):
            return None
        lo, hi = sorted([k_long, k_short])
        if s_t >= hi:
            return "full_win"
        if s_t >= lo:
            return "partial_win"
        return "loss"

    if structure in ("Put Debit Spread", "Put Credit Spread",
                     "Ratio Put Backspread"):
        k_long  = candidate.get("long_strike")
        k_short = candidate.get("short_strike")
        if None in (k_long, k_short):
            return None
        lo, hi = sorted([k_long, k_short])
        if s_t <= lo:
            return "full_win"
        if s_t <= hi:
            return "partial_win"
        return "loss"

    if structure == "Cash Secured Put":
        k = candidate.get("short_strike")
        if k is None:
            return None
        return "full_win" if s_t >= k else "loss"

    if structure == "Covered Call":
        k = candidate.get("short_strike")
        if k is None:
            return None
        return "partial_win" if s_t >= k else "full_win"

    if structure in ("Long Call", "Long Put"):
        k = candidate.get("long_strike") or candidate.get("short_strike")
        if k is None:
            return None
        if structure == "Long Call":
            return "full_win" if s_t > k else "loss"
        return "full_win" if s_t < k else "loss"

    if structure in ("Long Strangle", "Long Straddle"):
        k_put  = candidate.get("put_short_strike") or candidate.get("short_strike")
        k_call = candidate.get("call_short_strike") or candidate.get("long_strike")
        if None in (k_put, k_call):
            return None
        lo, hi = sorted([k_put, k_call])
        return "loss" if lo <= s_t <= hi else "full_win"

    if structure == "Short Strangle":
        k_put  = candidate.get("put_short_strike")
        k_call = candidate.get("call_short_strike")
        if None in (k_put, k_call):
            return None
        lo, hi = sorted([k_put, k_call])
        return "full_win" if lo <= s_t <= hi else "loss"

    if structure == "Risk Reversal":
        k_put  = candidate.get("short_strike")   # short put
        k_call = candidate.get("long_strike")    # long call
        if None in (k_put, k_call):
            return None
        if s_t >= k_call:
            return "full_win"
        if s_t <= k_put:
            return "loss"
        return "partial_win"

    return None


def _eligibility_masks(outcome: dict) -> dict:
    """
    Derive training eligibility masks from the four core targets.
    Every row contributes to train_feasibility; higher stages require
    the preceding stage's target to be non-null and passing.
    """
    feasible = outcome.get("trade_feasible")
    quality  = outcome.get("trade_quality")
    win      = outcome.get("win")
    return {
        "train_feasibility": True,
        "train_quality":     feasible is True,
        "train_direction":   outcome.get("direction_correct") is not None,
        "train_outcome":     feasible is True and quality is True and win is not None,
    }


def _provenance(method: str, settlement_source: str, dte: float) -> dict:
    """Standard provenance block appended to every outcome dict."""
    return {
        "method":               method,
        "settlement_source":    settlement_source,
        "label_confidence":     _label_confidence(settlement_source, dte),
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "quality_rule_set":     QUALITY_RULESET_FINGERPRINT,
    }

# Alias — label_rejected_candidates uses this name; authoritative map is _DIRECTION_WIN_MAP above.
_DIRECTION_WIN = _DIRECTION_WIN_MAP


def _pick_forward_return(r: dict) -> tuple[float | None, str | None]:
    """Return (forward_return_fraction, source_label) using nearest-DTE logic."""
    try:
        dte = float(r.get("dte") or 0)
    except (TypeError, ValueError):
        dte = 5.0
    candidates = []
    if r.get("forward_1d") is not None:
        candidates.append((1,  r["forward_1d"],  "forward_1d"))
    if r.get("forward_3d") is not None:
        candidates.append((3,  r["forward_3d"],  "forward_3d"))
    if r.get("forward_5d") is not None:
        candidates.append((5,  r["forward_5d"],  "forward_5d"))
    if not candidates:
        return None, None
    # prefer the source whose horizon is closest to dte (without going under if possible)
    candidates.sort(key=lambda x: abs(x[0] - dte))
    _, fwd, label = candidates[0]
    try:
        return float(fwd), label
    except (TypeError, ValueError):
        return None, None


def _lookup_settlement(con, ticker: str, expiry_str: str) -> tuple[float | None, str]:
    """
    Return (settlement_price, source) for ticker on expiry_str.

    Lookup order:
      1. expiry_settlements  — canonical, written by evening check at expiry (confidence=1.0)
      2. intraday_bars       — 5m bar archive written the next morning
      3. (caller handles)    — forward-return approximation

    Using db.lookup_expiry_settlement so the hierarchy lives in one place.
    """
    from scripts.db import lookup_expiry_settlement
    return lookup_expiry_settlement(con, ticker, expiry_str[:10])


def label_rejected_candidates() -> dict:
    """
    Label snapshots that are still marked unlabelable=True and represent
    rejected candidates from the morning scan (no valid setup, quality filter
    failures, illiquid spreads).

    Four targets are stored in the outcome JSON per row:
      direction_correct  — did the underlying move in the predicted direction?
      trade_feasible     — could a valid structure be constructed?
      trade_quality      — did it pass risk/reward filters? (None if not feasible)
      realized_pnl       — reconstructed terminal payoff (None if not feasible)

    Settlement hierarchy for realized_pnl:
      1. intraday_bars on or before expiry  (settlement_source = 'intraday_bars')
      2. intraday_bars nearest prior session (settlement_source = 'intraday_bars_prev')
      3. spot * (1 + forward_return)        (settlement_source = forward_Nd label)

    Returns counts by outcome type.
    """
    from scripts.db import load_all_snapshots, update_snapshot_labels, connect

    records  = load_all_snapshots()
    today    = date.today()
    now_iso  = datetime.now().isoformat()
    labeled  = 0
    no_fwd   = 0
    skipped  = 0
    changed  = []

    with connect() as con:
        for r in records:
            outcome   = r.get("outcome") or {}
            candidate = r.get("candidate") or {}

            # Target rows:
            #   (a) previously marked unlabelable=True  (legacy backfill)
            #   (b) labeled=False, non-paper-trade, expiry passed  (new daily flow)
            is_legacy_unlabelable = r.get("labeled") and outcome.get("unlabelable")
            is_new_rejected = (
                not r.get("labeled")
                and r.get("source") != "paper_trade_entry"
                and candidate
                and r.get("expiry")
            )
            if not is_legacy_unlabelable and not is_new_rejected:
                continue

            # For new rejected rows, only process once expiry has passed
            if is_new_rejected:
                try:
                    exp_date = date.fromisoformat(str(r["expiry"])[:10])
                except Exception:
                    skipped += 1
                    continue
                if exp_date > today:
                    skipped += 1
                    continue

            structure = candidate.get("structure", "")
            ticker    = r.get("ticker", "")
            spot      = r.get("spot")
            em_pct    = r.get("expected_move_pct") or 0.0
            details   = candidate.get("details", "") or ""

            try:
                dte = float(r.get("dte") or 0)
            except (TypeError, ValueError):
                dte = 5.0

            # ── settlement price (canonical store → intraday_bars → forward estimate) ─
            expiry_str = (r.get("expiry") or "")[:10]
            s_t, settlement_src = (None, "") if not expiry_str else \
                _lookup_settlement(con, ticker, expiry_str)

            # ── direction_correct ─────────────────────────────────────────
            # Prefer exact settlement-based direction; fall back to forward return.
            direction_fn = _DIRECTION_WIN.get(structure)
            fwd, fwd_src = _pick_forward_return(r)
            direction_correct = None

            if direction_fn is not None and spot:
                if s_t is not None:
                    # Exact: actual underlying move over the full trade horizon
                    try:
                        exact_fwd = float(s_t) / float(spot) - 1.0
                        direction_correct = bool(direction_fn(exact_fwd, em_pct))
                        if not settlement_src:
                            settlement_src = "expiry_settlement"
                    except Exception:
                        pass
                if direction_correct is None and fwd is not None:
                    # Fallback: forward return proxy (horizon may not match DTE)
                    direction_correct = bool(direction_fn(fwd, em_pct))
                    if not settlement_src:
                        settlement_src = fwd_src

            if direction_correct is None:
                no_fwd += 1
                continue

            # ── feasibility & quality ─────────────────────────────────────
            trade_feasible = outcome.get("trade_feasible")
            trade_quality  = outcome.get("trade_quality")
            if trade_feasible is None:
                no_setup_phrases = ("no strikes found", "illiquid", "wide bid", "net credit <=0", "cannot price")
                trade_feasible = not any(p in details.lower() for p in no_setup_phrases)
                if trade_feasible:
                    meets_min  = candidate.get("meets_min_profit")
                    meets_loss = candidate.get("meets_max_loss")
                    trade_quality = (bool(meets_min) and bool(meets_loss)) if (
                        meets_min is not None or meets_loss is not None
                    ) else ("best ev" not in details.lower())

            # ── realized_pnl (CCS only) ───────────────────────────────────
            realized_pnl = None
            win          = None
            method       = "rejected_no_valid_setup"

            if trade_feasible:
                if s_t is None and spot and fwd is not None:
                    s_t = float(spot) * (1.0 + float(fwd))
                    settlement_src = fwd_src

                m_ccs = _CCS_STRIKES_RE.search(details)
                if m_ccs and structure == "Call Credit Spread" and s_t is not None:
                    k_short = float(m_ccs.group(1))
                    k_long  = float(m_ccs.group(2))
                    credit  = float(candidate.get("max_profit") or 0.0)
                    if s_t <= k_short:
                        realized_pnl = round(credit, 4)
                    elif s_t >= k_long:
                        realized_pnl = round(credit - (k_long - k_short), 4)
                    else:
                        realized_pnl = round(credit - (s_t - k_short), 4)
                    win    = realized_pnl > 0
                    method = "reconstructed_ccs"
                else:
                    method = "rejected_quality_filter"

            # ── label_confidence — use shared helper (covers all source types) ──
            confidence = _label_confidence(settlement_src, dte)

            r["labeled"]    = True
            r["labeled_at"] = now_iso
            r["outcome"] = {
                # core targets
                "direction_correct":     direction_correct,
                "trade_feasible":        trade_feasible,
                "trade_quality":         trade_quality,
                "realized_pnl":          realized_pnl,
                "win":                   win,
                # eligibility masks
                "train_feasibility":     True,
                "train_quality":         trade_feasible is True,
                "train_direction":       True,
                "train_outcome":         trade_feasible is True and trade_quality is True and win is not None,
                # provenance
                "unlabelable":           False,
                "method":                method,
                "settlement_source":     settlement_src,
                "label_confidence":      confidence,
                "label_schema_version":  LABEL_SCHEMA_VERSION,
                "quality_rule_set":      QUALITY_RULESET_FINGERPRINT,
            }
            if s_t is not None:
                r["spot_at_expiry"] = round(float(s_t), 2)
                r["zone_at_expiry"] = _zone_at_expiry(candidate, float(s_t))
            changed.append(r)
            labeled += 1

    # ── Backfill direction_correct for closed paper trades ───────────────────
    # label_pending_snapshots() sets direction_correct=None because forward
    # returns aren't available yet at close time. label_snapshots_with_forward_returns()
    # fills forward_1d/3d/5d on those rows later the same day. This pass picks
    # them up and computes direction_correct using the same logic as rejected rows.
    direction_backfilled = 0
    direction_changed = []
    for r in records:
        outcome = r.get("outcome") or {}
        if (
            r.get("labeled")
            and r.get("source") == "paper_trade_entry"
            and outcome.get("direction_correct") is None
            and outcome.get("trade_feasible") is True
        ):
            fwd, _ = _pick_forward_return(r)
            if fwd is None:
                continue
            candidate = r.get("candidate") or {}
            structure = candidate.get("structure", "")
            direction_fn = _DIRECTION_WIN_MAP.get(structure)
            if direction_fn is None:
                continue
            try:
                em = float(candidate.get("expected_move") or candidate.get("expected_move_pct") or 0)
            except (TypeError, ValueError):
                em = 0.0
            try:
                outcome["direction_correct"] = bool(direction_fn(fwd, em))
                outcome["train_direction"] = True
                r["outcome"] = outcome
                direction_changed.append(r)
                direction_backfilled += 1
            except Exception:
                continue

    if direction_changed:
        update_snapshot_labels(direction_changed)

    update_snapshot_labels(changed)
    return {
        "labeled":              labeled,
        "no_fwd_data":          no_fwd,
        "skipped_future":       skipped,
        "total_attempted":      labeled + no_fwd + skipped,
        "direction_backfilled": direction_backfilled,
    }


def validate_labels(records: list[dict] | None = None) -> dict:
    """
    Task 5 — Invariant validator.

    Checks every labeled row against the labeling invariants.  Runs cheaply
    after every labeling pass and before writing the manifest.

    Returns {"ok": bool, "violations": [...], "checked": N}.
    """
    if records is None:
        records = _load_all()

    violations = []

    def _v(snap_id: str, msg: str):
        violations.append({"snapshot_id": snap_id, "violation": msg})

    for r in records:
        o = r.get("outcome") or {}
        sid = r.get("snapshot_id", "?")

        if not r.get("labeled"):
            continue  # unlabeled rows are not subject to outcome invariants

        feasible  = o.get("trade_feasible")
        quality   = o.get("trade_quality")
        win       = o.get("win")
        pnl       = o.get("realized_pnl")
        conf      = o.get("label_confidence")
        method    = o.get("method")

        # I1: trade_quality non-null only when trade_feasible=True
        if quality is not None and feasible is not True:
            _v(sid, f"trade_quality={quality!r} but trade_feasible={feasible!r}")

        # I2: realized_pnl non-null only when settlement_source is set
        if pnl is not None and not o.get("settlement_source"):
            _v(sid, "realized_pnl set but no settlement_source")

        # I3: win=True impossible when realized_pnl < 0
        if win is True and pnl is not None and pnl < 0:
            _v(sid, f"win=True but realized_pnl={pnl}")

        # I4: win=False impossible when realized_pnl > 0
        if win is False and pnl is not None and pnl > 0:
            _v(sid, f"win=False but realized_pnl={pnl}")

        # I5: train_outcome=True requires feasible+quality+win
        if o.get("train_outcome"):
            if feasible is not True or quality is not True or win is None:
                _v(sid, f"train_outcome=True but feasible={feasible} quality={quality} win={win}")

        # I6: label_confidence in [0, 1] when present
        if conf is not None:
            try:
                if not (0.0 <= float(conf) <= 1.0):
                    _v(sid, f"label_confidence={conf} out of [0,1]")
            except (TypeError, ValueError):
                _v(sid, f"label_confidence={conf!r} not numeric")

        # I7: direction_correct is boolean or None — never another type
        dc = o.get("direction_correct")
        if dc is not None and not isinstance(dc, bool):
            _v(sid, f"direction_correct={dc!r} is not bool")

        # I8: every labeled row must reference a schema version
        if o.get("label_schema_version") is None and method not in (None, ""):
            _v(sid, "labeled row missing label_schema_version")

    return {"ok": len(violations) == 0, "checked": len([r for r in records if r.get("labeled")]),
            "violations": violations}


def write_labeling_manifest(run_stats: dict) -> str:
    """
    Task 6 — Labeling manifest.

    Produces a JSON audit file in data/labeling_manifests/ for every labeling
    run.  The manifest captures schema version, code state, row counts by label
    type, and validation results — giving a reproducible audit trail for the
    dataset as a whole.

    Returns the path of the written manifest file.
    """
    import json as _json
    import subprocess as _sub

    manifest_dir = _ROOT / "data" / "labeling_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    # Code commit hash — best-effort
    try:
        commit = _sub.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_ROOT), stderr=_sub.DEVNULL, text=True
        ).strip()
    except Exception:
        commit = "unknown"

    # Row counts from the live DB
    from scripts.db import connect as _connect
    counts: dict = {}
    try:
        with _connect(read_only=True) as con:
            counts["total_snapshots"]   = con.execute("SELECT COUNT(*) FROM training_snapshots").fetchone()[0]
            counts["labeled"]           = con.execute("SELECT COUNT(*) FROM training_snapshots WHERE labeled=true").fetchone()[0]
            counts["unlabeled"]         = counts["total_snapshots"] - counts["labeled"]
            counts["train_feasibility"] = con.execute(
                "SELECT COUNT(*) FROM training_snapshots WHERE labeled=true "
                "AND json_extract_string(outcome,'$.train_feasibility')='true'").fetchone()[0]
            counts["train_quality"]     = con.execute(
                "SELECT COUNT(*) FROM training_snapshots WHERE labeled=true "
                "AND json_extract_string(outcome,'$.train_quality')='true'").fetchone()[0]
            counts["train_direction"]   = con.execute(
                "SELECT COUNT(*) FROM training_snapshots WHERE labeled=true "
                "AND json_extract_string(outcome,'$.train_direction')='true'").fetchone()[0]
            counts["train_outcome"]     = con.execute(
                "SELECT COUNT(*) FROM training_snapshots WHERE labeled=true "
                "AND json_extract_string(outcome,'$.train_outcome')='true'").fetchone()[0]
            counts["still_unlabelable"] = con.execute(
                "SELECT COUNT(*) FROM training_snapshots WHERE labeled=true "
                "AND json_extract_string(outcome,'$.unlabelable')='true'").fetchone()[0]
    except Exception as exc:
        counts["error"] = str(exc)

    # Validation
    validation = validate_labels()

    run_ts = datetime.now().isoformat()
    manifest = {
        "run_timestamp":            run_ts,
        "label_schema_version":     LABEL_SCHEMA_VERSION,
        "quality_rule_set":         QUALITY_RULESET_FINGERPRINT,
        "code_commit":              commit,
        "run_stats":                run_stats,
        "db_counts":                counts,
        "validation":               {
            "ok":         validation["ok"],
            "checked":    validation["checked"],
            "violations": len(validation["violations"]),
            # Store first 10 violations for diagnosis — full list in validate_labels()
            "sample_violations": validation["violations"][:10],
        },
    }

    fname = manifest_dir / f"manifest_{run_ts[:19].replace(':', '-')}.json"
    fname.write_text(_json.dumps(manifest, indent=2))
    log.info(f"Labeling manifest written: {fname}")
    return str(fname)


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
    Captures all signals needed for the Phase 2 trade outcome model.
    Returns all fields as None on cache miss — callers always get the full key set.
    """
    _empty = {
        "ml_meta_score":      None,
        "ml_p_win":           None,
        "ml_confidence":      None,
        "ml_ranker_score":    None,
        "ml_return_score":    None,
        "ml_p_return_gt10":   None,
        "ml_iv_expanding":    None,
        "ml_composite_score": None,
        "ml_anomaly_score":   None,
        "ml_regime":          None,
        "ml_confidence_tier": None,
        "ml_p_up":            None,
        "ml_expected_vol":    None,
    }
    try:
        from scripts.ml_cache import ml_cache
        pred = ml_cache.get(ticker)
        if not pred:
            return _empty
        return {
            "ml_meta_score":      pred.get("meta_score"),
            "ml_p_win":           pred.get("pop_score"),
            "ml_confidence":      pred.get("analogues_win_rate"),
            "ml_ranker_score":    pred.get("ranker_score"),
            "ml_return_score":    pred.get("return_score"),
            "ml_p_return_gt10":   pred.get("p_return_gt10"),
            "ml_iv_expanding":    pred.get("iv_expanding_prob"),
            "ml_composite_score": pred.get("composite_score"),
            "ml_anomaly_score":   pred.get("anomaly_score"),
            "ml_regime":          pred.get("regime"),
            "ml_confidence_tier": pred.get("confidence_tier"),
            "ml_p_up":            pred.get("p_up"),
            "ml_expected_vol":    pred.get("expected_vol"),
        }
    except Exception:
        return _empty


def store_expiry_settlements(today_str: str | None = None) -> dict:
    """
    Canonical settlement store — write-once step called by the evening check.

    For every training snapshot whose expiry == today and that is not yet labeled,
    fetch the underlying closing price and persist it to expiry_settlements.
    All downstream labelers (label_pending_snapshots, label_rejected_candidates)
    check expiry_settlements first, so they get settlement_source='expiry_settlement'
    and label_confidence=1.0 instead of falling back to forward-return estimates.

    Returns {"stored": N, "tickers": [...], "errors": [...]}.
    """
    import yfinance as _yf
    from datetime import datetime as _dt
    from scripts.db import connect as _dbconnect, upsert_expiry_settlement, ensure_archive_tables

    ensure_archive_tables()

    today_str = today_str or date.today().isoformat()

    # Find unique tickers with unlabeled snapshots expiring today
    from scripts.db import load_all_snapshots
    records = load_all_snapshots()
    tickers_needed: set[str] = set()
    for r in records:
        if (
            not r.get("labeled")
            and r.get("source") != "paper_trade_entry"  # paper trades handled by run_evening_check
            and r.get("expiry", "")[:10] == today_str
            and r.get("ticker")
        ):
            tickers_needed.add(r["ticker"])

    stored, errors = 0, []
    stored_tickers = []

    for ticker in sorted(tickers_needed):
        # Skip if already stored for today (idempotent)
        try:
            with _dbconnect(read_only=True) as con:
                existing = con.execute(
                    "SELECT settlement_price FROM expiry_settlements WHERE ticker=? AND date=?",
                    [ticker, today_str],
                ).fetchone()
            if existing:
                stored_tickers.append(ticker)
                stored += 1
                continue
        except Exception:
            pass

        try:
            hist = _yf.Ticker(ticker).history(period="1d")
            if hist.empty:
                errors.append({"ticker": ticker, "error": "no price data"})
                continue
            price = float(hist["Close"].iloc[-1])
            upsert_expiry_settlement(ticker, today_str, price, source="yfinance_eod", confidence=1.0)
            stored_tickers.append(ticker)
            stored += 1
            log.info(f"Settlement stored: {ticker} {today_str} @ {price:.4f}")
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})
            log.warning(f"Failed to store settlement for {ticker} on {today_str}: {e}")

    return {"stored": stored, "tickers": stored_tickers, "errors": errors}


def _extract_trade_fields(candidate: dict | None, spot: float | None) -> dict:
    """
    Extract Phase-1 trade geometry, Greeks, and optimizer opinion from a candidate dict.
    Returns a flat dict ready to merge into a snapshot record.
    All fields default to None so missing data is explicit rather than absent.
    feature_schema_version = 2 when geometry is present, 1 otherwise.
    """
    if not candidate:
        return {"feature_schema_version": 1}

    c = candidate if isinstance(candidate, dict) else {}

    # Raw pricing inputs (allow Greek regeneration if calculation changes)
    expected_move   = c.get("expected_move")
    risk_free_rate  = c.get("risk_free_rate")

    # Greeks (net position)
    net_delta = c.get("net_delta")
    net_gamma = c.get("net_gamma")
    net_theta = c.get("net_theta")
    net_vega  = c.get("net_vega")

    # Geometry
    max_profit       = c.get("max_profit")
    max_loss         = c.get("max_loss")
    capital_required = c.get("capital_required")
    spread_width     = c.get("spread_width") or (
        (abs(max_profit) + abs(max_loss)) if (max_profit is not None and max_loss is not None) else None
    )
    credit_received  = c.get("credit") or (max_profit if c.get("is_credit") else None)

    # Strikes (absolute and as % of spot)
    short_strike = c.get("short_strike") or c.get("sell_strike")
    long_strike  = c.get("long_strike")  or c.get("buy_strike")
    short_strike_pct = (short_strike / spot) if (short_strike and spot) else None
    long_strike_pct  = (long_strike  / spot) if (long_strike  and spot) else None

    # Optimizer opinion (kept separate from geometry for clean ablation)
    pop           = c.get("pop")
    ev            = c.get("ev") or c.get("expected_value")
    quality_score = c.get("quality_score") or c.get("score")

    has_geometry = any(v is not None for v in (net_delta, spread_width, max_profit, pop))
    has_optimizer = any(v is not None for v in (pop, ev, quality_score))
    schema_version = 3 if has_optimizer else (2 if has_geometry else 1)

    return {
        "expected_move":      expected_move,
        "risk_free_rate":     risk_free_rate,
        "net_delta":          net_delta,
        "net_gamma":          net_gamma,
        "net_theta":          net_theta,
        "net_vega":           net_vega,
        "spread_width":       spread_width,
        "credit_received":    credit_received,
        "max_profit":         max_profit,
        "max_loss":           max_loss,
        "capital_required":   capital_required,
        "short_strike":       short_strike,
        "long_strike":        long_strike,
        "short_strike_pct":   short_strike_pct,
        "long_strike_pct":    long_strike_pct,
        "pop":                pop,
        "ev":                 ev,
        "quality_score":      quality_score,
        "feature_schema_version": schema_version,
    }


def write_paper_trade_snapshot(trade: dict, analyze_row: dict) -> None:
    """
    Write a training snapshot at the moment a paper trade is opened.

    Uses the already-fetched analyze_row (no re-fetch) so the morning scan
    doesn't pay an extra round-trip per trade. Tier 1 fields are populated
    directly from analyze_row (same source as scan snapshots); Tier 2-5
    fields (sector, macro, earnings) remain None since analyze_ticker
    doesn't fetch them. Labeling is done by label_pending_snapshots() once
    the trade closes, using the actual managed-exit P&L.
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
        "signal_pct":       analyze_row.get("signal_pct"),
        "candidate":        candidate,
        "expiry":           trade.get("expiry"),
        "dte":              trade.get("dte_at_entry"),
        # Tier 1 — available from analyze_ticker (same fields as write_scan_all_snapshots)
        "vol_oi_ratio":     analyze_row.get("vol_oi_ratio"),
        "call_vol":         analyze_row.get("call_vol"),
        "put_vol":          analyze_row.get("put_vol"),
        "iv_skew":          analyze_row.get("vol_skew_pct"),
        "short_interest_pct": analyze_row.get("short_interest"),
        "iv_term_slope":    analyze_row.get("iv_term_slope"),
        "otm_pcr":          None,
        "beta_60d":         analyze_row.get("beta_60d"),
        "atr_pct":          analyze_row.get("atr_pct"),
        "iv_rank_52w":      analyze_row.get("iv_rank_52w"),
        "max_pain_strike":  analyze_row.get("max_pain_strike"),
        "oi_concentration": analyze_row.get("oi_concentration"),
        "vvix":             analyze_row.get("vvix"),
        "vix_3m":           analyze_row.get("vix_3m"),
        "vix_term_slope":   analyze_row.get("vix_term_slope"),
        "move_index":       analyze_row.get("move_index"),
        "iv_change_5d":     analyze_row.get("iv_change_5d"),
        "unusual_activity": analyze_row.get("unusual_activity"),
        "iv_hv_ratio":      analyze_row.get("iv_hv_ratio"),
        "expected_move_pct": analyze_row.get("expected_move_pct"),
        "term_slope":       analyze_row.get("term_slope"),
        "vol_pcr":          analyze_row.get("vol_pcr"),
        "pcr_diverge":      analyze_row.get("pcr_diverge"),
        "hy_oas":           analyze_row.get("hy_oas"),
        "yield_10y":        analyze_row.get("yield_10y"),
        "yield_3m":         analyze_row.get("yield_3m"),
        "yield_curve":      analyze_row.get("yield_curve"),
        "fed_within_dte":   analyze_row.get("fed_within_dte"),
        "cpi_within_dte":   analyze_row.get("cpi_within_dte"),
        # Tier 2-5 — not available from analyze_ticker
        "sector_etf": None, "sector_trend": None, "sector_rsi": None,
        "sector_iv_ratio": None, "sector_return_1d": None,
        "spy_trend": None, "spy_rsi": None, "qqq_trend": None, "qqq_rsi": None,
        "iwm_trend": None, "iwm_rsi": None, "earnings_inside_expiry": None,
        "news_sentiment_score": None, "analyst_rec_change": None,
        "iv_skew_20d": None, "gex_proxy": None, "wings_iv_ratio": None,
        "dollar_index": None,
        # Paper trade tracking fields
        "source":           "paper_trade_entry",
        "paper_trade_id":   trade["id"],
        # ML model state at entry — for post-hoc audit of signal accuracy
        **_ml_scores_at_entry(ticker),
        # Phase 1: trade geometry, Greeks, optimizer opinion
        **_extract_trade_fields(candidate, analyze_row.get("spot")),
        "labeled":          False,
        "outcome":          None,
        "labeled_at":       None,
    }
    try:
        insert_snapshot(record)
        log.info(f"Training snapshot written for paper trade {trade['id']}")
    except Exception as e:
        log.warning(f"Failed to write training snapshot for {trade['id']}: {e}")


def write_scan_all_snapshots(rows: list[dict], scan_time: str, opened_tickers: set[str]) -> dict:
    """
    Write one training snapshot per scanned ticker using the already-fetched
    analyze_ticker rows from run_morning_scan — no re-fetch, zero extra API cost.

    Stores ALL 100 tickers, not just the top 3, so ML training gets:
      - Negative examples (tickers that scored poorly and why)
      - Scan-time features at real decision moments (10 AM / 2 PM ET)
      - 100x more rows for POP / regime / return / volatility models

    opened_tickers: set of ticker strings already written by write_paper_trade_snapshot
                    (skipped here to avoid duplicate snapshot_ids for the same scan).
    """
    from scripts.db import insert_snapshot
    from datetime import datetime

    source   = f"scan_{scan_time}"   # "scan_morning" or "scan_afternoon"
    _now     = datetime.now().isoformat()
    vix_price = _fetch_vix_now()
    saved, skipped, errors = 0, 0, []

    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue

        # Top-3 tickers already written with source="paper_trade_entry" and full trade context
        if ticker in opened_tickers:
            skipped += 1
            continue

        try:
            candidates  = row.get("candidates", [])
            recommended = next((c for c in candidates if c.get("recommended")), None)
            expiry      = (recommended or {}).get("expiry") or row.get("expiry")
            dte         = (recommended or {}).get("dte")    or row.get("dte")

            record = {
                "snapshot_id":             f"{source}-{ticker}-{_now}",
                "collected_at":            _now,
                "source":                  source,
                "ticker":                  ticker,
                "spot":                    row.get("spot"),
                "iv_env":                  row.get("iv_env"),
                "trend":                   row.get("trend"),
                "weekly_trend":            row.get("weekly_trend"),
                "regime":                  row.get("regime"),
                "rsi":                     row.get("rsi"),
                "macd_trend":              row.get("macd_trend"),
                "adx":                     row.get("adx"),
                "atm_iv":                  row.get("atm_iv"),
                "iv_rank_proxy":           row.get("iv_rank_proxy"),
                "hv20":                    row.get("hv20"),
                "pcr":                     row.get("pcr"),
                "vix":                     vix_price,
                "earnings_days_away":      None,
                "news_headlines":          [],
                "status":                  row.get("status"),
                "recommended_structure":   row.get("recommended_structure"),
                "signal_score":            row.get("signal_score"),
                "signal_pct":              row.get("signal_pct"),
                "candidate":               recommended,
                "expiry":                  expiry,
                "dte":                     dte,
                # Tier 1 — what analyze_ticker already computed
                "vol_oi_ratio":            row.get("vol_oi_ratio"),
                "call_vol":                row.get("call_vol"),
                "put_vol":                 row.get("put_vol"),
                "iv_skew":                 row.get("vol_skew_pct"),
                "short_interest_pct":      row.get("short_interest"),
                # Tier 1 supplemental — now available from analyze_ticker
                "iv_term_slope":    row.get("iv_term_slope"),
                "otm_pcr":          None,
                "beta_60d":         row.get("beta_60d"),
                "atr_pct":          row.get("atr_pct"),
                "iv_rank_52w":      row.get("iv_rank_52w"),
                "max_pain_strike":  row.get("max_pain_strike"),
                "oi_concentration": row.get("oi_concentration"),
                "vvix":             row.get("vvix"),
                "vix_3m":           row.get("vix_3m"),
                "vix_term_slope":   row.get("vix_term_slope"),
                "move_index":       row.get("move_index"),
                # Tier 2-5 — not available from analyze_ticker
                "sector_etf": None, "sector_trend": None, "sector_rsi": None,
                "sector_iv_ratio": None, "sector_return_1d": None,
                "spy_trend": None, "spy_rsi": None,
                "qqq_trend": None, "qqq_rsi": None, "iwm_trend": None,
                "iwm_rsi": None, "earnings_inside_expiry": None,
                "news_sentiment_score": None, "analyst_rec_change": None,
                "iv_skew_20d": None, "gex_proxy": None, "wings_iv_ratio": None,
                "iv_change_5d": row.get("iv_change_5d"),
                "unusual_activity": row.get("unusual_activity"),
                "iv_hv_ratio":      row.get("iv_hv_ratio"),
                "expected_move_pct": row.get("expected_move_pct"),
                "term_slope":       row.get("term_slope"),
                "vol_pcr":          row.get("vol_pcr"),
                "pcr_diverge":      row.get("pcr_diverge"),
                "hy_oas":           row.get("hy_oas"),
                "max_pain_strike": row.get("max_pain_strike"),
                "oi_concentration": row.get("oi_concentration"),
                "yield_10y":      row.get("yield_10y"),
                "yield_3m":       row.get("yield_3m"),
                "yield_curve":    row.get("yield_curve"),
                "dollar_index":   None,
                "fed_within_dte": row.get("fed_within_dte"),
                "cpi_within_dte": row.get("cpi_within_dte"),
                "garch_vol_at_entry": None,
                # ML state at scan time
                **_ml_scores_at_entry(ticker),
                # Phase 1: trade geometry, Greeks, optimizer opinion
                **_extract_trade_fields(recommended, row.get("spot")),
                "paper_trade_id":  None,
                "labeled":         False,
                "outcome":         None,
                "labeled_at":      None,
            }
            insert_snapshot(record)
            saved += 1
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})
            log.warning(f"Scan snapshot write failed for {ticker}: {e}")

    log.info(f"[{source}] scan snapshots: {saved} saved, {skipped} skipped (opened), {len(errors)} errors")
    return {"saved": saved, "skipped": skipped, "errors": errors}


def write_scan_candidate_snapshots(
    items: list[dict],
    scan_decision_id: str,
    scan_time: str,
    entered_tickers: set[str],
) -> dict:
    """
    Write one training_snapshot per gate-surviving candidate (ticker × structure).

    Complements write_scan_all_snapshots (one row per ticker, recommended candidate only).
    Captures every structure that cleared the filter + quality + portfolio gates so
    SQL queries can analyse selection quality across structure × outcome pairs.

    items: rank_candidates output — each dict has 'row', 'candidate', 'ev', 'composite',
           'evidence_n', 'confidence_tier'.
    entered_tickers: tickers already written by write_paper_trade_snapshot (skipped here
                     to avoid duplicating rows that already have richer trade context).
    """
    from scripts.db import insert_snapshot
    from datetime import datetime

    source = f"scan_candidate_{scan_time}"
    _now   = datetime.now().isoformat()
    vix_price = _fetch_vix_now()
    saved, skipped, errors = 0, 0, []

    for item in items:
        row       = item.get("row") or {}
        candidate = item.get("candidate") or {}
        ticker    = row.get("ticker") or candidate.get("ticker")
        structure = candidate.get("structure") or ""

        if not ticker or not structure:
            skipped += 1
            continue

        # Tickers entered as paper trades already have a richer paper_trade_entry snapshot.
        if ticker in entered_tickers:
            skipped += 1
            continue

        try:
            _struct_key = structure.replace(" ", "_").lower()
            snapshot_id = f"scan-cand-{scan_decision_id}-{ticker}-{_struct_key}"

            expiry = candidate.get("expiry") or row.get("expiry")
            dte    = candidate.get("dte")    or row.get("dte")
            spot   = row.get("spot")

            record = {
                "snapshot_id":             snapshot_id,
                "collected_at":            _now,
                "source":                  source,
                "scan_decision_id":        scan_decision_id,
                "ticker":                  ticker,
                "spot":                    spot,
                "iv_env":                  row.get("iv_env"),
                "trend":                   row.get("trend"),
                "weekly_trend":            row.get("weekly_trend"),
                "regime":                  row.get("regime"),
                "rsi":                     row.get("rsi"),
                "macd_trend":              row.get("macd_trend"),
                "adx":                     row.get("adx"),
                "atm_iv":                  row.get("atm_iv"),
                "iv_rank_proxy":           row.get("iv_rank_proxy"),
                "hv20":                    row.get("hv20"),
                "pcr":                     row.get("pcr"),
                "vix":                     vix_price,
                "earnings_days_away":      None,
                "news_headlines":          [],
                "status":                  row.get("status"),
                "recommended_structure":   row.get("recommended_structure"),
                "signal_score":            row.get("signal_score"),
                "signal_pct":              row.get("signal_pct"),
                "candidate":               candidate,
                "expiry":                  expiry,
                "dte":                     dte,
                # Tier 1
                "vol_oi_ratio":            row.get("vol_oi_ratio"),
                "call_vol":                row.get("call_vol"),
                "put_vol":                 row.get("put_vol"),
                "iv_skew":                 row.get("vol_skew_pct"),
                "short_interest_pct":      row.get("short_interest"),
                "iv_term_slope":           row.get("iv_term_slope"),
                "otm_pcr":                 None,
                "beta_60d":                row.get("beta_60d"),
                "atr_pct":                 row.get("atr_pct"),
                "iv_rank_52w":             row.get("iv_rank_52w"),
                "max_pain_strike":         row.get("max_pain_strike"),
                "oi_concentration":        row.get("oi_concentration"),
                "vvix":                    row.get("vvix"),
                "vix_3m":                  row.get("vix_3m"),
                "vix_term_slope":          row.get("vix_term_slope"),
                "move_index":              row.get("move_index"),
                # Tier 2-5
                "sector_etf": None, "sector_trend": None, "sector_rsi": None,
                "sector_iv_ratio": None, "sector_return_1d": None,
                "spy_trend": None, "spy_rsi": None,
                "qqq_trend": None, "qqq_rsi": None, "iwm_trend": None,
                "iwm_rsi": None, "earnings_inside_expiry": None,
                "news_sentiment_score": None, "analyst_rec_change": None,
                "iv_skew_20d": None, "gex_proxy": None, "wings_iv_ratio": None,
                "iv_change_5d":         row.get("iv_change_5d"),
                "unusual_activity":     row.get("unusual_activity"),
                "iv_hv_ratio":          row.get("iv_hv_ratio"),
                "expected_move_pct":    row.get("expected_move_pct"),
                "term_slope":           row.get("term_slope"),
                "vol_pcr":              row.get("vol_pcr"),
                "pcr_diverge":          row.get("pcr_diverge"),
                "hy_oas":               row.get("hy_oas"),
                "yield_10y":            row.get("yield_10y"),
                "yield_3m":             row.get("yield_3m"),
                "yield_curve":          row.get("yield_curve"),
                "dollar_index":         None,
                "fed_within_dte":       row.get("fed_within_dte"),
                "cpi_within_dte":       row.get("cpi_within_dte"),
                "garch_vol_at_entry":   None,
                # P1-B evidence quality
                "evidence_n":           item.get("evidence_n"),
                "confidence_tier":      item.get("confidence_tier"),
                # Composite score from rank_candidates (decision-quality signal)
                "composite_score":      item.get("composite"),
                # ML state at scan time
                **_ml_scores_at_entry(ticker),
                # Trade geometry, Greeks, optimizer opinion
                **_extract_trade_fields(candidate, spot),
                "paper_trade_id":  None,
                "labeled":         False,
                "outcome":         None,
                "labeled_at":      None,
            }
            insert_snapshot(record)
            saved += 1
        except Exception as e:
            errors.append({"ticker": ticker, "structure": structure, "error": str(e)})
            log.warning(f"Candidate snapshot write failed for {ticker} {structure}: {e}")

    log.info(f"[{source}] candidate snapshots: {saved} saved, {skipped} skipped, {len(errors)} errors")
    return {"saved": saved, "skipped": skipped, "errors": errors}


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


def label_snapshots_with_forward_returns() -> dict:
    """
    For snapshots collected 5+ days ago that have no forward return labels,
    compute forward_1d, forward_3d, forward_5d spot returns and future_hv5d
    (realized vol of the 5 daily log-returns following each snapshot).

    Uses yfinance close prices; groups by ticker to minimise API calls.
    Called daily alongside label_pending_snapshots() via _daily_label() in app.py.
    """
    from scripts.db import update_snapshot_forward_returns as _update_fwd
    import math

    records = _load_all()
    cutoff = date.today() - timedelta(days=5)

    import math as _math

    def _null_val(v) -> bool:
        if v is None:
            return True
        try:
            return _math.isnan(float(v))
        except (TypeError, ValueError):
            return False

    # Filter to snapshots that need forward return labels
    candidates = [
        r for r in records
        if r.get("ticker")
        and r.get("collected_at")
        and _null_val(r.get("forward_1d"))
        and (r.get("collected_at") or "")[:10] <= cutoff.isoformat()
    ]
    if not candidates:
        return {"labeled": 0, "tickers": []}

    # Group by ticker
    by_ticker: dict[str, list[dict]] = {}
    for r in candidates:
        by_ticker.setdefault(r["ticker"], []).append(r)

    updates, errors = [], []
    price_cache: dict[str, "pd.DataFrame"] = {}

    for ticker, snaps in by_ticker.items():
        try:
            if ticker not in price_cache:
                hist = yf.Ticker(ticker).history(period="1y")
                price_cache[ticker] = hist
            hist = price_cache[ticker]
            if hist is None or hist.empty:
                continue
            # Build date → close index
            hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
            closes = hist["Close"]
            date_index = [str(d.date()) for d in closes.index]
            close_list = closes.tolist()
            date_to_idx = {d: i for i, d in enumerate(date_index)}

            for snap in snaps:
                snap_date = (snap.get("collected_at") or "")[:10]
                base_idx = date_to_idx.get(snap_date)
                if base_idx is None:
                    # Try the nearest prior trading day
                    for d in sorted(date_to_idx.keys(), reverse=True):
                        if d <= snap_date:
                            base_idx = date_to_idx[d]
                            break
                if base_idx is None:
                    continue
                base_close = close_list[base_idx]
                if not base_close:
                    continue

                def _fwd_return(n_days: int) -> float | None:
                    target_idx = base_idx + n_days
                    if target_idx >= len(close_list):
                        return None
                    future_close = close_list[target_idx]
                    if not future_close:
                        return None
                    return round((future_close / base_close) - 1, 6)

                def _hv5d() -> float | None:
                    end_idx = base_idx + 6
                    if end_idx >= len(close_list):
                        return None
                    window = close_list[base_idx:end_idx + 1]
                    if len(window) < 2:
                        return None
                    log_rets = [math.log(window[i] / window[i-1]) for i in range(1, len(window))]
                    if not log_rets:
                        return None
                    mean = sum(log_rets) / len(log_rets)
                    variance = sum((r - mean) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
                    return round(math.sqrt(variance * 252), 4)

                updates.append({
                    "snapshot_id": snap["snapshot_id"],
                    "forward_1d":  _fwd_return(1),
                    "forward_3d":  _fwd_return(3),
                    "forward_5d":  _fwd_return(5),
                    "future_hv5d": _hv5d(),
                })
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

    n_updated = _update_fwd(updates)
    labeled_tickers = list(by_ticker.keys())
    log.info(f"[forward_returns] {n_updated} snapshots labeled across {len(labeled_tickers)} tickers")
    return {"labeled": n_updated, "tickers": labeled_tickers, "errors": errors}


def backfill_snapshot_features() -> dict:
    """
    Backfill new ML feature columns for existing snapshots.

    Covers two feature groups introduced after initial collection:

    Event calendar (#9): ppi_days_away, ppi_within_dte, jobs_days_away,
      jobs_within_dte, days_to_opex, opex_within_dte, is_opex_week,
      is_monthly_opex, is_quarterly_opex — computed deterministically from
      each snapshot's collected_at date and the hardcoded economic calendar.

    Cross-sectional ranks (#6): iv_pct_rank, gamma_pct_rank, volume_pct_rank,
      momentum_pct_rank, oi_pct_rank — computed within each calendar-day
      group of snapshots (same approach used going forward in collect_snapshots).

    Forward return labels (#8) are handled separately by
    label_snapshots_with_forward_returns() which runs nightly.

    Safe to run multiple times — skips snapshots that already have values.
    """
    from scripts.db import connect, SNAPSHOTS_TABLE, update_snapshot_forward_returns as _noop  # noqa: ensure migrated
    import json as _json

    import math as _math

    def _null(v) -> bool:
        """True when a value from DuckDB/pandas is effectively NULL (None or NaN)."""
        if v is None:
            return True
        try:
            return _math.isnan(float(v))
        except (TypeError, ValueError):
            return False

    records = _load_all()
    if not records:
        return {"event_calendar": 0, "xsec_ranks": 0}

    # ── Event calendar backfill ───────────────────────────────────────────────
    _DTE_WINDOW = 14   # match the DTE used in collect_snapshots
    event_updates: list[tuple] = []

    for r in records:
        if not _null(r.get("ppi_days_away")):
            continue  # already filled
        snap_date_str = (r.get("collected_at") or "")[:10]
        if not snap_date_str:
            continue
        try:
            snap_date = date.fromisoformat(snap_date_str)
        except ValueError:
            continue

        ppi_d   = _days_to_next_event(_ALL_PPI,  from_date=snap_date)
        jobs_d  = _days_to_next_event(_ALL_JOBS, from_date=snap_date)
        opex    = _opex_info(from_date=snap_date)

        fed_d   = _days_to_next_event(_ALL_FOMC, from_date=snap_date)
        cpi_d   = _days_to_next_event(_ALL_CPI,  from_date=snap_date)
        od      = opex["days_to_opex"]

        event_updates.append((
            ppi_d,
            int(ppi_d  is not None and ppi_d  <= _DTE_WINDOW),
            jobs_d,
            int(jobs_d is not None and jobs_d <= _DTE_WINDOW),
            od,
            int(od     is not None and od     <= _DTE_WINDOW),
            opex["is_opex_week"],
            opex["is_monthly_opex"],
            opex["is_quarterly_opex"],
            r["snapshot_id"],
        ))

    if event_updates:
        with connect() as con:
            for vals in event_updates:
                con.execute(
                    f"UPDATE {SNAPSHOTS_TABLE} SET "
                    f"ppi_days_away=?, ppi_within_dte=?, "
                    f"jobs_days_away=?, jobs_within_dte=?, "
                    f"days_to_opex=?, opex_within_dte=?, "
                    f"is_opex_week=?, is_monthly_opex=?, is_quarterly_opex=? "
                    f"WHERE snapshot_id=?",
                    list(vals),
                )
            con.commit()

    # ── Cross-sectional rank backfill ─────────────────────────────────────────
    # Group by calendar date so we rank within each day's collected batch
    by_date: dict[str, list[dict]] = {}
    for r in records:
        d = (r.get("collected_at") or "")[:10]
        if d:
            by_date.setdefault(d, []).append(r)

    rank_updates = 0
    with connect() as con:
        for day_records in by_date.values():
            # Only process if at least one record in this day lacks ranks
            if all(not _null(r.get("iv_pct_rank")) for r in day_records):
                continue
            _add_xsec_ranks(day_records)
            for r in day_records:
                if r.get("iv_pct_rank") is None:
                    continue
                con.execute(
                    f"UPDATE {SNAPSHOTS_TABLE} SET "
                    f"iv_pct_rank=?, gamma_pct_rank=?, volume_pct_rank=?, "
                    f"momentum_pct_rank=?, oi_pct_rank=? "
                    f"WHERE snapshot_id=?",
                    [r.get("iv_pct_rank"), r.get("gamma_pct_rank"),
                     r.get("volume_pct_rank"), r.get("momentum_pct_rank"),
                     r.get("oi_pct_rank"), r["snapshot_id"]],
                )
                rank_updates += 1
        con.commit()

    log.info(
        f"[backfill] event_calendar={len(event_updates)}, xsec_ranks={rank_updates}"
    )
    return {"event_calendar": len(event_updates), "xsec_ranks": rank_updates}
