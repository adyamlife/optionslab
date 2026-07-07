"""
Position Snapshot Tracking (generalized — Live Positions AND Paper Trades)

Persists a "first seen" Greeks/IV snapshot for each held position (from
either Live Positions or Paper Trades), so the UI can show drift since entry
(IV crush/expansion, delta/theta/gamma/vega movement). /api/analyze can't
provide this on its own — it only prices the rulebook's newly-suggested
structure, not the user's exact held strikes — so this module prices the
position's ACTUAL legs directly via the same Black-Scholes functions and
risk-free rate analyze.py uses, for consistency.

Callers must adapt their own shape into the canonical position dict this
module expects:
    {
      "id":         optional stable id (Paper Trades has one; prefer it over
                    the composite key so re-fetches always resolve to the
                    same snapshot even if other fields are re-ordered)
      "ticker":     str
      "structure":  str
      "expiry":     "YYYY-MM-DD"
      "ul_price":   float
      "is_credit":  bool | None
      "opt_type":   "put" | "call" | None   (single-leg only)
      "qty":        number | None            (single-leg only; sign = side)
      "strike":     float | None              (single-leg)
      "strike_hi":  float | None              (2-strike spread)
      "strike_lo":  float | None              (2-strike spread)
      "put_short", "put_long", "call_short", "call_long": float | None (Iron Condor / Jade Lizard)
    }
See paper_trade_to_position_shape() below for the Paper Trades adapter;
Live Positions' own spread objects already match this shape natively.
"""
import json
from datetime import date, datetime
from pathlib import Path

_TRAINING_FILE = Path(__file__).resolve().parent.parent / "data" / "training_snapshots.jsonl"
_CHAIN_FILE     = Path(__file__).resolve().parent.parent / "data" / "option_chain_snapshots.jsonl"

_training_iv_cache: dict | None = None    # {ticker: {date_str: atm_iv_pct}}
_chain_greeks_cache: dict | None = None   # {ticker: {date_str: {(strike,opt_type): {delta,gamma,theta,vega,iv}}}}


def _get_historical_atm_iv(ticker: str, date_str: str) -> float | None:
    """
    Look up ATM IV (as a percentage) from training snapshots for ticker on a given date.
    Returns None if no snapshot exists for that ticker/date combination.
    """
    global _training_iv_cache
    if _training_iv_cache is None:
        _training_iv_cache = {}
        if _TRAINING_FILE.exists():
            try:
                with _TRAINING_FILE.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                            t = r.get("ticker", "")
                            ca = (r.get("collected_at") or "")[:10]
                            iv = r.get("atm_iv")
                            if t and ca and iv is not None:
                                _training_iv_cache.setdefault(t, {})
                                if ca not in _training_iv_cache[t]:
                                    _training_iv_cache[t][ca] = round(float(iv) * 100, 1)
                        except Exception:
                            pass
            except Exception:
                pass
    return (_training_iv_cache.get(ticker) or {}).get(date_str)


def _get_historical_leg_greeks(ticker: str, date_str: str, strike: float, opt_type: str) -> dict | None:
    """
    Look up per-leg Greeks from option chain snapshots for the given ticker/date/strike/type.
    Returns {iv, delta, gamma, theta, vega} or None if not found.
    iv is a decimal fraction (not percentage) to match compute_position_greeks output convention.
    """
    global _chain_greeks_cache
    if _chain_greeks_cache is None:
        _chain_greeks_cache = {}
        if _CHAIN_FILE.exists():
            try:
                with _CHAIN_FILE.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                            t = r.get("ticker", "")
                            ca = (r.get("collected_at") or "")[:10]
                            if not t or not ca:
                                continue
                            _chain_greeks_cache.setdefault(t, {}).setdefault(ca, {})
                            for s in r.get("strikes") or []:
                                key = (round(float(s["strike"]), 2), s["opt_type"])
                                # Keep earliest snapshot of the day; only store if Greeks present
                                if key not in _chain_greeks_cache[t][ca]:
                                    _chain_greeks_cache[t][ca][key] = {
                                        "iv":    float(s.get("iv") or 0),
                                        "delta": s.get("delta"),
                                        "gamma": s.get("gamma"),
                                        "theta": s.get("theta"),
                                        "vega":  s.get("vega"),
                                    }
                        except Exception:
                            pass
            except Exception:
                pass
    key = (round(float(strike), 2), opt_type)
    entry = ((_chain_greeks_cache.get(ticker) or {}).get(date_str) or {}).get(key)
    return entry if entry else None

from scripts.black_scholes import delta as bs_delta, theta as bs_theta, gamma as bs_gamma, vega as bs_vega
from scripts.data_fetch import get_risk_free_rate
from scripts.paper_trade_engine import _fetch_leg

_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_FILE = _ROOT / "data" / "position_snapshots.json"

RISK_FREE_RATE = get_risk_free_rate()


def _load():
    if not _SNAPSHOT_FILE.exists():
        return {}
    try:
        return json.loads(_SNAPSHOT_FILE.read_text())
    except Exception:
        return {}


