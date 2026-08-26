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

def _struct_abbr(name: str) -> str:
    """Return the unique 3-char trade-ID abbreviation defined on the structure object."""
    from config.structures import get_or_none as _gst
    st = _gst(name)
    return st.abbr if st and st.abbr else name[:3].upper()
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
TRADES_PATH    = DATA_DIR / "paper_trades.json"
DECISIONS_PATH = DATA_DIR / "scan_decisions.jsonl"


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
        risk    = cfg.get("risk_limits", {})
        return {
            "profit_target_pct":   float(mgmt.get("profit_target_pct",    0.50)),
            "stop_loss_mult":      float(mgmt.get("stop_loss_mult",        3.0)),
            "max_risk_pct":        float(capital.get("max_risk_pct",       0.12)),
            "capital_amount":      float(capital.get("amount",          1000.0)),
            "max_daily_loss_pct":       float(risk.get("max_daily_loss_pct",        0.03)),
            "max_weekly_loss_pct":      float(risk.get("max_weekly_loss_pct",       0.06)),
            "circuit_breakers_enabled": bool(risk.get("circuit_breakers_enabled",  False)),
            "paper_trades":             cfg.get("paper_trades", {}),
        }
    except Exception:
        return dict(_SETTINGS_DEFAULTS)


def _open_capital(trades: list) -> float:
    """Sum of capital committed to all currently-open positions, in dollars.

    capital_required is already stored per-contract (confirmed: e.g. a trade
    with entry_credit=1.44 has capital_required=144.0 — already ×100, not
    ×1). max_loss is per-share and needs the ×100 conversion. Mixing these
    with one blanket ×100 (pre-2026-08-24 behavior) inflated capital_required
    entries 100x, e.g. capital_required=1420.0 became $142,000 instead of
    $1,420 — the root cause of a reported -$140,426 "buying power" that was
    actually a unit bug, not a real 94x over-leveraged book. This function is
    intentionally unconditional (not gated by circuit_breakers_enabled) — see
    check_circuit_breakers() docstring for why the two were wrongly coupled.
    """
    open_capital = 0.0
    for t in trades:
        if t.get("status") != "open":
            continue
        cr = t.get("capital_required")
        if cr is not None:
            open_capital += float(cr)                 # already per-contract
        else:
            open_capital += float(t.get("max_loss") or 0) * 100  # per-share -> per-contract
    return open_capital


