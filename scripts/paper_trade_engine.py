"""
Paper trade validation engine.

  Morning (10 AM EDT): run_morning_scan()
    - Runs the full analyzer across the watchlist
    - Picks top-3 candidates (same ranking as the live page, no AI call)
    - Re-fetches live bid/ask for a realistic entry price (bid-side credit)
    - Appends records to data/paper_trades.json

  Evening (5 PM EDT): run_evening_check()
    - Loads all open trades
    - Fetches current spread mark
    - Applies managed-exit rules: close at 50% profit, stop at 200% loss
    - On expiry day: computes final P&L from underlying close price
    - Updates records in place

  get_performance_summary() — returns stats dict for the dashboard.
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from config.rules import (
    MARKET_CLOSE_HOUR, EARLY_CLOSE_PCT,
    IV_EDGE_FLAG_VP,
)
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

EDT        = ZoneInfo("America/New_York")
_ROOT      = Path(__file__).parent.parent
DATA_DIR   = _ROOT / "data"
TRADES_PATH = DATA_DIR / "paper_trades.json"


_SETTINGS_DEFAULTS = {
    "profit_target_pct": 0.50,
    "stop_loss_mult":    3.0,
    "max_risk_pct":      0.12,
}


def _load_settings() -> dict:
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        cfg  = tomllib.loads((_ROOT / "config" / "settings.toml").read_text(encoding="utf-8"))
        mgmt    = cfg.get("management", {})
        capital = cfg.get("capital", {})
        return {
            "profit_target_pct": float(mgmt.get("profit_target_pct", 0.50)),
            "stop_loss_mult":    float(mgmt.get("stop_loss_mult",    3.0)),
            "max_risk_pct":      float(capital.get("max_risk_pct",   0.12)),
        }
    except Exception:
        return dict(_SETTINGS_DEFAULTS)


# Read at module load for backwards-compat constants; callers that care about
# live changes should call _load_settings() directly.
_SETTINGS         = _load_settings()
PROFIT_TARGET_PCT = _SETTINGS["profit_target_pct"]
STOP_LOSS_MULT    = _SETTINGS["stop_loss_mult"]
MAX_RISK_PCT      = _SETTINGS["max_risk_pct"]


# ── NYSE holiday list (2026-2028) ─────────────────────────────────────────────

_NYSE_HOLIDAYS = {
    date(2026,  1,  1), date(2026,  1, 19), date(2026,  2, 16),
    date(2026,  4,  3), date(2026,  5, 25), date(2026,  7,  3),
    date(2026,  9,  7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027,  1,  1), date(2027,  1, 18), date(2027,  2, 15),
    date(2027,  3, 26), date(2027,  5, 31), date(2027,  7,  5),
    date(2027,  9,  6), date(2027, 11, 25), date(2027, 12, 24),
    date(2028,  1,  3), date(2028,  1, 17), date(2028,  2, 21),
    date(2028,  3, 14), date(2028,  5, 29), date(2028,  7,  4),
    date(2028,  9,  4), date(2028, 11, 23), date(2028, 12, 25),
}


def is_market_day(dt=None):
    d = dt or date.today()
    return d.weekday() < 5 and d not in _NYSE_HOLIDAYS


# ── Storage ───────────────────────────────────────────────────────────────────

def load_trades():
    if not TRADES_PATH.exists():
        return []
    try:
        return json.loads(TRADES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_trades(trades):
    import numpy as np

    class _Enc(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, np.integer):  return int(obj)
            if isinstance(obj, np.floating): return float(obj)
            if isinstance(obj, np.bool_):    return bool(obj)
            return str(obj)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_PATH.write_text(json.dumps(trades, indent=2, cls=_Enc), encoding="utf-8")


# ── Live option-price helpers ─────────────────────────────────────────────────

def fetch_underlying_price(ticker):
    """Current underlying price — E*TRADE real-time first, yfinance fallback."""
    import yfinance as yf

    try:
        from scripts import etrade_client as et
        pref = et.ds_pref("quotes")
        use_et = (pref == "etrade") or (pref == "auto" and et.is_authenticated())
        if use_et:
            q = et.get_quote(ticker)
            if q and q.get("last"):
                return float(q["last"])
    except Exception:
        pass
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="5m")
        return float(hist["Close"].dropna().iloc[-1]) if not hist.empty else None
    except Exception:
        return None


def _fetch_leg(ticker, expiry, strike, opt_type):
    """Fetch live bid/ask/mid for one option leg. E*TRADE first, yfinance fallback."""
    if strike is None:
        return None
    strike = float(strike)
    expiry = str(expiry)[:10]

    def _parse_row(row):
        bid  = float(row.get("bid")  or 0)
        ask  = float(row.get("ask")  or 0)
        last = float(row.get("lastPrice") or 0)
        mid  = round((bid + ask) / 2, 4) if bid + ask > 0 else last
        iv_raw = float(row.get("impliedVolatility") or 0)  # fraction, unrounded — use this for any further math
        return {
            "strike": float(row["strike"]),
            "bid": bid, "ask": ask, "mid": mid,
            "iv":     round(iv_raw * 100, 1),  # display-only, rounded — do not feed back into calculations
            "iv_raw": iv_raw,
            "volume": int(row.get("volume") or 0),
            "oi":     int(row.get("openInterest") or 0),
        }

    # Try E*TRADE first (real-time) if configured
    try:
        import sys
        sys.path.insert(0, str(_ROOT))
        from scripts import etrade_client as et
        pref = et.ds_pref("option_chain")
        use_et = (pref == "etrade") or (pref == "auto" and et.is_authenticated())
        if use_et:
            et_calls, et_puts = et.get_option_chain(ticker, expiry)
            df = et_puts if opt_type == "put" else et_calls
            if df is not None and not df.empty:
                row = df.iloc[(df["strike"] - strike).abs().argsort().iloc[0]]
                result = _parse_row(row)
                if result["bid"] > 0 or result["ask"] > 0:
                    return result
    except Exception as e:
        log.debug(f"_fetch_leg E*TRADE failed for {ticker}: {e}")

    # Fallback: yfinance (15–20 min delayed)
    import yfinance as yf
    try:
        chain = yf.Ticker(ticker).option_chain(expiry)
    except Exception as e:
        log.warning(f"option_chain({ticker}, {expiry}) failed: {e}")
        return None
    df = chain.puts if opt_type == "put" else chain.calls
    if df is None or df.empty:
        return None
    row = df.iloc[(df["strike"] - strike).abs().argsort().iloc[0]]
    return _parse_row(row)


def _entry_price(candidate):
    """
    Re-fetch live bid/ask for the specific strikes in a top-3 candidate.
    Returns {'credit_bid', 'credit_mid', 'legs'} or None.
    credit_bid = bid-side fill (conservative; what you'd actually receive).
    For debit spreads, credit_bid stores max_profit as the accounting value.
    """
    import sys; sys.path.insert(0, str(_ROOT))
    from config.structures import get_or_none
    from config.structures._base import StrikeSchema

    s      = candidate.get("structure", "")
    ticker = candidate.get("ticker", "")
    expiry = candidate.get("expiry", "")
    st     = get_or_none(s)

    try:
        if st is not None and st.strike_schema == StrikeSchema.IRON_CONDOR:
            ps = _fetch_leg(ticker, expiry, candidate.get("put_short_strike"),  "put")
            pl = _fetch_leg(ticker, expiry, candidate.get("put_long_strike"),   "put")
            cs = _fetch_leg(ticker, expiry, candidate.get("call_short_strike"), "call")
            cl = _fetch_leg(ticker, expiry, candidate.get("call_long_strike"),  "call")
            if not all([ps, pl, cs, cl]): return None
            bid = (ps["bid"] - pl["ask"]) + (cs["bid"] - cl["ask"])
            mid = (ps["mid"] - pl["mid"]) + (cs["mid"] - cl["mid"])
            return {
                "credit_bid": round(bid, 4), "credit_mid": round(mid, 4),
                "legs": {"put_short": ps, "put_long": pl, "call_short": cs, "call_long": cl},
            }

        if st is not None and st.strike_schema == StrikeSchema.SINGLE_LEG:
            opt = st.option_type
            sh  = _fetch_leg(ticker, expiry, candidate.get("short_strike"), opt)
            if not sh: return None
            return {
                "credit_bid": round(sh["bid"], 4),
                "credit_mid": round(sh["mid"], 4),
                "legs": {"short": sh},
            }

        if st is not None and st.strike_schema == StrikeSchema.TWO_LEG:
            opt  = st.option_type   # "put" or "call"
            sh   = _fetch_leg(ticker, expiry, candidate.get("short_strike"), opt)
            lo   = _fetch_leg(ticker, expiry, candidate.get("long_strike"),  opt)
            if st.is_credit:
                if not sh or not lo: return None
                return {
                    "credit_bid": round(sh["bid"] - lo["ask"], 4),
                    "credit_mid": round(sh["mid"] - lo["mid"], 4),
                    "legs": {"short": sh, "long": lo},
                }
            else:
                # Debit spread: store max_profit as the accounting credit_bid
                val = candidate.get("max_profit")
                if val is None: return None
                return {
                    "credit_bid": val, "credit_mid": val,
                    "legs": ({"long": lo, "short": sh} if lo and sh else {}),
                }

        # Fallback for structures with no fixed strike schema (Calendar, Diagonal, Jade Lizard)
        val = candidate.get("max_profit")
        if val is None: return None
        return {"credit_bid": val, "credit_mid": val, "legs": {}}

    except Exception as e:
        log.warning(f"_entry_price failed for {ticker} {s}: {e}")
        return None


def _current_mark(trade):
    """Current cost to close the spread — registry-driven dispatch."""
    import sys; sys.path.insert(0, str(_ROOT))
    from config.structures import get_or_none
    from config.structures._base import StrikeSchema

    ticker  = trade["ticker"]
    expiry  = trade["expiry"]
    strikes = trade.get("strikes", {})
    st      = get_or_none(trade["structure"])
    if st is None:
        return None

    try:
        if st.strike_schema == StrikeSchema.SINGLE_LEG:
            sh = _fetch_leg(ticker, expiry, strikes.get("short"), st.option_type)
            if not sh: return None
            return max(0.0, round(sh["ask"], 4))   # cost to buy back the naked short

        if st.strike_schema == StrikeSchema.IRON_CONDOR:
            ps = _fetch_leg(ticker, expiry, strikes.get("put_short"),  "put")
            pl = _fetch_leg(ticker, expiry, strikes.get("put_long"),   "put")
            cs = _fetch_leg(ticker, expiry, strikes.get("call_short"), "call")
            cl = _fetch_leg(ticker, expiry, strikes.get("call_long"),  "call")
            if not all([ps, pl, cs, cl]): return None
            return max(0.0, round((ps["ask"] - pl["bid"]) + (cs["ask"] - cl["bid"]), 4))

        if st.strike_schema == StrikeSchema.TWO_LEG:
            opt = st.option_type
            sh  = _fetch_leg(ticker, expiry, strikes.get("short"), opt)
            lo  = _fetch_leg(ticker, expiry, strikes.get("long"),  opt)
            if not sh or not lo: return None
            if st.is_credit:
                return max(0.0, round(sh["ask"] - lo["bid"], 4))
            else:
                return max(0.0, round(lo["bid"] - sh["ask"], 4))

    except Exception as e:
        log.warning(f"_current_mark failed for {ticker}: {e}")
    return None


def _expiry_pnl(trade, ul_price):
    """
    Compute spread value at expiry using intrinsic value — registry-driven dispatch.
    Returns (spread_value, pnl_per_share) or (None, None) for path-dependent structures.
    """
    import sys; sys.path.insert(0, str(_ROOT))
    from config.structures import get_or_none
    from config.structures._base import StrikeSchema

    strikes = trade.get("strikes", {})
    ec      = trade["entry_credit"]
    st      = get_or_none(trade["structure"])
    if st is None:
        return None, None

    def itm_put(k):  return max(0.0, k - ul_price)
    def itm_call(k): return max(0.0, ul_price - k)
    itm = itm_put if st.option_type == "put" else itm_call

    if st.strike_schema == StrikeSchema.SINGLE_LEG:
        sh_k = strikes.get("short")
        if sh_k is None: return None, None
        val = round(itm(sh_k), 4)
        return val, round(ec - val, 4)

    if st.strike_schema == StrikeSchema.IRON_CONDOR:
        put_val  = itm_put(strikes["put_short"])   - itm_put(strikes["put_long"])
        call_val = itm_call(strikes["call_short"])  - itm_call(strikes["call_long"])
        val = round(max(0.0, put_val + call_val), 4)
        return val, round(ec - val, 4)

    if st.strike_schema == StrikeSchema.TWO_LEG:
        sh_k = strikes.get("short")
        lo_k = strikes.get("long")
        if sh_k is None or lo_k is None:
            return None, None
        if st.is_credit:
            val = round(max(0.0, itm(sh_k) - itm(lo_k)), 4)
            return val, round(ec - val, 4)
        else:
            val = round(max(0.0, itm(lo_k) - itm(sh_k)), 4)
            entry_debit = trade.get("max_loss") or 0
            return val, round(val - entry_debit, 4)

    return None, None   # Calendar, Diagonal, Jade Lizard — path-dependent


# ── Select top-3 (mirrors app.py build_top_trades without AI) ────────────────

def _select_top3(rows, ml_snapshot: dict | None = None):
    """Return top-3 enterable candidates using the shared filter/rank pipeline.

    ml_snapshot: {ticker: pred_result} from predict_all or ml_cache. When
    provided it is attached to each row as row["ml"] so the confidence gate
    and Kelly sizing have access to pred_dist. When absent the ML gate is
    bypassed (backward compatible).
    """
    from scripts.candidate_ranker import rank_candidates
    from scripts.candidate_provider import kelly_from_pred_dist

    # Attach ML snapshot to rows so filter_candidates can read pred_dist
    if ml_snapshot:
        for row in rows:
            if "ml" not in row or row["ml"] is None:
                row["ml"] = ml_snapshot.get(row.get("ticker"))

    items = rank_candidates(rows, n=3)
    result = []
    for item in items:
        row, c = item["row"], item["candidate"]

        # Kelly sizing: use candidate POP as p_profit (MC not available here),
        # scaled by pred_dist.confidence
        pred_dist = item.get("pred_dist") or (row.get("ml") or {}).get("pred_dist")
        pop_pct   = c.get("pop")
        p_profit  = (pop_pct / 100.0) if pop_pct is not None else None
        kelly = kelly_from_pred_dist(pred_dist, c.get("max_profit"), c.get("max_loss"),
                                     p_profit=p_profit)

        result.append({
            "ticker":           row["ticker"],
            "structure":        c["structure"],
            "expiry":           row.get("expiry"),
            "dte":              row.get("dte"),
            "max_profit":       c.get("max_profit"),
            "max_loss":         c.get("max_loss"),
            "ev":               item["ev"],
            "meets_both":       item["meets_both"],
            "signal_score":     row.get("signal_score", 0) or 0,
            "signal_rating":    row.get("signal_rating", "Neutral"),
            "spot_at_entry":    c.get("spot_at_entry") or row.get("spot"),
            "short_strike":     c.get("short_strike"),
            "long_strike":      c.get("long_strike"),
            "put_short_strike": c.get("put_short_strike"),
            "put_long_strike":  c.get("put_long_strike"),
            "call_short_strike":c.get("call_short_strike"),
            "call_long_strike": c.get("call_long_strike"),
            "iv_edge_vp":       item["iv_edge_vp"],
            "iv_edge_label":    item["iv_edge_label"],
            "pred_dist":        pred_dist,
            "kelly":            kelly,
        })
    return result


# ── Morning scan ──────────────────────────────────────────────────────────────

def run_morning_scan(params=None, force=False):
    """
    Record today's top-3 as paper trades.
    Set force=True to run even on non-market days (for testing).
    """
    import sys
    sys.path.insert(0, str(_ROOT))

    if not force and not is_market_day():
        return {"skipped": True, "reason": "Not a market day"}

    from scripts.analyze import analyze_ticker, DEFAULT_PARAMS
    from config.watchlist import WATCHLIST

    p    = {**DEFAULT_PARAMS, **(params or {})}
    rows = []
    for ticker in WATCHLIST:
        try:
            rows.append(analyze_ticker(ticker, p))
        except Exception as e:
            log.warning(f"analyze_ticker({ticker}) failed: {e}")

    row_by_ticker = {r["ticker"]: r for r in rows}

    # Fetch ML predictions so the confidence gate and Kelly sizing work.
    # Uses ml_cache when warm (already populated by the scheduler); falls back
    # to a fresh synchronous predict_all when cold (e.g. first boot).
    _ml_snapshot: dict = {}
    try:
        from scripts.ml_cache import ml_cache as _mlc
        _ml_snapshot = _mlc.get_all()
        if not _ml_snapshot:
            from scripts.regime_predictor import predict_all as _pa
            _pr = _pa(list(row_by_ticker.keys()))
            _ml_snapshot = {p["ticker"]: p for p in _pr.get("predictions", []) if p.get("ok")}
    except Exception as _mle:
        log.warning(f"ML snapshot unavailable — confidence gate bypassed: {_mle}")

    top3 = _select_top3(rows, ml_snapshot=_ml_snapshot)
    trades    = load_trades()
    today_str = date.today().strftime("%Y%m%d")
    seen      = {t["id"] for t in trades}
    new       = []

    for rank, c in enumerate(top3, 1):
        tid = f"{today_str}_{c['ticker']}_{c['structure'][:3].upper()}_{rank}"
        if tid in seen:
            continue

        ep = _entry_price(c)
        if ep is None or ep["credit_bid"] <= 0:
            ep = {"credit_bid": c["max_profit"] or 0, "credit_mid": c["max_profit"] or 0, "legs": {}}

        ec    = ep["credit_bid"]
        width = (c.get("max_profit") or 0) + (c.get("max_loss") or 0)
        _s    = _load_settings()  # reload so runtime settings.toml changes take effect

        from config.structures import get_or_none as _gst
        from config.structures._base import StrikeSchema as _SS
        _cst = _gst(c["structure"])
        if _cst is not None and _cst.strike_schema == _SS.IRON_CONDOR:
            strike_dict = {
                "put_long":   c.get("put_long_strike"),
                "put_short":  c.get("put_short_strike"),
                "call_short": c.get("call_short_strike"),
                "call_long":  c.get("call_long_strike"),
            }
            if any(v is None for v in strike_dict.values()):
                log.warning(f"Skipping {c['ticker']} Iron Condor — one or more strikes are None: {strike_dict}")
                continue
        elif _cst is not None and _cst.strike_schema == _SS.SINGLE_LEG:
            strike_dict = {"short": c.get("short_strike")}
            if strike_dict["short"] is None:
                log.warning(f"Skipping {c['ticker']} {c['structure']} — short strike is None")
                continue
        else:
            strike_dict = {"short": c.get("short_strike"), "long": c.get("long_strike")}
            if strike_dict["short"] is None or strike_dict["long"] is None:
                log.warning(f"Skipping {c['ticker']} {c['structure']} — short or long strike is None")
                continue

        trade = {
            "id":           tid,
            "rank":         rank,
            "entered_at":   datetime.now(EDT).isoformat(),
            "ticker":       c["ticker"],
            "structure":    c["structure"],
            "expiry":       c["expiry"],
            "strikes":      strike_dict,
            "width":        round(width, 4),
            "entry_credit": ec,
            "entry_mid":    ep["credit_mid"],
            "entry_legs":   ep["legs"],
            "max_profit":   ec,
            "max_loss":     round(width - ec, 4) if width > ec else c.get("max_loss"),
            "dte_at_entry": c["dte"],
            "spot_at_entry":c["spot_at_entry"],
            "signal_rating":c["signal_rating"],
            "signal_score": c["signal_score"],
            "profit_target":round(ec * _s["profit_target_pct"], 4),
            "stop_loss":    round(ec * _s["stop_loss_mult"], 4),
            "status":       "open",
            "snapshots":    [],
            "exit":         None,
            "iv_edge_vp":   c.get("iv_edge_vp"),
            "iv_edge_label":c.get("iv_edge_label"),
            # Kelly sizing from calibrated ML pred_dist
            "kelly_fraction":  (c.get("kelly") or {}).get("kelly_f"),
            "kelly_pct":       (c.get("kelly") or {}).get("kelly_pct"),
            "kelly_contracts": (c.get("kelly") or {}).get("kelly_contracts"),
            "kelly_capital":   (c.get("kelly") or {}).get("kelly_capital"),
            "ml_p_win":        (c.get("kelly") or {}).get("p_win"),
            "ml_confidence":   (c.get("kelly") or {}).get("confidence"),
        }
        trades.append(trade)
        new.append(trade)
        log.info(f"Paper trade #{rank}: {tid}  credit={ec:.3f}  expiry={c['expiry']}")

        # Write a training snapshot at entry so managed-exit outcomes feed POP model
        try:
            from scripts.training_data_collector import write_paper_trade_snapshot
            analyze_row = row_by_ticker.get(c["ticker"], {})
            write_paper_trade_snapshot(trade, analyze_row)
        except Exception as _snap_err:
            log.warning(f"Training snapshot write failed for {tid}: {_snap_err}")

    save_trades(trades)
    return {"ok": True, "date": today_str, "recorded": len(new), "trades": new}


# ── Evening check ─────────────────────────────────────────────────────────────

def run_evening_check(force=False):
    """
    Update open paper trades with current marks.
    Close expired or rule-triggered positions.
    Set force=True for testing on non-market days.
    """
    import yfinance as yf

    if not force and not is_market_day():
        return {"skipped": True, "reason": "Not a market day"}

    trades  = load_trades()
    now     = datetime.now(EDT)
    today   = date.today()
    updated = []

    for trade in trades:
        if trade["status"] != "open":
            continue

        ticker   = trade["ticker"]
        expiry   = trade["expiry"]
        ec       = trade["entry_credit"]

        try:
            exp_date = date.fromisoformat(expiry)
        except Exception:
            continue

        ul_price = fetch_underlying_price(ticker)

        market_closed_today = now.hour >= MARKET_CLOSE_HOUR
        if exp_date < today or (exp_date == today and market_closed_today):
            # ── Expired ──────────────────────────────────────────────────────
            if ul_price is None:
                log.warning(f"Cannot fetch {ticker} price for expiry check, skipping")
                continue
            spread_val, pnl_ps = _expiry_pnl(trade, ul_price)
            if pnl_ps is None:
                continue
            win = pnl_ps > 0
            trade["status"] = "expired_profit" if win else "expired_loss"
            trade["exit"] = {
                "ts":               now.isoformat(),
                "reason":           "expired",
                "spread_val":       spread_val,
                "ul_price":         ul_price,
                "pnl_per_share":    pnl_ps,
                "pnl_total":        round(pnl_ps * 100, 2),
                "pnl_pct_of_max":   round(pnl_ps / ec * 100, 1) if ec else None,
                "win":              win,
            }
            log.info(f"EXPIRED {trade['id']}: ul={ul_price}  P&L=${trade['exit']['pnl_total']:.2f}  {'WIN' if win else 'LOSS'}")

        else:
            # ── Still open: snapshot, and auto-close if 100% of max profit is hit ─
            mark = _current_mark(trade)
            if mark is None:
                continue
            is_debit   = trade.get("structure", "") in ("Call Debit Spread", "Put Debit Spread")
            entry_cost = trade.get("max_loss") or 0 if is_debit else ec
            unrealized = round(mark - entry_cost, 4) if is_debit else round(ec - mark, 4)
            pnl_pct_of_max = round(unrealized / ec * 100, 1) if ec else None

            if pnl_pct_of_max is not None and pnl_pct_of_max >= EARLY_CLOSE_PCT:
                # Max profit achieved before expiry — close now rather than risk
                # giving back gains while waiting for settlement.
                trade["status"] = "closed_target"
                trade["exit"] = {
                    "ts":               now.isoformat(),
                    "reason":           "max_profit",
                    "spread_val":       mark,
                    "ul_price":         ul_price,
                    "pnl_per_share":    unrealized,
                    "pnl_total":        round(unrealized * 100, 2),
                    "pnl_pct_of_max":   pnl_pct_of_max,
                    "win":              True,
                }
                log.info(f"CLOSED (max profit) {trade['id']}: mark={mark}  "
                         f"P&L=${trade['exit']['pnl_total']:.2f} ({pnl_pct_of_max}% of max)")
            else:
                # Check if short strike is now expensive vs vol surface (take-profit signal)
                iv_flag = None
                try:
                    from scripts.vol_surface import compute_mispricing
                    _mp = compute_mispricing(ticker, max_dte=60)
                    if _mp.get("ok") and _mp.get("slices"):
                        short_s = (trade.get("strikes") or {}).get("short") \
                                  or (trade.get("strikes") or {}).get("put_short") \
                                  or (trade.get("strikes") or {}).get("call_short")
                        if short_s is not None:
                            for sl in _mp["slices"]:
                                if sl["expiry"] == expiry:
                                    for sk in sl["strikes"]:
                                        if abs(sk["strike"] - float(short_s)) <= 1.0:
                                            vp = sk["mispricing"]
                                            if vp > IV_EDGE_FLAG_VP:
                                                iv_flag = f"short strike {short_s} now +{vp:.1f}vp expensive — consider closing"
                                            elif vp < -IV_EDGE_FLAG_VP:
                                                iv_flag = f"short strike {short_s} now {vp:.1f}vp cheap — vol moved against position"
                                            break
                                    break
                except Exception:
                    pass

                snap = {
                    "ts":         now.isoformat(),
                    "mark":       mark,
                    "ul_price":   ul_price,
                    "unrealized": unrealized,
                }
                if iv_flag:
                    snap["iv_flag"] = iv_flag
                    log.info(f"IV surface flag on {trade['id']}: {iv_flag}")
                trade["snapshots"].append(snap)

        updated.append(trade["id"])

    save_trades(trades)
    return {"ok": True, "updated": len(updated), "ids": updated}


# ── Performance summary ───────────────────────────────────────────────────────

def get_performance_summary():
    trades = load_trades()
    closed = [t for t in trades if t.get("exit") is not None]
    open_  = [t for t in trades if t["status"] == "open"]

    def _stats(subset):
        if not subset:
            return {"count": 0}
        exits  = [t["exit"] for t in subset]
        wins   = [e for e in exits if e.get("win")]
        losses = [e for e in exits if not e.get("win")]
        pnls   = [e["pnl_per_share"] for e in exits if e.get("pnl_per_share") is not None]
        totals = [e["pnl_total"]     for e in exits if e.get("pnl_total")     is not None]
        avg_w  = round(sum(e["pnl_per_share"] for e in wins)   / len(wins),   4) if wins   else 0
        avg_l  = round(sum(e["pnl_per_share"] for e in losses) / len(losses), 4) if losses else 0
        wr     = len(wins) / len(subset) if subset else 0
        return {
            "count":      len(subset),
            "wins":       len(wins),
            "losses":     len(losses),
            "win_rate":   round(wr * 100, 1),
            "avg_win":    avg_w,
            "avg_loss":   avg_l,
            "expectancy": round(wr * avg_w + (1 - wr) * avg_l, 4),
            "total_pnl":  round(sum(totals), 2),
        }

    # Breakdown tables
    def _breakdowns(key):
        groups = {}
        for t in closed:
            k = t.get(key) or "Unknown"
            groups.setdefault(k, []).append(t)
        return {k: _stats(v) for k, v in groups.items()}

    # Equity curve
    sorted_closed = sorted(closed, key=lambda t: t["exit"]["ts"])
    equity, running = [], 0.0
    for t in sorted_closed:
        pnl      = t["exit"].get("pnl_total") or 0
        running += pnl
        equity.append({
            "date":       t["exit"]["ts"][:10],
            "id":         t["id"],
            "ticker":     t["ticker"],
            "structure":  t["structure"],
            "pnl":        round(pnl, 2),
            "cumulative": round(running, 2),
            "win":        t["exit"].get("win"),
        })

    # Open trade snapshots — attach latest unrealized P&L
    for t in open_:
        snaps = t.get("snapshots") or []
        t["latest_unrealized"] = snaps[-1]["unrealized"] if snaps else None
        t["latest_mark"]       = snaps[-1]["mark"]       if snaps else None
        t["latest_ul"]         = snaps[-1]["ul_price"]   if snaps else None

    return {
        "overall":        _stats(closed),
        "open_count":     len(open_),
        "closed_count":   len(closed),
        "by_structure":   _breakdowns("structure"),
        "by_signal":      _breakdowns("signal_rating"),
        "equity_curve":   equity,
        "open_trades":    open_,
        "recent_closed":  sorted_closed[-30:],
        "all_trades":     trades,
    }


# ── Live per-leg marks for open trades ───────────────────────────────────────

def get_live_marks():
    """
    Fetch current bid/ask for every leg of every open paper trade.
    Called by the dashboard on page load to show a live per-leg breakdown.
    Returns {trade_id: {"mark", "unrealized", "legs"}} or {"error": str}.
    """
    trades = load_trades()
    result = {}

    for trade in trades:
        if trade["status"] != "open":
            continue
        tid     = trade["id"]
        ticker  = trade["ticker"]
        expiry  = trade["expiry"]
        s       = trade["structure"]
        strikes = trade.get("strikes", {})

        try:
            from config.structures import get_or_none as _get_st
            from config.structures._base import StrikeSchema
            st = _get_st(s)
            if st is None:
                result[tid] = {"error": f"Unknown structure: {s}"}
                continue

            if st.strike_schema == StrikeSchema.SINGLE_LEG:
                sh = _fetch_leg(ticker, expiry, strikes.get("short"), st.option_type)
                if sh:
                    mark = max(0.0, round(sh["ask"], 4))
                    result[tid] = {
                        "mark":       mark,
                        "unrealized": round(trade["entry_credit"] - mark, 4),
                        "legs":       {"short": sh},
                    }

            elif st.strike_schema == StrikeSchema.IRON_CONDOR:
                ps = _fetch_leg(ticker, expiry, strikes.get("put_short"),  "put")
                pl = _fetch_leg(ticker, expiry, strikes.get("put_long"),   "put")
                cs = _fetch_leg(ticker, expiry, strikes.get("call_short"), "call")
                cl = _fetch_leg(ticker, expiry, strikes.get("call_long"),  "call")
                if all([ps, pl, cs, cl]):
                    mark = max(0.0, round(
                        (ps["ask"] - pl["bid"]) + (cs["ask"] - cl["bid"]), 4
                    ))
                    result[tid] = {
                        "mark":       mark,
                        "unrealized": round(trade["entry_credit"] - mark, 4),
                        "legs": {"put_short": ps, "put_long": pl, "call_short": cs, "call_long": cl},
                    }

            elif st.strike_schema == StrikeSchema.TWO_LEG:
                opt = st.option_type
                sh = _fetch_leg(ticker, expiry, strikes.get("short"), opt)
                lo = _fetch_leg(ticker, expiry, strikes.get("long"),  opt)
                if sh and lo:
                    if st.is_credit:
                        mark = max(0.0, round(sh["ask"] - lo["bid"], 4))
                        result[tid] = {
                            "mark":       mark,
                            "unrealized": round(trade["entry_credit"] - mark, 4),
                            "legs":       {"short": sh, "long": lo},
                        }
                    else:
                        spread_val = max(0.0, round(lo["bid"] - sh["ask"], 4))
                        entry_debit = trade.get("max_loss") or 0
                        result[tid] = {
                            "mark":         spread_val,
                            "unrealized":   round(spread_val - entry_debit, 4),
                            "debit_spread": True,
                            "legs":         {"long": lo, "short": sh},
                        }

        except Exception as e:
            log.warning(f"get_live_marks failed for {tid}: {e}")
            result[tid] = {"error": str(e)}

    return result


def get_live_marks_iter():
    """Generator version — yields (trade_id, mark_data) one trade at a time for SSE streaming."""
    trades = load_trades()
    for trade in trades:
        if trade["status"] != "open":
            continue
        tid     = trade["id"]
        ticker  = trade["ticker"]
        expiry  = trade["expiry"]
        s       = trade["structure"]
        strikes = trade.get("strikes", {})
        try:
            from config.structures import get_or_none as _get_st
            from config.structures._base import StrikeSchema
            st = _get_st(s)
            if st is None:
                yield tid, {"error": f"Unknown structure: {s}"}
                continue

            mark_data = None
            if st.strike_schema == StrikeSchema.SINGLE_LEG:
                sh = _fetch_leg(ticker, expiry, strikes.get("short"), st.option_type)
                if sh:
                    mark = max(0.0, round(sh["ask"], 4))
                    mark_data = {"mark": mark, "unrealized": round(trade["entry_credit"] - mark, 4),
                                 "legs": {"short": sh}}

            elif st.strike_schema == StrikeSchema.IRON_CONDOR:
                ps = _fetch_leg(ticker, expiry, strikes.get("put_short"),  "put")
                pl = _fetch_leg(ticker, expiry, strikes.get("put_long"),   "put")
                cs = _fetch_leg(ticker, expiry, strikes.get("call_short"), "call")
                cl = _fetch_leg(ticker, expiry, strikes.get("call_long"),  "call")
                if all([ps, pl, cs, cl]):
                    mark = max(0.0, round((ps["ask"] - pl["bid"]) + (cs["ask"] - cl["bid"]), 4))
                    mark_data = {"mark": mark, "unrealized": round(trade["entry_credit"] - mark, 4),
                                 "legs": {"put_short": ps, "put_long": pl, "call_short": cs, "call_long": cl}}

            elif st.strike_schema == StrikeSchema.TWO_LEG:
                opt = st.option_type
                sh = _fetch_leg(ticker, expiry, strikes.get("short"), opt)
                lo = _fetch_leg(ticker, expiry, strikes.get("long"),  opt)
                if sh and lo:
                    if st.is_credit:
                        mark = max(0.0, round(sh["ask"] - lo["bid"], 4))
                        mark_data = {"mark": mark, "unrealized": round(trade["entry_credit"] - mark, 4),
                                     "legs": {"short": sh, "long": lo}}
                    else:
                        spread_val  = max(0.0, round(lo["bid"] - sh["ask"], 4))
                        entry_debit = trade.get("max_loss") or 0
                        mark_data   = {"mark": spread_val, "unrealized": round(spread_val - entry_debit, 4),
                                       "debit_spread": True, "legs": {"long": lo, "short": sh}}

            yield tid, mark_data if mark_data else {"error": "No quote"}
        except Exception as e:
            log.warning(f"get_live_marks_iter failed for {tid}: {e}")
            yield tid, {"error": str(e)}