def _save(store):
    _SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT_FILE.write_text(json.dumps(store, indent=2))


def paper_trade_to_position_shape(trade: dict, ul_price: float) -> dict:
    """Adapt a Paper Trades trade dict into the canonical position shape."""
    strikes = trade.get("strikes", {}) or {}
    structure = trade.get("structure", "")
    position = {
        "id":        trade.get("id"),
        "ticker":    trade.get("ticker"),
        "structure": structure,
        "expiry":    trade.get("expiry"),
        "ul_price":  ul_price,
        "is_credit": "Credit" in structure,
    }
    if any(k in strikes for k in ("put_long", "put_short", "call_short", "call_long")):
        position.update({
            "put_long":   strikes.get("put_long"),
            "put_short":  strikes.get("put_short"),
            "call_short": strikes.get("call_short"),
            "call_long":  strikes.get("call_long"),
        })
    elif "long" in strikes and strikes.get("long") is not None:
        position["strike_hi"] = max(strikes["short"], strikes["long"])
        position["strike_lo"] = min(strikes["short"], strikes["long"])
    elif "short" in strikes and strikes.get("short") is not None:
        # Single-leg short structure (Cash Secured Put / Covered Call)
        position["strike"]   = strikes["short"]
        position["qty"]      = -1
        position["opt_type"] = "put" if "Put" in structure else "call"
    return position


def make_position_key(position: dict) -> str:
    """
    Stable identity for a held position. Prefers an explicit id (e.g. Paper
    Trades' trade id) when present; falls back to a composite key for
    sources with no stable id (Live Positions, re-fetched fresh each load).
    """
    if position.get("id"):
        return f"id:{position['id']}"

    parts = [
        position.get("ticker", ""),
        position.get("structure", ""),
        str(position.get("expiry", "")),
        str(position.get("strike_hi", "")),
        str(position.get("strike_lo", "")),
        str(position.get("put_short", "")),
        str(position.get("put_long", "")),
        str(position.get("call_short", "")),
        str(position.get("call_long", "")),
        str(position.get("strike", "")),
    ]
    return "|".join(parts)


def _position_legs(position: dict) -> list[tuple]:
    """
    Return [(strike, opt_type, side), ...] for a position dict.
    side = +1 (long) or -1 (short). Returns [] if geometry is incomplete.
    Mirrors the leg-building logic in compute_position_greeks.
    """
    legs = []
    if position.get("put_short") is not None:
        legs.append((position["put_short"], "put", -1))
    if position.get("put_long") is not None:
        legs.append((position["put_long"], "put", +1))
    if position.get("call_short") is not None:
        legs.append((position["call_short"], "call", -1))
    if position.get("call_long") is not None:
        legs.append((position["call_long"], "call", +1))

    if not legs:
        opt_type = (position.get("opt_type") or "call").lower()
        if position.get("strike") is not None:
            qty = position.get("qty") or 1
            legs.append((position["strike"], opt_type, 1 if qty > 0 else -1))
        else:
            hi, lo = position.get("strike_hi"), position.get("strike_lo")
            if hi is None or lo is None:
                return []
            is_credit = position.get("is_credit")
            near, far = (hi, lo) if opt_type == "put" else (lo, hi)
            if is_credit:
                legs.append((near, opt_type, -1))
                legs.append((far,  opt_type, +1))
            else:
                legs.append((near, opt_type, +1))
                legs.append((far,  opt_type, -1))
    return legs


