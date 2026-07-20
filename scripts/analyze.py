import sys
import os
from concurrent.futures import ThreadPoolExecutor as _TPE
from datetime import datetime, date

import pandas as pd

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from config.watchlist import WATCHLIST
from config import rules
from scripts.data_fetch import (
    get_price_history, get_trend, get_weekly_trend, pick_expiry, get_option_chain,
    get_iv_rank_proxy, get_atm_iv, get_rsi, get_macd, pick_back_expiry,
    get_next_earnings_date, get_news_sentiment, get_adx, get_relative_volume,
    get_options_flow, get_ema200, get_iv_term_structure,
    get_dividend_info, get_short_interest, get_vol_skew,
    get_hv_and_iv_premium, get_analyst_sentiment, get_risk_free_rate,
    get_live_spot, get_beta, get_atr_pct, get_iv_rank_52w,
    get_max_pain, get_oi_concentration, get_vix_context, get_macro_context,
)
from scripts.black_scholes import delta as bs_delta, theta as bs_theta, gamma as bs_gamma, vega as bs_vega
from scripts.binomial_pricer import (
    price as _bin_price,
    delta as _bin_delta,
    theta as _bin_theta,
    should_use_binomial as _should_use_binomial,
)
import yfinance as yf
from config import scoring as sc

# Live risk-free rate fetched once per process; falls back to 5 % if unavailable
RISK_FREE_RATE = get_risk_free_rate()

from config.structures import (
    CREDIT_STRUCTURES, DEBIT_STRUCTURES, ALL_STRUCTURES,
    STRUCTURE_MATRIX, get as get_structure,
)


def _net_greeks(spot, T, long_row, short_row, option_type, short_T=None, r=RISK_FREE_RATE):
    """Net delta, daily theta, gamma, and vega for a two-leg spread.
    long_row  = the leg we BUY  (positive position)
    short_row = the leg we SELL (negative position)
    short_T   = override T for the short leg (used for Calendar Spread).
    Returns (net_delta, net_theta_daily, net_gamma, net_vega) or (None, None, None, None) on error.
    """
    try:
        l_iv = float(long_row.get("impliedVolatility")  or 0)
        s_iv = float(short_row.get("impliedVolatility") or 0)
        l_d  = float(long_row.get("delta")  or 0)
        s_d  = float(short_row.get("delta") or 0)
        net_d = round(l_d - s_d, 3)
        sT = short_T if short_T else T
        l_th = bs_theta(spot, long_row["strike"],  T,  r, l_iv, option_type) if l_iv > 0 else 0.0
        s_th = bs_theta(spot, short_row["strike"], sT, r, s_iv, option_type) if s_iv > 0 else 0.0
        l_gm = bs_gamma(spot, long_row["strike"],  T,  r, l_iv) if l_iv > 0 else 0.0
        s_gm = bs_gamma(spot, short_row["strike"], sT, r, s_iv) if s_iv > 0 else 0.0
        l_vg = bs_vega(spot, long_row["strike"],   T,  r, l_iv) if l_iv > 0 else 0.0
        s_vg = bs_vega(spot, short_row["strike"],  sT, r, s_iv) if s_iv > 0 else 0.0
        return (round(net_d, 3), round(l_th - s_th, 4),
                round(l_gm - s_gm, 6), round(l_vg - s_vg, 4))
    except Exception:
        return None, None, None, None


def _option_price(
    spot:        float,
    strike:      float,
    T:           float,
    r:           float,
    sigma:       float,
    option_type: str,
    days_to_ex_div: int | None = None,
    is_index:    bool = False,
) -> float:
    """
    Return option price using CRR binomial tree when early exercise is relevant
    (individual stock puts, or calls near ex-dividend date), otherwise Black-Scholes.

    Index options (SPX, SPY, QQQ) are always European — BS is exact.
    """
    if not is_index and _should_use_binomial(option_type, int(T * 365) if T else 0, days_to_ex_div):
        try:
            return _bin_price(spot, strike, T, r, sigma, option_type, american=True)
        except Exception:
            pass  # fall through to BS on any numeric error
    # Black-Scholes (European)
    from scipy.stats import norm
    import math
    if T <= 0 or sigma <= 0:
        return max(spot - strike, 0) if option_type == "call" else max(strike - spot, 0)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc_K = strike * math.exp(-r * T)
    if option_type == "call":
        return float(spot * norm.cdf(d1) - disc_K * norm.cdf(d2))
    return float(disc_K * norm.cdf(-d2) - spot * norm.cdf(-d1))


# STRUCTURE_MATRIX, ALL_STRUCTURES imported from config.structures above

# ---------- Overridable parameters ----------
# Defaults mirror config/rules.py. Each entry: (default value, description shown on the web form)
PARAM_INFO = {
    "min_dte":                 (rules.MIN_DTE, "Minimum days-to-expiry when picking an option expiry."),
    "max_dte":                 (rules.MAX_DTE, "Maximum days-to-expiry when picking an option expiry."),
    "event_blackout_days":     (rules.EVENT_BLACKOUT_DAYS, "Skip the trade if earnings fall within this many days of expiry."),
    "iv_rank_high_threshold":  (rules.IV_RANK_HIGH_THRESHOLD, "IV rank (%) at or above this counts as a 'High IV' environment."),
    "sma_short":               (rules.SMA_SHORT, "Short SMA period (days) used for trend classification."),
    "sma_long":                (rules.SMA_LONG, "Long SMA period (days) used for trend classification."),
    "trend_band_pct":          (rules.TREND_BAND_PCT, "Price must be this fraction beyond the short SMA to count as trending (else Range-bound)."),
    "credit_short_delta_lo":   (rules.CREDIT_SHORT_DELTA_RANGE[0], "Lower bound of |delta| for the short leg of a credit spread."),
    "credit_short_delta_hi":   (rules.CREDIT_SHORT_DELTA_RANGE[1], "Upper bound of |delta| for the short leg of a credit spread."),
    "credit_min_pct_of_width": (rules.CREDIT_MIN_CREDIT_PCT_OF_WIDTH, "Minimum credit received as a fraction of spread width (e.g. 0.25 = 25%)."),
    "debit_long_delta_lo":     (rules.DEBIT_LONG_DELTA_RANGE[0], "Lower bound of |delta| for the long (main) leg of a debit spread."),
    "debit_long_delta_hi":     (rules.DEBIT_LONG_DELTA_RANGE[1], "Upper bound of |delta| for the long (main) leg of a debit spread."),
    "debit_short_delta_lo":    (rules.DEBIT_SHORT_DELTA_RANGE[0], "Lower bound of |delta| for the short (hedge) leg of a debit spread."),
    "debit_short_delta_hi":    (rules.DEBIT_SHORT_DELTA_RANGE[1], "Upper bound of |delta| for the short (hedge) leg of a debit spread."),
    "max_risk_pct":            (rules.MAX_RISK_PCT, "Max loss per trade as a fraction of capital - used to size spread widths."),
    "min_open_interest":       (rules.MIN_OPEN_INTEREST, "Minimum open interest required on each leg's strike - filters out thin-OI strikes even if bid/ask look tradeable."),
    "min_profit_amount":       (rules.MIN_PROFIT_AMOUNT, "Minimum max-profit per share (credit received, or width minus debit) for a candidate to be flagged as meeting the profit goal."),
    "calendar_min_gap_days":   (rules.CALENDAR_MIN_GAP_DAYS, "Minimum days between the front-month expiry and the back-month (long) expiry for a Calendar Spread."),
    "calendar_max_gap_days":   (rules.CALENDAR_MAX_GAP_DAYS, "Maximum days between the front-month expiry and the back-month (long) expiry for a Calendar Spread."),
    "jade_lizard_put_delta_lo": (rules.JADE_LIZARD_PUT_DELTA_RANGE[0], "Lower bound of |delta| for the naked short put leg of a Jade Lizard."),
    "jade_lizard_put_delta_hi": (rules.JADE_LIZARD_PUT_DELTA_RANGE[1], "Upper bound of |delta| for the naked short put leg of a Jade Lizard."),
    "rr_put_delta_lo":  (rules.RISK_REVERSAL_PUT_DELTA_RANGE[0],  "Lower bound of |delta| for the short put leg of a Risk Reversal."),
    "rr_put_delta_hi":  (rules.RISK_REVERSAL_PUT_DELTA_RANGE[1],  "Upper bound of |delta| for the short put leg of a Risk Reversal."),
    "rr_call_delta_lo": (rules.RISK_REVERSAL_CALL_DELTA_RANGE[0], "Lower bound of |delta| for the long call leg of a Risk Reversal."),
    "rr_call_delta_hi": (rules.RISK_REVERSAL_CALL_DELTA_RANGE[1], "Upper bound of |delta| for the long call leg of a Risk Reversal."),
    "flc_put_short_delta_lo":  (rules.FLC_PUT_SHORT_DELTA_RANGE[0],  "Lower bound of |delta| for the short put leg (credit spread) in a Financed Long Call."),
    "flc_put_short_delta_hi":  (rules.FLC_PUT_SHORT_DELTA_RANGE[1],  "Upper bound of |delta| for the short put leg in a Financed Long Call."),
    "flc_call_long_delta_lo":  (rules.FLC_CALL_LONG_DELTA_RANGE[0],  "Lower bound of |delta| for the standalone long call in a Financed Long Call."),
    "flc_call_long_delta_hi":  (rules.FLC_CALL_LONG_DELTA_RANGE[1],  "Upper bound of |delta| for the standalone long call in a Financed Long Call."),
    "flp_call_short_delta_lo": (rules.FLP_CALL_SHORT_DELTA_RANGE[0], "Lower bound of |delta| for the short call leg (credit spread) in a Financed Long Put."),
    "flp_call_short_delta_hi": (rules.FLP_CALL_SHORT_DELTA_RANGE[1], "Upper bound of |delta| for the short call leg in a Financed Long Put."),
    "flp_put_long_delta_lo":   (rules.FLP_PUT_LONG_DELTA_RANGE[0],   "Lower bound of |delta| for the standalone long put in a Financed Long Put."),
    "flp_put_long_delta_hi":   (rules.FLP_PUT_LONG_DELTA_RANGE[1],   "Upper bound of |delta| for the standalone long put in a Financed Long Put."),
    "rb_short_delta_lo":  (rules.RATIO_BACKSPREAD_SHORT_DELTA_RANGE[0], "Lower bound of |delta| for the short (near-ATM, 1×) leg of a Ratio Backspread."),
    "rb_short_delta_hi":  (rules.RATIO_BACKSPREAD_SHORT_DELTA_RANGE[1], "Upper bound of |delta| for the short (near-ATM, 1×) leg of a Ratio Backspread."),
    "rb_long_delta_lo":   (rules.RATIO_BACKSPREAD_LONG_DELTA_RANGE[0],  "Lower bound of |delta| for the long (OTM, 2×) legs of a Ratio Backspread."),
    "rb_long_delta_hi":   (rules.RATIO_BACKSPREAD_LONG_DELTA_RANGE[1],  "Upper bound of |delta| for the long (OTM, 2×) legs of a Ratio Backspread."),
    "ls_call_delta_lo":   (rules.LONG_STRANGLE_CALL_DELTA_RANGE[0], "Lower bound of |delta| for the OTM call leg of a Long Strangle."),
    "ls_call_delta_hi":   (rules.LONG_STRANGLE_CALL_DELTA_RANGE[1], "Upper bound of |delta| for the OTM call leg of a Long Strangle."),
    "ls_put_delta_lo":    (rules.LONG_STRANGLE_PUT_DELTA_RANGE[0],  "Lower bound of |delta| for the OTM put leg of a Long Strangle."),
    "ls_put_delta_hi":    (rules.LONG_STRANGLE_PUT_DELTA_RANGE[1],  "Upper bound of |delta| for the OTM put leg of a Long Strangle."),
    "bc_put_long_delta_lo":   (rules.BEAR_COMBO_PUT_LONG_DELTA_RANGE[0],   "Lower bound of |delta| for the long (ATM-ish) put in a Bear Combo."),
    "bc_put_long_delta_hi":   (rules.BEAR_COMBO_PUT_LONG_DELTA_RANGE[1],   "Upper bound of |delta| for the long (ATM-ish) put in a Bear Combo."),
    "bc_put_short_delta_lo":  (rules.BEAR_COMBO_PUT_SHORT_DELTA_RANGE[0],  "Lower bound of |delta| for the short (OTM) put in a Bear Combo."),
    "bc_put_short_delta_hi":  (rules.BEAR_COMBO_PUT_SHORT_DELTA_RANGE[1],  "Upper bound of |delta| for the short (OTM) put in a Bear Combo."),
    "bc_call_short_delta_lo": (rules.BEAR_COMBO_CALL_SHORT_DELTA_RANGE[0], "Lower bound of |delta| for the short (OTM) call in a Bear Combo."),
    "bc_call_short_delta_hi": (rules.BEAR_COMBO_CALL_SHORT_DELTA_RANGE[1], "Upper bound of |delta| for the short (OTM) call in a Bear Combo."),
    "bc_call_long_delta_lo":  (rules.BEAR_COMBO_CALL_LONG_DELTA_RANGE[0],  "Lower bound of |delta| for the long (far OTM) call in a Bear Combo."),
    "bc_call_long_delta_hi":  (rules.BEAR_COMBO_CALL_LONG_DELTA_RANGE[1],  "Upper bound of |delta| for the long (far OTM) call in a Bear Combo."),
    "profit_target_pct":       (rules.PROFIT_TARGET_PCT, "Take-profit target as a fraction of max profit (e.g. 0.50 = close at 50% of max profit)."),
    "diagonal_long_delta_lo":  (rules.DIAGONAL_LONG_DELTA_RANGE[0],  "Lower bound of |delta| for the back-month (long) leg of a Diagonal Spread."),
    "diagonal_long_delta_hi":  (rules.DIAGONAL_LONG_DELTA_RANGE[1],  "Upper bound of |delta| for the back-month (long) leg of a Diagonal Spread."),
    "diagonal_short_delta_lo": (rules.DIAGONAL_SHORT_DELTA_RANGE[0], "Lower bound of |delta| for the front-month (short) leg of a Diagonal Spread."),
    "diagonal_short_delta_hi": (rules.DIAGONAL_SHORT_DELTA_RANGE[1], "Upper bound of |delta| for the front-month (short) leg of a Diagonal Spread."),
    "diagonal_min_gap_days":   (rules.DIAGONAL_MIN_GAP_DAYS, "Minimum days between front and back expiry for a Diagonal Spread."),
    "diagonal_max_gap_days":   (rules.DIAGONAL_MAX_GAP_DAYS, "Maximum days between front and back expiry for a Diagonal Spread."),
}

DEFAULT_PARAMS = {k: v[0] for k, v in PARAM_INFO.items()}


def get_params(overrides=None):
    p = dict(DEFAULT_PARAMS)
    if overrides:
        for k, v in overrides.items():
            if k in p and v is not None:
                p[k] = type(p[k])(v)
    return p


def days_to_earnings(ticker_obj):
    earn_date = get_next_earnings_date(ticker_obj)
    if not earn_date:
        return None
    return (earn_date - date.today()).days


def add_deltas(df, spot, T, option_type, fallback_vol=None):
    df = df.copy()
    # Use median chain IV as fallback when individual rows have 0 IV
    chain_median_iv = df["impliedVolatility"].replace(0, float("nan")).median()
    if not chain_median_iv or chain_median_iv != chain_median_iv:  # NaN check
        chain_median_iv = None
    default_vol = chain_median_iv or fallback_vol or 0.25

    df["delta"] = df.apply(
        lambda row: bs_delta(spot, row["strike"], T, RISK_FREE_RATE,
                              row["impliedVolatility"] if row["impliedVolatility"] > 1e-3
                              else default_vol, option_type),
        axis=1,
    )
    return df


def find_short_strike(df, option_type, delta_range, min_oi=0):
    lo, hi = delta_range
    cand = df[df["delta"].abs().between(lo, hi)]
    if min_oi > 0:
        cand = cand[cand["openInterest"].fillna(0) >= min_oi]
    # Prefer liquid legs; fall back to all if none pass the bid-ask gate
    if "ba_ok" in cand.columns:
        liquid = cand[cand["ba_ok"] == True]
        if not liquid.empty:
            cand = liquid
    if cand.empty:
        return None
    mid = (lo + hi) / 2
    cand = cand.copy()
    cand["dist"] = (cand["delta"].abs() - mid).abs()
    return cand.sort_values("dist").iloc[0]


def find_long_strike_for_credit_spread(df, short_row, option_type, width_target, min_oi=0):
    """Pick the strike further OTM than short_row by approx width_target."""
    if option_type == "put":
        cand = df[df["strike"] < short_row["strike"]]
        cand = cand.copy()
        cand["width"] = short_row["strike"] - cand["strike"]
    else:
        cand = df[df["strike"] > short_row["strike"]]
        cand = cand.copy()
        cand["width"] = cand["strike"] - short_row["strike"]
    cand = cand[cand["width"] > 0]
    if min_oi > 0:
        cand = cand[cand["openInterest"].fillna(0) >= min_oi]
    # Prefer liquid legs; fall back to all if none pass the bid-ask gate
    if "ba_ok" in cand.columns:
        liquid = cand[cand["ba_ok"] == True]
        if not liquid.empty:
            cand = liquid
    if cand.empty:
        return None
    cand["dist"] = (cand["width"] - width_target).abs()
    return cand.sort_values("dist").iloc[0]


def pop_ev_credit(short_delta, credit, width):
    """Approx probability of profit (short leg expires OTM) and expected value."""
    pop = max(0.0, min(1.0, 1 - abs(short_delta)))
    max_loss = width - credit
    ev = pop * credit - (1 - pop) * max_loss
    return round(pop * 100, 1), round(ev, 3)


def pop_ev_debit(long_delta, debit, width):
    """Approx probability of profit (long leg finishes ITM) and expected value."""
    pop = max(0.0, min(1.0, abs(long_delta)))
    max_profit = width - debit
    ev = pop * max_profit - (1 - pop) * debit
    return round(pop * 100, 1), round(ev, 3)


def pop_ev_iron_condor(put_short_delta, call_short_delta, total_credit, width):
    pop = max(0.0, min(1.0, 1 - (abs(put_short_delta) + abs(call_short_delta))))
    max_loss = width - total_credit
    ev = pop * total_credit - (1 - pop) * max_loss
    return round(pop * 100, 1), round(ev, 3)


def profit_note(max_profit, min_profit_amount):
    """Returns (meets_min_profit, note string) comparing a trade's max profit
    per share against the min_profit_amount parameter."""
    meets = bool(max_profit >= min_profit_amount)
    note = f"max profit ${max_profit:.2f} {'meets' if meets else 'below'} ${min_profit_amount:.2f} min"
    return meets, note


def loss_note(max_loss, max_loss_limit):
    """Returns (meets_max_loss, note string) comparing a trade's max loss
    per share against the max_loss_limit (derived from max_risk_pct)."""
    meets = bool(max_loss <= max_loss_limit)
    note = f"max loss ${max_loss:.2f} {'within' if meets else 'exceeds'} ${max_loss_limit:.2f} risk cap"
    return meets, note