def check_circuit_breakers(trades: list) -> dict:
    """
    Check daily and weekly realized-loss circuit breakers against closed trades,
    and report current buying power against all open positions.

    circuit_breakers_enabled controls ONLY whether loss-breach enforcement is
    active (the "ok"/"breaches" halt-new-trades behavior) — it must NOT affect
    whether buying_power reflects real open-position capital. Prior to
    2026-08-24, disabling circuit breakers made this function short-circuit
    and return buying_power=capital_amount unconditionally, silently
    disabling the capital-feasibility gate for every scan regardless of how
    much capital was already committed — the book accumulated 42 open
    positions with no effective cumulative cap.

    Returns:
      {
        "ok": bool,               # False = halt new trades (loss breach; always True when disabled)
        "enabled": bool,          # whether breach enforcement is active
        "daily_loss":   float,    # realized P&L today (negative = loss)
        "weekly_loss":  float,    # realized P&L this Mon-today window
        "daily_limit":  float,    # dollar threshold for daily halt (0 when disabled)
        "weekly_limit": float,    # dollar threshold for weekly halt (0 when disabled)
        "breaches":     [str],    # human-readable breach descriptions
        "buying_power": float,    # capital_amount - open_capital — ALWAYS computed live,
                                   # regardless of circuit_breakers_enabled. Can go negative
                                   # if the book is over-committed; callers must treat a
                                   # negative value as "no capital available", not clamp it
                                   # to 0 (clamping would silently readmit over-leveraged
                                   # books to full capacity). Verified: downstream capital
                                   # gates use `required > buying_power`, which correctly
                                   # rejects every candidate when buying_power is negative
                                   # without needing special-case handling.
      }
    """
    s       = _load_settings()
    enabled = s.get("circuit_breakers_enabled", False)
    capital = s.get("capital_amount", 1000.0)

    open_capital = _open_capital(trades)
    buying_power = capital - open_capital  # intentionally unclamped — see docstring

    if not enabled:
        return {
            "ok": True, "enabled": False,
            "daily_loss": 0.0, "weekly_loss": 0.0,
            "daily_limit": 0.0, "weekly_limit": 0.0,
            "breaches": [], "buying_power": round(buying_power, 2),
        }

    daily_lim = capital * s["max_daily_loss_pct"]
    week_lim  = capital * s["max_weekly_loss_pct"]

    today     = date.today()
    # Week window: Monday of the current week through today
    week_start = today - timedelta(days=today.weekday())

    daily_pnl  = 0.0
    weekly_pnl = 0.0

    for t in trades:
        exit_info = t.get("exit") or {}
        pnl = exit_info.get("pnl_total")
        if pnl is not None:
            try:
                exit_date = date.fromisoformat(exit_info.get("ts", "")[:10])
            except ValueError:
                continue
            if exit_date == today:
                daily_pnl += pnl
            if week_start <= exit_date <= today:
                weekly_pnl += pnl

    breaches = []
    if daily_pnl < 0 and abs(daily_pnl) >= daily_lim:
        breaches.append(
            f"Daily loss ${abs(daily_pnl):.2f} ≥ limit ${daily_lim:.2f} "
            f"({s['max_daily_loss_pct']:.0%} of capital) — halting new trades today."
        )
    if weekly_pnl < 0 and abs(weekly_pnl) >= week_lim:
        breaches.append(
            f"Weekly loss ${abs(weekly_pnl):.2f} ≥ limit ${week_lim:.2f} "
            f"({s['max_weekly_loss_pct']:.0%} of capital) — halting until Monday."
        )

    return {
        "ok":           len(breaches) == 0,
        "enabled":      True,
        "daily_loss":   round(daily_pnl,  2),
        "weekly_loss":  round(weekly_pnl, 2),
        "daily_limit":  round(daily_lim,  2),
        "weekly_limit": round(week_lim,   2),
        "breaches":     breaches,
        "buying_power": round(buying_power, 2),
    }


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
                "fill_source": "live_quote",
            }

        if st is not None and st.strike_schema == StrikeSchema.SINGLE_LEG:
            opt = st.option_type
            sh  = _fetch_leg(ticker, expiry, candidate.get("short_strike"), opt)
            if not sh: return None
            return {
                "credit_bid": round(sh["bid"], 4),
                "credit_mid": round(sh["mid"], 4),
                "legs": {"short": sh},
                "fill_source": "live_quote",
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
                    "fill_source": "live_quote",
                }
            else:
                # Debit spread: use live ask-side fill (conservative debit paid).
                # lo["ask"] is what we pay for the long leg; sh["bid"] is what we
                # receive for the short leg. Fall back to scan-time max_profit only
                # when the leg fetch fails (market closed, chain unavailable).
                if lo and sh and lo.get("ask") and sh.get("bid"):
                    live_debit = round(lo["ask"] - sh["bid"], 4)
                    live_mid   = round(lo["mid"] - sh["mid"], 4) if lo.get("mid") and sh.get("mid") else live_debit
                    return {
                        "credit_bid": live_debit,
                        "credit_mid": live_mid,
                        "legs": {"long": lo, "short": sh},
                        "fill_source": "live_quote",
                    }
                val = candidate.get("max_profit")
                if val is None: return None
                return {
                    "credit_bid": val, "credit_mid": val,
                    "legs": ({"long": lo, "short": sh} if lo and sh else {}),
                    "fill_source": "fallback_scan",
                }

        # Long Strangle — buy put at short_strike + buy call at long_strike
        if s == "Long Strangle":
            put_leg  = _fetch_leg(ticker, expiry, candidate.get("short_strike"), "put")
            call_leg = _fetch_leg(ticker, expiry, candidate.get("long_strike"),  "call")
            if not put_leg or not call_leg: return None
            debit_bid = put_leg["ask"] + call_leg["ask"]   # worst-case cost to enter
            debit_mid = put_leg["mid"] + call_leg["mid"]
            return {
                "credit_bid": round(debit_bid, 4),
                "credit_mid": round(debit_mid, 4),
                "legs": {"put": put_leg, "call": call_leg},
                "fill_source": "live_quote",
            }

        # Fallback for structures with no fixed strike schema (Calendar, Diagonal, Jade Lizard)
        val = candidate.get("max_profit")
        if val is None: return None
        return {"credit_bid": val, "credit_mid": val, "legs": {}, "fill_source": "fallback_scan"}

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
            return max(0.0, round(sh["mid"], 4))

        if st.strike_schema == StrikeSchema.IRON_CONDOR:
            ps = _fetch_leg(ticker, expiry, strikes.get("put_short"),  "put")
            pl = _fetch_leg(ticker, expiry, strikes.get("put_long"),   "put")
            cs = _fetch_leg(ticker, expiry, strikes.get("call_short"), "call")
            cl = _fetch_leg(ticker, expiry, strikes.get("call_long"),  "call")
            if not all([ps, pl, cs, cl]): return None
            return max(0.0, round((ps["mid"] - pl["mid"]) + (cs["mid"] - cl["mid"]), 4))

        if st.strike_schema == StrikeSchema.TWO_LEG:
            opt = st.option_type
            sh  = _fetch_leg(ticker, expiry, strikes.get("short"), opt)
            lo  = _fetch_leg(ticker, expiry, strikes.get("long"),  opt)
            if not sh or not lo: return None
            if st.is_credit:
                return max(0.0, round(sh["mid"] - lo["mid"], 4))
            else:
                return max(0.0, round(lo["mid"] - sh["mid"], 4))

        if trade["structure"] == "Long Strangle":
            put_leg  = _fetch_leg(ticker, expiry, strikes.get("short"), "put")
            call_leg = _fetch_leg(ticker, expiry, strikes.get("long"),  "call")
            if not put_leg or not call_leg: return None
            return max(0.0, round(put_leg["mid"] + call_leg["mid"], 4))

        if trade["structure"] in ("Calendar Spread", "Diagonal Spread"):
            # Both legs use calls; fetch against stored (front) expiry as a best-effort
            # approximation. Far-month leg is underpriced this way, so the mark is a
            # lower bound — acceptable for stop-loss/target monitoring.
            opt = "call"
            sh  = _fetch_leg(ticker, expiry, strikes.get("short"), opt)
            lo  = _fetch_leg(ticker, expiry, strikes.get("long"),  opt)
            if not sh or not lo: return None
            return max(0.0, round(lo["mid"] - sh["mid"], 4))

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
            return val, round(val - ec, 4)  # ec = entry_credit = debit paid

    if st.name == "Long Strangle":
        put_k  = strikes.get("short")   # put leg (lower strike)
        call_k = strikes.get("long")    # call leg (higher strike)
        if put_k is None or call_k is None: return None, None
        put_val  = max(0.0, put_k  - ul_price)
        call_val = max(0.0, ul_price - call_k)
        val      = round(put_val + call_val, 4)
        return val, round(val - ec, 4)  # ec = entry_credit = total debit paid

    # Short Strangle / Short Straddle — sell put + sell call, both defined by strikes dict
    if trade["structure"] in ("Short Strangle", "Short Straddle"):
        put_k  = strikes.get("put_short") or strikes.get("short")
        call_k = strikes.get("call_short") or strikes.get("long")
        if put_k is None or call_k is None: return None, None
        put_val  = max(0.0, put_k  - ul_price)
        call_val = max(0.0, ul_price - call_k)
        val      = round(put_val + call_val, 4)
        return val, round(ec - val, 4)

    return None, None   # Calendar, Diagonal, Jade Lizard — path-dependent


# ── Capital-rejected persistence ──────────────────────────────────────────────

def _persist_capital_rejected(entries: list) -> None:
    """Append capital-rejected trade entries to data/capital_rejected.jsonl."""
    if not entries:
        return
    import json as _json
    from datetime import datetime as _dt
    _path = Path(__file__).resolve().parent.parent / "data" / "capital_rejected.jsonl"
    _path.parent.mkdir(parents=True, exist_ok=True)
    _now = _dt.now()
    _date_str = _now.strftime("%Y-%m-%d")
    _time_str = _now.strftime("%H:%M:%S")
    with open(_path, "a", encoding="utf-8") as _f:
        for entry in entries:
            _f.write(_json.dumps({"date": _date_str, "scan_time": _time_str, **entry}) + "\n")
    log.info("[capital_rejected] %d entries persisted to capital_rejected.jsonl", len(entries))


