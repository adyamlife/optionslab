"""
E*TRADE API client — OAuth 1.0a + REST endpoints.

Authentication flow (one-time per session, tokens expire at midnight ET):
  1. Call get_request_token()  → get authorize_url
  2. Open authorize_url in browser, log in, copy the verifier code
  3. Call get_access_token(verifier) → stores access token in _token_cache
  4. All subsequent calls use the cached access token automatically

Data endpoints (all return None on any failure so callers can fall back to yfinance):
  - get_quote(symbol)           → dict with last, bid, ask, volume, iv (annualized)
  - get_option_chain(symbol, expiry_str)  → (calls_df, puts_df) like yfinance .option_chain
  - get_account_balance()       → {"buying_power": float, "net_value": float, "account_id": str}

Config is read from config/secrets.toml [etrade] section.
"""

import os, sys, json, time, logging
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("etrade")

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------
try:
    from requests_oauthlib import OAuth1Session
except ImportError:
    OAuth1Session = None  # handled gracefully below

import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent

def _load_cfg():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # pip install tomli for Python <3.11
    p = _ROOT / "config" / "secrets.toml"
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f).get("etrade", {})

_cfg = _load_cfg()

_CONSUMER_KEY    = _cfg.get("consumer_key", "")
_CONSUMER_SECRET = _cfg.get("consumer_secret", "")
_SANDBOX         = _cfg.get("use_sandbox", True)


