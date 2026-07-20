"""
Intraday Entry Timing Filter — rule-based gate applied to morning scan candidates.

Fetches live SPY bars and computes three fast signals:
  1. SPY intraday RSI (Wilder) — if overbought (>65) on a short-vol candidate,
                         the market momentum contradicts the trade. If oversold
                         (<35) on a long-vol candidate, buying premium into an
                         exhausted sell-off carries adverse fill risk.
  2. VIX term slope    — if VIX > VIX3M by more than _VIX_BACKWARDATION_THRESH
                         (default 5%), short-term fear exceeds medium-term;
                         short-vol credit structures face elevated tail risk.
  3. Opening range vs daily ATR — if SPY is already outside its first-30-min
                         opening range by more than _OR_BREAK_ATR × ATR(14d),
                         the session is trending; IC/credit-spread entries
                         into a moving market have poor fill quality.

ATR methodology
---------------
ATR uses 14 daily true ranges (Wilder smoothing), giving a session-stable
denominator. Each true range captures overnight gaps via
max(H-L, |H-PrevClose|, |PrevClose-L|). Today's intraday bars define the
opening range; the ATR denominator comes from daily bars so it doesn't shift
as the intraday session accumulates more bars.

Fail-open behaviour
-------------------
On data fetch failures this module returns pass=True and reason="<code>"
with a separate "detail" key carrying the raw exception. Set _FAIL_OPEN=False
to fail closed (block trades when data is unavailable).

Returns:
  {
    "pass":    bool,          # True = proceed with entry
    "reason":  str,           # clean reason code ("ok", "vix_backwardation", ...)
    "detail":  str,           # human-readable elaboration (empty string when ok)
    "signals": {              # raw signal values for logging / display
        "spy_rsi":            float,
        "vix_backwardation":  bool,
        "vix_bp_ratio":       float,   # (VIX - VIX3M) / VIX3M
        "spy_outside_or":     bool,
        "spy_or_break_atr":   float,   # multiples of 14d ATR outside opening range
        "atr14":              float,   # daily Wilder ATR(14) used as denominator
        "vix":                float,
        "vix_3m":             float,
    }
  }

Usage:
  from scripts.intraday_filter import check
  result = check(structure="Iron Condor")
  if not result["pass"]:
      print("Skip —", result["reason"], result["detail"])

Standalone:
  python -m scripts.intraday_filter
  python -m scripts.intraday_filter --structure "Long Call"
"""
import argparse
import logging
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")

# Configurable thresholds
_RSI_OVERBOUGHT           = 65.0
_RSI_OVERSOLD             = 35.0
_ATR_PERIOD               = 14    # daily Wilder ATR period
_OR_BREAK_ATR             = 0.5   # fraction of daily ATR14 above which OR-break triggers
_VIX_BACKWARDATION_THRESH = 0.05  # (VIX - VIX3M)/VIX3M must exceed 5% to trigger
_FAIL_OPEN                = True  # True = pass=True on data failures; False = fail closed


# ── Indicators ────────────────────────────────────────────────────────────────

def _rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    """
    Wilder's smoothed RSI — matches TradingView / Thinkorswim values.

    Seeds with the simple average of the first `period` changes, then
    applies exponential smoothing: avg = (avg*(period-1) + current) / period.
    Simple-average RSI (sum/N) gives different values, especially on short
    series — Wilder's is the convention for broker chart matching.
    """
    if len(closes) < period + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Seed: simple average of first `period` changes
    avg_g = sum(max(c, 0) for c in changes[:period]) / period
    avg_l = sum(max(-c, 0) for c in changes[:period]) / period

    # Wilder's exponential smoothing over remaining changes
    for c in changes[period:]:
        avg_g = (avg_g * (period - 1) + max(c, 0))  / period
        avg_l = (avg_l * (period - 1) + max(-c, 0)) / period

    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)