def load_capital_rejected(days: int = 30, from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Read capital_rejected.jsonl and return entries grouped by date, newest first.

    If from_date/to_date (YYYY-MM-DD) are given they take precedence over days.
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    _path = Path(__file__).resolve().parent.parent / "data" / "capital_rejected.jsonl"
    if not _path.exists():
        return []
    if from_date or to_date:
        lo = from_date or "0000-00-00"
        hi = to_date   or "9999-99-99"
    else:
        lo = (_dt.now() - _td(days=days)).strftime("%Y-%m-%d")
        hi = "9999-99-99"
    by_date: dict[str, list] = {}
    try:
        with open(_path, encoding="utf-8") as _f:
            for line in _f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:
                    continue
                d = rec.get("date", "")
                if d < lo or d > hi:
                    continue
                by_date.setdefault(d, []).append(rec)
    except Exception as e:
        log.warning("[capital_rejected] Failed to read file: %s", e)
        return []
    return [{"date": d, "trades": by_date[d]} for d in sorted(by_date, reverse=True)]


# ── Select top-3 (mirrors app.py build_top_trades without AI) ────────────────

def _select_top3(rows, ml_snapshot: dict | None = None, open_positions: list | None = None, buying_power: float | None = None, _decision_meta_out: list | None = None):
    """Return (candidates, cap_rejected, rank_items) using the shared filter/rank pipeline.

    rank_items: full ranked list from rank_candidates (all gate-surviving candidates,
    pre-3-trade-cap) — passed to write_scan_candidate_snapshots for per-candidate
    training snapshots.

    ml_snapshot: {ticker: pred_result} from predict_all or ml_cache. When
    provided it is attached to each row as row["ml"] so the confidence gate
    and Kelly sizing have access to pred_dist. When absent the ML gate is
    bypassed (backward compatible).
    open_positions: list of currently open trade dicts for portfolio risk check.
    cap_rejected: list of dicts for trades filtered by Gate 10 (capital feasibility).
    """
    from scripts.candidate_ranker import rank_candidates
    from scripts.candidate_provider import kelly_from_pred_dist

    # Attach ML snapshot to rows so filter_candidates can read pred_dist
    if ml_snapshot:
        for row in rows:
            if "ml" not in row or row["ml"] is None:
                row["ml"] = ml_snapshot.get(row.get("ticker"))

    # Load excluded structures so they are filtered before the n=3 cap
    try:
        _pt_excl = _load_settings().get("paper_trades", {}).get("exclude_structures", [])
        _excluded = {s.strip() for s in _pt_excl}
    except Exception:
        _excluded = set()

    # Request extra candidates so excluded ones and capital-blocked ones don't shrink the final list.
    # Capital gate in run_morning_scan can reject the top items (e.g. expensive debit spreads vs
    # low buying power), so fetch enough that there are fallbacks after those rejections.
    _n_fetch = max(15, 3 + len(_excluded) * 2 + 6)
    _cap_rejected_out: list = []
    items = rank_candidates(rows, n=_n_fetch, quality_floor=0, open_positions=open_positions or [], paper_trade=True, buying_power=buying_power, _cap_rejected_out=_cap_rejected_out, _decision_meta_out=_decision_meta_out)
    # Return up to _n_fetch candidates (not capped at 3 here) so the caller's
    # capital gate and strike checks can skip expensive candidates and still fill 3 slots.
    result = []
    for item in items:
        if len(result) >= _n_fetch:
            break
        row, c = item["row"], item["candidate"]
        if c["structure"] in _excluded:
            log.info(
                f"[paper_trade] Skipping {row['ticker']} {c['structure']} "
                f"— listed in paper_trades.exclude_structures"
            )
            continue

        # Kelly sizing: prefer MC prob_profit_sim as p_profit when available.
        pred_dist = item.get("pred_dist") or (row.get("ml") or {}).get("pred_dist")
        pop_pct   = c.get("pop")
        _mc_pps   = c.get("mc_prob_profit_sim")
        p_profit  = (_mc_pps / 100.0) if _mc_pps is not None else (
                    (pop_pct / 100.0) if pop_pct is not None else None)
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
            # EV calibration fields — snapshotted at entry so post-hoc analysis
            # can compare predicted EV against realized P&L per trade.
            "entry_ev_mc":           item.get("ev_mc"),
            "entry_ev_delta_proxy":  item.get("ev_delta_proxy"),
            "entry_ev_is_proxy":     item.get("ev_is_proxy"),
            "entry_prob_profit_sim": c.get("mc_prob_profit_sim"),
            "entry_cvar_loss":       c.get("mc_cvar_loss"),
            "entry_prob_of_touch":   c.get("mc_prob_of_touch"),
            "entry_mc_vol_source":   c.get("mc_vol_source"),
            # Phase 2A: S_T distribution snapshot at entry (observational)
            "entry_mc_expiry_mean":   c.get("mc_expiry_mean"),
            "entry_mc_expiry_median": c.get("mc_expiry_median"),
            "entry_mc_expiry_p10":    c.get("mc_expiry_p10"),
            "entry_mc_expiry_p25":    c.get("mc_expiry_p25"),
            "entry_mc_expiry_p50":    c.get("mc_expiry_p50"),
            "entry_mc_expiry_p75":    c.get("mc_expiry_p75"),
            "entry_mc_expiry_p90":    c.get("mc_expiry_p90"),
            "entry_distribution_model_version": c.get("distribution_model_version"),
            "entry_mc_zone_below_long":    c.get("mc_zone_below_long"),
            "entry_mc_zone_between":       c.get("mc_zone_between"),
            "entry_mc_zone_above_short":   c.get("mc_zone_above_short"),
            "entry_mc_zone_in_profit":     c.get("mc_zone_in_profit"),
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
            # Entry analytics — zero-cost to capture (already computed in analyze.py)
            "pop":              c.get("pop"),
            "net_delta":        c.get("net_delta"),
            "net_theta":        c.get("net_theta"),
            "net_gamma":        c.get("net_gamma"),
            "net_vega":         c.get("net_vega"),
            "breakeven":        c.get("breakeven"),
            "hv30":             row.get("hv30"),
            "iv_rank_52w":      row.get("iv_rank_52w"),
        })
    return result, _cap_rejected_out, items


# ── ML scores snapshot at trade entry ─────────────────────────────────────────