def compute_position_greeks(position: dict):
    """
    Price the position's EXACT held legs (not the rulebook's newly-suggested
    structure) and sum per-leg Greeks, weighted by long(+1)/short(-1).
    Returns {iv, net_delta, net_theta, net_gamma, net_vega} or None if any
    required leg can't be priced.
    """
    ticker = position.get("ticker")
    expiry = position.get("expiry")
    ul     = position.get("ul_price")
    if not ticker or not expiry or ul is None:
        return None

    try:
        exp_date = date.fromisoformat(str(expiry)[:10])
        T = max((exp_date - date.today()).days, 0) / 365.0
    except Exception:
        return None
    if T <= 0:
        return None

    # Each leg: (strike, option_type, side) where side = +1 long, -1 short
    legs = []
    if position.get("put_short") is not None:
        legs.append((position["put_short"], "put", -1))
    if position.get("put_long") is not None:
        legs.append((position["put_long"], "put", +1))
    if position.get("call_short") is not None:
        legs.append((position["call_short"], "call", -1))
    if position.get("call_long") is not None:
        legs.append((position["call_long"], "call", +1))

    if not legs:
        opt_type = (position.get("opt_type") or "call").lower()
        if position.get("strike") is not None:
            qty = position.get("qty") or 1
            legs.append((position["strike"], opt_type, 1 if qty > 0 else -1))
        else:
            hi, lo = position.get("strike_hi"), position.get("strike_lo")
            if hi is None or lo is None:
                return None
            is_credit = position.get("is_credit")
            # Put spreads: the higher strike is nearer the money. Call spreads:
            # the lower strike is nearer the money. Credit spreads sell the
            # near strike and buy the far one; debit spreads do the reverse.
            near, far = (hi, lo) if opt_type == "put" else (lo, hi)
            if is_credit:
                legs.append((near, opt_type, -1))
                legs.append((far, opt_type, +1))
            else:
                legs.append((near, opt_type, +1))
                legs.append((far, opt_type, -1))

    net_delta = net_theta = net_gamma = net_vega = 0.0
    ivs = []
    for strike, opt_type, side in legs:
        leg = _fetch_leg(ticker, expiry, strike, opt_type)
        if not leg:
            return None
        sigma = leg.get("iv_raw") or 0
        if sigma <= 0:
            # Market closed / stale data — yfinance returns 0 IV but a valid lastPrice.
            # Estimate IV from lastPrice via a simple ATM approximation so the card
            # still renders after hours (rough but better than blank).
            mid = leg.get("mid") or leg.get("ask") or 0
            if mid <= 0 or T <= 0:
                return None
            import math
            sigma = max(mid / (ul * math.sqrt(T / (2 * math.pi))), 0.01)
        ivs.append(sigma * 100)
        net_delta += side * bs_delta(ul, strike, T, RISK_FREE_RATE, sigma, opt_type)
        net_theta += side * bs_theta(ul, strike, T, RISK_FREE_RATE, sigma, opt_type)
        net_gamma += side * bs_gamma(ul, strike, T, RISK_FREE_RATE, sigma)
        net_vega  += side * bs_vega(ul, strike, T, RISK_FREE_RATE, sigma)

    return {
        "iv":        round(sum(ivs) / len(ivs), 1) if ivs else None,
        "net_delta": round(net_delta, 4),
        "net_theta": round(net_theta, 4),
        "net_gamma": round(net_gamma, 6),
        "net_vega":  round(net_vega, 4),
        "ul_price":  ul,
    }


def get_entry_snapshot_and_drift(position: dict):
    """
    Look up (or create, on first sighting) the entry snapshot for this
    position, price it now, and return entry + current + the drift between
    them. Returns None if the position's legs can't currently be priced.
    """
    current = compute_position_greeks(position)
    if current is None:
        return None

    key   = make_position_key(position)
    store = _load()
    entry = store.get(key)

    if entry is None:
        date_acquired = position.get("date_acquired")
        entry = {
            **current,
            "ts": datetime.now().isoformat(),
            "date_acquired": date_acquired,
        }
        if date_acquired:
            ticker = position.get("ticker", "")
            # Try per-leg Greeks from chain snapshots first (E*TRADE source has full Greeks).
            # Reconstruct the net Greeks the same way compute_position_greeks does,
            # but from stored historical values instead of live re-pricing.
            legs = _position_legs(position)
            if legs:
                net_delta = net_theta = net_gamma = net_vega = 0.0
                ivs = []
                all_found = True
                for strike, opt_type, side in legs:
                    g = _get_historical_leg_greeks(ticker, date_acquired, strike, opt_type)
                    if g is None:
                        all_found = False
                        break
                    if g.get("iv"):
                        ivs.append(g["iv"] * 100)  # store as pct in entry snapshot
                    if g.get("delta") is not None:
                        net_delta += side * g["delta"]
                    if g.get("gamma") is not None:
                        net_gamma += side * g["gamma"]
                    if g.get("theta") is not None:
                        net_theta += side * g["theta"]
                    if g.get("vega") is not None:
                        net_vega  += side * g["vega"]
                if all_found and ivs:
                    entry["iv"]        = round(sum(ivs) / len(ivs), 1)
                    entry["net_delta"] = round(net_delta, 4)
                    entry["net_theta"] = round(net_theta, 4)
                    entry["net_gamma"] = round(net_gamma, 6)
                    entry["net_vega"]  = round(net_vega, 4)
                elif not all_found:
                    # Chain greeks unavailable (yfinance source or strike not stored);
                    # fall back to ATM IV from training snapshots for IV at minimum.
                    hist_iv = _get_historical_atm_iv(ticker, date_acquired)
                    if hist_iv is not None:
                        entry["iv"] = hist_iv
            else:
                # No leg geometry (shouldn't happen), fall back to ATM IV only
                hist_iv = _get_historical_atm_iv(ticker, date_acquired)
                if hist_iv is not None:
                    entry["iv"] = hist_iv
        store[key] = entry
        _save(store)

    def _diff(field, digits):
        a, b = current.get(field), entry.get(field)
        return round(a - b, digits) if a is not None and b is not None else None

    drift = {
        "iv":        _diff("iv", 1),
        "net_delta": _diff("net_delta", 4),
        "net_theta": _diff("net_theta", 4),
        "net_gamma": _diff("net_gamma", 6),
        "net_vega":  _diff("net_vega", 4),
    }

    return {"entry": entry, "current": current, "drift": drift}