def _load_ds_cfg() -> dict:
    """Load [data_sources] section from secrets.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    p = _ROOT / "config" / "secrets.toml"
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f).get("data_sources", {})


def ds_pref(key: str) -> str:
    """
    Return the configured preference for a data-source category.

    key: "quotes" | "option_chain" | "expirations" | "market_context"
    Returns: "etrade" | "yfinance" | "auto"

    "auto"     → use E*TRADE if authenticated, else yfinance
    "etrade"   → always try E*TRADE (error/None if not authenticated)
    "yfinance" → always use yfinance, skip E*TRADE entirely
    """
    ds = _load_ds_cfg()
    return ds.get(key, "auto").strip().lower()

_BASE_URL   = "https://apisb.etrade.com" if _SANDBOX else "https://api.etrade.com"
_AUTH_URL   = "https://us.etrade.com/e/t/etws/authorize"

# Set when any API call receives a 401 — prevents retrying expired token for
# the rest of this process lifetime.  Cleared on successful get_access_token().
_session_invalidated: bool = False


def _handle_401():
    """Called on first 401. Clears token file, sets flag, logs once."""
    global _session_invalidated
    if not _session_invalidated:
        _session_invalidated = True
        _clear_token()
        _log.warning(
            "[etrade] 401 Unauthorized — session expired. "
            "Re-authenticate via the E*TRADE Connect button in the UI. "
            "All market data will fall back to yfinance until then."
        )

# yfinance ticker → E*TRADE equivalent
_YF_TO_ET: dict[str, str] = {
    "^VIX":  "$VIX",
    "^IRX":  "$IRX",
    "^GSPC": "$SPX",
    "ES=F":  "ES:GLOBEX",
    "NQ=F":  "NQ:GLOBEX",
    "YM=F":  "YM:CBOT",
    "RTY=F": "RTY:GLOBEX",
}

# Tickers that E*TRADE rejects for options endpoints — always use yfinance.
_ET_OPTIONS_UNSUPPORTED: frozenset[str] = frozenset({"BRK-B", "BRK/B"})
_ET_TO_YF: dict[str, str] = {v: k for k, v in _YF_TO_ET.items()}


def et_symbol(yf_ticker: str) -> str:
    """Translate a yfinance ticker to its E*TRADE equivalent."""
    return _YF_TO_ET.get(yf_ticker, yf_ticker)

# Token persistence — survive Flask reloads during the same session
_TOKEN_FILE = _ROOT / "data" / ".etrade_token.json"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_configured() -> bool:
    return bool(_CONSUMER_KEY and _CONSUMER_SECRET and OAuth1Session is not None)


def _save_token(token: dict):
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_TOKEN_FILE, "w") as f:
        json.dump({**token, "_saved_at": time.time()}, f)


def _load_token() -> dict | None:
    if not _TOKEN_FILE.exists():
        return None
    try:
        with open(_TOKEN_FILE) as f:
            t = json.load(f)
        # E*TRADE access tokens expire at midnight ET (≈ 28 800 s max safe window)
        age = time.time() - t.get("_saved_at", 0)
        if age > 28000:
            return None
        return t
    except Exception:
        return None


def _clear_token():
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()


def _session(token=None, token_secret=None) -> "OAuth1Session":
    return OAuth1Session(
        _CONSUMER_KEY,
        client_secret=_CONSUMER_SECRET,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
    )


def _authed_session():
    """Return an OAuth1Session pre-loaded with the cached access token, or None."""
    if _session_invalidated:
        return None
    t = _load_token()
    if not t:
        return None
    return _session(t["oauth_token"], t["oauth_token_secret"])


# ---------------------------------------------------------------------------
# Public OAuth helpers  (called from Flask routes)
# ---------------------------------------------------------------------------

def get_request_token() -> dict:
    """
    Step 1: Fetch a request token and return the URL the user must visit.
    Returns {"authorize_url": str, "oauth_token": str, "oauth_token_secret": str}
    Raises RuntimeError if not configured.
    """
    if not _is_configured():
        raise RuntimeError(
            "E*TRADE not configured. Add [etrade] keys to config/secrets.toml "
            "and install requests-oauthlib: pip install requests-oauthlib"
        )
    sess = OAuth1Session(
        _CONSUMER_KEY,
        client_secret=_CONSUMER_SECRET,
        callback_uri="oob",   # out-of-band: E*TRADE shows verifier code on screen
    )
    resp = sess.fetch_request_token(f"{_BASE_URL}/oauth/request_token")
    oauth_token        = resp["oauth_token"]
    oauth_token_secret = resp["oauth_token_secret"]

    authorize_url = f"{_AUTH_URL}?key={_CONSUMER_KEY}&token={oauth_token}"
    return {
        "authorize_url":      authorize_url,
        "oauth_token":        oauth_token,
        "oauth_token_secret": oauth_token_secret,
    }


def get_access_token(oauth_token: str, oauth_token_secret: str, verifier: str) -> dict:
    """
    Step 3: Exchange request token + verifier for an access token.
    Persists the token to disk and returns it.
    """
    global _session_invalidated
    sess = _session(oauth_token, oauth_token_secret)
    sess._client.client.verifier = verifier
    resp = sess.fetch_access_token(f"{_BASE_URL}/oauth/access_token")
    _save_token(resp)
    _session_invalidated = False  # fresh token — re-enable E*TRADE calls
    return resp


def is_authenticated() -> bool:
    """True if a valid (non-expired) access token is cached."""
    return _authed_session() is not None


def logout():
    """Revoke the cached token (next request will require re-auth)."""
    _clear_token()


# ---------------------------------------------------------------------------
# Market data helpers
# ---------------------------------------------------------------------------

def get_quote(symbol: str) -> dict | None:
    """
    Returns a dict with keys: last, bid, ask, volume, change_pct
    Returns None on any error (caller falls back to yfinance).
    """
    sess = _authed_session()
    if not sess:
        return None
    try:
        url = f"{_BASE_URL}/v1/market/quote/{symbol.upper()}.json"
        r = sess.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        q    = data["QuoteResponse"]["QuoteData"][0]["All"]
        last = q.get("lastTrade") or q.get("close")
        prev = q.get("previousClose") or q.get("previousDayClose")
        chg_pct = q.get("changeClosePercentage")
        if chg_pct is None and last and prev and float(prev) != 0:
            chg_pct = round((float(last) - float(prev)) / float(prev) * 100, 2)
        return {
            "last":       float(last) if last else None,
            "bid":        q.get("bid"),
            "ask":        q.get("ask"),
            "volume":     q.get("totalVolume"),
            "change_pct": round(float(chg_pct), 2) if chg_pct is not None else None,
            "iv":         q.get("iv"),
        }
    except Exception:
        return None


def get_quotes(yf_symbols: list[str]) -> dict[str, dict] | None:
    """
    Batch quote for multiple symbols in one HTTP call.
    Accepts yfinance-style tickers (^VIX, ES=F, XLK …) — translates internally.
    Returns {yf_symbol: {last, change_pct}} or None on failure.
    """
    sess = _authed_session()
    if not sess:
        return None
    try:
        et_syms   = [et_symbol(s) for s in yf_symbols]
        sym_str   = ",".join(et_syms)
        url       = f"{_BASE_URL}/v1/market/quote/{sym_str}.json"
        r         = sess.get(url, timeout=15)
        r.raise_for_status()
        data      = r.json()
        results   = {}
        for item in data["QuoteResponse"]["QuoteData"]:
            q        = item.get("All", {})
            et_sym   = item.get("Product", {}).get("symbol", "")
            yf_sym   = _ET_TO_YF.get(et_sym, et_sym)   # map back to yfinance key
            last     = q.get("lastTrade") or q.get("close")
            prev     = q.get("previousClose") or q.get("previousDayClose")
            chg_pct  = q.get("changeClosePercentage")
            if chg_pct is None and last and prev and float(prev) != 0:
                chg_pct = round((float(last) - float(prev)) / float(prev) * 100, 2)
            if last:
                last_f = float(last)
                prev_f = float(prev) if prev else None
                chg    = round(last_f - prev_f, 4) if prev_f else None
                results[yf_sym] = {
                    "last":       last_f,
                    "change":     chg,
                    "change_pct": round(float(chg_pct), 2) if chg_pct is not None else None,
                }
        return results or None
    except Exception as exc:
        if "401" in str(exc):
            _handle_401()
        else:
            _log.warning("[etrade] batch quote error: %s", exc)
        return None


def get_option_expirations(symbol: str) -> list[str] | None:
    """
    Return available option expiry dates as ['YYYY-MM-DD', …] sorted ascending.
    Returns None on any failure so caller falls back to yfinance.
    """
    if symbol.upper() in _ET_OPTIONS_UNSUPPORTED:
        return None
    sess = _authed_session()
    if not sess:
        return None
    try:
        url  = f"{_BASE_URL}/v1/market/optionexpiredate.json"
        r    = sess.get(url, params={"symbol": symbol.upper()}, timeout=10)
        r.raise_for_status()
        dates = r.json()["OptionExpireDateResponse"]["ExpirationDate"]
        result = []
        for d in dates:
            y, m, day = str(d.get("year","")), str(d.get("month","")).zfill(2), str(d.get("day","")).zfill(2)
            if y and m and day:
                result.append(f"{y}-{m}-{day}")
        return sorted(result) if result else None
    except Exception as exc:
        if "401" in str(exc):
            _handle_401()
        else:
            _log.warning("[etrade] option expirations error for %s: %s", symbol, exc)
        return None


def get_option_chain(symbol: str, expiry_str: str):
    """
    Fetch an option chain from E*TRADE and return (calls_df, puts_df) with
    the same columns that yfinance returns, so data_fetch.py can use either.

    expiry_str: "YYYY-MM-DD"
    Returns (None, None) on any error.
    """
    if symbol.upper() in _ET_OPTIONS_UNSUPPORTED:
        return None, None
    sess = _authed_session()
    if not sess:
        return None, None
    try:
        year, month, day = expiry_str.split("-")
        url = f"{_BASE_URL}/v1/market/optionchains.json"
        params = {
            "symbol":           symbol.upper(),
            "expiryYear":       year,
            "expiryMonth":      month,
            "expiryDay":        day,
            "optionCategory":   "STANDARD",
            "chainType":        "CALLPUT",
            "skipAdjusted":     "true",
            "noOfStrikes":      40,
        }
        r = sess.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        pairs = data["OptionChainResponse"]["OptionPair"]
        calls, puts = [], []

        for pair in pairs:
            for side, bucket in (("Call", calls), ("Put", puts)):
                leg = pair.get(side)
                if not leg:
                    continue
                # E*TRADE nests greeks under OptionGreeks; IV is already a decimal
                greeks = leg.get("OptionGreeks") or {}
                bucket.append({
                    "contractSymbol":    leg.get("optionSymbol", ""),
                    "strike":            float(leg.get("strikePrice", 0)),
                    "lastPrice":         float(leg.get("lastPrice") or 0),
                    "bid":               float(leg.get("bid") or 0),
                    "ask":               float(leg.get("ask") or 0),
                    "volume":            int(leg.get("volume") or 0),
                    "openInterest":      int(leg.get("openInterest") or 0),
                    "impliedVolatility": float(greeks.get("iv") or greeks.get("impliedVolatility") or 0),
                    "delta":             float(greeks.get("delta") or 0),
                    "gamma":             float(greeks.get("gamma") or 0),
                    "theta":             float(greeks.get("theta") or 0),
                    "vega":              float(greeks.get("vega") or 0),
                    "inTheMoney":        bool(leg.get("inTheMoney")),
                })

        # Keep strike as a regular column (matches yfinance option_chain format)
        calls_df = pd.DataFrame(calls) if calls else pd.DataFrame()
        puts_df  = pd.DataFrame(puts)  if puts  else pd.DataFrame()
        return calls_df, puts_df

    except Exception as exc:
        if "401" in str(exc):
            _handle_401()
        else:
            _log.warning("[etrade] option chain error for %s: %s", symbol, exc)
        return None, None


# ---------------------------------------------------------------------------
# Account data
# ---------------------------------------------------------------------------

def get_account_balance() -> dict | None:
    """
    Returns {"buying_power": float, "net_value": float, "account_id": str}
    or None if not authenticated or request fails.
    """
    sess = _authed_session()
    if not sess:
        return None
    try:
        # List accounts first to get the accountIdKey
        r = sess.get(f"{_BASE_URL}/v1/accounts/list.json", timeout=10)
        r.raise_for_status()
        accounts = r.json()["AccountListResponse"]["Accounts"]["Account"]
        if not accounts:
            return None
        acct = accounts[0]
        key  = acct["accountIdKey"]

        r2 = sess.get(f"{_BASE_URL}/v1/accounts/{key}/balance.json",
                      params={"instType": "BROKERAGE", "realTimeNAV": "true"},
                      timeout=10)
        r2.raise_for_status()
        bal = r2.json()["BalanceResponse"]
        cp  = bal.get("Computed", {})
        return {
            "account_id":   acct.get("accountId", key),
            "account_name": acct.get("accountDesc", "E*TRADE Account"),
            "buying_power": float(cp.get("cashBuyingPower") or cp.get("marginBuyingPower") or 0),
            "net_value":    float(cp.get("RealTimeValues", {}).get("totalAccountValue") or
                                  cp.get("netMv") or 0),
            "cash":         float(cp.get("cashAvailableForInvestment") or 0),
        }
    except Exception as exc:
        if "401" in str(exc):
            _handle_401()
        else:
            _log.warning("[etrade] balance error: %s", exc)
        return None


def get_positions() -> list[dict] | None:
    """
    Returns all open positions from the E*TRADE account with full option details.

    Each dict includes:
      symbol, description, qty (neg = short), cost_per_share, market_value,
      pnl, day_gain, last_price, change, change_pct,
      # option-specific (None for equities):
      underlying, security_type ("OPTN"/"EQ"), call_put, strike,
      expiry_year, expiry_month, expiry_day, expiry  ("YYYY-MM-DD")
    """
    sess = _authed_session()
    if not sess:
        return None
    try:
        r = sess.get(f"{_BASE_URL}/v1/accounts/list.json", timeout=10)
        r.raise_for_status()
        accounts = r.json()["AccountListResponse"]["Accounts"]["Account"]
        if not accounts:
            return []
        key = accounts[0]["accountIdKey"]

        r2 = sess.get(
            f"{_BASE_URL}/v1/accounts/{key}/portfolio.json",
            params={"view": "COMPLETE"},
            timeout=15,
        )
        r2.raise_for_status()
        portfolios = r2.json()["PortfolioResponse"]["AccountPortfolio"]
        positions: list[dict] = []
        for pf in portfolios:
            for pos in pf.get("Position", []):
                prod    = pos.get("Product", {})
                quick   = pos.get("Quick", {})
                sec_type = prod.get("securityType", "EQ")
                qty      = float(pos.get("quantity", 0))

                expiry_yr  = prod.get("expiryYear")
                expiry_mo  = prod.get("expiryMonth")
                expiry_day = prod.get("expiryDay")
                expiry_iso = None
                if expiry_yr and expiry_mo and expiry_day:
                    expiry_iso = f"{expiry_yr:04d}-{expiry_mo:02d}-{expiry_day:02d}"

                # dateAcquired is a Unix timestamp in milliseconds; convert to ISO date string
                date_acquired_ms = pos.get("dateAcquired")
                date_acquired_iso = None
                if date_acquired_ms:
                    try:
                        date_acquired_iso = datetime.fromtimestamp(
                            int(date_acquired_ms) / 1000, tz=timezone.utc
                        ).strftime("%Y-%m-%d")
                    except Exception:
                        pass

                positions.append({
                    "symbol":        pos.get("symbolDescription", ""),
                    "underlying":    prod.get("symbol", ""),
                    "security_type": sec_type,
                    "call_put":      prod.get("callPut", ""),        # "CALL" or "PUT"
                    "strike":        prod.get("strikePrice"),
                    "expiry_year":   expiry_yr,
                    "expiry_month":  expiry_mo,
                    "expiry_day":    expiry_day,
                    "expiry":        expiry_iso,
                    "qty":           qty,
                    "cost_per_share": float(pos.get("costPerShare") or 0),
                    "market_value":  float(pos.get("marketValue") or 0),
                    "pnl":           float(pos.get("totalGain") or 0),
                    "day_gain":      float(pos.get("daysGain") or 0),
                    "last_price":    float(quick.get("lastTrade") or 0),
                    "change":        float(quick.get("change") or 0),
                    "change_pct":    float(quick.get("changePct") or 0),
                    "date_acquired": date_acquired_iso,   # "YYYY-MM-DD" or None
                })
        return positions
    except Exception as exc:
        if "401" in str(exc):
            _handle_401()
        else:
            _log.warning("[etrade] positions error: %s", exc)
        return None