def _ml_scores_at_entry(candidate: dict, ml_snapshot: dict) -> dict:
    """
    Capture the ML model outputs at the moment a paper trade is entered.
    Stored verbatim in the trade record so post-hoc analysis can ask:
    "Did the model signal match the outcome?"

    Includes model version metadata (trained_at, git_sha) so future audits
    can identify which model version was live when each trade was taken.
    """
    ticker = candidate.get("ticker", "")
    ml     = (ml_snapshot or {}).get(ticker) or {}

    scores = {
        "regime":            ml.get("regime"),
        "regime_prob":       ml.get("regime_prob"),
        "p_up":              ml.get("p_up"),
        "p_flat":            ml.get("p_flat"),
        "p_down":            ml.get("p_down"),
        "expected_return":   ml.get("expected_return"),
        "expected_vol":      ml.get("expected_vol"),
        "expected_move_pct": ml.get("expected_move_pct"),
        "iv_direction":      ml.get("iv_direction"),
        "iv_expanding_prob": ml.get("iv_expanding_prob"),
        "pop_score":         ml.get("pop_score"),
        "pop_threshold":     ml.get("pop_threshold"),
        "meta_score":        ml.get("meta_score"),
        # Added 2026-08-22: needed to run the composite-score ablation
        # (meta_score in/out, return_classifier in/out) against real outcomes
        # once enough trades close — previously missing, blocking that
        # analysis entirely (only meta_score was captured, and even that on
        # just 31/187 closed trades). See [[project_composite_ablation]].
        "p_return_gt10":     ml.get("p_return_gt10"),
        "composite_score":   ml.get("composite_score"),
        "confidence_tier":   ml.get("confidence_tier"),
        "is_anomaly":        ml.get("is_anomaly"),
        "anomaly_score":     ml.get("anomaly_score"),
        "model_versions":    {},
    }

    # Capture trained_at + git_sha for each model artifact that was loaded.
    import joblib
    for name, path in [
        ("pop",       _ROOT / "data" / "models" / "pop_classifier.joblib"),
        ("regime",    _ROOT / "data" / "models" / "regime_classifier.joblib"),
        ("return",    _ROOT / "data" / "models" / "return_regressor.joblib"),
        ("vol",       _ROOT / "data" / "models" / "volatility_regressor.joblib"),
        ("meta",      _ROOT / "data" / "models" / "meta_ensemble.joblib"),
    ]:
        try:
            art = joblib.load(path)
            scores["model_versions"][name] = {
                "trained_at": art.get("trained_at"),
                "git_sha":    art.get("git_sha"),
            }
        except Exception:
            pass

    return scores


# ── Morning scan ──────────────────────────────────────────────────────────────