def compute_signal_alignment(
    recommended_structure: str,
    trend, weekly_trend, rsi, macd_trend, news_sentiment,
    adx=None, rel_volume=None, pcr=None, pcr_sentiment=None,
    ema200_position=None, iv_term_shape=None,
    short_interest=None, vol_skew_pct=None,
    analyst_label=None, iv_premium=None,
    regime="chop",
) -> dict:
    """
    Score how well current signals support the given structure.

    Each sub-factor contributes ±weight where weight = get_sub_weight(factor, sub, regime).
    Both the contribution and the applicable weight are tracked independently so that
    pct = score / effective_max is comparable across structures with different numbers
    of applicable checks — a structure with fewer applicable sub-factors is not penalised
    by the global regime budget.

    effective_max is the sum of weights for all checks *applicable* to this structure —
    meaning the structure has a scoring rule for that sub-factor. A check that fired with
    value=0 (neutral zone, e.g. ADX 20–25) still enters effective_max because the
    evidence existed; only checks where the structure has no rule at all are excluded.

    Returns:
        score         — raw weighted sum (positive = aligned, negative = conflicted)
        effective_max — sum of weights for applicable checks (always ≥ 0)
        pct           — score / effective_max; comparable across structures for ranking
        rating        — qualitative label from score_to_rating(pct)
        notes         — flat list of all explanations (scored + monitoring)
        contributions — scored items only, with rich metadata for UI/audit
        regime        — regime used for weight computation
    """
    score         = 0.0
    effective_max = 0.0
    contributions: list[dict] = []
    notes:         list[str]  = []

    def w(factor: str, sub: str) -> float:
        return sc.get_sub_weight(factor, sub, regime)

    def _add(factor: str, subfactor: str, wt: float, value: float, explanation: str) -> None:
        """
        Record an applicable check.

        wt always enters effective_max (the check was applicable to this structure).
        value=0 means the signal was neutral — the denominator still grows, keeping
        pct honest. value=0 entries are NOT added to contributions or notes since
        they add no signal and would clutter the UI.
        """
        nonlocal score, effective_max
        score         += value
        effective_max += wt
        if value != 0:
            contributions.append({
                "factor":       factor,
                "subfactor":    subfactor,
                "weight":       round(wt, 4),
                "contribution": round(value, 4),
                "direction":    "positive" if value > 0 else "negative",
                "explanation":  explanation,
            })
            if explanation:
                notes.append(explanation)

    def _note(explanation: str) -> None:
        """Monitoring note only — no score, not in contributions."""
        notes.append(explanation)

    # ── Technical signals ─────────────────────────────────────────────────────

    # EMA 200 — institutional trend filter
    if ema200_position is not None:
        _bullish = ("Put Credit Spread", "Call Debit Spread", "Diagonal Spread",
                    "Risk Reversal", "Financed Long Call", "Ratio Call Backspread")
        _bearish = ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                    "Financed Long Put", "Ratio Put Backspread")
        wt = w("technical", "ema200")
        if recommended_structure in _bullish:
            if ema200_position == "above":
                _add("Technical", "EMA200", wt, +wt,
                     "Price above EMA200 — institutional uptrend confirms bullish bias")
            else:
                _add("Technical", "EMA200", wt, -wt,
                     "Price below EMA200 — institutional downtrend is headwind for bullish trade")
        elif recommended_structure in _bearish:
            if ema200_position == "below":
                _add("Technical", "EMA200", wt, +wt,
                     "Price below EMA200 — institutional downtrend confirms bearish bias")
            else:
                _add("Technical", "EMA200", wt, -wt,
                     "Price above EMA200 — institutional uptrend is headwind for bearish trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            _note(f"Price {ema200_position} EMA200 — monitor for trend resumption out of range")

    # Weekly trend
    if weekly_trend and weekly_trend not in ("N/A", "Range-bound"):
        wt = w("technical", "weekly_trend")
        if weekly_trend == trend:
            _add("Technical", "Weekly Trend", wt, +wt,
                 f"Weekly trend confirms {trend}")
        else:
            _add("Technical", "Weekly Trend", wt, -wt,
                 f"Weekly trend ({weekly_trend}) conflicts with daily ({trend})")

    # ADX — trend strength / choppiness
    if adx is not None:
        wt          = w("technical", "adx")
        _directional = recommended_structure in (
            "Put Credit Spread", "Call Credit Spread", "Call Debit Spread", "Put Debit Spread"
        )
        _neutral_struct = recommended_structure in ("Iron Condor", "Calendar Spread")
        if _directional or _neutral_struct:
            if adx > 25:
                if _directional:
                    _add("Technical", "ADX", wt, +wt,
                         f"ADX {adx:.0f} strong trend — confirms directional trade")
                else:
                    _add("Technical", "ADX", wt, -wt,
                         f"ADX {adx:.0f} strong trend — breakout risk for neutral trade")
            elif adx < 20:
                if _directional:
                    _add("Technical", "ADX", wt, -wt,
                         f"ADX {adx:.0f} choppy market — weak case for directional trade")
                else:
                    _add("Technical", "ADX", wt, +wt,
                         f"ADX {adx:.0f} choppy range — supports neutral trade")
            else:
                # ADX 20–25: indeterminate — still applicable evidence, contributes 0 to score
                _add("Technical", "ADX", wt, 0, "")

    # RSI
    if rsi is not None:
        wt = w("technical", "rsi")
        if recommended_structure == "Put Credit Spread":
            # All RSI values produce a contribution (no neutral zone for this structure)
            if rsi < 50:
                _add("Technical", "RSI", wt, -wt,
                     f"RSI {rsi:.0f} below midline — weak bullish momentum")
            elif rsi > 70:
                _add("Technical", "RSI", wt, -wt,
                     f"RSI {rsi:.0f} overbought — pullback risk")
            else:
                _add("Technical", "RSI", wt, +wt,
                     f"RSI {rsi:.0f} healthy for uptrend")
        elif recommended_structure == "Call Credit Spread":
            if rsi > 50:
                _add("Technical", "RSI", wt, -wt,
                     f"RSI {rsi:.0f} above midline — weak bearish momentum")
            elif rsi < 30:
                _add("Technical", "RSI", wt, -wt,
                     f"RSI {rsi:.0f} oversold — bounce risk")
            else:
                _add("Technical", "RSI", wt, +wt,
                     f"RSI {rsi:.0f} healthy for downtrend")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            if 40 <= rsi <= 60:
                _add("Technical", "RSI", wt, +wt,
                     f"RSI {rsi:.0f} neutral — supports range-bound")
            elif rsi > 70 or rsi < 30:
                _add("Technical", "RSI", wt, -wt,
                     f"RSI {rsi:.0f} extended — breakout/breakdown risk for neutral trade")
            else:
                # RSI 30–40 or 60–70: mild momentum present — applicable but ambiguous
                _add("Technical", "RSI", wt, 0, "")
        elif recommended_structure == "Call Debit Spread":
            if 50 <= rsi <= 70:
                _add("Technical", "RSI", wt, +wt,
                     f"RSI {rsi:.0f} bullish momentum")
            elif rsi < 40:
                _add("Technical", "RSI", wt, -wt,
                     f"RSI {rsi:.0f} weak for bullish trade")
            else:
                # RSI 40–50: neither confirming nor contradicting — applicable, 0 contribution
                _add("Technical", "RSI", wt, 0, "")
        elif recommended_structure == "Put Debit Spread":
            if 30 <= rsi <= 50:
                _add("Technical", "RSI", wt, +wt,
                     f"RSI {rsi:.0f} bearish momentum")
            elif rsi > 60:
                _add("Technical", "RSI", wt, -wt,
                     f"RSI {rsi:.0f} strong — weak bearish case")
            else:
                # RSI 50–60: momentum neutral for downside trade — applicable, 0 contribution
                _add("Technical", "RSI", wt, 0, "")

    # MACD
    if macd_trend and macd_trend not in ("N/A",):
        wt = w("technical", "macd")
        _bullish = ("Put Credit Spread", "Call Debit Spread", "Risk Reversal",
                    "Financed Long Call", "Ratio Call Backspread")
        _bearish = ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                    "Financed Long Put", "Ratio Put Backspread")
        if recommended_structure in _bullish:
            if macd_trend == "Bullish":
                _add("Technical", "MACD", wt, +wt, "MACD Bullish confirms upside bias")
            else:
                _add("Technical", "MACD", wt, -wt, "MACD Bearish conflicts with upside bias")
        elif recommended_structure in _bearish:
            if macd_trend == "Bearish":
                _add("Technical", "MACD", wt, +wt, "MACD Bearish confirms downside bias")
            else:
                _add("Technical", "MACD", wt, -wt, "MACD Bullish conflicts with downside bias")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            _note(f"MACD {macd_trend} — watch for breakout")

    # IV Term Structure — edge signal for calendars and diagonals only
    if iv_term_shape is not None:
        wt = w("technical", "vol_skew")   # reuses vol_skew budget; dedicated sub can be added later
        if recommended_structure in ("Calendar Spread", "Diagonal Spread"):
            if iv_term_shape == "Backwardation":
                _add("Technical", "IV Term Structure", wt, +wt,
                     "IV backwardation — selling richer near-term vol adds edge to time spread")
            elif iv_term_shape == "Contango":
                _add("Technical", "IV Term Structure", wt, -wt,
                     "IV contango — no near-term vol premium; time spread has no vol-edge advantage")
            else:
                # Flat term structure: applicable check, but no vol-edge in either direction
                _add("Technical", "IV Term Structure", wt, 0, "")
                _note("Flat IV term structure — neutral vol edge for calendar/diagonal")

    # ── Flow signals ─────────────────────────────────────────────────────────

    # News sentiment
    if news_sentiment and news_sentiment not in ("Neutral", "N/A"):
        wt = w("flow", "news")
        _bullish = ("Put Credit Spread", "Call Debit Spread", "Risk Reversal",
                    "Financed Long Call", "Ratio Call Backspread")
        _bearish = ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                    "Financed Long Put", "Ratio Put Backspread")
        if recommended_structure in _bullish:
            if news_sentiment == "Bullish":
                _add("Flow", "News", wt, +wt,
                     "News sentiment Bullish — supports upside trade")
            elif news_sentiment == "Bearish":
                _add("Flow", "News", wt, -wt,
                     "News sentiment Bearish — headwind for upside trade")
        elif recommended_structure in _bearish:
            if news_sentiment == "Bearish":
                _add("Flow", "News", wt, +wt,
                     "News sentiment Bearish — supports downside trade")
            elif news_sentiment == "Bullish":
                _add("Flow", "News", wt, -wt,
                     "News sentiment Bullish — headwind for downside trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            if news_sentiment == "Mixed":
                _add("Flow", "News", wt, +wt,
                     "Mixed news — consistent with range-bound expectation")
            elif news_sentiment in ("Bullish", "Bearish"):
                _add("Flow", "News", wt, -wt,
                     f"News {news_sentiment} — directional bias adds breakout risk to neutral trade")

    # Put/Call Ratio
    if pcr_sentiment and pcr_sentiment not in ("Neutral", "N/A"):
        wt = w("flow", "pcr")
        _bullish = ("Put Credit Spread", "Call Debit Spread", "Risk Reversal",
                    "Financed Long Call", "Ratio Call Backspread")
        _bearish = ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                    "Financed Long Put", "Ratio Put Backspread")
        if recommended_structure in _bullish:
            if pcr_sentiment == "Bullish":
                _add("Flow", "PCR", wt, +wt,
                     f"PCR {pcr} call-heavy OI — confirms upside bias")
            elif pcr_sentiment == "Bearish":
                _add("Flow", "PCR", wt, -wt,
                     f"PCR {pcr} put-heavy OI — headwind for upside trade")
        elif recommended_structure in _bearish:
            if pcr_sentiment == "Bearish":
                _add("Flow", "PCR", wt, +wt,
                     f"PCR {pcr} put-heavy OI — confirms downside bias")
            elif pcr_sentiment == "Bullish":
                _add("Flow", "PCR", wt, -wt,
                     f"PCR {pcr} call-heavy OI — headwind for downside trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            # pcr_sentiment "Neutral" is already filtered by the outer guard;
            # Bullish/Bearish for neutral structures is a monitoring note only
            _note(f"PCR {pcr} {(pcr_sentiment or '').lower()} — directional OI skew adds breakout risk")

    # Relative Volume — confirmation-only signal (never penalises)
    # Applicable to directional structures only; neutral structs don't benefit from vol confirmation.
    if rel_volume is not None and trend != "Range-bound":
        wt = w("flow", "rel_volume")
        if rel_volume > 1.5:
            _add("Flow", "Relative Volume", wt, +wt,
                 f"Rel volume {rel_volume:.1f}x — elevated volume confirms move")
        else:
            # Normal or thin volume: no confirmation, but also not a contradiction.
            # Still enters effective_max so pct is not artificially inflated.
            _add("Flow", "Relative Volume", wt, 0, "")
            if rel_volume < 0.5:
                _note(f"Rel volume {rel_volume:.1f}x — thin volume, low conviction")

    # Analyst consensus
    if analyst_label and analyst_label not in ("N/A", "Neutral"):
        wt = w("flow", "analyst")
        _bullish = ("Put Credit Spread", "Call Debit Spread", "Risk Reversal",
                    "Financed Long Call", "Ratio Call Backspread")
        _bearish = ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                    "Financed Long Put", "Ratio Put Backspread")
        if recommended_structure in _bullish:
            if analyst_label == "Bullish":
                _add("Flow", "Analyst", wt, +wt,
                     "Analyst consensus Bullish — supports upside trade")
            elif analyst_label == "Bearish":
                _add("Flow", "Analyst", wt, -wt,
                     "Analyst consensus Bearish — headwind for upside trade")
        elif recommended_structure in _bearish:
            if analyst_label == "Bearish":
                _add("Flow", "Analyst", wt, +wt,
                     "Analyst consensus Bearish — confirms downside trade")
            elif analyst_label == "Bullish":
                _add("Flow", "Analyst", wt, -wt,
                     "Analyst consensus Bullish — headwind for bearish trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            if analyst_label in ("Bullish", "Bearish"):
                half_wt = wt * 0.5
                _add("Flow", "Analyst", half_wt, -half_wt,
                     f"Analyst consensus {analyst_label} — directional bias adds breakout risk to neutral trade")

    # IV Premium vs HV20
    if iv_premium is not None:
        wt = w("technical", "iv_premium")
        if iv_premium > 0.03:       # IV at least 3 ppt above HV — options are rich
            if recommended_structure in CREDIT_STRUCTURES:
                _add("Technical", "IV Premium", wt, +wt,
                     f"IV premium {iv_premium*100:+.1f}% over HV20 — selling rich options adds edge")
            else:
                _add("Technical", "IV Premium", wt, -wt,
                     f"IV premium {iv_premium*100:+.1f}% over HV20 — buying expensive options reduces edge")
        elif iv_premium < -0.03:    # IV at least 3 ppt below HV — options are cheap
            if recommended_structure not in CREDIT_STRUCTURES:
                _add("Technical", "IV Premium", wt, +wt,
                     f"IV discount {iv_premium*100:+.1f}% vs HV20 — buying cheap options adds edge")
            else:
                _add("Technical", "IV Premium", wt, -wt,
                     f"IV discount {iv_premium*100:+.1f}% vs HV20 — selling cheap options reduces edge")
        else:
            # |iv_premium| ≤ 0.03: IV near parity — applicable evidence but no pricing edge either way
            _add("Technical", "IV Premium", wt, 0, "")

    # Short interest — squeeze risk
    if short_interest is not None:
        wt = w("flow", "short_interest")
        _bullish = ("Put Credit Spread", "Call Debit Spread", "Risk Reversal",
                    "Financed Long Call", "Ratio Call Backspread")
        _bearish = ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                    "Financed Long Put", "Ratio Put Backspread")
        if short_interest > 20:
            if recommended_structure in _bullish:
                _add("Flow", "Short Interest", wt, +wt,
                     f"Short interest {short_interest:.1f}% — high short float, squeeze risk favors upside")
            elif recommended_structure in _bearish:
                _add("Flow", "Short Interest", wt, -wt,
                     f"Short interest {short_interest:.1f}% — potential squeeze is headwind for bearish trade")
        elif short_interest < 3:
            if recommended_structure in _bearish:
                _add("Flow", "Short Interest", wt, +wt,
                     f"Short interest {short_interest:.1f}% — low short float supports steady downside")

    # Vol skew — put vs call IV imbalance
    if vol_skew_pct is not None:
        wt = w("technical", "vol_skew")
        if vol_skew_pct > 5:
            if recommended_structure == "Put Credit Spread":
                _add("Technical", "Vol Skew", wt, -wt,
                     f"Vol skew +{vol_skew_pct:.1f}% — elevated put IV suggests downside fear, caution for short put")
            elif recommended_structure in ("Put Debit Spread", "Call Credit Spread"):
                _add("Technical", "Vol Skew", wt, +wt,
                     f"Vol skew +{vol_skew_pct:.1f}% — bearish skew, puts richly priced — supports bearish trade")
            elif recommended_structure == "Iron Condor":
                _add("Technical", "Vol Skew", wt, +wt,
                     f"Vol skew +{vol_skew_pct:.1f}% — selling rich put IV in condor adds edge")
        elif vol_skew_pct < -5:
            if recommended_structure == "Call Debit Spread":
                _add("Technical", "Vol Skew", wt, +wt,
                     f"Vol skew {vol_skew_pct:.1f}% — calls richer than puts, market pricing upside move")
            elif recommended_structure == "Call Credit Spread":
                _add("Technical", "Vol Skew", wt, -wt,
                     f"Vol skew {vol_skew_pct:.1f}% — elevated call IV, breakout risk for short call")

    pct    = round(score / effective_max, 4) if effective_max > 0 else 0.0
    rating = sc.score_to_rating(pct)
    return {
        "score":              round(score, 4),
        "effective_max":      round(effective_max, 4),
        "pct":                pct,
        "rating":             rating,
        "notes":              notes,
        "regime":             regime,
        "regime_explanation": sc.regime_explanation(regime),
        "weight_profile":     sc.weight_profile(regime),
        "contributions":      contributions,
    }


def _dte_quality(dte: int | None, structure_name: str) -> tuple[float, list[dict]]:
    """
    Return (multiplier, execution_notes) reflecting how well the available DTE
    matches the structure's preferred theta/gamma window from the registry.

    This is NOT a signal about the market — it is an execution quality check.
    It is applied AFTER signal alignment scoring and kept separate so that
    a DTE penalty is never confused with a market disagreement.

    The multiplier range is [0.5, 1.0]:
      - 1.0 = DTE is inside the structure's ideal window (no penalty)
      - 0.5 = gamma danger zone (DTE < 14) — theta/gamma ratio has inverted

    Registry is the authority on ideal_lo / ideal_hi via structure.dte_min /
    structure.dte_max. No strategy-specific hardcoding here.

    Future: this function can be extended to accept a per-candidate dte once
    the data model tracks candidate-specific expiries (Calendar front-leg DTE
    differs from the global dte; that distinction belongs in a future refactor).
    """
    if dte is None:
        return 1.0, []

    from config.structures import get_or_none
    st = get_or_none(structure_name)
    if st is None:
        return 1.0, []

    ideal_lo = st.dte_min or 21
    ideal_hi = st.dte_max or 50
    notes: list[dict] = []

    if ideal_lo <= dte <= ideal_hi:
        return 1.0, []

    if dte < 14:
        notes.append({
            "type":     "dte",
            "severity": "critical",
            "message":  (f"DTE {dte} is in the gamma danger zone (below 14) — "
                         f"theta/gamma ratio has inverted for {structure_name}"),
        })
        return 0.50, notes

    if dte < ideal_lo:
        # Linear interpolation: 0.50 at DTE=14, 1.0 at DTE=ideal_lo
        q = 0.50 + 0.50 * (dte - 14) / max(ideal_lo - 14, 1)
        notes.append({
            "type":     "dte",
            "severity": "warning",
            "message":  (f"DTE {dte} below preferred {ideal_lo}–{ideal_hi} window — "
                         f"gamma exposure elevated for {structure_name}"),
        })
        return round(q, 2), notes

    if dte > 60:
        notes.append({
            "type":     "dte",
            "severity": "info",
            "message":  (f"DTE {dte} above preferred {ideal_lo}–{ideal_hi} window — "
                         f"theta decay slow; more directional drift exposure"),
        })
        return 0.80, notes

    # ideal_hi < dte ≤ 60: slightly long but not excessive
    notes.append({
        "type":     "dte",
        "severity": "info",
        "message":  (f"DTE {dte} above preferred {ideal_lo}–{ideal_hi} window — "
                     f"acceptable but not optimal for {structure_name}"),
    })
    return 0.90, notes