def _true_range(high: float, low: float, prev_close: float) -> float:
    """True range captures gaps: max(H-L, |H-PrevClose|, |PrevClose-L|)."""
    return max(high - low, abs(high - prev_close), abs(prev_close - low))


def _wilder_atr(tr_values: list[float], period: int) -> float:
    """
    Wilder's smoothed ATR over a list of true-range values.
    Seeds with the simple mean of the first `period` values, then smooths.
    Returns 0.0 if there are fewer values than the period.
    """
    if len(tr_values) < period:
        return 0.0
    atr = sum(tr_values[:period]) / period
    for tr in tr_values[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_spy_5m(lookback_days: int = 2):
    """5-min SPY bars for the last `lookback_days` calendar days."""
    import yfinance as yf
    return yf.download("SPY", period=f"{lookback_days}d", interval="5m",
                       progress=False, auto_adjust=True)


def _fetch_spy_daily(lookback_days: int = 30):
    """Daily SPY bars used to compute a session-stable ATR(14)."""
    import yfinance as yf
    return yf.download("SPY", period=f"{lookback_days}d", interval="1d",
                       progress=False, auto_adjust=True)


def _compute_daily_atr14(period: int = _ATR_PERIOD) -> float:
    """
    Compute Wilder ATR(period) from daily SPY bars.
    Returns 0.0 on any fetch failure (caller skips the OR filter when atr==0).
    """
    try:
        import pandas as pd
        df = _fetch_spy_daily(lookback_days=period * 3)
        if df.empty or len(df) < period + 1:
            return 0.0

        # Flatten MultiIndex if present (single ticker fetch is usually flat)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        closes = df["Close"].astype(float).tolist()
        highs  = df["High"].astype(float).tolist()
        lows   = df["Low"].astype(float).tolist()

        trs = [_true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, len(closes))]
        return _wilder_atr(trs, period)
    except Exception as e:
        log.warning("[intraday_filter] daily ATR fetch failed: %s", e)
        return 0.0


def _fetch_vix_levels() -> tuple[float | None, float | None]:
    """
    Return (VIX, VIX3M) spot levels.

    Uses .xs() with an explicit level to avoid breaking on either MultiIndex
    ordering (Field, Ticker) or (Ticker, Field) depending on yfinance version.
    Falls back to direct column access when the frame is not MultiIndex.
    """
    try:
        import yfinance as yf
        import pandas as pd

        data = yf.download("^VIX ^VIX3M", period="2d", interval="1d",
                           progress=False, auto_adjust=True)
        if data.empty:
            return None, None

        def _extract(ticker: str) -> "pd.Series":
            if isinstance(data.columns, pd.MultiIndex):
                lvl0 = data.columns.get_level_values(0).unique().tolist()
                lvl1 = data.columns.get_level_values(1).unique().tolist()
                # Determine which level holds field names ("Close") vs tickers ("^VIX")
                if "Close" in lvl0:
                    # (Field, Ticker) ordering
                    return data.xs("Close", level=0, axis=1)[ticker].dropna()
                elif "Close" in lvl1:
                    # (Ticker, Field) ordering
                    return data.xs("Close", level=1, axis=1)[ticker].dropna()
                else:
                    return pd.Series(dtype=float)
            else:
                # Single-column frame (shouldn't happen with two tickers)
                return data["Close"].dropna() if ticker in ("^VIX",) else pd.Series(dtype=float)

        vix_s   = _extract("^VIX")
        vix3m_s = _extract("^VIX3M")
        vix   = float(vix_s.iloc[-1])   if not vix_s.empty   else None
        vix3m = float(vix3m_s.iloc[-1]) if not vix3m_s.empty else None
        return vix, vix3m

    except Exception as e:
        log.warning("[intraday_filter] VIX fetch failed: %s", e)
        return None, None


# ── Strategy classification ───────────────────────────────────────────────────