def run_morning_scan(params=None, force=False, scan_time="morning"):
    """
    Record today's top-3 as paper trades.

    scan_time: "morning" (default, 10 AM) or "afternoon" (2 PM).
      - Trade IDs are prefixed AM/PM so the same ticker can appear in both scans.
      - A short-DTE pass (max_dte=10) runs after the normal pass to collect
        weekly-expiry candidates that expire and label within 7-10 days.
    Set force=True to run even on non-market days (for testing).
    """
    import sys
    sys.path.insert(0, str(_ROOT))

    if not force and not is_market_day():
        return {"skipped": True, "reason": "Not a market day"}

    from scripts.analyze import analyze_ticker, DEFAULT_PARAMS
    from config.watchlist import PAPER_WATCHLIST

    scan_tag  = "AM" if scan_time == "morning" else "PM"
    p         = {**DEFAULT_PARAMS, **(params or {})}

    # Paper-trade parameter overrides from settings.toml [paper_trades]
    try:
        from pathlib import Path as _Ppt
        try:
            import tomllib as _tlpt
        except ImportError:
            import tomli as _tlpt
        _pt_cfg = _tlpt.loads(
            (_Ppt(__file__).resolve().parent.parent / "config" / "settings.toml")
            .read_text(encoding="utf-8")
        ).get("paper_trades", {})
        for _key in ("min_open_interest",):
            if _key in _pt_cfg and _key in p:
                p[_key] = type(p[_key])(_pt_cfg[_key])
    except Exception:
        pass

    short_p   = {**p, "min_dte": 7, "max_dte": 10}   # weekly expiry pass

    from scripts.data_fetch import warmup_data_sources, clear_scan_cache
    warmup_data_sources(log)

    # Fetch ML predictions BEFORE analyze_ticker() workers so each ticker gets
    # the correct ML regime rather than the DEFAULT_PARAMS fallback ("chop").
    # Uses ml_cache when warm; falls back to a fresh predict_all when cold.
    _ml_snapshot: dict = {}
    try:
        from scripts.ml_cache import ml_cache as _mlc
        _cache_stale = not _mlc.is_warm()
        if _cache_stale:
            _age = _mlc.age_seconds()
            log.warning(
                "ML cache stale (age: %ds) — forcing fresh predict_all before scan",
                int(_age) if _age is not None else -1,
            )
        _ml_snapshot = _mlc.get_all()
        if not _ml_snapshot or _cache_stale:
            from scripts.regime_predictor import predict_all as _pa
            _pr = _pa(list(PAPER_WATCHLIST))
            _ml_snapshot = {_pred["ticker"]: _pred for _pred in _pr.get("predictions", []) if _pred.get("ok")}
            if _ml_snapshot:
                _mlc.set_from_snapshot(_ml_snapshot)
    except Exception as _mle:
        log.warning(f"ML snapshot unavailable — regime defaults to 'chop': {_mle}")

    _ml_regime_by_ticker = {
        t: (snap.get("regime") or "chop") for t, snap in _ml_snapshot.items()
    }

    def _fetch_ticker_rows(ticker):
        _regime = _ml_regime_by_ticker.get(ticker, "chop")
        results = []
        try:
            results.append(analyze_ticker(ticker, p, regime=_regime))
        except Exception as e:
            log.warning(f"analyze_ticker({ticker}) failed: {e}")
        try:
            short_row = analyze_ticker(ticker, short_p, regime=_regime)
            if short_row.get("dte") and short_row["dte"] <= 10:
                short_row["_short_dte_pass"] = True
                results.append(short_row)
        except Exception as e:
            log.warning(f"analyze_ticker({ticker}, short_dte) failed: {e}")
        return results

    # Phase 1 — serial I/O prefetch (main thread, before any workers start).
    # Batch-downloads price history, then fetches expirations + chains one ticker
    # at a time at a controlled rate. Workers in Phase 2 read from cache only.
    from scripts.scan_prefetch import prefetch_scan_data
    prefetch_scan_data(list(PAPER_WATCHLIST), p, short_p, log_obj=log)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from scripts.data_fetch import _SCAN_CACHE
    _chain_keys = sum(1 for k in _SCAN_CACHE if k.startswith("chain:"))
    log.info(f"[scan] cache ready — {len(_SCAN_CACHE)} total keys, {_chain_keys} chain keys")

    rows = []
    # Phase 2 — pure-compute workers: cache hits only, zero live API calls.
    _max_workers = 8
    with ThreadPoolExecutor(max_workers=_max_workers) as _pool:
        _futures = {_pool.submit(_fetch_ticker_rows, t): t for t in PAPER_WATCHLIST}
        for _fut in as_completed(_futures):
            rows.extend(_fut.result())

    clear_scan_cache()  # release prefetch memory after workers finish

    row_by_ticker = {r["ticker"]: r for r in rows}

    trades      = load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]

    # ── Circuit breakers — halt before doing any work if limits are breached ──
    cb = check_circuit_breakers(trades)
    if not cb["ok"]:
        for breach in cb["breaches"]:
            log.warning("[circuit breaker] %s", breach)
        return {
            "ok":             False,
            "halted":         True,
            "circuit_breaker": cb,
            "recorded":       0,
            "trades":         [],
        }
    if cb.get("enabled", True):
        log.info(
            "[circuit breaker] OK — daily P&L $%.2f / limit $%.2f | "
            "weekly P&L $%.2f / limit $%.2f | buying power $%.2f",
            cb["daily_loss"], cb["daily_limit"],
            cb["weekly_loss"], cb["weekly_limit"],
            cb["buying_power"],
        )
    else:
        log.debug("[circuit breaker] disabled — skipping loss/buying-power enforcement.")

    # capital_gate_basis: "cumulative" (default, correct long-term behavior) gates
    # each scan against TRUE remaining capital (cb["buying_power"] = capital_amount
    # - all open positions' committed capital). "per_scan" is a TEMPORARY bridge —
    # added 2026-08-24 alongside the check_circuit_breakers() accounting fix, which
    # correctly revealed a $141,926.50 pre-existing backlog against $1,500 capital
    # (buying_power = -$140,426.50), blocking all new trades until ~2026-09-18.
    # "per_scan" explicitly ignores that backlog for gating purposes — ONLY this
    # scan's own picks are capped at capital_amount — so trade flow (and the
    # 2026-08-24 EV-formula fix in analyze.py) can be validated against fresh
    # data without waiting three weeks. The true cumulative figure (cb["buying_power"])
    # is still computed and logged above regardless of this setting — nothing here
    # hides the real number, it only changes what basis new-trade gating uses.
    # REVISIT: see [[project_capital_accounting_bridge]] — intended to be temporary,
    # not a silent permanent fallback (same mistake as the max_net_delta TEMP comment
    # found stale on 2026-08-22).
    _cap_basis = _load_settings().get("paper_trades", {}).get("capital_gate_basis", "cumulative")
    if _cap_basis == "per_scan":
        _gate_buying_power = float(_load_settings().get("capital_amount", 1500.0))
        log.warning(
            "[capital gate] TEMPORARY per_scan basis active — gating against $%.2f "
            "fresh allowance, ignoring true cumulative buying_power $%.2f. "
            "See [[project_capital_accounting_bridge]] in memory.",
            _gate_buying_power, cb["buying_power"],
        )
    else:
        _gate_buying_power = cb["buying_power"]

    _decision_meta_out: list = []
    candidates, _cap_rejected, _rank_items = _select_top3(rows, ml_snapshot=_ml_snapshot, open_positions=open_trades, buying_power=_gate_buying_power, _decision_meta_out=_decision_meta_out)
    _persist_capital_rejected(_cap_rejected)
    today_str = date.today().strftime("%Y%m%d")
    scan_decision_id = f"{today_str}{scan_tag}"
    seen      = {t["id"] for t in trades}
    new       = []
    rank      = 0  # incremented only when a trade is actually recorded

    _s_for_struct = _load_settings()
    _pt_cfg_s = (_s_for_struct.get("paper_trades") or {})
    _max_per_struct = int(_pt_cfg_s.get("max_trades_per_structure", 99))
    _excluded_s = set(_pt_cfg_s.get("exclude_structures", []))
    _struct_counts: dict[str, int] = {}

    for c in candidates:
        if len(new) >= 3:
            break
        rank += 1
        tid = f"{today_str}{scan_tag}_{c['ticker']}_{_struct_abbr(c['structure'])}_{rank}"
        if tid in seen:
            rank -= 1  # slot not consumed
            continue

        _struct_key = c["structure"]
        if _struct_key in _excluded_s:
            log.info(
                "[paper_trade] Skipping %s %s — in exclude_structures (repair-path bypass guard)",
                c["ticker"], _struct_key,
            )
            rank -= 1
            continue

        if _struct_counts.get(_struct_key, 0) >= _max_per_struct:
            log.info(
                "[paper_trade] Skipping %s %s — structure cap reached (%d/%d)",
                c["ticker"], _struct_key, _struct_counts[_struct_key], _max_per_struct,
            )
            rank -= 1
            continue

        ep = _entry_price(c)
        if ep is None or ep["credit_mid"] <= 0:
            ep = {"credit_bid": c["max_profit"] or 0, "credit_mid": c["max_profit"] or 0, "legs": {}}

        ec    = ep["credit_mid"]   # mid-price entry for realistic P&L
        width = (c.get("max_profit") or 0) + (c.get("max_loss") or 0)
        _s    = _load_settings()  # reload so runtime settings.toml changes take effect

        from config.structures import get_or_none as _gst
        from config.structures._base import StrikeSchema as _SS
        from scripts.candidate_provider import compute_capital_required, check_balance_for_candidate
        _cst = _gst(c["structure"])

        # Config-based margin gate — skip requires_margin structures when
        # paper_trades.margin_account = false (stable account-type filter,
        # not a balance check, so it belongs before any other per-trade logic)
        _pt_cfg = _s.get("paper_trades", {})
        _margin_ok = bool(_pt_cfg.get("margin_account", False))
        if not _margin_ok and _cst is not None and _cst.requires_margin:
            log.info(
                f"[paper_trade] Skipping {c['ticker']} {c['structure']} "
                f"— requires_margin=True but paper_trades.margin_account=false"
            )
            rank -= 1
            continue

        if _cst is not None and _cst.strike_schema == _SS.IRON_CONDOR:
            strike_dict = {
                "put_long":   c.get("put_long_strike"),
                "put_short":  c.get("put_short_strike"),
                "call_short": c.get("call_short_strike"),
                "call_long":  c.get("call_long_strike"),
            }
            if any(v is None for v in strike_dict.values()):
                log.warning(f"Skipping {c['ticker']} Iron Condor — one or more strikes are None: {strike_dict}")
                rank -= 1
                continue
        elif _cst is not None and _cst.strike_schema == _SS.SINGLE_LEG:
            strike_dict = {"short": c.get("short_strike")}
            if strike_dict["short"] is None:
                log.warning(f"Skipping {c['ticker']} {c['structure']} — short strike is None")
                rank -= 1
                continue
        else:
            strike_dict = {"short": c.get("short_strike"), "long": c.get("long_strike")}
            if strike_dict["short"] is None or strike_dict["long"] is None:
                log.warning(f"Skipping {c['ticker']} {c['structure']} — short or long strike is None")
                rank -= 1
                continue

        # Buying power check — enforced against circuit breaker's computed figure.
        from scripts.candidate_provider import compute_capital_required as _cap_req
        # Prefer capital_required pre-computed by analyze.py (structure-specific formula);
        # fall back to generic compute_capital_required only when not already set.
        _cap_needed = (c.get("capital_required") or _cap_req(c)) or 0
        if _cap_needed > 0 and _cap_needed > cb["buying_power"]:
            log.warning(
                "[capital] Skipping %s %s — needs $%.2f but only $%.2f buying power available.",
                c["ticker"], c["structure"], _cap_needed, cb["buying_power"],
            )
            rank -= 1
            continue
        # Deduct this trade's capital from buying_power so subsequent candidates
        # in the same loop don't double-count available funds.
        cb["buying_power"] = max(0.0, cb["buying_power"] - _cap_needed)

        _entry_mid    = ep.get("credit_mid", ec)
        _slippage_pct = (
            round((_entry_mid - ec) / _entry_mid, 4)
            if _entry_mid and _entry_mid > 0 and ec != _entry_mid
            else 0.0
        )
        # Debit spreads (CDS/PDS) with a real live-quoted price: ec = debit paid.
        # max_profit = width - debit, max_loss = debit.
        # Guards: is_credit=False + live_quote (Calendar/Diagonal fall back to max_profit as ec,
        # so they use the original formula) + max_profit≠None (Long Strangle has None).
        _debit_live = (c.get("is_credit") is False
                       and ep.get("fill_source") == "live_quote"
                       and c.get("max_profit") is not None)
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
            "entry_mid":    _entry_mid,
            "entry_legs":   ep["legs"],
            "scan_credit":  round(float(c.get("max_profit") or 0), 4),
            "slippage_pct": _slippage_pct,
            "fill_source":  ep.get("fill_source", "fallback_scan"),
            "max_profit":   (round(width - ec, 4) if width > ec else 0) if _debit_live else ec,
            "max_loss":     ec if _debit_live else (ec if c["structure"] == "Long Strangle" else (round(width - ec, 4) if width > ec else c.get("max_loss"))),
            "dte_at_entry": c["dte"],
            "spot_at_entry":c["spot_at_entry"],
            "signal_rating":c["signal_rating"],
            "signal_score": c["signal_score"],
            "profit_target":round((width - ec if _debit_live else ec) * _s["profit_target_pct"], 4),
            "stop_loss":    round(ec if _debit_live else ec * _s["stop_loss_mult"], 4),
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
            "ml_p_win":        (c.get("kelly") or {}).get("p_profit"),
            "ml_confidence":   (c.get("kelly") or {}).get("confidence"),
            "capital_required":        compute_capital_required(c),
            "capital_type":            _cst.capital_type    if _cst else None,
            "requires_margin":         _cst.requires_margin if _cst else False,
            "scan_time":               scan_time,
            "ranker_score":            c.get("ranker_score"),
            "position_size_factor":    c.get("position_size_factor"),
            "suggested_allocation_pct": c.get("suggested_allocation_pct"),
            "scan_decision_id":         scan_decision_id,
            "ml_scores_at_entry":      _ml_scores_at_entry(c, _ml_snapshot),
            # Entry analytics — captured for post-hoc win-rate-by-pop and Greek-drift analysis
            "pop_at_entry":            c.get("pop"),
            "net_delta_at_entry":      c.get("net_delta"),
            "net_theta_at_entry":      c.get("net_theta"),
            "net_gamma_at_entry":      c.get("net_gamma"),
            "net_vega_at_entry":       c.get("net_vega"),
            "breakeven_at_entry":      c.get("breakeven"),
            "hv30_at_entry":           c.get("hv30"),
            "iv_rank_at_entry":        c.get("iv_rank_52w"),
            # Full optimizer candidate preserved for auditability and ML feature recovery.
            # selected_candidate ⊇ rejected_candidate at the candidate JSON level.
            "optimizer_candidate":     c,
        }
        trades.append(trade)
        new.append(trade)
        _struct_counts[_struct_key] = _struct_counts.get(_struct_key, 0) + 1
        log.info(f"Paper trade #{rank}: {tid}  credit={ec:.3f}  expiry={c['expiry']}")

        # Write a training snapshot at entry so managed-exit outcomes feed POP model
        try:
            from scripts.training_data_collector import write_paper_trade_snapshot
            analyze_row = row_by_ticker.get(c["ticker"], {})
            write_paper_trade_snapshot(trade, analyze_row)
        except Exception as _snap_err:
            log.warning(f"Training snapshot write failed for {tid}: {_snap_err}")

    save_trades(trades)

    # Phase 2 observational: persist the scan decision (TRADE or NO_TRADE) to DuckDB.
    # This is the single source of truth for the full decision funnel — all future
    # Phase 2 analysis (evidence_n vs outcome, NO TRADE counterfactuals, repair lift,
    # selection quality) joins scan_decisions to training_snapshots via scan_decision_id.
    try:
        from scripts.training_data_collector import write_scan_decision as _wsd
        _meta = _decision_meta_out[0] if _decision_meta_out else {
            "status": "TRADE" if new else "NO_TRADE",
            "reason": None,
            "best_score": None, "required_score": None,
            "candidates_evaluated": len(rows),
            "candidates_survived_filter": None,
            "candidates_cleared_quality_gate": None,
            "portfolio_eligible": None,
            "market_regime": None,
            "decision_snapshot": [], "repair_summary": [],
        }
        _wsd(
            meta=_meta,
            scan_decision_id=scan_decision_id,
            scan_date=date.today().isoformat(),
            scan_time=scan_time,
            trade_ids=[t["id"] for t in new],
        )
    except Exception as _dec_err:
        log.warning(f"Scan decision write failed: {_dec_err}")

    # Write snapshots for ALL scanned tickers (not just top 3) so ML training
    # gets negative examples, scan-time features, and 100x more labeled rows.
    try:
        from scripts.training_data_collector import write_scan_all_snapshots
        opened_tickers = {t["ticker"] for t in new}
        snap_result = write_scan_all_snapshots(rows, scan_time, opened_tickers)
        log.info(f"Scan snapshots: {snap_result['saved']} saved, {snap_result['skipped']} skipped")
    except Exception as _snap_err:
        log.warning(f"Scan snapshot bulk write failed: {_snap_err}")

    # Write one training_snapshot per gate-surviving candidate (ticker × structure).
    # Complements write_scan_all_snapshots (which writes one row per ticker using only
    # the recommended candidate); this captures all structures that cleared every gate,
    # enabling SQL-based selection quality analysis across structure × outcome pairs.
    try:
        from scripts.training_data_collector import write_scan_candidate_snapshots
        opened_tickers_for_cand = {t["ticker"] for t in new}
        cand_result = write_scan_candidate_snapshots(
            _rank_items, scan_decision_id, scan_time, opened_tickers_for_cand
        )
        log.info(
            f"Candidate snapshots: {cand_result['saved']} saved, "
            f"{cand_result['skipped']} skipped, {len(cand_result['errors'])} errors"
        )
    except Exception as _cand_err:
        log.warning(f"Candidate snapshot write failed: {_cand_err}")

    return {"ok": True, "date": today_str, "recorded": len(new), "trades": new,
            "circuit_breaker": cb}


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

    from scripts.data_fetch import warmup_data_sources
    warmup_data_sources(log)

    trades        = load_trades()
    now           = datetime.now(EDT)
    today         = date.today()
    updated       = []
    newly_labeled = 0

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
            spread_val, pnl_ps = None, None
            if ul_price is not None:
                spread_val, pnl_ps = _expiry_pnl(trade, ul_price)

            if pnl_ps is None:
                # P&L unknown (path-dependent structure, missing strikes, or
                # price fetch failed).  Still mark closed so the trade doesn't
                # accumulate as a ghost position in portfolio risk checks.
                days_past = (today - exp_date).days
                if days_past >= 1:
                    trade["status"] = "expired_unknown"
                    trade["exit"] = {
                        "ts":            now.isoformat(),
                        "reason":        "expired_unknown",
                        "ul_price":      ul_price,
                        "pnl_per_share": None,
                        "pnl_total":     None,
                        "win":           None,
                    }
                    newly_labeled += 1
                    log.info(f"EXPIRED(unknown P&L) {trade['id']}: ul={ul_price}  structure={trade['structure']}")
                else:
                    log.warning(f"Cannot compute P&L for {trade['id']} on expiry day — will retry tonight")
                continue

            win = pnl_ps > 0
            trade["status"] = "expired_profit" if win else "expired_loss"
            _snaps = trade.get("snapshots") or []
            _unr   = [s["unrealized"] for s in _snaps if s.get("unrealized") is not None]
            _mae   = round(min(_unr) / ec * 100, 1) if _unr and ec else None
            _mfe   = round(max(_unr) / ec * 100, 1) if _unr and ec else None
            try:
                _days = (date.fromisoformat(now.isoformat()[:10])
                         - date.fromisoformat(trade["entered_at"][:10])).days
            except Exception:
                _days = None
            _max_profit_exp = (ec if trade.get("structure") == "Long Strangle"
                               else (trade.get("width", 0) - ec) if trade.get("structure") in
                               ("Call Debit Spread", "Put Debit Spread", "Calendar Spread", "Diagonal Spread")
                               else ec)
            _pnl_pct = round(pnl_ps / _max_profit_exp * 100, 1) if _max_profit_exp else None
            trade["exit"] = {
                "ts":             now.isoformat(),
                "reason":         "expired",
                "spread_val":     spread_val,
                "ul_price":       ul_price,
                "pnl_per_share":  pnl_ps,
                "pnl_total":      round(pnl_ps * 100, 2),
                "pnl_pct_of_max": _pnl_pct,
                "win":            win,
                "hit_tp":         win,
                "hit_sl":         not win,
                "mae_pct":        _mae,
                "mfe_pct":        _mfe,
                "days_held":      _days,
            }
            newly_labeled += 1
            log.info(f"EXPIRED {trade['id']}: ul={ul_price}  P&L=${trade['exit']['pnl_total']:.2f}  {'WIN' if win else 'LOSS'}")
            try:
                from scripts.offline_eval import update_trade_outcome as _uto
                _ret_pct = round(pnl_ps / ec * 100, 2) if ec else 0.0
                _uto(trade["id"], _ret_pct, "win" if win else "loss")
            except Exception as _e:
                log.debug(f"[eval] outcome record failed: {_e}")

        else:
            # ── Still open: snapshot, check managed-exit rules ────────────────
            mark = _current_mark(trade)
            if mark is None:
                continue
            is_debit   = trade.get("structure", "") in ("Call Debit Spread", "Put Debit Spread",
                                                          "Long Strangle", "Calendar Spread", "Diagonal Spread")
            # Clamp mark to spread width for defined-risk structures to guard against
            # bad quotes (e.g. short-leg bid=$0 inflating mark beyond the true max).
            _width = trade.get("width") or 0
            if is_debit and _width > 0:
                mark = min(mark, _width)
            # entry_credit always stores the actual entry price (debit paid or credit received),
            # so use it as entry_cost for both debit and credit structures.
            # (Old trades had max_loss swapped with max_profit for CDS/PDS, so don't use max_loss here.)
            entry_cost = ec
            unrealized = round(mark - entry_cost, 4) if is_debit else round(ec - mark, 4)
            # max_profit base: for debit spreads = width - debit; for Long Strangle use debit as proxy.
            # trade["width"] is always correct (stored as candidate max_profit + max_loss = spread width).
            _max_profit_base = (ec if trade.get("structure") == "Long Strangle"
                                else (trade.get("width", 0) - ec) if is_debit
                                else ec)
            pnl_pct_of_max = round(unrealized / _max_profit_base * 100, 1) if _max_profit_base else None

            _s = _load_settings()
            _stop_pct = -(_s["stop_loss_mult"] - 1) * 100   # e.g. -200% for mult=3.0

            def _exit_common(reason: str, hit_tp: bool, hit_sl: bool):
                _snaps = trade.get("snapshots") or []
                _unr   = [s["unrealized"] for s in _snaps if s.get("unrealized") is not None]
                _mae   = round(min(_unr) / ec * 100, 1) if _unr and ec else None
                _mfe   = round(max(_unr) / ec * 100, 1) if _unr and ec else None
                try:
                    _days = (date.fromisoformat(now.isoformat()[:10])
                             - date.fromisoformat(trade["entered_at"][:10])).days
                except Exception:
                    _days = None
                return {
                    "ts":             now.isoformat(),
                    "reason":         reason,
                    "spread_val":     mark,
                    "ul_price":       ul_price,
                    "pnl_per_share":  unrealized,
                    "pnl_total":      round(unrealized * 100, 2),
                    "pnl_pct_of_max": pnl_pct_of_max,
                    "win":            unrealized > 0,
                    "hit_tp":         hit_tp,
                    "hit_sl":         hit_sl,
                    "mae_pct":        _mae,
                    "mfe_pct":        _mfe,
                    "days_held":      _days,
                }

            if pnl_pct_of_max is not None and pnl_pct_of_max >= EARLY_CLOSE_PCT:
                # Max profit achieved before expiry
                trade["status"] = "closed_target"
                trade["exit"]   = _exit_common("max_profit", hit_tp=True, hit_sl=False)
                newly_labeled += 1
                log.info(f"CLOSED (max profit) {trade['id']}: mark={mark}  "
                         f"P&L=${trade['exit']['pnl_total']:.2f} ({pnl_pct_of_max}% of max)")
                try:
                    from scripts.offline_eval import update_trade_outcome as _uto
                    _uto(trade["id"], float(pnl_pct_of_max or 0), "win")
                except Exception as _e:
                    log.debug(f"[eval] outcome record failed: {_e}")

            elif pnl_pct_of_max is not None and pnl_pct_of_max <= _stop_pct:
                # Stop-loss skipped for paper trades — let positions run to expiry
                log.info(f"STOP-LOSS SKIPPED (paper trade) {trade['id']}: "
                         f"mark={mark}  pnl={pnl_pct_of_max}% (threshold {_stop_pct}%)")
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

                # Remark live Greeks at each evening snapshot so the morning
                # scan can use current (not entry-time) portfolio delta/theta/gamma.
                try:
                    from scripts.position_snapshots import (
                        paper_trade_to_position_shape,
                        compute_position_greeks,
                    )
                    _pos_shape = paper_trade_to_position_shape(trade, ul_price)
                    _live_g    = compute_position_greeks(_pos_shape)
                    if _live_g:
                        snap["net_delta"] = _live_g["net_delta"]
                        snap["net_theta"] = _live_g["net_theta"]
                        snap["net_gamma"] = _live_g["net_gamma"]
                        snap["net_vega"]  = _live_g["net_vega"]
                except Exception as _ge:
                    log.debug(f"Greeks remark skipped for {trade['id']}: {_ge}")

                trade["snapshots"].append(snap)

        updated.append(trade["id"])

    save_trades(trades)
    return {"ok": True, "updated": len(updated), "ids": updated, "newly_labeled": newly_labeled}