def select_structure(
    iv_env:     str,
    trend:      str,
    signals:    dict,
    regime:     str,
    dte:        int | None = None,
    min_pct:    float | None = None,
    min_margin: float | None = None,
) -> dict:
    """
    Choose the best-fitting structure for the current market conditions.

    Pipeline (order matters):
      1. Candidate shortlist — registry lookup: (iv_env, trend) → candidate names
      2. Signal alignment — compute_signal_alignment() per candidate; pct = score /
         effective_max (comparable across structures with different applicable checks)
      3. DTE quality — _dte_quality() multiplier applied AFTER alignment; keeps
         'the market looks good' separate from 'this contract is well-timed'
      4. trade_quality_pct = signal pct × dte_quality — ranking and gates use this
      5. No Trade gate — winner.trade_quality_pct < min_alignment_pct
      6. Near-tie gate — both candidates must clear min_pct; only then is a small
         gap meaningful (a weak market with two marginal candidates is not a near-tie)

    Output separates two distinct questions:
      signal_alignment   — what does the market say about the structure?
      execution_quality  — is this specific contract well-timed?

    Args:
        signals:    kwargs for compute_signal_alignment() (minus structure + regime)
        dte:        days-to-expiry of the selected front contract; used for DTE quality.
                    None = skip DTE adjustment (preliminary call before options data).
        min_pct:    minimum trade_quality_pct to recommend any trade.
                    Defaults to scoring.toml gates.min_alignment_pct.
        min_margin: gap below which two candidates are flagged as a near-tie.
                    Defaults to scoring.toml gates.near_tie_margin.
    """
    from config.structures import STRUCTURE_CANDIDATES

    if min_pct is None:
        min_pct = sc.gate("min_alignment_pct") or 0.15
    if min_margin is None:
        min_margin = sc.gate("near_tie_margin") or 0.05

    candidate_names = STRUCTURE_CANDIDATES.get((iv_env, trend), [])
    if not candidate_names:
        return {
            "structure":        "No Trade",
            "signal_alignment": None,
            "execution_quality": None,
            "alignment":        None,
            "candidates":       [],
            "near_tie":         False,
            "near_tie_with":    None,
        }

    scored: list[dict] = []
    for name in candidate_names:
        alignment            = compute_signal_alignment(name, **signals, regime=regime)
        dq, exec_notes       = _dte_quality(dte, name)
        trade_quality_pct    = round(alignment["pct"] * dq, 4)

        scored.append({
            "structure": name,

            # signal_alignment: what the market says — never touched by DTE
            "signal_alignment": {
                "pct":    alignment["pct"],
                "rating": alignment["rating"],
            },

            # execution_quality: how well this specific contract is timed
            "execution_quality": {
                "dte_quality":       dq,
                "trade_quality_pct": trade_quality_pct,
                "notes":             exec_notes,
                # future: liquidity_quality, earnings_quality multiply here
            },

            # trade_quality_pct at top level for sort/gate convenience
            "trade_quality_pct": trade_quality_pct,

            # full alignment detail preserved for contributions / explainability
            "alignment": alignment,
        })

    scored.sort(key=lambda x: x["trade_quality_pct"], reverse=True)

    winner    = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    no_trade  = winner["trade_quality_pct"] < min_pct

    # Near-tie requires BOTH candidates to be genuinely viable (each clears min_pct).
    # Winner at 16%, runner at 15% is a weak setup, not structural ambiguity.
    near_tie = (
        not no_trade
        and runner_up is not None
        and runner_up["trade_quality_pct"] >= min_pct
        and (winner["trade_quality_pct"] - runner_up["trade_quality_pct"]) < min_margin
    )

    return {
        "structure":         "No Trade" if no_trade else winner["structure"],
        "signal_alignment":  None if no_trade else winner["signal_alignment"],
        "execution_quality": None if no_trade else winner["execution_quality"],
        "alignment":         None if no_trade else winner["alignment"],
        "candidates":        scored,
        "near_tie":          near_tie,
        "near_tie_with":     runner_up["structure"] if near_tie else None,
    }


# ---------- "Best EV" search for the rulebook-recommended structure ----------
# These scan a wider grid of strikes/widths than the fixed delta-range pick used
# above, so that the recommended structure for this ticker can be presented with
# whatever strike combination currently has the highest expected value - even if
# that combination falls outside the default delta range or width target.
from config.rules import (
    CREDIT_DELTA_GRID, WIDTH_GRID,
    DEBIT_LONG_DELTA_GRID, DEBIT_SHORT_DELTA_GRID,
    BREAKEVEN_CUSHION_IV_SCALE,
)


def _valid_price(x):
    return x is not None and not pd.isna(x) and x > 0


def optimize_credit_spread(df, option_type, min_oi=0, min_profit_amount=0, max_loss_limit=None):
    """Search short-delta/width combos for the credit spread with the highest EV.

    Selection priority (highest EV within each tier):
      1. combos whose credit clears min_profit_amount AND max_loss (width-credit)
         is within max_loss_limit
      2. combos whose max_loss is within max_loss_limit (regardless of profit)
      3. overall highest-EV combo (any max_loss)
    Sets met_min_profit / met_max_loss on the returned combo so callers can
    flag when a tier was relaxed."""
    best_full = None
    best_capped = None
    best_overall = None
    for sd in CREDIT_DELTA_GRID:
        short_row = find_short_strike(df, option_type, (max(sd - 0.03, 0.01), sd + 0.03), min_oi)
        if short_row is None or not _valid_price(short_row["bid"]):
            continue
        for w in WIDTH_GRID:
            long_row = find_long_strike_for_credit_spread(df, short_row, option_type, w, min_oi)
            if long_row is None or not _valid_price(long_row["ask"]):
                continue
            credit = short_row["bid"] - long_row["ask"]
            width = abs(short_row["strike"] - long_row["strike"])
            if credit <= 0 or width <= 0:
                continue
            max_loss = width - credit
            pop, ev = pop_ev_credit(short_row["delta"], credit, width)
            pct = credit / width * 100
            entry = {"short": short_row, "long": long_row, "credit": credit,
                     "width": width, "pop": pop, "ev": ev, "pct": pct, "max_loss": max_loss}
            if best_overall is None or ev > best_overall["ev"]:
                best_overall = entry
            within_loss = max_loss_limit is None or max_loss <= max_loss_limit
            if within_loss and (best_capped is None or ev > best_capped["ev"]):
                best_capped = entry
            if within_loss and credit >= min_profit_amount and (best_full is None or ev > best_full["ev"]):
                best_full = entry
    if best_full is not None:
        best_full["met_min_profit"] = True
        best_full["met_max_loss"] = True
        return best_full
    if best_capped is not None:
        best_capped["met_min_profit"] = best_capped["credit"] >= min_profit_amount
        best_capped["met_max_loss"] = True
        return best_capped
    if best_overall is not None:
        best_overall["met_min_profit"] = best_overall["credit"] >= min_profit_amount
        best_overall["met_max_loss"] = (max_loss_limit is None or best_overall["max_loss"] <= max_loss_limit)
        return best_overall
    return None


def optimize_debit_spread(df, option_type, min_oi=0, min_profit_amount=0, max_loss_limit=None):
    """Search long/short-delta combos for the debit spread with the highest EV.

    Selection priority (highest EV within each tier):
      1. combos whose max profit (width-debit) clears min_profit_amount AND
         max_loss (the debit paid) is within max_loss_limit
      2. combos whose max_loss is within max_loss_limit (regardless of profit)
      3. overall highest-EV combo (any max_loss)
    Sets met_min_profit / met_max_loss on the returned combo so callers can
    flag when a tier was relaxed."""
    best_full = None
    best_capped = None
    best_overall = None
    for ld in DEBIT_LONG_DELTA_GRID:
        long_row = find_short_strike(df, option_type, (max(ld - 0.03, 0.01), ld + 0.03), min_oi)
        if long_row is None or not _valid_price(long_row["ask"]):
            continue
        for sd in DEBIT_SHORT_DELTA_GRID:
            short_row = find_short_strike(df, option_type, (max(sd - 0.03, 0.01), sd + 0.03), min_oi)
            if short_row is None or not _valid_price(short_row["bid"]):
                continue
            if option_type == "call" and short_row["strike"] <= long_row["strike"]:
                continue
            if option_type == "put" and short_row["strike"] >= long_row["strike"]:
                continue
            debit = long_row["ask"] - short_row["bid"]
            width = abs(short_row["strike"] - long_row["strike"])
            if debit <= 0 or width <= 0 or debit >= width:
                continue
            max_loss = debit
            pop, ev = pop_ev_debit(long_row["delta"], debit, width)
            entry = {"long": long_row, "short": short_row, "debit": debit,
                     "width": width, "pop": pop, "ev": ev, "max_loss": max_loss}
            if best_overall is None or ev > best_overall["ev"]:
                best_overall = entry
            within_loss = max_loss_limit is None or max_loss <= max_loss_limit
            if within_loss and (best_capped is None or ev > best_capped["ev"]):
                best_capped = entry
            if within_loss and (width - debit) >= min_profit_amount and (best_full is None or ev > best_full["ev"]):
                best_full = entry
    if best_full is not None:
        best_full["met_min_profit"] = True
        best_full["met_max_loss"] = True
        return best_full
    if best_capped is not None:
        best_capped["met_min_profit"] = (best_capped["width"] - best_capped["debit"]) >= min_profit_amount
        best_capped["met_max_loss"] = True
        return best_capped
    if best_overall is not None:
        best_overall["met_min_profit"] = (best_overall["width"] - best_overall["debit"]) >= min_profit_amount
        best_overall["met_max_loss"] = (max_loss_limit is None or best_overall["max_loss"] <= max_loss_limit)
        return best_overall
    return None