def _is_long_vol_structure(structure: str) -> bool:
    """True for structures that profit from rising vol or large directional moves."""
    s = (structure or "").lower()
    return any(k in s for k in [
        "long call", "long put", "debit spread", "call debit", "put debit",
        "straddle", "strangle", "calendar", "diagonal", "long butterfly",
    ])


def _is_short_vol_structure(structure: str) -> bool:
    """True for structures that profit from low vol / range action (positive theta)."""
    s = (structure or "").lower()
    keywords = [
        "iron condor", "credit spread", "put credit", "call credit",
        "short strangle", "condor",
        "jade lizard",   # short put + short call spread — positive theta, short vol
    ]
    if any(k in s for k in keywords):
        return True
    # "butterfly" without "long" qualifier = short-vol range trade
    # "long butterfly" is long-vol (caught above) and excluded here
    return "butterfly" in s and "long" not in s


# ── Public API ────────────────────────────────────────────────────────────────

def _fail(reason: str, detail: str, signals: dict) -> dict:
    return {"pass": _FAIL_OPEN, "reason": reason, "detail": detail, "signals": signals}


def check(structure: str = "") -> dict:
    """
    Run the intraday filter for a given structure type.

    RSI rule (short-vol structures): reject when SPY Wilder RSI > 65.
      An overbought reading into a credit-spread entry risks whipsaw on reversal.

    RSI rule (long-vol structures): reject when SPY Wilder RSI < 35.
      Buying premium into an already-exhausted sell-off pays for a move that
      may be largely priced in. This is a conservative policy choice — breakout
      systems may prefer the opposite. Adjust _RSI_OVERSOLD or disable this
      gate if your entries are momentum-based.

    Args:
        structure: e.g. "Iron Condor", "Long Call", "Put Credit Spread"

    Returns dict with keys: pass, reason, detail, signals
    """
    signals: dict = {
        "spy_rsi":           None,
        "vix_backwardation": None,
        "vix_bp_ratio":      None,
        "spy_outside_or":    None,
        "spy_or_break_atr":  None,
        "atr14":             None,
        "vix":               None,
        "vix_3m":            None,
    }
    reason_codes:  list[str] = []
    detail_parts:  list[str] = []
    passed = True

    # ── 1. Fetch 5-min bars ────────────────────────────────────────────────
    try:
        df = _fetch_spy_5m(lookback_days=2)
        if df.empty:
            return _fail("intraday_data_unavailable", "yfinance returned empty 5m frame", signals)

        # Convert index to NY time before date-filtering to avoid UTC-midnight mismatch
        if hasattr(df.index, "tz") and df.index.tz is not None:
            _to_date = lambda x: x.astimezone(_NY_TZ).date()
        else:
            _to_date = lambda x: x.date()

        today_date = _to_date(df.index[-1])
        today_bars = df[df.index.map(_to_date) == today_date]

        if today_bars.empty:
            return _fail("market_not_open_yet", f"no 5m bars for {today_date}", signals)

        closes = list(today_bars["Close"].dropna().astype(float))
        highs  = list(today_bars["High"].dropna().astype(float))
        lows   = list(today_bars["Low"].dropna().astype(float))

        prev_bars  = df[df.index.map(_to_date) < today_date]
        prev_close = float(prev_bars["Close"].dropna().iloc[-1]) if not prev_bars.empty else closes[0]

    except Exception as e:
        log.warning("[intraday_filter] SPY 5m fetch failed: %s", e)
        return _fail("fetch_error", str(e), signals)

    # ── 2. SPY intraday RSI (Wilder) ──────────────────────────────────────
    rsi_period = min(14, len(closes) - 1)
    rsi_val    = _rsi_wilder(closes, period=rsi_period) if rsi_period >= 2 else None
    signals["spy_rsi"] = rsi_val

    if rsi_val is not None:
        if _is_short_vol_structure(structure) and rsi_val > _RSI_OVERBOUGHT:
            passed = False
            reason_codes.append("rsi_overbought")
            detail_parts.append(
                f"SPY RSI={rsi_val:.1f} overbought — credit/range structures "
                "vulnerable to reversal into an extended market"
            )
        elif _is_long_vol_structure(structure) and rsi_val < _RSI_OVERSOLD:
            passed = False
            reason_codes.append("rsi_oversold")
            detail_parts.append(
                f"SPY RSI={rsi_val:.1f} oversold — paying premium into an already "
                "exhausted sell-off; adverse entry for long-vol"
            )

    # ── 3. VIX term structure ─────────────────────────────────────────────
    vix, vix3m = _fetch_vix_levels()
    signals["vix"]    = vix
    signals["vix_3m"] = vix3m

    if vix is not None and vix3m is not None and vix3m > 0:
        bp_ratio = (vix - vix3m) / vix3m
        signals["vix_bp_ratio"]      = round(bp_ratio, 4)
        signals["vix_backwardation"] = bp_ratio > _VIX_BACKWARDATION_THRESH
    else:
        signals["vix_bp_ratio"]      = None
        signals["vix_backwardation"] = None

    if signals["vix_backwardation"] and _is_short_vol_structure(structure):
        bp_pct = signals["vix_bp_ratio"] * 100
        passed = False
        reason_codes.append("vix_backwardation")
        detail_parts.append(
            f"VIX term backwardation (VIX={vix:.1f}, VIX3M={vix3m:.1f}, "
            f"spread={bp_pct:+.1f}%) — elevated short-term fear; avoid new short-vol entries"
        )

    # ── 4. Opening range vs daily ATR(14) ─────────────────────────────────
    # Opening range = first 6 intraday bars (first 30 min of 5-min session).
    # The OR-break threshold is expressed in units of the *daily* Wilder ATR(14)
    # so it stays stable throughout the session (no intraday bar-count drift).
    or_bars = min(6, len(highs))
    if or_bars >= 2:
        or_high = max(highs[:or_bars])
        or_low  = min(lows[:or_bars])
        current = closes[-1]

        atr14 = _compute_daily_atr14()
        signals["atr14"] = round(atr14, 4) if atr14 else None

        outside_or   = current > or_high or current < or_low
        or_break_atr = 0.0
        if atr14 > 0 and outside_or:
            or_break_atr = (current - or_high) / atr14 if current > or_high else (or_low - current) / atr14

        signals["spy_outside_or"]   = outside_or
        signals["spy_or_break_atr"] = round(or_break_atr, 3)

        if outside_or and or_break_atr > _OR_BREAK_ATR and _is_short_vol_structure(structure):
            passed = False
            reason_codes.append("or_atr_break")
            detail_parts.append(
                f"SPY is {or_break_atr:.2f}× daily ATR(14) outside opening range — "
                "session is trending; credit/range entry quality poor"
            )

    return {
        "pass":    passed,
        "reason":  "; ".join(reason_codes) if reason_codes else "ok",
        "detail":  "; ".join(detail_parts),
        "signals": signals,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Intraday entry timing filter")
    parser.add_argument("--structure", default="Iron Condor",
                        help="Structure type to evaluate (e.g. 'Iron Condor', 'Long Call')")
    args = parser.parse_args()

    result = check(structure=args.structure)

    print(f"\n=== Intraday Filter === {args.structure} ===")
    print(f"Pass:    {result['pass']}")
    print(f"Reason:  {result['reason']}")
    if result["detail"]:
        print(f"Detail:  {result['detail']}")
    print("\nSignals:")
    for k, v in result["signals"].items():
        if v is None:
            print(f"  {k:<25} N/A")
        elif isinstance(v, bool):
            print(f"  {k:<25} {v}")
        elif isinstance(v, float):
            print(f"  {k:<25} {v:.4f}")
        else:
            print(f"  {k:<25} {v}")