# ── Performance summary ───────────────────────────────────────────────────────

def get_performance_summary():
    trades = load_trades()
    closed = [t for t in trades if t.get("exit") is not None]
    open_  = [t for t in trades if t["status"] == "open"]

    def _stats(subset):
        if not subset:
            return {"count": 0}
        exits  = [t["exit"] for t in subset]
        # win is None for unlabelable outcomes (e.g. expired_unknown — path-dependent
        # structures like Diagonal/Calendar Spread whose payoff can't be computed from
        # intrinsic value alone). Exclude those from win/loss stats entirely rather than
        # letting `not None == True` silently count them as losses — and their
        # pnl_per_share is also None, which crashed sum() with a raw dict index
        # (unsupported operand type(s) for +: 'float' and 'NoneType'). Fixed 2026-08-24.
        wins   = [e for e in exits if e.get("win") is True]
        losses = [e for e in exits if e.get("win") is False]
        pnls   = [e["pnl_per_share"] for e in exits if e.get("pnl_per_share") is not None]
        totals = [e["pnl_total"]     for e in exits if e.get("pnl_total")     is not None]
        avg_w  = round(sum(e.get("pnl_per_share") or 0 for e in wins)   / len(wins),   4) if wins   else 0
        avg_l  = round(sum(e.get("pnl_per_share") or 0 for e in losses) / len(losses), 4) if losses else 0
        wr     = len(wins) / (len(wins) + len(losses)) if (len(wins) + len(losses)) else 0
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
                    mark = max(0.0, round(sh["mid"], 4))
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
                        (ps["mid"] - pl["mid"]) + (cs["mid"] - cl["mid"]), 4
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
                        mark = max(0.0, round(sh["mid"] - lo["mid"], 4))
                        result[tid] = {
                            "mark":       mark,
                            "unrealized": round(trade["entry_credit"] - mark, 4),
                            "legs":       {"short": sh, "long": lo},
                        }
                    else:
                        spread_val = max(0.0, round(lo["mid"] - sh["mid"], 4))
                        entry_debit = trade.get("entry_credit") or 0
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
                    mark = max(0.0, round(sh["mid"], 4))
                    mark_data = {"mark": mark, "unrealized": round(trade["entry_credit"] - mark, 4),
                                 "legs": {"short": sh}}

            elif st.strike_schema == StrikeSchema.IRON_CONDOR:
                ps = _fetch_leg(ticker, expiry, strikes.get("put_short"),  "put")
                pl = _fetch_leg(ticker, expiry, strikes.get("put_long"),   "put")
                cs = _fetch_leg(ticker, expiry, strikes.get("call_short"), "call")
                cl = _fetch_leg(ticker, expiry, strikes.get("call_long"),  "call")
                if all([ps, pl, cs, cl]):
                    mark = max(0.0, round((ps["mid"] - pl["mid"]) + (cs["mid"] - cl["mid"]), 4))
                    mark_data = {"mark": mark, "unrealized": round(trade["entry_credit"] - mark, 4),
                                 "legs": {"put_short": ps, "put_long": pl, "call_short": cs, "call_long": cl}}

            elif st.strike_schema == StrikeSchema.TWO_LEG:
                opt = st.option_type
                sh = _fetch_leg(ticker, expiry, strikes.get("short"), opt)
                lo = _fetch_leg(ticker, expiry, strikes.get("long"),  opt)
                if sh and lo:
                    if st.is_credit:
                        mark = max(0.0, round(sh["mid"] - lo["mid"], 4))
                        mark_data = {"mark": mark, "unrealized": round(trade["entry_credit"] - mark, 4),
                                     "legs": {"short": sh, "long": lo}}
                    else:
                        spread_val  = max(0.0, round(lo["mid"] - sh["mid"], 4))
                        entry_debit = trade.get("entry_credit") or 0
                        mark_data   = {"mark": spread_val, "unrealized": round(spread_val - entry_debit, 4),
                                       "debit_spread": True, "legs": {"long": lo, "short": sh}}

            if mark_data:
                mark_data["ul_price"] = fetch_underlying_price(ticker)
            yield tid, mark_data if mark_data else {"error": "No quote"}
        except Exception as e:
            log.warning(f"get_live_marks_iter failed for {tid}: {e}")
            yield tid, {"error": str(e)}