def analyze_ticker(ticker, params=None, regime: str = "chop"):
    p = get_params(params)
    result = {"ticker": ticker, "risk_free_rate": RISK_FREE_RATE}

    # Set all analysis fields to defaults upfront — they'll be overwritten if full analysis succeeds
    result["trend"] = "Range-bound"
    result["iv_env"] = "Low"
    result["signal_rating"] = "Neutral"
    result["recommended_structure"] = "No Trade"
    result["adx"] = None
    result["rsi"] = None
    result["pcr"] = None
    result["rel_volume"] = None
    result["macd_trend"] = None

    tkr = yf.Ticker(ticker)

    hist = get_price_history(ticker)
    if hist.empty:
        result["status"] = "No price data"
        return result
    close_series = hist["Close"].dropna()
    spot = close_series.iloc[-1]
    result["spot"] = round(spot, 2)
    if len(close_series) >= 2:
        prev_close = float(close_series.iloc[-2])
        price_chg  = round(spot - prev_close, 2)
        price_chg_pct = round((spot - prev_close) / prev_close * 100, 2) if prev_close else 0.0
    else:
        prev_close = None; price_chg = None; price_chg_pct = None
    result["prev_close"]      = round(prev_close, 2) if prev_close else None
    result["price_change"]    = price_chg
    result["price_change_pct"] = price_chg_pct

    # Override spot with live E*TRADE quote when authenticated (real-time vs daily close)
    live_q = get_live_spot(ticker)
    if live_q and live_q.get("last"):
        live_spot = live_q["last"]
        result["spot"] = round(live_spot, 2)
        if prev_close:
            result["price_change"]     = round(live_spot - prev_close, 2)
            result["price_change_pct"] = round((live_spot - prev_close) / prev_close * 100, 2)
        spot = live_spot   # use live price for strike selection and BS calculations below

    # Calculate basic analysis metrics so they're always available (even for single-leg positions)
    trend = get_trend(hist, p["sma_short"], p["sma_long"], p["trend_band_pct"])
    result["trend"] = trend

    rsi = get_rsi(hist)
    result["rsi"] = rsi

    macd = get_macd(hist)
    result["macd_hist"] = macd["hist"]
    macd_trend = macd["trend"]
    result["macd_trend"] = macd_trend

    adx = get_adx(hist)
    result["adx"] = adx

    rel_volume = get_relative_volume(hist)
    result["rel_volume"] = rel_volume

    # Basic IV determination from price history (before we know if options exist)
    iv_rank = get_iv_rank_proxy(hist, 0.25, ticker=ticker)  # fallback vol
    result["iv_rank_proxy"] = iv_rank
    iv_env = "High" if (iv_rank is not None and iv_rank >= p["iv_rank_high_threshold"]) else "Low"
    result["iv_env"] = iv_env

    # Preliminary structure — signals not yet available; select_structure() replaces this below
    recommended_structure = STRUCTURE_MATRIX.get((iv_env, trend), "No Trade")
    result["recommended_structure"] = recommended_structure

    expiry, dte = pick_expiry(tkr, p["min_dte"], p["max_dte"])
    result["expiry"] = expiry
    result["dte"] = dte

    if expiry is None:
        result["status"] = "No option chain data (no expirations found)"
        result["candidates"] = []
        return result

    # Event blackout check
    dte_earn = days_to_earnings(tkr)
    if dte_earn is not None and 0 <= dte_earn <= p["event_blackout_days"]:
        result["status"] = f"SKIP - earnings in {dte_earn}d (within blackout window)"
        result["candidates"] = []
        return result
    # Fetch all independent I/O-bound data in parallel — cuts per-ticker latency ~50%
    with _TPE(max_workers=4) as _io:
        _f_weekly   = _io.submit(get_weekly_trend, ticker, p["sma_short"], p["sma_long"], p["trend_band_pct"])
        _f_ema200   = _io.submit(get_ema200, hist)
        _f_news     = _io.submit(get_news_sentiment, tkr)
        _f_analyst  = _io.submit(get_analyst_sentiment, tkr)
        _f_div      = _io.submit(get_dividend_info, tkr)
        _f_short    = _io.submit(get_short_interest, tkr)
        weekly_trend            = _f_weekly.result()
        ema200_val, ema200_position = _f_ema200.result()
        news                    = _f_news.result()
        analyst                 = _f_analyst.result()
        div_info                = _f_div.result()
        short_interest          = _f_short.result()

    result["weekly_trend"]    = weekly_trend
    result["ema200"]          = ema200_val
    result["ema200_position"] = ema200_position

    result["news_sentiment"]  = news["sentiment"]
    result["news_headlines"]  = news["headlines"]
    result["news_bullish"]    = news["bullish_count"]
    result["news_bearish"]    = news["bearish_count"]

    result["analyst_buy"]       = analyst["buy"]
    result["analyst_hold"]      = analyst["hold"]
    result["analyst_sell"]      = analyst["sell"]
    result["analyst_net_score"] = analyst["net_score"]
    result["analyst_label"]     = analyst["label"]

    result["div_ex_date"]    = div_info["ex_date"]
    result["div_days_to_ex"] = div_info["days_to_ex"]
    result["div_yield"]      = div_info["annual_yield"]
    _div_in_window = (div_info["days_to_ex"] is not None
                      and 0 <= div_info["days_to_ex"] <= dte + sc.gate("dividend_blackout_days"))
    result["div_in_window"]  = _div_in_window

    result["short_interest"] = short_interest

    calls, puts = get_option_chain(tkr, expiry, spot=spot, dte=dte)
    if calls.empty or puts.empty:
        result["status"] = "No option chain data"
        result["candidates"] = []
        return result

    # Vol skew (25-delta approximation)
    vol_skew_data = get_vol_skew(calls, puts, spot)
    result["vol_skew_put_iv"]  = vol_skew_data["put_iv"]  if vol_skew_data else None
    result["vol_skew_call_iv"] = vol_skew_data["call_iv"] if vol_skew_data else None
    result["vol_skew_pct"]     = vol_skew_data["skew_pct"] if vol_skew_data else None

    # Bid-ask quality gate per leg
    bid_ask_gate = sc.gate("bid_ask_max_pct")

    def _ba_ok(row):
        try:
            bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
            mid = (bid + ask) / 2
            return mid > 0 and (ask - bid) / mid <= bid_ask_gate
        except Exception:
            return True  # don't block if data missing

    calls["ba_ok"] = calls.apply(_ba_ok, axis=1)
    puts["ba_ok"]  = puts.apply(_ba_ok, axis=1)

    flow = get_options_flow(ticker, expiry, calls, puts)
    result["pcr"] = flow["pcr"]
    result["pcr_sentiment"] = flow["pcr_sentiment"]
    result["unusual_activity"] = flow["unusual_activity"]
    result["vol_oi_ratio"] = flow["vol_oi_ratio"]
    result["call_oi"] = flow["call_oi"]
    result["put_oi"] = flow["put_oi"]
    result["call_vol"] = flow["call_vol"]
    result["put_vol"] = flow["put_vol"]
    result["oi_delta_calls"] = flow["oi_delta_calls"]
    result["oi_delta_puts"] = flow["oi_delta_puts"]

    # Back-month expiry — needed for Calendar, Diagonal, and IV term structure
    back_expiry, back_dte = pick_back_expiry(
        tkr, expiry, p["calendar_min_gap_days"], p["calendar_max_gap_days"]
    )
    result["back_expiry"] = back_expiry
    result["back_dte"] = back_dte

    # IV term structure (requires both expiries)
    iv_ts = None
    if back_expiry:
        iv_ts = get_iv_term_structure(tkr, expiry, back_expiry, spot, dte, back_dte)
    result["iv_term_shape"]  = iv_ts["shape"]  if iv_ts else None
    result["iv_term_note"]   = iv_ts["note"]   if iv_ts else None
    result["iv_front_iv"]    = iv_ts["front_iv"] if iv_ts else None
    result["iv_back_iv"]     = iv_ts["back_iv"]  if iv_ts else None
    result["iv_edge_pct"]    = iv_ts["edge_pct"] if iv_ts else None

    atm_iv = get_atm_iv(calls, puts, spot)
    result["atm_iv"] = round(atm_iv, 4)

    # HV20 and IV premium/discount (Phase C)
    # If option chain returned 0 IV (E*TRADE greeks unavailable or market closed),
    # fall back to hv20 so downstream metrics aren't all zero.
    # Only call once: derive effective_iv first using a seed call only when atm_iv is near-zero.
    if atm_iv > 1e-3:
        effective_iv = atm_iv
    else:
        _hv_seed = get_hv_and_iv_premium(hist, 0.01, window=20)
        effective_iv = (_hv_seed["hv20"] if _hv_seed else None) or 0.01
    hv_data = get_hv_and_iv_premium(hist, effective_iv, window=20)
    result["hv20"]        = hv_data["hv20"]       if hv_data else None
    result["iv_premium"]  = hv_data["iv_premium"]  if hv_data else None
    result["iv_discount"] = hv_data["iv_discount"] if hv_data else None
    result["iv_hv_ratio"] = hv_data["iv_hv_ratio"] if hv_data else None

    # ── Phase 1 market metrics (zero new API dependencies) ───────────────────
    spy_hist = get_price_history("SPY")
    result["beta_60d"]         = get_beta(hist, spy_hist, window=60)
    result["atr_pct"]          = get_atr_pct(hist, spot)
    result["iv_rank_52w"]      = get_iv_rank_52w(ticker, atm_iv)
    result["max_pain_strike"]  = get_max_pain(calls, puts)
    result["oi_concentration"] = get_oi_concentration(calls, puts, spot)
    vix_ctx = get_vix_context()
    result["vvix"]             = vix_ctx["vvix"]
    result["vix_3m"]           = vix_ctx["vix_3m"]
    result["vix_term_slope"]   = vix_ctx["vix_term_slope"]
    macro = get_macro_context(dte=dte)
    result["yield_10y"]        = macro["yield_10y"]
    result["yield_3m"]         = macro["yield_3m"]
    result["yield_curve"]      = macro["yield_curve"]
    result["fed_within_dte"]   = macro["fed_within_dte"]
    result["cpi_within_dte"]   = macro["cpi_within_dte"]
    # ─────────────────────────────────────────────────────────────────────────

    iv_rank = get_iv_rank_proxy(hist, atm_iv, ticker=ticker)
    result["iv_rank_proxy"] = iv_rank
    iv_env = "High" if (iv_rank is not None and iv_rank >= p["iv_rank_high_threshold"]) else "Low"
    result["iv_env"] = iv_env

    _signals = dict(
        trend=trend, weekly_trend=weekly_trend, rsi=rsi,
        macd_trend=macd_trend, news_sentiment=news["sentiment"],
        adx=adx, rel_volume=rel_volume,
        pcr=flow["pcr"], pcr_sentiment=flow["pcr_sentiment"],
        ema200_position=ema200_position,
        iv_term_shape=iv_ts["shape"] if iv_ts else None,
        short_interest=short_interest,
        vol_skew_pct=vol_skew_data["skew_pct"] if vol_skew_data else None,
        analyst_label=analyst["label"],
        iv_premium=hv_data["iv_premium"] if hv_data else None,
    )
    selection = select_structure(iv_env, trend, _signals, regime, dte=dte)
    recommended_structure = selection["structure"]
    result["recommended_structure"] = recommended_structure

    _sa  = selection["signal_alignment"]  or {}   # market says…
    _eq  = selection["execution_quality"] or {}   # contract timing…
    _aln = selection["alignment"]         or {}   # full detail

    result["regime"]                = regime
    result["signal_score"]          = _aln.get("score")
    result["signal_pct"]            = _sa.get("pct")
    result["signal_rating"]         = _sa.get("rating")
    result["signal_notes"]          = _aln.get("notes", [])
    result["trade_quality_pct"]     = _eq.get("trade_quality_pct")
    result["dte_quality"]           = _eq.get("dte_quality")
    result["execution_notes"]       = _eq.get("notes", [])
    result["signal_candidates"]     = selection["candidates"]
    result["signal_near_tie"]       = selection["near_tie"]
    result["signal_near_tie_with"]  = selection["near_tie_with"]

    T = dte / 365.0
    _hv_fallback = result.get("hv20") or None
    calls = add_deltas(calls, spot, T, "call", fallback_vol=_hv_fallback)
    puts  = add_deltas(puts,  spot, T, "put",  fallback_vol=_hv_fallback)

    width_target = rules.CAPITAL * p["max_risk_pct"] / 100  # rough width in $ per contract
    min_oi = p["min_open_interest"]
    min_profit_amount = p["min_profit_amount"]
    candidates = []

    # --- Put Credit Spread ---
    short_put = find_short_strike(puts, "put", (p["credit_short_delta_lo"], p["credit_short_delta_hi"]), min_oi)
    long_put = find_long_strike_for_credit_spread(puts, short_put, "put", width_target, min_oi) if short_put is not None else None
    put_leg_liquid = (short_put is not None and long_put is not None
                       and short_put["bid"] > 0 and long_put["ask"] > 0)
    if short_put is not None and long_put is not None and not put_leg_liquid:
        candidates.append({
            "structure": "Put Credit Spread", "recommended": recommended_structure == "Put Credit Spread",
            "details": "Illiquid (no bid/ask) on one or both legs - cannot price reliably", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })
    elif short_put is not None and long_put is not None:
        credit_put = short_put["bid"] - long_put["ask"]
        width_put = short_put["strike"] - long_put["strike"]
        if credit_put <= 0:
            candidates.append({
                "structure": "Put Credit Spread", "recommended": recommended_structure == "Put Credit Spread",
                "details": (f"SELL {short_put['strike']}P / BUY {long_put['strike']}P - "
                             f"wide bid/ask spread gives net credit ~${credit_put:.2f} (<=0), not tradeable as a credit spread"),
                "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            })
        else:
            pct = credit_put / width_put * 100 if width_put else 0
            pop, ev = pop_ev_credit(short_put["delta"], credit_put, width_put)
            meets_profit, profit_msg = profit_note(credit_put, min_profit_amount)
            max_loss_put = width_put - credit_put
            meets_loss, loss_msg = loss_note(max_loss_put, width_target)
            _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, long_put, short_put, "put")
            candidates.append({
                "structure": "Put Credit Spread",
                "recommended": recommended_structure == "Put Credit Spread",
                "details": (f"SELL {short_put['strike']}P / BUY {long_put['strike']}P "
                             f"(short delta {short_put['delta']:.2f}, credit ~${credit_put:.2f}, "
                             f"width ${width_put:.0f}, credit/width {pct:.0f}%, need >={p['credit_min_pct_of_width']*100:.0f}%, "
                             f"{profit_msg}, {loss_msg})"),
                "pop": pop, "ev": ev, "max_profit": round(credit_put, 3), "meets_min_profit": meets_profit,
                "max_loss": round(max_loss_put, 3), "meets_max_loss": meets_loss,
                "net_delta": _nd, "net_theta": _nth, "net_gamma": _ngm, "net_vega": _nvg,
                "is_credit": True,
                "long_strike": long_put["strike"], "short_strike": short_put["strike"],
                "spot_at_entry": round(spot, 2),
            })
    else:
        candidates.append({
            "structure": "Put Credit Spread", "recommended": recommended_structure == "Put Credit Spread",
            "details": "No strikes found in target delta range", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })

    # --- Cash Secured Put ---
    # Reuses short_put from PCS section (same delta target). Single naked short put,
    # cash-secured meaning broker holds strike * 100 as collateral.
    if short_put is not None and _valid_price(short_put["bid"]):
        csp_credit   = short_put["bid"]
        csp_breakeven = round(short_put["strike"] - csp_credit, 2)
        csp_max_loss  = csp_breakeven   # worst case: stock → 0, lose strike - credit
        csp_pop       = round((1.0 - abs(float(short_put.get("delta") or 0))) * 100, 1)
        meets_profit, profit_msg = profit_note(csp_credit, min_profit_amount)
        # Greeks: short put — flip sign vs long put
        _csp_iv = float(short_put.get("impliedVolatility") or 0)
        _csp_th = round(-(bs_theta(spot, short_put["strike"], T, RISK_FREE_RATE, _csp_iv, "put") if _csp_iv > 0 else 0.0), 4)
        _csp_gm = round(-(bs_gamma(spot, short_put["strike"], T, RISK_FREE_RATE, _csp_iv) if _csp_iv > 0 else 0.0), 4)
        _csp_vg = round(-(bs_vega(spot,  short_put["strike"], T, RISK_FREE_RATE, _csp_iv) if _csp_iv > 0 else 0.0), 4)
        _csp_nd = round(-float(short_put.get("delta") or 0), 3)
        candidates.append({
            "structure":       "Cash Secured Put",
            "recommended":     recommended_structure in ("Put Credit Spread",) and iv_env == "High",
            "details":         (f"SELL {short_put['strike']}P (delta {short_put['delta']:.2f}, "
                                f"credit ~${csp_credit:.2f}, breakeven ${csp_breakeven:.2f}, "
                                f"max loss ${csp_max_loss:.2f}/share if stock → 0, {profit_msg})"),
            "pop":             csp_pop,
            "ev":              None,   # unbounded downside — EV not meaningful
            "max_profit":      round(csp_credit, 3),
            "meets_min_profit":meets_profit,
            "max_loss":        round(csp_max_loss, 3),
            "meets_max_loss":  None,   # undefined — no spread width to compare against
            "net_delta":       _csp_nd,
            "net_theta":       _csp_th,
            "net_gamma":       _csp_gm,
            "net_vega":        _csp_vg,
            "is_credit":       True,
            "short_strike":    short_put["strike"],
            "spot_at_entry":   round(spot, 2),
        })
    else:
        candidates.append({
            "structure": "Cash Secured Put", "recommended": False,
            "details": "No liquid short put found in target delta range", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })

    # --- Covered Call ---
    # Reuses short_call from CCS section (same delta target). Single call sold against owned shares.
    # Collects premium but caps upside at the short strike (shares may be called away).
    # Only show if we have a valid short call strike
    short_call_cc = find_short_strike(calls, "call", (p["credit_short_delta_lo"], p["credit_short_delta_hi"]), min_oi)
    if short_call_cc is not None and _valid_price(short_call_cc["bid"]):
        cc_credit    = short_call_cc["bid"]
        cc_breakeven = round(short_call_cc["strike"] - cc_credit, 2)  # cost basis minus premium
        cc_max_profit = round(short_call_cc["strike"] - spot + cc_credit, 2)  # profit if called away
        cc_pop       = round((1.0 - abs(float(short_call_cc.get("delta") or 0))) * 100, 1)  # probability call expires OTM
        meets_profit, profit_msg = profit_note(cc_credit, min_profit_amount)
        # Greeks: short call — flip sign vs long call
        _cc_iv = float(short_call_cc.get("impliedVolatility") or 0)
        _cc_th = round(-(bs_theta(spot, short_call_cc["strike"], T, RISK_FREE_RATE, _cc_iv, "call") if _cc_iv > 0 else 0.0), 4)
        _cc_gm = round(-(bs_gamma(spot, short_call_cc["strike"], T, RISK_FREE_RATE, _cc_iv) if _cc_iv > 0 else 0.0), 4)
        _cc_vg = round(-(bs_vega(spot,  short_call_cc["strike"], T, RISK_FREE_RATE, _cc_iv) if _cc_iv > 0 else 0.0), 4)
        _cc_nd = round(-float(short_call_cc.get("delta") or 0), 3)
        candidates.append({
            "structure":       "Covered Call",
            "recommended":     recommended_structure == "Covered Call",
            "details":         (f"SELL {short_call_cc['strike']}C (delta {short_call_cc['delta']:.2f}, "
                                f"credit ~${cc_credit:.2f}, max profit if called ${cc_max_profit:.2f}/share, "
                                f"breakeven ${cc_breakeven:.2f}, {profit_msg})"),
            "pop":             cc_pop,
            "ev":              None,   # path-dependent on assignment — EV not straightforward
            "max_profit":      round(cc_max_profit, 3),
            "meets_min_profit": meets_profit,
            "max_loss":        None,   # unlimited above strike — no fixed max loss
            "meets_max_loss":  None,
            "net_delta":       _cc_nd,
            "net_theta":       _cc_th,
            "net_gamma":       _cc_gm,
            "net_vega":        _cc_vg,
            "is_credit":       True,
            "short_strike":    short_call_cc["strike"],
            "spot_at_entry":   round(spot, 2),
        })
    else:
        candidates.append({
            "structure": "Covered Call", "recommended": False,
            "details": "No liquid short call found in target delta range", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })

    # --- Call Credit Spread ---
    short_call = find_short_strike(calls, "call", (p["credit_short_delta_lo"], p["credit_short_delta_hi"]), min_oi)
    long_call_c = find_long_strike_for_credit_spread(calls, short_call, "call", width_target, min_oi) if short_call is not None else None
    call_leg_liquid = (short_call is not None and long_call_c is not None
                        and short_call["bid"] > 0 and long_call_c["ask"] > 0)
    if short_call is not None and long_call_c is not None and not call_leg_liquid:
        candidates.append({
            "structure": "Call Credit Spread", "recommended": recommended_structure == "Call Credit Spread",
            "details": "Illiquid (no bid/ask) on one or both legs - cannot price reliably", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })
    elif short_call is not None and long_call_c is not None:
        credit_call = short_call["bid"] - long_call_c["ask"]
        width_call = long_call_c["strike"] - short_call["strike"]
        if credit_call <= 0:
            candidates.append({
                "structure": "Call Credit Spread", "recommended": recommended_structure == "Call Credit Spread",
                "details": (f"SELL {short_call['strike']}C / BUY {long_call_c['strike']}C - "
                             f"wide bid/ask spread gives net credit ~${credit_call:.2f} (<=0), not tradeable as a credit spread"),
                "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            })
        else:
            pct = credit_call / width_call * 100 if width_call else 0
            pop, ev = pop_ev_credit(short_call["delta"], credit_call, width_call)
            meets_profit, profit_msg = profit_note(credit_call, min_profit_amount)
            max_loss_call = width_call - credit_call
            meets_loss, loss_msg = loss_note(max_loss_call, width_target)
            _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, long_call_c, short_call, "call")
            candidates.append({
                "structure": "Call Credit Spread",
                "recommended": recommended_structure == "Call Credit Spread",
                "details": (f"SELL {short_call['strike']}C / BUY {long_call_c['strike']}C "
                             f"(short delta {short_call['delta']:.2f}, credit ~${credit_call:.2f}, "
                             f"width ${width_call:.0f}, credit/width {pct:.0f}%, need >={p['credit_min_pct_of_width']*100:.0f}%, "
                             f"{profit_msg}, {loss_msg})"),
                "pop": pop, "ev": ev, "max_profit": round(credit_call, 3), "meets_min_profit": meets_profit,
                "max_loss": round(max_loss_call, 3), "meets_max_loss": meets_loss,
                "net_delta": _nd, "net_theta": _nth, "net_gamma": _ngm, "net_vega": _nvg,
                "is_credit": True,
                "long_strike": long_call_c["strike"], "short_strike": short_call["strike"],
                "spot_at_entry": round(spot, 2),
            })
    else:
        candidates.append({
            "structure": "Call Credit Spread", "recommended": recommended_structure == "Call Credit Spread",
            "details": "No strikes found in target delta range", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })

    # --- Iron Condor (combines the two credit spreads above) ---
    # OI gate: all 4 legs must clear the minimum threshold
    _ic_legs_present = (short_put is not None and long_put is not None
                        and short_call is not None and long_call_c is not None)
    _ic_leg_ois = (
        [int(short_put.get("openInterest") or 0), int(long_put.get("openInterest") or 0),
         int(short_call.get("openInterest") or 0), int(long_call_c.get("openInterest") or 0)]
        if _ic_legs_present else []
    )
    _ic_oi_ok = _ic_legs_present and (min_oi == 0 or min(_ic_leg_ois) >= min_oi)

    if _ic_legs_present and not _ic_oi_ok:
        candidates.append({
            "structure": "Iron Condor", "recommended": False,
            "details": (f"Skipped — lowest leg OI is {min(_ic_leg_ois)} "
                         f"(min required: {min_oi}). Chain too thin for a reliable Iron Condor."),
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
        })
    elif _ic_legs_present and _ic_oi_ok and not (put_leg_liquid and call_leg_liquid and all(
            r.get("ba_ok", True) for r in [short_put, long_put, short_call, long_call_c])):
        candidates.append({
            "structure": "Iron Condor", "recommended": recommended_structure == "Iron Condor",
            "details": "Illiquid or wide bid-ask on one or more legs - cannot price reliably", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })
    elif _ic_legs_present and _ic_oi_ok and (credit_put <= 0 or credit_call <= 0):
        candidates.append({
            "structure": "Iron Condor", "recommended": recommended_structure == "Iron Condor",
            "details": (f"Put credit ~${credit_put:.2f} / call credit ~${credit_call:.2f} - "
                         f"wide bid/ask spread on one or both sides gives net credit <=0, not tradeable"),
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
        })
    elif _ic_legs_present and _ic_oi_ok:
        total_credit = credit_put + credit_call
        ic_width = max(width_put, width_call)
        pct_put = credit_put / width_put * 100 if width_put else 0
        pct_call = credit_call / width_call * 100 if width_call else 0
        pop, ev = pop_ev_iron_condor(short_put["delta"], short_call["delta"], total_credit, ic_width)
        meets_profit, profit_msg = profit_note(total_credit, min_profit_amount)
        max_loss_ic = ic_width - total_credit
        meets_loss, loss_msg = loss_note(max_loss_ic, width_target)
        _pd, _pth, _pgm, _pvg = _net_greeks(spot, T, long_put,    short_put,   "put")
        _cd, _cth, _cgm, _cvg = _net_greeks(spot, T, long_call_c, short_call,  "call")
        _ic_nd  = round((_pd  or 0) + (_cd  or 0), 3)
        _ic_nth = round((_pth or 0) + (_cth or 0), 4)
        _ic_ngm = round((_pgm or 0) + (_cgm or 0), 6)
        _ic_nvg = round((_pvg or 0) + (_cvg or 0), 4)
        candidates.append({
            "structure": "Iron Condor",
            "recommended": recommended_structure == "Iron Condor",
            "details": (f"SELL {short_put['strike']}P/BUY {long_put['strike']}P + "
                         f"SELL {short_call['strike']}C/BUY {long_call_c['strike']}C "
                         f"(total credit ~${total_credit:.2f}, put {pct_put:.0f}% / call {pct_call:.0f}% of width, "
                         f"need >={p['credit_min_pct_of_width']*100:.0f}%, {profit_msg}, {loss_msg})"),
            "pop": pop, "ev": ev, "max_profit": round(total_credit, 3), "meets_min_profit": meets_profit,
            "max_loss": round(max_loss_ic, 3), "meets_max_loss": meets_loss,
            "net_delta": _ic_nd, "net_theta": _ic_nth, "net_gamma": _ic_ngm, "net_vega": _ic_nvg,
            "is_credit": True,
            "put_long_strike": long_put["strike"], "put_short_strike": short_put["strike"],
            "call_short_strike": short_call["strike"], "call_long_strike": long_call_c["strike"],
            "spot_at_entry": round(spot, 2),
        })
    elif not _ic_legs_present:
        candidates.append({
            "structure": "Iron Condor", "recommended": False,
            "details": "Missing put or call credit spread leg — no valid strikes in delta range",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
        })

    # --- Call Debit Spread ---
    long_call_d = find_short_strike(calls, "call", (p["debit_long_delta_lo"], p["debit_long_delta_hi"]), min_oi)
    short_call_d = find_short_strike(calls, "call", (p["debit_short_delta_lo"], p["debit_short_delta_hi"]), min_oi)
    if long_call_d is not None and short_call_d is not None and short_call_d["strike"] > long_call_d["strike"] \
            and (long_call_d["ask"] <= 0 or short_call_d["bid"] <= 0):
        candidates.append({
            "structure": "Call Debit Spread", "recommended": recommended_structure == "Call Debit Spread",
            "details": "Illiquid (no bid/ask) on one or both legs - cannot price reliably", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })
    elif long_call_d is not None and short_call_d is not None and short_call_d["strike"] > long_call_d["strike"]:
        debit = long_call_d["ask"] - short_call_d["bid"]
        width_d = short_call_d["strike"] - long_call_d["strike"]
        pop, ev = pop_ev_debit(long_call_d["delta"], debit, width_d)
        max_profit_d = width_d - debit
        meets_profit, profit_msg = profit_note(max_profit_d, min_profit_amount)
        meets_loss, loss_msg = loss_note(debit, width_target)
        _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, long_call_d, short_call_d, "call")
        candidates.append({
            "structure": "Call Debit Spread",
            "recommended": recommended_structure == "Call Debit Spread",
            "details": (f"BUY {long_call_d['strike']}C / SELL {short_call_d['strike']}C "
                         f"(long delta {long_call_d['delta']:.2f}, debit ~${debit:.2f}, "
                         f"width ${width_d:.0f}, {profit_msg}, {loss_msg})"),
            "pop": pop, "ev": ev, "max_profit": round(max_profit_d, 3), "meets_min_profit": meets_profit,
            "max_loss": round(debit, 3), "meets_max_loss": meets_loss,
            "net_delta": _nd, "net_theta": _nth, "net_gamma": _ngm, "net_vega": _nvg,
            "is_credit": False,
            "long_strike": long_call_d["strike"], "short_strike": short_call_d["strike"],
            "spot_at_entry": round(spot, 2),
        })
    else:
        candidates.append({
            "structure": "Call Debit Spread", "recommended": recommended_structure == "Call Debit Spread",
            "details": "No strikes found in target delta ranges", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })

    # --- Put Debit Spread ---
    long_put_d = find_short_strike(puts, "put", (p["debit_long_delta_lo"], p["debit_long_delta_hi"]), min_oi)
    short_put_d = find_short_strike(puts, "put", (p["debit_short_delta_lo"], p["debit_short_delta_hi"]), min_oi)
    if long_put_d is not None and short_put_d is not None and short_put_d["strike"] < long_put_d["strike"] \
            and (long_put_d["ask"] <= 0 or short_put_d["bid"] <= 0):
        candidates.append({
            "structure": "Put Debit Spread", "recommended": recommended_structure == "Put Debit Spread",
            "details": "Illiquid (no bid/ask) on one or both legs - cannot price reliably", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })
    elif long_put_d is not None and short_put_d is not None and short_put_d["strike"] < long_put_d["strike"]:
        debit = long_put_d["ask"] - short_put_d["bid"]
        width_d = long_put_d["strike"] - short_put_d["strike"]
        pop, ev = pop_ev_debit(long_put_d["delta"], debit, width_d)
        max_profit_d = width_d - debit
        meets_profit, profit_msg = profit_note(max_profit_d, min_profit_amount)
        meets_loss, loss_msg = loss_note(debit, width_target)
        _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, long_put_d, short_put_d, "put")
        candidates.append({
            "structure": "Put Debit Spread",
            "recommended": recommended_structure == "Put Debit Spread",
            "details": (f"BUY {long_put_d['strike']}P / SELL {short_put_d['strike']}P "
                         f"(long delta {long_put_d['delta']:.2f}, debit ~${debit:.2f}, "
                         f"width ${width_d:.0f}, {profit_msg}, {loss_msg})"),
            "pop": pop, "ev": ev, "max_profit": round(max_profit_d, 3), "meets_min_profit": meets_profit,
            "max_loss": round(debit, 3), "meets_max_loss": meets_loss,
            "net_delta": _nd, "net_theta": _nth, "net_gamma": _ngm, "net_vega": _nvg,
            "is_credit": False,
            "long_strike": long_put_d["strike"], "short_strike": short_put_d["strike"],
            "spot_at_entry": round(spot, 2),
        })
    else:
        candidates.append({
            "structure": "Put Debit Spread", "recommended": recommended_structure == "Put Debit Spread",
            "details": "No strikes found in target delta ranges", "pop": None, "ev": None,
            "max_profit": None, "meets_min_profit": None,
        })

    # --- Jade Lizard (naked short put + call credit spread) ---
    # No upside risk if total credit collected >= the call spread's width, since
    # the naked put leg carries the (undefined, below the put strike) downside risk.
    short_put_jl = find_short_strike(puts, "put", (p["jade_lizard_put_delta_lo"], p["jade_lizard_put_delta_hi"]), min_oi)
    if short_put_jl is not None and short_call is not None and long_call_c is not None and call_leg_liquid \
            and _valid_price(short_put_jl["bid"]) and credit_call > 0:
        put_credit_jl = short_put_jl["bid"]
        total_credit_jl = put_credit_jl + credit_call
        no_upside_risk = total_credit_jl >= width_call
        downside_breakeven = short_put_jl["strike"] - total_credit_jl
        pop = max(0.0, min(1.0, 1 - (abs(short_put_jl["delta"]) + abs(short_call["delta"])))) * 100
        meets_profit, profit_msg = profit_note(total_credit_jl, min_profit_amount)
        # Reg-T style margin estimate for the naked short put (the undefined-risk leg):
        # max(20% of underlying - OTM amount, 10% of strike) per share, x100 shares/contract.
        otm_amount = max(0.0, spot - short_put_jl["strike"])
        margin_per_share = max(0.20 * spot - otm_amount, 0.10 * short_put_jl["strike"])
        margin_required = margin_per_share * 100
        fits_capital = bool(margin_required <= rules.CAPITAL)
        margin_note = (f"naked put requires ~${margin_required:.0f} margin "
                        f"({'fits' if fits_capital else 'exceeds'} ${rules.CAPITAL:.0f} capital)")
        # Jade Lizard greeks: naked short put + call credit spread
        _jl_put_iv = float(short_put_jl.get("impliedVolatility") or 0)
        _jl_put_th_raw = bs_theta(spot, short_put_jl["strike"], T, RISK_FREE_RATE, _jl_put_iv, "put") if _jl_put_iv > 0 else 0.0
        _jl_put_gm_raw = bs_gamma(spot, short_put_jl["strike"], T, RISK_FREE_RATE, _jl_put_iv) if _jl_put_iv > 0 else 0.0
        _jl_put_vg_raw = bs_vega(spot,  short_put_jl["strike"], T, RISK_FREE_RATE, _jl_put_iv) if _jl_put_iv > 0 else 0.0
        _jl_nd_call, _jl_nth_call, _jl_ngm_call, _jl_nvg_call = _net_greeks(spot, T, long_call_c, short_call, "call")
        _jl_net_d  = round(-float(short_put_jl.get("delta") or 0) + (_jl_nd_call or 0), 3)
        _jl_net_th = round(-_jl_put_th_raw + (_jl_nth_call or 0), 4)
        _jl_net_gm = round(-_jl_put_gm_raw + (_jl_ngm_call or 0), 6)
        _jl_net_vg = round(-_jl_put_vg_raw + (_jl_nvg_call or 0), 4)
        candidates.append({
            "structure": "Jade Lizard",
            "recommended": (fits_capital and iv_env == "High"
                             and recommended_structure in ("Put Credit Spread", "Iron Condor")),
            "details": (f"SELL {short_put_jl['strike']}P (naked, delta {short_put_jl['delta']:.2f}) + "
                         f"SELL {short_call['strike']}C / BUY {long_call_c['strike']}C "
                         f"(total credit ~${total_credit_jl:.2f}, call spread width ${width_call:.0f}) - "
                         f"{'no upside risk' if no_upside_risk else 'upside risk: credit < call spread width'}, "
                         f"downside breakeven ${downside_breakeven:.2f} (uncapped risk below that), {profit_msg}, "
                         f"{margin_note})"),
            "pop": round(pop, 1), "ev": None,
            "max_profit": round(total_credit_jl, 3), "meets_min_profit": meets_profit,
            "max_loss": None, "meets_max_loss": None,
            "capital_required": round(margin_required, 2),
            "net_delta": _jl_net_d, "net_theta": _jl_net_th, "net_gamma": _jl_net_gm, "net_vega": _jl_net_vg,
            "is_credit": True,
        })
    else:
        candidates.append({
            "structure": "Jade Lizard", "recommended": False,
            "details": "Could not build naked short put + call credit spread legs (missing/illiquid strikes)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Risk Reversal (sell OTM put + buy OTM call) ---
    # Bullish synthetic exposure: short put finances the long call via put skew.
    # Net is usually a small credit (Low IV + put skew > call skew).
    # Requires margin for the naked short put leg.
    #
    # RISK PROFILE:
    #   Upside: unlimited (stock can go to any price above call strike)
    #   Downside: up to put_strike − net_credit per share at stock = $0 (LARGE)
    #   The −10% spot reference is NOT used because an OTM put at δ≈0.25 is
    #   typically 20-30% below spot — spot−10% is still above the put strike and
    #   shows zero loss, which would understate risk badly.
    #   Instead we show: loss at put_strike (assignment zone) and true max loss.
    _rr_put  = find_short_strike(puts,  "put",  (p["rr_put_delta_lo"],  p["rr_put_delta_hi"]),  min_oi)
    _rr_call = find_short_strike(calls, "call", (p["rr_call_delta_lo"], p["rr_call_delta_hi"]), min_oi)
    if (_rr_put is not None and _rr_call is not None
            and _valid_price(_rr_put["bid"]) and _valid_price(_rr_call["ask"])):
        _rr_put_bid  = float(_rr_put["bid"])
        _rr_call_ask = float(_rr_call["ask"])
        _rr_net      = round(_rr_put_bid - _rr_call_ask, 3)   # >0 = net credit, <0 = net debit
        _rr_is_cred  = _rr_net >= 0
        _rr_put_k    = float(_rr_put["strike"])
        _rr_call_k   = float(_rr_call["strike"])

        # Margin requirement on the short put (Reg-T style)
        _rr_otm_amt  = max(0.0, spot - _rr_put_k)
        _rr_margin   = max(0.20 * spot - _rr_otm_amt, 0.10 * _rr_put_k) * 100
        _rr_fits_cap = bool(_rr_margin <= rules.CAPITAL)
        _rr_margin_note = (f"naked put requires ~${_rr_margin:.0f} margin "
                           f"({'fits' if _rr_fits_cap else 'exceeds'} ${rules.CAPITAL:.0f} capital)")

        # Downside breakeven: stock price below which we start losing money at expiry
        _rr_downside_be = round(_rr_put_k - abs(_rr_net), 2)

        # True maximum loss: stock goes to $0 at expiry
        # P&L at $0 = 0 (call) − put_strike (short put assignment at full strike) + net_credit
        #           = net_credit − put_strike  (negative, large)
        _rr_true_max_loss = round(_rr_put_k - _rr_net, 3)  # positive = dollar loss per share

        # Upside reference: profit at +10% above spot
        _rr_ref_up  = round(spot * 1.10, 2)
        _rr_pnl_up  = round(max(0.0, _rr_ref_up - _rr_call_k) - max(0.0, _rr_put_k - _rr_ref_up) + _rr_net, 3)

        # Downside reference: loss at 20% below the put strike (assignment + crash scenario)
        # This is always below the put strike so it always shows a real loss.
        _rr_ref_dn  = round(_rr_put_k * 0.80, 2)
        _rr_pnl_dn  = round(max(0.0, _rr_ref_dn - _rr_call_k) - max(0.0, _rr_put_k - _rr_ref_dn) + _rr_net, 3)

        _rr_max_profit = max(_rr_pnl_up, abs(_rr_net) if _rr_is_cred else 0.0)
        meets_profit_rr, profit_msg_rr = profit_note(_rr_max_profit, min_profit_amount)

        # POP ≈ P(stock stays above downside breakeven) ≈ 1 − |put delta|
        _rr_pop = max(0.0, min(100.0, (1 - abs(float(_rr_put.get("delta") or 0))) * 100))

        # EV: probability-weighted over three zones using delta approximations
        # Note: EV uses reference P&L, not true extremes, to keep it comparable to other structures
        _rr_p_up  = abs(float(_rr_call.get("delta") or 0))
        _rr_p_dn  = abs(float(_rr_put.get("delta")  or 0))
        _rr_p_mid = max(0.0, 1 - _rr_p_up - _rr_p_dn)
        _rr_ev    = round(_rr_p_up * _rr_pnl_up + _rr_p_mid * _rr_net + _rr_p_dn * _rr_pnl_dn, 3) if _rr_max_profit > 0 else None

        # Greeks: short put (negative delta/gamma/vega) + long call (positive)
        _rr_put_iv  = float(_rr_put.get("impliedVolatility")  or 0)
        _rr_call_iv = float(_rr_call.get("impliedVolatility") or 0)
        _rr_nd  = round(-abs(float(_rr_put.get("delta")  or 0)) + abs(float(_rr_call.get("delta") or 0)), 3)
        _rr_nth = round(
            -(bs_theta(spot, _rr_put_k,  T, RISK_FREE_RATE, _rr_put_iv,  "put")  if _rr_put_iv  > 0 else 0.0)
            +(bs_theta(spot, _rr_call_k, T, RISK_FREE_RATE, _rr_call_iv, "call") if _rr_call_iv > 0 else 0.0),
            4)
        _rr_ngm = round(
            -(bs_gamma(spot, _rr_put_k,  T, RISK_FREE_RATE, _rr_put_iv)  if _rr_put_iv  > 0 else 0.0)
            +(bs_gamma(spot, _rr_call_k, T, RISK_FREE_RATE, _rr_call_iv) if _rr_call_iv > 0 else 0.0),
            6)
        _rr_nvg = round(
            -(bs_vega(spot, _rr_put_k,  T, RISK_FREE_RATE, _rr_put_iv)  if _rr_put_iv  > 0 else 0.0)
            +(bs_vega(spot, _rr_call_k, T, RISK_FREE_RATE, _rr_call_iv) if _rr_call_iv > 0 else 0.0),
            4)

        candidates.append({
            "structure": "Risk Reversal",
            "recommended": (_rr_fits_cap and iv_env == "Low"
                            and recommended_structure == "Risk Reversal"),
            "details": (
                f"SELL {_rr_put_k:.0f}P (naked, δ {float(_rr_put.get('delta',0)):.2f}) + "
                f"BUY {_rr_call_k:.0f}C (δ {float(_rr_call.get('delta',0)):.2f}) — "
                f"{'net credit' if _rr_is_cred else 'net debit'} ~${abs(_rr_net):.2f}, "
                f"downside breakeven ${_rr_downside_be:.2f}, "
                f"TRUE MAX LOSS if stock→$0: ${_rr_true_max_loss:.2f}/sh (${_rr_true_max_loss*100:.0f}/contract), "
                f"P&L at +10%: ${_rr_pnl_up:+.2f} / at put_k−20% (${_rr_ref_dn:.0f}): ${_rr_pnl_dn:+.2f}, "
                f"{profit_msg_rr}, {_rr_margin_note}"
            ),
            "pop": round(_rr_pop, 1),
            "ev": _rr_ev,
            "max_profit": round(_rr_max_profit, 3),
            "meets_min_profit": meets_profit_rr,
            # max_loss = true worst case (stock→$0) so Kelly sizing is appropriately conservative
            "max_loss": round(_rr_true_max_loss, 3),
            "meets_max_loss": None,   # uncapped; bypass loss-cap gate, rated on margin
            "capital_required": round(_rr_margin, 2),
            "net_delta": _rr_nd,
            "net_theta": _rr_nth,
            "net_gamma": _rr_ngm,
            "net_vega":  _rr_nvg,
            "is_credit": _rr_is_cred,
            "short_strike":       _rr_put_k,        # put strike (the risk leg)
            "long_strike":        _rr_call_k,        # call strike (the upside leg)
            "spot_at_entry":      round(spot, 2),
            "rr_net_credit":      _rr_net,           # signed: +credit, −debit
            "rr_downside_be":     _rr_downside_be,
            "rr_true_max_loss":   _rr_true_max_loss, # loss per share if stock → $0
            "rr_ref_up":          _rr_ref_up,        # spot × 1.10
            "rr_ref_dn":          _rr_ref_dn,        # put_strike × 0.80
            "rr_pnl_dn":          _rr_pnl_dn,        # P&L at ref_dn (always negative)
        })
    else:
        candidates.append({
            "structure": "Risk Reversal",
            "recommended": False,
            "details": "Could not build Risk Reversal legs (missing or illiquid put/call at target delta)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Bear Combo (bear put spread + bear call spread, 4 legs, fully defined risk) ---
    # Structure: BUY ATM-ish put + SELL OTM put + SELL OTM call + BUY far-OTM call
    # The call credit partially (or fully) offsets the put debit, keeping net cost low.
    # Max profit  = put_width − net_cost  (stock falls below OTM put at expiry)
    # Max loss    = call_width + net_cost  (stock rises above far-OTM call)
    # No naked exposure — all four legs are defined.
    _bc_long_put  = find_short_strike(puts,  "put",  (p["bc_put_long_delta_lo"],  p["bc_put_long_delta_hi"]),  min_oi)
    _bc_long_call = find_short_strike(calls, "call", (p["bc_call_long_delta_lo"], p["bc_call_long_delta_hi"]), min_oi)
    # short_put and short_call (OTM, δ 0.15-0.25) are reused from the PCS / CCS sections
    _bc_legs_ok = (
        _bc_long_put  is not None and short_put     is not None
        and _bc_long_call is not None and short_call is not None
        and _valid_price(_bc_long_put["ask"])  and _valid_price(short_put["bid"])
        and _valid_price(short_call["bid"])    and _valid_price(_bc_long_call["ask"])
        and _bc_long_put["strike"] > short_put["strike"]      # long put above short put
        and _bc_long_call["strike"] < short_call["strike"]    # long call below short call (further OTM)... wait, no
    )
    # Reclarify: bear call spread = sell lower-strike call, buy higher-strike call (cap).
    # short_call is closer to ATM (higher delta, lower strike if OTM calls); long call is further OTM (higher strike).
    # So long_call_strike > short_call_strike.
    _bc_legs_ok = (
        _bc_long_put  is not None and short_put  is not None
        and _bc_long_call is not None and short_call is not None
        and _valid_price(_bc_long_put["ask"])  and _valid_price(short_put["bid"])
        and _valid_price(short_call["bid"])    and _valid_price(_bc_long_call["ask"])
        and float(_bc_long_put["strike"])  > float(short_put["strike"])   # long put above short put (wider spread)
        and float(_bc_long_call["strike"]) > float(short_call["strike"])  # long call above short call (cap)
    )
    if _bc_legs_ok:
        _bc_lp_k  = float(_bc_long_put["strike"])
        _bc_sp_k  = float(short_put["strike"])
        _bc_sc_k  = float(short_call["strike"])
        _bc_lc_k  = float(_bc_long_call["strike"])

        _bc_put_debit   = round(float(_bc_long_put["ask"]) - float(short_put["bid"]), 3)
        _bc_call_credit = round(float(short_call["bid"]) - float(_bc_long_call["ask"]), 3)
        _bc_net_cost    = round(_bc_put_debit - _bc_call_credit, 3)   # positive = net debit
        _bc_is_credit   = _bc_net_cost < 0

        _bc_put_width  = round(_bc_lp_k - _bc_sp_k, 2)
        _bc_call_width = round(_bc_lc_k - _bc_sc_k, 2)

        _bc_max_profit = round(_bc_put_width - _bc_net_cost, 3)
        _bc_max_loss   = round(_bc_call_width + _bc_net_cost, 3)

        # Breakevens:
        #   Downside BE: long_put_k − net_cost  (profit zone = stock below this)
        #   Upside BE:   short_call_k + call_credit  (loss zone = stock above this)
        _bc_lower_be = round(_bc_lp_k - _bc_net_cost, 2)
        _bc_upper_be = round(_bc_sc_k + _bc_call_credit, 2)

        # POP: probability stock ends below the lower BE ≈ abs(long_put_delta)
        _bc_lp_delta = abs(float(_bc_long_put.get("delta") or 0))
        _bc_pop = round(_bc_lp_delta * 100, 1)
        _bc_ev  = round(_bc_pop / 100 * _bc_max_profit - (1 - _bc_pop / 100) * _bc_max_loss, 3)

        _bc_meets_profit, _bc_profit_msg = profit_note(_bc_max_profit, min_profit_amount)
        _bc_meets_loss,   _bc_loss_msg   = loss_note(_bc_max_loss, width_target)

        _bc_fits_cap = bool(_bc_max_loss <= rules.MAX_LOSS_PER_TRADE)

        # Greeks: put spread (long - short) + call spread (long - short, short on the sell side)
        _bc_pd, _bc_pth, _bc_pgm, _bc_pvg = _net_greeks(spot, T, _bc_long_put,  short_put,   "put")
        _bc_cd, _bc_cth, _bc_cgm, _bc_cvg = _net_greeks(spot, T, _bc_long_call, short_call,  "call")
        _bc_nd  = round((_bc_pd  or 0) + (_bc_cd  or 0), 3)
        _bc_nth = round((_bc_pth or 0) + (_bc_cth or 0), 4)
        _bc_ngm = round((_bc_pgm or 0) + (_bc_cgm or 0), 6)
        _bc_nvg = round((_bc_pvg or 0) + (_bc_cvg or 0), 4)

        candidates.append({
            "structure": "Bear Combo",
            "recommended": (
                _bc_fits_cap and _bc_meets_profit and _bc_meets_loss
                and recommended_structure in ("Call Credit Spread", "Put Debit Spread", "Bear Combo")
            ),
            "details": (
                f"BUY {_bc_lp_k:.0f}P / SELL {_bc_sp_k:.0f}P (put spread debit ~${_bc_put_debit:.2f}) + "
                f"SELL {_bc_sc_k:.0f}C / BUY {_bc_lc_k:.0f}C (call spread credit ~${_bc_call_credit:.2f}) — "
                f"net {'credit' if _bc_is_credit else 'debit'} ~${abs(_bc_net_cost):.2f}, "
                f"max profit ${_bc_max_profit:.2f}/sh at expiry (stock < {_bc_sp_k:.0f}), "
                f"max loss ${_bc_max_loss:.2f}/sh (stock > {_bc_lc_k:.0f}), "
                f"lower BE ${_bc_lower_be:.2f}, upper BE ${_bc_upper_be:.2f}, "
                f"{_bc_profit_msg}, {_bc_loss_msg}"
            ),
            "pop": _bc_pop, "ev": _bc_ev,
            "max_profit":       round(_bc_max_profit, 3),
            "meets_min_profit": _bc_meets_profit,
            "max_loss":         round(_bc_max_loss, 3),
            "meets_max_loss":   _bc_meets_loss,
            "net_delta": _bc_nd, "net_theta": _bc_nth, "net_gamma": _bc_ngm, "net_vega": _bc_nvg,
            "is_credit": _bc_is_credit,
            "long_put_strike":  _bc_lp_k,
            "short_put_strike": _bc_sp_k,
            "short_call_strike": _bc_sc_k,
            "long_call_strike":  _bc_lc_k,
            "bc_put_debit":    _bc_put_debit,
            "bc_call_credit":  _bc_call_credit,
            "bc_net_cost":     _bc_net_cost,
            "bc_put_width":    _bc_put_width,
            "bc_call_width":   _bc_call_width,
            "bc_lower_be":     _bc_lower_be,
            "bc_upper_be":     _bc_upper_be,
        })
    else:
        candidates.append({
            "structure": "Bear Combo",
            "recommended": False,
            "details": "Could not build Bear Combo legs (missing or illiquid put/call at target delta)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Financed Long Call (put credit spread + standalone long call) ---
    # Moderate bullish + IV contracting. The put spread credit pays for the OTM call.
    # Max loss = put_spread_width + net_cost  (always defined, no margin needed)
    # Max profit = unlimited (the long call has no cap)
    _flc_short_put  = find_short_strike(puts,  "put",  (p["flc_put_short_delta_lo"],  p["flc_put_short_delta_hi"]),  min_oi)
    _flc_long_put   = find_long_strike_for_credit_spread(puts, _flc_short_put, "put", width_target, min_oi) if _flc_short_put is not None else None
    _flc_long_call  = find_short_strike(calls, "call", (p["flc_call_long_delta_lo"],  p["flc_call_long_delta_hi"]),  min_oi)
    _flc_legs_ok = (
        _flc_short_put is not None and _flc_long_put is not None and _flc_long_call is not None
        and _valid_price(_flc_short_put["bid"]) and _valid_price(_flc_long_put["ask"])
        and _valid_price(_flc_long_call["ask"])
        and float(_flc_short_put["strike"]) > float(_flc_long_put["strike"])  # short put above long put
    )
    if _flc_legs_ok:
        _flc_sp_k  = float(_flc_short_put["strike"])
        _flc_lp_k  = float(_flc_long_put["strike"])
        _flc_lc_k  = float(_flc_long_call["strike"])

        _flc_put_credit   = round(float(_flc_short_put["bid"]) - float(_flc_long_put["ask"]), 3)
        _flc_call_debit   = round(float(_flc_long_call["ask"]), 3)
        _flc_net_cost     = round(_flc_call_debit - _flc_put_credit, 3)   # positive = net debit
        _flc_is_credit    = _flc_net_cost < 0
        _flc_put_width    = round(_flc_sp_k - _flc_lp_k, 2)

        # max_loss at stock <= long_put: put spread at full loss, call worthless
        _flc_max_loss  = round(_flc_put_width + _flc_net_cost, 3)
        # max_profit unlimited — the long call is uncapped
        # lower BE: stock enters the put spread zone
        _flc_lower_be  = round(_flc_sp_k + _flc_net_cost, 2)   # = sp_k − net_credit
        # upper BE: call covers net_cost (only if net debit; net credit means already above BE at call strike)
        _flc_upper_be  = round(_flc_lc_k + max(_flc_net_cost, 0), 2)

        _flc_meets_profit, _flc_profit_msg = True, "unlimited profit potential"
        _flc_meets_loss,   _flc_loss_msg   = loss_note(_flc_max_loss, width_target)
        _flc_fits_cap = bool(_flc_max_loss <= rules.MAX_LOSS_PER_TRADE)

        # POP ≈ prob stock ends above the put credit spread zone ≈ 1 - short_put_delta
        _flc_pop = round((1.0 - abs(float(_flc_short_put.get("delta") or 0))) * 100, 1)
        _flc_ev  = None   # unlimited upside makes simple EV undefined

        # Greeks: put spread (short_put - long_put, net short) + long call
        _flc_pd, _flc_pth, _flc_pgm, _flc_pvg = _net_greeks(spot, T, _flc_long_put,  _flc_short_put, "put")
        _flc_lc_iv = float(_flc_long_call.get("impliedVolatility") or 0)
        _flc_cd  = round(float(_flc_long_call.get("delta") or 0), 3)
        _flc_cth = round( bs_theta(spot, _flc_lc_k, T, RISK_FREE_RATE, _flc_lc_iv, "call") if _flc_lc_iv > 0 else 0.0, 4)
        _flc_cgm = round( bs_gamma(spot, _flc_lc_k, T, RISK_FREE_RATE, _flc_lc_iv)         if _flc_lc_iv > 0 else 0.0, 6)
        _flc_cvg = round( bs_vega( spot, _flc_lc_k, T, RISK_FREE_RATE, _flc_lc_iv)         if _flc_lc_iv > 0 else 0.0, 4)
        _flc_nd  = round((_flc_pd or 0) + _flc_cd,  3)
        _flc_nth = round((_flc_pth or 0) + _flc_cth, 4)
        _flc_ngm = round((_flc_pgm or 0) + _flc_cgm, 6)
        _flc_nvg = round((_flc_pvg or 0) + _flc_cvg, 4)

        candidates.append({
            "structure": "Financed Long Call",
            "recommended": (
                _flc_fits_cap and _flc_meets_loss
                and recommended_structure in ("Put Credit Spread", "Call Debit Spread", "Risk Reversal",
                                              "Financed Long Call", "Ratio Call Backspread")
            ),
            "details": (
                f"SELL {_flc_sp_k:.0f}P / BUY {_flc_lp_k:.0f}P (put spread credit ~${_flc_put_credit:.2f}) + "
                f"BUY {_flc_lc_k:.0f}C (call debit ~${_flc_call_debit:.2f}) — "
                f"net {'credit' if _flc_is_credit else 'debit'} ~${abs(_flc_net_cost):.2f}, "
                f"max loss ${_flc_max_loss:.2f}/sh, unlimited upside above ${_flc_upper_be:.2f}, "
                f"lower BE ${_flc_lower_be:.2f}, {_flc_loss_msg}"
            ),
            "pop": _flc_pop, "ev": _flc_ev,
            "max_profit":       None,      # unlimited
            "meets_min_profit": True,      # unlimited always passes
            "max_loss":         round(_flc_max_loss, 3),
            "meets_max_loss":   _flc_meets_loss,
            "net_delta": _flc_nd, "net_theta": _flc_nth, "net_gamma": _flc_ngm, "net_vega": _flc_nvg,
            "is_credit": _flc_is_credit,
            "short_put_strike": _flc_sp_k,
            "long_put_strike":  _flc_lp_k,
            "call_strike":      _flc_lc_k,
            "flc_put_credit":   _flc_put_credit,
            "flc_call_debit":   _flc_call_debit,
            "flc_net_cost":     _flc_net_cost,
            "flc_put_width":    _flc_put_width,
            "flc_lower_be":     _flc_lower_be,
            "flc_upper_be":     _flc_upper_be,
        })
    else:
        candidates.append({
            "structure": "Financed Long Call", "recommended": False,
            "details": "Could not build Financed Long Call legs (missing or illiquid strikes at target delta)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Financed Long Put (call credit spread + standalone long put) ---
    # Moderate bearish + IV contracting. Mirror of Financed Long Call.
    # Max loss = call_spread_width + net_cost  (defined, no margin)
    # Max profit = unlimited downside (the long put has no floor above $0)
    _flp_short_call = find_short_strike(calls, "call", (p["flp_call_short_delta_lo"], p["flp_call_short_delta_hi"]), min_oi)
    _flp_long_call  = find_long_strike_for_credit_spread(calls, _flp_short_call, "call", width_target, min_oi) if _flp_short_call is not None else None
    _flp_long_put   = find_short_strike(puts,  "put",  (p["flp_put_long_delta_lo"],  p["flp_put_long_delta_hi"]),  min_oi)
    _flp_legs_ok = (
        _flp_short_call is not None and _flp_long_call is not None and _flp_long_put is not None
        and _valid_price(_flp_short_call["bid"]) and _valid_price(_flp_long_call["ask"])
        and _valid_price(_flp_long_put["ask"])
        and float(_flp_long_call["strike"]) > float(_flp_short_call["strike"])
    )
    if _flp_legs_ok:
        _flp_sc_k = float(_flp_short_call["strike"])
        _flp_lc_k = float(_flp_long_call["strike"])
        _flp_lp_k = float(_flp_long_put["strike"])

        _flp_call_credit  = round(float(_flp_short_call["bid"]) - float(_flp_long_call["ask"]), 3)
        _flp_put_debit    = round(float(_flp_long_put["ask"]), 3)
        _flp_net_cost     = round(_flp_put_debit - _flp_call_credit, 3)
        _flp_is_credit    = _flp_net_cost < 0
        _flp_call_width   = round(_flp_lc_k - _flp_sc_k, 2)

        _flp_max_loss  = round(_flp_call_width + _flp_net_cost, 3)
        _flp_upper_be  = round(_flp_sc_k - _flp_net_cost, 2)   # = sc_k + net_credit
        _flp_lower_be  = round(_flp_lp_k - max(_flp_net_cost, 0), 2)  # put BE if net debit

        _flp_meets_loss, _flp_loss_msg = loss_note(_flp_max_loss, width_target)
        _flp_fits_cap = bool(_flp_max_loss <= rules.MAX_LOSS_PER_TRADE)

        _flp_pop = round((1.0 - abs(float(_flp_short_call.get("delta") or 0))) * 100, 1)
        _flp_ev  = None

        _flp_cd, _flp_cth, _flp_cgm, _flp_cvg = _net_greeks(spot, T, _flp_long_call, _flp_short_call, "call")
        _flp_lp_iv = float(_flp_long_put.get("impliedVolatility") or 0)
        _flp_pd  = round(-float(_flp_long_put.get("delta") or 0), 3)  # long put has negative delta; net positive
        _flp_pth = round( bs_theta(spot, _flp_lp_k, T, RISK_FREE_RATE, _flp_lp_iv, "put") if _flp_lp_iv > 0 else 0.0, 4)
        _flp_pgm = round( bs_gamma(spot, _flp_lp_k, T, RISK_FREE_RATE, _flp_lp_iv)         if _flp_lp_iv > 0 else 0.0, 6)
        _flp_pvg = round( bs_vega( spot, _flp_lp_k, T, RISK_FREE_RATE, _flp_lp_iv)         if _flp_lp_iv > 0 else 0.0, 4)
        _flp_nd  = round((_flp_cd or 0) + _flp_pd,  3)
        _flp_nth = round((_flp_cth or 0) + _flp_pth, 4)
        _flp_ngm = round((_flp_cgm or 0) + _flp_pgm, 6)
        _flp_nvg = round((_flp_cvg or 0) + _flp_pvg, 4)

        candidates.append({
            "structure": "Financed Long Put",
            "recommended": (
                _flp_fits_cap and _flp_meets_loss
                and recommended_structure in ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                                              "Financed Long Put", "Ratio Put Backspread")
            ),
            "details": (
                f"SELL {_flp_sc_k:.0f}C / BUY {_flp_lc_k:.0f}C (call spread credit ~${_flp_call_credit:.2f}) + "
                f"BUY {_flp_lp_k:.0f}P (put debit ~${_flp_put_debit:.2f}) — "
                f"net {'credit' if _flp_is_credit else 'debit'} ~${abs(_flp_net_cost):.2f}, "
                f"max loss ${_flp_max_loss:.2f}/sh, unlimited downside below ${_flp_lower_be:.2f}, "
                f"upper BE ${_flp_upper_be:.2f}, {_flp_loss_msg}"
            ),
            "pop": _flp_pop, "ev": _flp_ev,
            "max_profit":       None,
            "meets_min_profit": True,
            "max_loss":         round(_flp_max_loss, 3),
            "meets_max_loss":   _flp_meets_loss,
            "net_delta": _flp_nd, "net_theta": _flp_nth, "net_gamma": _flp_ngm, "net_vega": _flp_nvg,
            "is_credit": _flp_is_credit,
            "short_call_strike": _flp_sc_k,
            "long_call_strike":  _flp_lc_k,
            "put_strike":        _flp_lp_k,
            "flp_call_credit":   _flp_call_credit,
            "flp_put_debit":     _flp_put_debit,
            "flp_net_cost":      _flp_net_cost,
            "flp_call_width":    _flp_call_width,
            "flp_upper_be":      _flp_upper_be,
            "flp_lower_be":      _flp_lower_be,
        })
    else:
        candidates.append({
            "structure": "Financed Long Put", "recommended": False,
            "details": "Could not build Financed Long Put legs (missing or illiquid strikes at target delta)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Ratio Call Backspread (sell 1 near-ATM call, buy 2 OTM calls) ---
    # Best for: strong bullish + IV expanding. Entered near-zero cost (small credit typical).
    # Max profit: unlimited (2× long call exposure above the dead zone)
    # Max loss: dead zone at expiry = short_k < stock < long_k; loss = long_k − short_k − net_credit
    # Below short_k: net credit kept (all expire worthless) — secondary profit zone
    _rb_short_call = find_short_strike(calls, "call", (p["rb_short_delta_lo"], p["rb_short_delta_hi"]), min_oi)
    _rb_long_call  = find_short_strike(calls, "call", (p["rb_long_delta_lo"],  p["rb_long_delta_hi"]),  min_oi)
    _rb_call_ok = (
        _rb_short_call is not None and _rb_long_call is not None
        and _valid_price(_rb_short_call["bid"]) and _valid_price(_rb_long_call["ask"])
        and float(_rb_long_call["strike"]) > float(_rb_short_call["strike"])
    )
    if _rb_call_ok:
        _rbc_short_k  = float(_rb_short_call["strike"])
        _rbc_long_k   = float(_rb_long_call["strike"])
        _rbc_short_cr = float(_rb_short_call["bid"])     # credit received for 1× short
        _rbc_long_db  = float(_rb_long_call["ask"])      # debit paid per long call
        # 1 short call, 2 long calls
        _rbc_net_cost = round(_rbc_long_db * 2 - _rbc_short_cr, 3)  # positive = net debit, negative = net credit
        _rbc_is_credit = _rbc_net_cost < 0
        _rbc_spread_w  = round(_rbc_long_k - _rbc_short_k, 2)

        # Max loss = dead-zone width − net_credit (occurs at expiry exactly at long_k)
        _rbc_max_loss  = round(_rbc_spread_w + _rbc_net_cost, 3)   # = spread_w − net_credit
        # Upside BE: 2×(spy−long_k) = net_cost → spy = long_k + net_cost/2 (above long_k)
        _rbc_upper_be  = round(_rbc_long_k + max(_rbc_net_cost, 0) / 2, 2)
        # Downside BE (credit case): net credit is profit if stock < short_k (no BE needed below)
        # Between short_k and long_k: loss grows. BE between these = short_k + net_credit
        _rbc_dead_be   = round(_rbc_short_k - _rbc_net_cost, 2) if _rbc_net_cost < 0 else _rbc_short_k

        _rbc_meets_loss, _rbc_loss_msg = loss_note(_rbc_max_loss, width_target)
        _rbc_fits_cap = bool(_rbc_max_loss <= rules.MAX_LOSS_PER_TRADE)

        # POP rough: prob stock ends above upper_be OR below short_k (net credit case)
        _rbc_short_delta = abs(float(_rb_short_call.get("delta") or 0))
        _rbc_long_delta  = abs(float(_rb_long_call.get("delta") or 0))
        _rbc_pop = round((_rbc_long_delta + (1 - _rbc_short_delta)) * 50, 1)   # rough average

        # Greeks: net = 2×long - 1×short
        _rbc_lc_iv = float(_rb_long_call.get("impliedVolatility") or 0)
        _rbc_sc_iv = float(_rb_short_call.get("impliedVolatility") or 0)
        _rbc_nd  = round(2 * float(_rb_long_call.get("delta") or 0) - float(_rb_short_call.get("delta") or 0), 3)
        _rbc_nth = round(2 * (bs_theta(spot, _rbc_long_k, T, RISK_FREE_RATE, _rbc_lc_iv, "call") if _rbc_lc_iv > 0 else 0)
                       - (bs_theta(spot, _rbc_short_k, T, RISK_FREE_RATE, _rbc_sc_iv, "call") if _rbc_sc_iv > 0 else 0), 4)
        _rbc_ngm = round(2 * (bs_gamma(spot, _rbc_long_k, T, RISK_FREE_RATE, _rbc_lc_iv) if _rbc_lc_iv > 0 else 0)
                       - (bs_gamma(spot, _rbc_short_k, T, RISK_FREE_RATE, _rbc_sc_iv) if _rbc_sc_iv > 0 else 0), 6)
        _rbc_nvg = round(2 * (bs_vega(spot, _rbc_long_k, T, RISK_FREE_RATE, _rbc_lc_iv) if _rbc_lc_iv > 0 else 0)
                       - (bs_vega(spot, _rbc_short_k, T, RISK_FREE_RATE, _rbc_sc_iv) if _rbc_sc_iv > 0 else 0), 4)

        candidates.append({
            "structure": "Ratio Call Backspread",
            "recommended": (
                _rbc_fits_cap and _rbc_meets_loss and iv_env == "Low"
                and recommended_structure in ("Put Credit Spread", "Call Debit Spread", "Risk Reversal",
                                              "Financed Long Call", "Ratio Call Backspread")
            ),
            "details": (
                f"SELL 1× {_rbc_short_k:.0f}C / BUY 2× {_rbc_long_k:.0f}C — "
                f"net {'credit' if _rbc_is_credit else 'debit'} ~${abs(_rbc_net_cost):.2f}, "
                f"dead zone max loss ${_rbc_max_loss:.2f}/sh (stock between {_rbc_short_k:.0f}–{_rbc_long_k:.0f}), "
                f"unlimited profit above ${_rbc_upper_be:.2f}, "
                f"{'credit kept if stock < ' + str(int(_rbc_short_k)) if _rbc_is_credit else 'loss if stock < ' + str(int(_rbc_dead_be))}, "
                f"{_rbc_loss_msg}"
            ),
            "pop": _rbc_pop, "ev": None,
            "max_profit":       None,
            "meets_min_profit": True,
            "max_loss":         round(_rbc_max_loss, 3),
            "meets_max_loss":   _rbc_meets_loss,
            "net_delta": _rbc_nd, "net_theta": _rbc_nth, "net_gamma": _rbc_ngm, "net_vega": _rbc_nvg,
            "is_credit": _rbc_is_credit,
            "short_strike": _rbc_short_k,
            "long_strike":  _rbc_long_k,
            "rbc_net_cost":  _rbc_net_cost,
            "rbc_spread_w":  _rbc_spread_w,
            "rbc_upper_be":  _rbc_upper_be,
            "rbc_dead_be":   _rbc_dead_be,
            "rbc_max_loss":  _rbc_max_loss,
        })
    else:
        candidates.append({
            "structure": "Ratio Call Backspread", "recommended": False,
            "details": "Could not build Ratio Call Backspread legs (missing or illiquid calls at target delta)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Ratio Put Backspread (sell 1 near-ATM put, buy 2 OTM puts) ---
    # Best for: strong bearish + IV expanding. Mirror of Ratio Call Backspread.
    # Max profit: unlimited downside (2× long put exposure below the dead zone)
    # Max loss: dead zone = long_k < stock < short_k; loss = short_k − long_k − net_credit
    _rb_short_put = find_short_strike(puts, "put", (p["rb_short_delta_lo"], p["rb_short_delta_hi"]), min_oi)
    _rb_long_put2 = find_short_strike(puts, "put", (p["rb_long_delta_lo"],  p["rb_long_delta_hi"]),  min_oi)
    _rb_put_ok = (
        _rb_short_put is not None and _rb_long_put2 is not None
        and _valid_price(_rb_short_put["bid"]) and _valid_price(_rb_long_put2["ask"])
        and float(_rb_short_put["strike"]) > float(_rb_long_put2["strike"])
    )
    if _rb_put_ok:
        _rbp_short_k  = float(_rb_short_put["strike"])
        _rbp_long_k   = float(_rb_long_put2["strike"])
        _rbp_short_cr = float(_rb_short_put["bid"])
        _rbp_long_db  = float(_rb_long_put2["ask"])
        _rbp_net_cost  = round(_rbp_long_db * 2 - _rbp_short_cr, 3)
        _rbp_is_credit = _rbp_net_cost < 0
        _rbp_spread_w  = round(_rbp_short_k - _rbp_long_k, 2)

        _rbp_max_loss  = round(_rbp_spread_w + _rbp_net_cost, 3)
        # Downside BE (below long_k): 2×(long_k−spy) − short_credit = net_cost → spy = long_k − net_cost/2
        _rbp_lower_be  = round(_rbp_long_k - max(_rbp_net_cost, 0) / 2, 2)
        _rbp_dead_be   = round(_rbp_short_k + _rbp_net_cost, 2) if _rbp_net_cost < 0 else _rbp_short_k

        _rbp_meets_loss, _rbp_loss_msg = loss_note(_rbp_max_loss, width_target)
        _rbp_fits_cap = bool(_rbp_max_loss <= rules.MAX_LOSS_PER_TRADE)

        _rbp_short_delta = abs(float(_rb_short_put.get("delta") or 0))
        _rbp_long_delta  = abs(float(_rb_long_put2.get("delta") or 0))
        _rbp_pop = round((_rbp_long_delta + (1 - _rbp_short_delta)) * 50, 1)

        _rbp_lp_iv = float(_rb_long_put2.get("impliedVolatility") or 0)
        _rbp_sp_iv = float(_rb_short_put.get("impliedVolatility") or 0)
        _rbp_nd  = round(2 * float(_rb_long_put2.get("delta") or 0) - float(_rb_short_put.get("delta") or 0), 3)
        _rbp_nth = round(2 * (bs_theta(spot, _rbp_long_k,  T, RISK_FREE_RATE, _rbp_lp_iv, "put") if _rbp_lp_iv > 0 else 0)
                       - (bs_theta(spot, _rbp_short_k, T, RISK_FREE_RATE, _rbp_sp_iv, "put") if _rbp_sp_iv > 0 else 0), 4)
        _rbp_ngm = round(2 * (bs_gamma(spot, _rbp_long_k,  T, RISK_FREE_RATE, _rbp_lp_iv) if _rbp_lp_iv > 0 else 0)
                       - (bs_gamma(spot, _rbp_short_k, T, RISK_FREE_RATE, _rbp_sp_iv) if _rbp_sp_iv > 0 else 0), 6)
        _rbp_nvg = round(2 * (bs_vega(spot, _rbp_long_k,  T, RISK_FREE_RATE, _rbp_lp_iv) if _rbp_lp_iv > 0 else 0)
                       - (bs_vega(spot, _rbp_short_k, T, RISK_FREE_RATE, _rbp_sp_iv) if _rbp_sp_iv > 0 else 0), 4)

        candidates.append({
            "structure": "Ratio Put Backspread",
            "recommended": (
                _rbp_fits_cap and _rbp_meets_loss and iv_env == "Low"
                and recommended_structure in ("Call Credit Spread", "Put Debit Spread", "Bear Combo",
                                              "Financed Long Put", "Ratio Put Backspread")
            ),
            "details": (
                f"SELL 1× {_rbp_short_k:.0f}P / BUY 2× {_rbp_long_k:.0f}P — "
                f"net {'credit' if _rbp_is_credit else 'debit'} ~${abs(_rbp_net_cost):.2f}, "
                f"dead zone max loss ${_rbp_max_loss:.2f}/sh (stock between {_rbp_long_k:.0f}–{_rbp_short_k:.0f}), "
                f"unlimited profit below ${_rbp_lower_be:.2f}, "
                f"{'credit kept if stock > ' + str(int(_rbp_short_k)) if _rbp_is_credit else 'loss if stock > ' + str(int(_rbp_dead_be))}, "
                f"{_rbp_loss_msg}"
            ),
            "pop": _rbp_pop, "ev": None,
            "max_profit":       None,
            "meets_min_profit": True,
            "max_loss":         round(_rbp_max_loss, 3),
            "meets_max_loss":   _rbp_meets_loss,
            "net_delta": _rbp_nd, "net_theta": _rbp_nth, "net_gamma": _rbp_ngm, "net_vega": _rbp_nvg,
            "is_credit": _rbp_is_credit,
            "short_strike": _rbp_short_k,
            "long_strike":  _rbp_long_k,
            "rbp_net_cost":  _rbp_net_cost,
            "rbp_spread_w":  _rbp_spread_w,
            "rbp_lower_be":  _rbp_lower_be,
            "rbp_dead_be":   _rbp_dead_be,
            "rbp_max_loss":  _rbp_max_loss,
        })
    else:
        candidates.append({
            "structure": "Ratio Put Backspread", "recommended": False,
            "details": "Could not build Ratio Put Backspread legs (missing or illiquid puts at target delta)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Long Strangle (buy OTM call + buy OTM put) ---
    # Pure long-vol / large-move play. Profits from any large directional move OR IV expansion.
    # Option B: always show candidate (informational even if capital doesn't fit).
    # Option C: recommend only when total debit fits within MAX_LOSS_PER_TRADE.
    _ls_call = find_short_strike(calls, "call", (p["ls_call_delta_lo"], p["ls_call_delta_hi"]), min_oi)
    _ls_put  = find_short_strike(puts,  "put",  (p["ls_put_delta_lo"],  p["ls_put_delta_hi"]),  min_oi)
    _ls_legs_ok = (
        _ls_call is not None and _ls_put is not None
        and _valid_price(_ls_call["ask"]) and _valid_price(_ls_put["ask"])
        and float(_ls_call["strike"]) > float(_ls_put["strike"])
    )
    if _ls_legs_ok:
        _ls_call_k    = float(_ls_call["strike"])
        _ls_put_k     = float(_ls_put["strike"])
        _ls_call_debit = round(float(_ls_call["ask"]), 3)
        _ls_put_debit  = round(float(_ls_put["ask"]), 3)
        _ls_total_debit = round(_ls_call_debit + _ls_put_debit, 3)

        _ls_call_be = round(_ls_call_k + _ls_total_debit, 2)
        _ls_put_be  = round(_ls_put_k  - _ls_total_debit, 2)
        _ls_fits_cap = bool(_ls_total_debit <= rules.MAX_LOSS_PER_TRADE)

        # POP rough: prob of large move past either breakeven ≈ call_delta + put_delta
        _ls_pop = round((abs(float(_ls_call.get("delta") or 0)) + abs(float(_ls_put.get("delta") or 0))) * 100, 1)

        _ls_call_iv = float(_ls_call.get("impliedVolatility") or 0)
        _ls_put_iv  = float(_ls_put.get("impliedVolatility") or 0)
        _ls_nd  = round(float(_ls_call.get("delta") or 0) + float(_ls_put.get("delta") or 0), 3)
        _ls_nth = round((bs_theta(spot, _ls_call_k, T, RISK_FREE_RATE, _ls_call_iv, "call") if _ls_call_iv > 0 else 0)
                      + (bs_theta(spot, _ls_put_k,  T, RISK_FREE_RATE, _ls_put_iv,  "put") if _ls_put_iv  > 0 else 0), 4)
        _ls_ngm = round((bs_gamma(spot, _ls_call_k, T, RISK_FREE_RATE, _ls_call_iv) if _ls_call_iv > 0 else 0)
                      + (bs_gamma(spot, _ls_put_k,  T, RISK_FREE_RATE, _ls_put_iv)  if _ls_put_iv  > 0 else 0), 6)
        _ls_nvg = round((bs_vega(spot, _ls_call_k, T, RISK_FREE_RATE, _ls_call_iv) if _ls_call_iv > 0 else 0)
                      + (bs_vega(spot, _ls_put_k,  T, RISK_FREE_RATE, _ls_put_iv)  if _ls_put_iv  > 0 else 0), 4)

        _ls_capital_note = (f"fits within ${rules.MAX_LOSS_PER_TRADE:.0f} capital limit"
                            if _ls_fits_cap else
                            f"⚠ total debit ${_ls_total_debit:.2f}/sh (${_ls_total_debit * 100:.0f}/contract) exceeds "
                            f"${rules.MAX_LOSS_PER_TRADE:.0f} risk limit — shown informational only")

        candidates.append({
            "structure": "Long Strangle",
            "recommended": (
                _ls_fits_cap
                and iv_env == "Low"
                and (result.get("atm_iv") or 0) > 0.15
                # require at least 15% annualised IV so the strangle isn't a theta bleed on a moribund stock
            ),
            "details": (
                f"BUY {_ls_call_k:.0f}C (${_ls_call_debit:.2f}) + BUY {_ls_put_k:.0f}P (${_ls_put_debit:.2f}) — "
                f"total debit ${_ls_total_debit:.2f}/sh, "
                f"upper BE ${_ls_call_be:.2f}, lower BE ${_ls_put_be:.2f}, "
                f"profit if stock moves > ${_ls_total_debit:.2f} from strikes by expiry, "
                f"{_ls_capital_note}"
            ),
            "pop": _ls_pop, "ev": None,
            "max_profit":       None,
            "meets_min_profit": True,
            "max_loss":         round(_ls_total_debit, 3),
            "meets_max_loss":   _ls_fits_cap,
            "net_delta": _ls_nd, "net_theta": _ls_nth, "net_gamma": _ls_ngm, "net_vega": _ls_nvg,
            "is_credit": False,
            "short_strike": _ls_put_k,    # put strike (lower)
            "long_strike":  _ls_call_k,   # call strike (upper)
            "ls_call_k":    _ls_call_k,
            "ls_put_k":     _ls_put_k,
            "ls_call_debit": _ls_call_debit,
            "ls_put_debit":  _ls_put_debit,
            "ls_total_debit": _ls_total_debit,
            "ls_call_be":   _ls_call_be,
            "ls_put_be":    _ls_put_be,
            "ls_fits_cap":  _ls_fits_cap,
        })
    else:
        candidates.append({
            "structure": "Long Strangle", "recommended": False,
            "details": "Could not build Long Strangle legs (missing or illiquid OTM call/put at target delta)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            "max_loss": None, "meets_max_loss": None,
        })

    # --- Calendar Spread (sell front-month ATM call, buy same-strike back-month call) ---
    # back_expiry already fetched above for IV term structure
    if back_expiry is None:
        candidates.append({
            "structure": "Calendar Spread", "recommended": False,
            "details": (f"No back-month expiry found {p['calendar_min_gap_days']}-{p['calendar_max_gap_days']} "
                         f"days after {expiry}"),
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
        })
    else:
        back_calls, _ = get_option_chain(tkr, back_expiry, spot=spot, dte=back_dte)
        calls_by_dist = calls.copy()
        calls_by_dist["dist"] = (calls_by_dist["strike"] - spot).abs()
        front_atm = calls_by_dist.sort_values("dist").iloc[0]
        back_match = back_calls[back_calls["strike"] == front_atm["strike"]]
        if back_match.empty or not _valid_price(front_atm["bid"]) or not _valid_price(back_match.iloc[0]["ask"]):
            candidates.append({
                "structure": "Calendar Spread", "recommended": False,
                "details": (f"Could not price ATM calendar at strike {front_atm['strike']} "
                             f"({expiry} / {back_expiry}) - missing strike or bid/ask"),
                "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            })
        else:
            back_atm = back_match.iloc[0]
            debit = back_atm["ask"] - front_atm["bid"]
            T_back = back_dte / 365.0
            iv_edge = front_atm["impliedVolatility"] - back_atm["impliedVolatility"]
            edge_msg = ("favorable (front IV > back IV - selling richer near-term vol)" if iv_edge > 0
                         else "unfavorable (front IV <= back IV)")
            meets_loss, loss_msg = loss_note(debit, width_target)
            # Calendar: BUY back-month call (long_row), SELL front-month call (short_row)
            # Use short_T override so front-month uses its own T
            _cal_nd, _cal_nth, _cal_ngm, _cal_nvg = _net_greeks(spot, T_back, back_atm, front_atm, "call", short_T=T)
            candidates.append({
                "structure": "Calendar Spread",
                "recommended": recommended_structure == "No Trade",
                "details": (f"SELL {front_atm['strike']}C ({expiry}, {dte}d) / BUY {front_atm['strike']}C "
                             f"({back_expiry}, {back_dte}d) - debit ~${debit:.2f}, front IV "
                             f"{front_atm['impliedVolatility']*100:.0f}% vs back IV {back_atm['impliedVolatility']*100:.0f}% "
                             f"({edge_msg}). Max loss ~debit if price moves far from {front_atm['strike']} before "
                             f"{expiry}; profit zone is near {front_atm['strike']} as the front-month decays faster, "
                             f"{loss_msg}."),
                "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
                "max_loss": round(debit, 3), "meets_max_loss": meets_loss,
                "net_delta": _cal_nd, "net_theta": _cal_nth, "net_gamma": _cal_ngm, "net_vega": _cal_nvg,
                "is_credit": False,
            })

    # --- Diagonal Spread ---
    # Bullish: BUY back-month call (lower strike, higher delta), SELL front-month call (higher strike, lower delta)
    # Bearish: BUY back-month put (higher strike, higher delta), SELL front-month put (lower strike, lower delta)
    # Direction determined by current trend.
    diag_back_expiry, diag_back_dte = pick_back_expiry(
        tkr, expiry, p["diagonal_min_gap_days"], p["diagonal_max_gap_days"]
    )
    _diag_direction = "bullish" if trend == "Uptrend" else ("bearish" if trend == "Downtrend" else None)
    if diag_back_expiry is None:
        candidates.append({
            "structure": "Diagonal Spread", "recommended": False,
            "details": (f"No back-month expiry found {p['diagonal_min_gap_days']}-{p['diagonal_max_gap_days']} "
                        f"days after {expiry}"),
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
        })
    elif _diag_direction is None:
        candidates.append({
            "structure": "Diagonal Spread", "recommended": False,
            "details": "Range-bound market — diagonal spread needs a directional bias (Uptrend or Downtrend)",
            "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
        })
    else:
        _opt_type = "call" if _diag_direction == "bullish" else "put"
        _back_calls, _back_puts = get_option_chain(tkr, diag_back_expiry, spot=spot, dte=diag_back_dte)
        _back_chain = _back_calls if _opt_type == "call" else _back_puts
        _front_chain = calls if _opt_type == "call" else puts
        T_diag_back = diag_back_dte / 365.0
        _back_chain = add_deltas(_back_chain, spot, T_diag_back, _opt_type, fallback_vol=_hv_fallback)
        # Back-month long leg: higher delta (closer to ATM), deeper in-the-money
        _diag_long = find_short_strike(_back_chain, _opt_type,
                                        (p["diagonal_long_delta_lo"], p["diagonal_long_delta_hi"]), min_oi)
        # Front-month short leg: lower delta (further OTM), decays faster
        _diag_short = find_short_strike(_front_chain, _opt_type,
                                         (p["diagonal_short_delta_lo"], p["diagonal_short_delta_hi"]), min_oi)
        if _diag_long is None or _diag_short is None:
            candidates.append({
                "structure": "Diagonal Spread", "recommended": False,
                "details": f"No strikes found in target delta ranges ({_diag_direction} diagonal)",
                "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            })
        elif not _valid_price(_diag_long["ask"]) or not _valid_price(_diag_short["bid"]):
            candidates.append({
                "structure": "Diagonal Spread", "recommended": False,
                "details": "Illiquid (no bid/ask) on one or both legs",
                "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
            })
        else:
            # Validate strike direction: bullish call diagonal = short strike > long strike
            _strike_ok = (
                (_opt_type == "call" and _diag_short["strike"] >= _diag_long["strike"]) or
                (_opt_type == "put"  and _diag_short["strike"] <= _diag_long["strike"])
            )
            if not _strike_ok:
                candidates.append({
                    "structure": "Diagonal Spread", "recommended": False,
                    "details": f"Strike mismatch for {_diag_direction} diagonal — cannot build valid spread",
                    "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
                })
            else:
                _diag_debit = _diag_long["ask"] - _diag_short["bid"]
                if _diag_debit <= 0:
                    candidates.append({
                        "structure": "Diagonal Spread", "recommended": False,
                        "details": (f"Net debit ≤ 0 (${_diag_debit:.2f}) — "
                                    f"short leg bid too wide to offset long leg cost"),
                        "pop": None, "ev": None, "max_profit": None, "meets_min_profit": None,
                    })
                else:
                    # Max profit approx: if front-month expires worthless, back-month intrinsic
                    # value vs cost basis. Use width × (long_delta - short_delta) as proxy.
                    _diag_width = abs(_diag_short["strike"] - _diag_long["strike"])
                    _diag_max_profit_est = round(
                        max(0.0, _diag_width * abs(_diag_long["delta"] - _diag_short["delta"])), 3
                    )
                    _diag_max_loss = _diag_debit  # max loss = net debit paid
                    meets_profit, profit_msg = profit_note(_diag_max_profit_est, min_profit_amount)
                    meets_loss, loss_msg = loss_note(_diag_max_loss, width_target)
                    # Greeks: long back-month, short front-month (different strikes)
                    _diag_nd, _diag_nth, _diag_ngm, _diag_nvg = _net_greeks(
                        spot, T_diag_back, _diag_long, _diag_short, _opt_type, short_T=T
                    )
                    # Approx POP: probability long delta finishes ITM
                    _diag_pop = round(abs(_diag_long["delta"]) * 100, 1)
                    iv_edge_msg = ""
                    if iv_ts:
                        iv_edge_msg = f" | {iv_ts['shape']}: {iv_ts['note']}"
                    _leg_label = "C" if _opt_type == "call" else "P"
                    candidates.append({
                        "structure": "Diagonal Spread",
                        "recommended": (
                            trend in ("Uptrend", "Downtrend") and
                            recommended_structure in ("Call Debit Spread", "Put Debit Spread",
                                                      "No Trade", "Call Credit Spread", "Put Credit Spread")
                        ),
                        "details": (
                            f"{'Bullish' if _diag_direction == 'bullish' else 'Bearish'} diagonal: "
                            f"BUY {_diag_long['strike']}{_leg_label} ({diag_back_expiry}, {diag_back_dte}d, "
                            f"delta {_diag_long['delta']:.2f}) / "
                            f"SELL {_diag_short['strike']}{_leg_label} ({expiry}, {dte}d, "
                            f"delta {_diag_short['delta']:.2f}) — "
                            f"net debit ~${_diag_debit:.2f}, est. max profit ~${_diag_max_profit_est:.2f}, "
                            f"{profit_msg}, {loss_msg}{iv_edge_msg}"
                        ),
                        "pop": _diag_pop,
                        "ev": None,
                        "max_profit": _diag_max_profit_est,
                        "meets_min_profit": meets_profit,
                        "max_loss": round(_diag_max_loss, 3),
                        "meets_max_loss": meets_loss,
                        "net_delta": _diag_nd,
                        "net_theta": _diag_nth,
                        "net_gamma": _diag_ngm,
                        "net_vega": _diag_nvg,
                        "is_credit": False,
                    })

    # --- For the rulebook-recommended structure, search for the best-EV version ---
    # of that trade across a wider strike/width grid, and use it in place of the
    # fixed delta-range pick above so "Rec." rows show the most profitable setup
    # currently available rather than whatever the default delta midpoint landed on.
    threshold_pct = p["credit_min_pct_of_width"] * 100
    if recommended_structure == "Put Credit Spread":
        opt = optimize_credit_spread(puts, "put", min_oi, min_profit_amount, width_target)
        if opt:
            meets = "meets" if opt["pct"] >= threshold_pct else "below"
            meets_profit, profit_msg = profit_note(opt["credit"], min_profit_amount)
            meets_loss, loss_msg = loss_note(opt["max_loss"], width_target)
            fallback_bits = []
            if not opt["met_min_profit"]:
                fallback_bits.append("min profit")
            if not opt["met_max_loss"]:
                fallback_bits.append("max loss cap")
            fallback_note = (f" [no combo met {' and '.join(fallback_bits)} - showing best EV overall]"
                              if fallback_bits else "")
            _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, opt["long"], opt["short"], "put")
            for c in candidates:
                if c["structure"] == "Put Credit Spread":
                    c["details"] = (f"[Best EV] SELL {opt['short']['strike']}P / BUY {opt['long']['strike']}P "
                                     f"(short delta {opt['short']['delta']:.2f}, credit ~${opt['credit']:.2f}, "
                                     f"width ${opt['width']:.0f}, credit/width {opt['pct']:.0f}% - {meets} "
                                     f"the {threshold_pct:.0f}% min, {profit_msg}, {loss_msg}){fallback_note}")
                    c["pop"] = opt["pop"]
                    c["ev"] = opt["ev"]
                    c["max_profit"] = round(opt["credit"], 3)
                    c["meets_min_profit"] = meets_profit
                    c["max_loss"] = round(opt["max_loss"], 3)
                    c["meets_max_loss"] = meets_loss
                    c["net_delta"] = _nd; c["net_theta"] = _nth
                    c["net_gamma"] = _ngm; c["net_vega"] = _nvg
    elif recommended_structure == "Call Credit Spread":
        opt = optimize_credit_spread(calls, "call", min_oi, min_profit_amount, width_target)
        if opt:
            meets = "meets" if opt["pct"] >= threshold_pct else "below"
            meets_profit, profit_msg = profit_note(opt["credit"], min_profit_amount)
            meets_loss, loss_msg = loss_note(opt["max_loss"], width_target)
            fallback_bits = []
            if not opt["met_min_profit"]:
                fallback_bits.append("min profit")
            if not opt["met_max_loss"]:
                fallback_bits.append("max loss cap")
            fallback_note = (f" [no combo met {' and '.join(fallback_bits)} - showing best EV overall]"
                              if fallback_bits else "")
            _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, opt["long"], opt["short"], "call")
            for c in candidates:
                if c["structure"] == "Call Credit Spread":
                    c["details"] = (f"[Best EV] SELL {opt['short']['strike']}C / BUY {opt['long']['strike']}C "
                                     f"(short delta {opt['short']['delta']:.2f}, credit ~${opt['credit']:.2f}, "
                                     f"width ${opt['width']:.0f}, credit/width {opt['pct']:.0f}% - {meets} "
                                     f"the {threshold_pct:.0f}% min, {profit_msg}, {loss_msg}){fallback_note}")
                    c["pop"] = opt["pop"]
                    c["ev"] = opt["ev"]
                    c["max_profit"] = round(opt["credit"], 3)
                    c["meets_min_profit"] = meets_profit
                    c["max_loss"] = round(opt["max_loss"], 3)
                    c["meets_max_loss"] = meets_loss
                    c["net_delta"] = _nd; c["net_theta"] = _nth
                    c["net_gamma"] = _ngm; c["net_vega"] = _nvg
    elif recommended_structure == "Iron Condor":
        opt_p = optimize_credit_spread(puts, "put", min_oi, min_profit_amount / 2, width_target)
        opt_c = optimize_credit_spread(calls, "call", min_oi, min_profit_amount / 2, width_target)
        if opt_p and opt_c:
            total_credit = opt_p["credit"] + opt_c["credit"]
            ic_width = max(opt_p["width"], opt_c["width"])
            pop, ev = pop_ev_iron_condor(opt_p["short"]["delta"], opt_c["short"]["delta"], total_credit, ic_width)
            meets_p = "meets" if opt_p["pct"] >= threshold_pct else "below"
            meets_c = "meets" if opt_c["pct"] >= threshold_pct else "below"
            meets_profit, profit_msg = profit_note(total_credit, min_profit_amount)
            max_loss_ic = ic_width - total_credit
            meets_loss, loss_msg = loss_note(max_loss_ic, width_target)
            fallback_bits = []
            if not (opt_p["met_min_profit"] and opt_c["met_min_profit"]):
                fallback_bits.append("min profit (per leg)")
            if not (opt_p["met_max_loss"] and opt_c["met_max_loss"]):
                fallback_bits.append("max loss cap (per leg)")
            fallback_note = (f" [no combo met {' and '.join(fallback_bits)} - showing best EV overall]"
                              if fallback_bits else "")
            _pd, _pth, _pgm, _pvg = _net_greeks(spot, T, opt_p["long"], opt_p["short"], "put")
            _cd, _cth, _cgm, _cvg = _net_greeks(spot, T, opt_c["long"], opt_c["short"], "call")
            # OI check — skip IC if any leg is below the minimum threshold
            ic_legs_oi = [
                int(opt_p["short"].get("openInterest") or 0),
                int(opt_p["long"].get("openInterest")  or 0),
                int(opt_c["short"].get("openInterest") or 0),
                int(opt_c["long"].get("openInterest")  or 0),
            ]
            ic_min_oi_actual = min(ic_legs_oi)
            if ic_min_oi_actual < min_oi:
                for c in candidates:
                    if c["structure"] == "Iron Condor":
                        c["details"] = (f"Skipped — lowest leg OI is {ic_min_oi_actual} "
                                         f"(min required: {min_oi}). Chain too thin for a reliable Iron Condor.")
                        c["recommended"] = False
                        c["pop"] = None; c["ev"] = None
                        c["max_profit"] = None; c["meets_min_profit"] = None
            else:
                for c in candidates:
                    if c["structure"] == "Iron Condor":
                        c["details"] = (f"[Best EV] SELL {opt_p['short']['strike']}P/BUY {opt_p['long']['strike']}P + "
                                         f"SELL {opt_c['short']['strike']}C/BUY {opt_c['long']['strike']}C "
                                         f"(total credit ~${total_credit:.2f}, put {opt_p['pct']:.0f}% ({meets_p}) / "
                                         f"call {opt_c['pct']:.0f}% ({meets_c}) of width, need >={threshold_pct:.0f}%, "
                                         f"{profit_msg}, {loss_msg}){fallback_note}")
                        c["pop"] = pop
                        c["ev"] = ev
                        c["max_profit"] = round(total_credit, 3)
                        c["meets_min_profit"] = meets_profit
                        c["max_loss"] = round(max_loss_ic, 3)
                        c["meets_max_loss"] = meets_loss
                        c["net_delta"] = round((_pd or 0) + (_cd or 0), 3)
                        c["net_theta"] = round((_pth or 0) + (_cth or 0), 4)
                        c["net_gamma"] = round((_pgm or 0) + (_cgm or 0), 6)
                        c["net_vega"]  = round((_pvg or 0) + (_cvg or 0), 4)
                        # Strike fields — must be set here when optimizer overrides initial scan
                        c["put_long_strike"]   = opt_p["long"]["strike"]
                        c["put_short_strike"]  = opt_p["short"]["strike"]
                        c["call_short_strike"] = opt_c["short"]["strike"]
                        c["call_long_strike"]  = opt_c["long"]["strike"]
                        c["is_credit"]         = True
                        c["spot_at_entry"]     = round(spot, 2)
    elif recommended_structure == "Call Debit Spread":
        opt = optimize_debit_spread(calls, "call", min_oi, min_profit_amount, width_target)
        if opt:
            max_profit_opt = opt["width"] - opt["debit"]
            meets_profit, profit_msg = profit_note(max_profit_opt, min_profit_amount)
            meets_loss, loss_msg = loss_note(opt["debit"], width_target)
            fallback_bits = []
            if not opt["met_min_profit"]:
                fallback_bits.append("min profit")
            if not opt["met_max_loss"]:
                fallback_bits.append("max loss cap")
            fallback_note = (f" [no combo met {' and '.join(fallback_bits)} - showing best EV overall]"
                              if fallback_bits else "")
            _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, opt["long"], opt["short"], "call")
            for c in candidates:
                if c["structure"] == "Call Debit Spread":
                    c["details"] = (f"[Best EV] BUY {opt['long']['strike']}C / SELL {opt['short']['strike']}C "
                                     f"(long delta {opt['long']['delta']:.2f}, debit ~${opt['debit']:.2f}, "
                                     f"width ${opt['width']:.0f}, {profit_msg}, {loss_msg}){fallback_note}")
                    c["pop"] = opt["pop"]
                    c["ev"] = opt["ev"]
                    c["max_profit"] = round(max_profit_opt, 3)
                    c["meets_min_profit"] = meets_profit
                    c["max_loss"] = round(opt["debit"], 3)
                    c["meets_max_loss"] = meets_loss
                    c["net_delta"] = _nd; c["net_theta"] = _nth
                    c["net_gamma"] = _ngm; c["net_vega"] = _nvg
                    c["long_strike"] = opt["long"]["strike"]; c["short_strike"] = opt["short"]["strike"]
                    c["spot_at_entry"] = round(spot, 2)
    elif recommended_structure == "Put Debit Spread":
        opt = optimize_debit_spread(puts, "put", min_oi, min_profit_amount, width_target)
        if opt:
            max_profit_opt = opt["width"] - opt["debit"]
            meets_profit, profit_msg = profit_note(max_profit_opt, min_profit_amount)
            meets_loss, loss_msg = loss_note(opt["debit"], width_target)
            fallback_bits = []
            if not opt["met_min_profit"]:
                fallback_bits.append("min profit")
            if not opt["met_max_loss"]:
                fallback_bits.append("max loss cap")
            fallback_note = (f" [no combo met {' and '.join(fallback_bits)} - showing best EV overall]"
                              if fallback_bits else "")
            _nd, _nth, _ngm, _nvg = _net_greeks(spot, T, opt["long"], opt["short"], "put")
            for c in candidates:
                if c["structure"] == "Put Debit Spread":
                    c["details"] = (f"[Best EV] BUY {opt['long']['strike']}P / SELL {opt['short']['strike']}P "
                                     f"(long delta {opt['long']['delta']:.2f}, debit ~${opt['debit']:.2f}, "
                                     f"width ${opt['width']:.0f}, {profit_msg}, {loss_msg}){fallback_note}")
                    c["pop"] = opt["pop"]
                    c["ev"] = opt["ev"]
                    c["max_profit"] = round(max_profit_opt, 3)
                    c["meets_min_profit"] = meets_profit
                    c["max_loss"] = round(opt["debit"], 3)
                    c["meets_max_loss"] = meets_loss
                    c["net_delta"] = _nd; c["net_theta"] = _nth
                    c["net_gamma"] = _ngm; c["net_vega"] = _nvg
                    c["long_strike"] = opt["long"]["strike"]; c["short_strike"] = opt["short"]["strike"]
                    c["spot_at_entry"] = round(spot, 2)

    # Take-profit target: close once this fraction of max profit is captured
    profit_target_pct = p["profit_target_pct"]
    for c in candidates:
        if c["max_profit"] is None:
            c["profit_target"] = None
        else:
            target = round(c["max_profit"] * profit_target_pct, 3)
            c["profit_target"] = target
            c["details"] += f" | take-profit target ~${target:.2f} ({profit_target_pct * 100:.0f}% of max profit)"

        # Capital required: for defined-risk structures this is the max loss
        # per contract (broker margin = max loss); Jade Lizard already set its
        # own (naked-put margin) value above.
        if "capital_required" not in c:
            c["capital_required"] = round(c["max_loss"] * 100, 2) if c.get("max_loss") is not None else None

    # Apply gamma penalty, dividend penalty, and strike-proximity penalty to signal_score
    _near_expiry_dte   = sc.gate("near_expiry_dte") or 14
    _gamma_base        = sc.penalty("gamma_base")
    _bid_ask_pen       = sc.penalty("bid_ask_base")
    _proximity_base    = sc.penalty("strike_proximity_base")
    _proximity_danger_base  = sc.penalty("strike_danger_pct") or 2.0
    _proximity_caution_base = sc.penalty("strike_caution_pct") or 5.0
    # Scale thresholds upward when IV is high: high-IV stocks move further per unit time,
    # so the same absolute cushion % is thinner. atm_iv=0.20 → no scaling above base;
    # atm_iv=0.60 → effective thresholds expand by (0.60*scale*100) additional pct points.
    _atm_iv_for_cushion = result.get("atm_iv") or 0.20
    _iv_cushion_add = _atm_iv_for_cushion * 100 * BREAKEVEN_CUSHION_IV_SCALE
    _proximity_danger  = _proximity_danger_base  + _iv_cushion_add
    _proximity_caution = _proximity_caution_base + _iv_cushion_add * 2
    for c in candidates:
        # Gamma penalty: high gamma at near-expiry amplifies pin/explosion risk
        gm = c.get("net_gamma")
        if gm is not None and dte <= _near_expiry_dte and abs(gm) > 0:
            pen = round(_gamma_base * abs(gm) * 100, 3)
            c["gamma_penalty"] = pen
        else:
            c["gamma_penalty"] = 0.0

        # Dividend gate: penalize (not block) when ex-div falls in trade window
        c["div_warning"] = _div_in_window
        c["div_penalty"] = round(_bid_ask_pen, 3) if _div_in_window else 0.0

        # Strike-proximity penalty: entering a credit/short structure whose
        # short strike is already close to spot is objectively riskier than
        # the same structure with a healthy cushion — penalize accordingly
        # rather than scoring every "Iron Condor" candidate identically
        # regardless of how close the danger zone already is.
        short_strikes = [
            s for s in (c.get("short_strike"), c.get("put_short_strike"), c.get("call_short_strike"))
            if s is not None
        ]
        proximity_pct = None
        if short_strikes and spot:
            proximity_pct = min(abs(spot - s) / spot * 100 for s in short_strikes)

        if proximity_pct is not None and proximity_pct <= _proximity_danger:
            c["proximity_penalty"] = round(_proximity_base, 3)
            c["details"] += f" | ⚠ short strike only {proximity_pct:.1f}% from spot — danger zone"
        elif proximity_pct is not None and proximity_pct <= _proximity_caution:
            c["proximity_penalty"] = round(_proximity_base * 0.5, 3)
            c["details"] += f" | short strike {proximity_pct:.1f}% from spot — caution"
        else:
            c["proximity_penalty"] = 0.0

        # Total per-candidate score adjustment (subtracted in app.py)
        c["signal_score_adj"] = -(c["gamma_penalty"] + c["div_penalty"] + c["proximity_penalty"])

    # ── SVI mispricing: fit vol surface, annotate every candidate ────────────────
    try:
        from scripts.vol_surface import fit_svi_slice as _fit_svi
        import pandas as _pd

        # Combine calls + puts per strike (average IV to reduce bid-ask noise)
        _c = calls[["strike", "impliedVolatility", "openInterest"]].copy().rename(
            columns={"impliedVolatility": "iv"})
        _p = puts[["strike", "impliedVolatility", "openInterest"]].copy().rename(
            columns={"impliedVolatility": "iv"})
        _chain = _pd.concat([_c, _p]).groupby("strike", as_index=False).agg(
            iv=("iv", "mean"), open_interest=("openInterest", "sum"))

        _svi_fit = _fit_svi(_chain, spot=spot, expiry=expiry, dte=dte, weight_col="open_interest")

        if _svi_fit is not None:
            result["svi_rmse"]   = round(_svi_fit.rmse * 100, 3)
            result["svi_params"] = {k: round(v, 6) for k, v in _svi_fit.params.items()}

            # Build strike → mispricing lookup (vol points, %)
            _mp_lookup = {
                round(float(r["strike"]), 2): {
                    "market_iv":    round(float(r["iv"]) * 100, 2),
                    "model_iv":     round(float(r["model_iv"]) * 100, 2),
                    "misprice_vp":  round(float(r["mispricing"]) * 100, 2),
                    "misprice_pct": round(float(r["misprice_pct"]), 1),
                }
                for _, r in _svi_fit.strikes.iterrows()
            }

            def _nearest_mp(strike):
                """Return mispricing for closest fitted strike (within $2 tolerance)."""
                if strike is None:
                    return None
                key = min(_mp_lookup.keys(), key=lambda k: abs(k - strike), default=None)
                if key is None or abs(key - strike) > 2.0:
                    return None
                return _mp_lookup[key]

            def _iv_edge_label(misprice_vp, is_selling):
                """cheap/fair/expensive relative to trade direction."""
                if misprice_vp is None:
                    return "fair"
                if misprice_vp > 1.5:
                    return "expensive" if is_selling else "overpay"
                if misprice_vp < -1.5:
                    return "cheap" if not is_selling else "undersell"
                return "fair"

            for c in candidates:
                if c.get("max_profit") is None:
                    continue
                is_selling = c.get("is_credit", True)
                short_s = c.get("short_strike") or c.get("put_short_strike") or c.get("call_short_strike")
                long_s  = c.get("long_strike")  or c.get("put_long_strike")  or c.get("call_long_strike")

                short_mp = _nearest_mp(short_s)
                long_mp  = _nearest_mp(long_s)

                c["short_misprice_vp"]  = short_mp["misprice_vp"]  if short_mp else None
                c["short_misprice_pct"] = short_mp["misprice_pct"] if short_mp else None
                c["long_misprice_vp"]   = long_mp["misprice_vp"]   if long_mp  else None
                c["long_misprice_pct"]  = long_mp["misprice_pct"]  if long_mp  else None

                # Net edge = what we sell minus what we buy
                net_vp = None
                if is_selling and short_mp is not None:
                    net_vp = short_mp["misprice_vp"]
                    if long_mp is not None:
                        net_vp -= long_mp["misprice_vp"]  # selling expensive, buying cheap is best
                elif not is_selling and long_mp is not None:
                    net_vp = -long_mp["misprice_vp"]  # buying cheap is positive edge

                c["iv_edge_vp"]    = round(net_vp, 2) if net_vp is not None else None
                c["iv_edge_label"] = _iv_edge_label(
                    short_mp["misprice_vp"] if short_mp else None, is_selling)
        else:
            result["svi_rmse"]   = None
            result["svi_params"] = None
    except Exception as _e:
        result["svi_rmse"]   = None
        result["svi_params"] = None

    result["candidates"] = candidates
    result["status"] = "No Trade (matrix says Low IV + Range-bound)" if recommended_structure == "No Trade" else "OK"
    return result


def main():
    tickers = [sys.argv[1].upper()] if len(sys.argv) > 1 else WATCHLIST
    rows = []
    for ticker in tickers:
        try:
            rows.append(analyze_ticker(ticker))
        except Exception as e:
            rows.append({"ticker": ticker, "status": f"ERROR - {e}"})

    for row in rows:
        print(f"\n=== {row['ticker']} ===")
        for k, v in row.items():
            if k == "candidates":
                continue
            print(f"  {k}: {v}")
        for c in row.get("candidates", []):
            rec = " (RECOMMENDED)" if c["recommended"] else ""
            print(f"  {c['structure']}{rec}: {c['details']} | POP={c['pop']} EV={c['ev']}")

    out_dir = os.path.join(os.path.dirname(__file__), "..", "output")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"suggestions_{date.today().isoformat()}.csv")
    flat = []
    for row in rows:
        for c in row.get("candidates", []):
            flat.append({**{k: v for k, v in row.items() if k != "candidates"}, **c})
    pd.DataFrame(flat).to_csv(out_file, index=False)
    print(f"\nSaved to {out_file}")


if __name__ == "__main__":
    main()
