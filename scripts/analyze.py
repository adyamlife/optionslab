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
    get_live_spot,
)
from scripts.black_scholes import delta as bs_delta, theta as bs_theta, gamma as bs_gamma, vega as bs_vega
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


def compute_signal_alignment(recommended_structure, trend, weekly_trend, rsi, macd_trend, news_sentiment,
                              adx=None, rel_volume=None, pcr=None, pcr_sentiment=None,
                              ema200_position=None, iv_term_shape=None,
                              short_interest=None, vol_skew_pct=None,
                              analyst_label=None, iv_premium=None,
                              regime="chop"):
    """
    Score how well technical and flow signals agree with the recommended structure.
    Each sub-factor contributes ±get_sub_weight(factor, sub, regime) instead of flat ±1,
    so the total budget per factor stays constant regardless of how sub-weights are tuned.
    Returns a dict with score, rating, and notes.
    """
    score = 0.0
    notes = []

    def w(factor, sub):
        return sc.get_sub_weight(factor, sub, regime)

    # ── Technical signals ─────────────────────────────────────────────────────

    # --- EMA 200 (institutional trend filter) ---
    if ema200_position is not None:
        bullish_structures = ("Put Credit Spread", "Call Debit Spread", "Diagonal Spread")
        bearish_structures = ("Call Credit Spread", "Put Debit Spread")
        wt = w("technical", "ema200")
        if recommended_structure in bullish_structures:
            if ema200_position == "above":
                score += wt; notes.append("Price above EMA200 — institutional uptrend confirms bullish bias")
            else:
                score -= wt; notes.append("Price below EMA200 — institutional downtrend is headwind for bullish trade")
        elif recommended_structure in bearish_structures:
            if ema200_position == "below":
                score += wt; notes.append("Price below EMA200 — institutional downtrend confirms bearish bias")
            else:
                score -= wt; notes.append("Price above EMA200 — institutional uptrend is headwind for bearish trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            notes.append(f"Price {ema200_position} EMA200 — monitor for trend resumption out of range")

    # --- Weekly trend ---
    if weekly_trend and weekly_trend not in ("N/A", "Range-bound"):
        wt = w("technical", "weekly_trend")
        if weekly_trend == trend:
            score += wt; notes.append(f"weekly trend confirms {trend}")
        else:
            score -= wt; notes.append(f"weekly trend ({weekly_trend}) conflicts with daily ({trend})")

    # --- ADX (trend strength) ---
    if adx is not None:
        wt          = w("technical", "adx")
        directional = recommended_structure in (
            "Put Credit Spread", "Call Credit Spread", "Call Debit Spread", "Put Debit Spread"
        )
        neutral     = recommended_structure in ("Iron Condor", "Calendar Spread")
        if adx > 25:
            if directional:
                score += wt; notes.append(f"ADX {adx} strong trend — confirms directional trade")
            elif neutral:
                score -= wt; notes.append(f"ADX {adx} strong trend — breakout risk for neutral trade")
        elif adx < 20:
            if directional:
                score -= wt; notes.append(f"ADX {adx} choppy market — weak case for directional trade")
            elif neutral:
                score += wt; notes.append(f"ADX {adx} choppy range — supports neutral trade")

    # --- RSI ---
    if rsi is not None:
        wt = w("technical", "rsi")
        if recommended_structure == "Put Credit Spread":
            if rsi < 50:
                score -= wt; notes.append(f"RSI {rsi} below midline — weak bullish momentum")
            elif rsi > 70:
                score -= wt; notes.append(f"RSI {rsi} overbought — pullback risk")
            else:
                score += wt; notes.append(f"RSI {rsi} healthy for uptrend")
        elif recommended_structure == "Call Credit Spread":
            if rsi > 50:
                score -= wt; notes.append(f"RSI {rsi} above midline — weak bearish momentum")
            elif rsi < 30:
                score -= wt; notes.append(f"RSI {rsi} oversold — bounce risk")
            else:
                score += wt; notes.append(f"RSI {rsi} healthy for downtrend")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            if 40 <= rsi <= 60:
                score += wt; notes.append(f"RSI {rsi} neutral — supports range-bound")
            elif rsi > 70 or rsi < 30:
                score -= wt; notes.append(f"RSI {rsi} extended — breakout/breakdown risk for neutral trade")
        elif recommended_structure == "Call Debit Spread":
            if 50 <= rsi <= 70:
                score += wt; notes.append(f"RSI {rsi} bullish momentum")
            elif rsi < 40:
                score -= wt; notes.append(f"RSI {rsi} weak for bullish trade")
        elif recommended_structure == "Put Debit Spread":
            if 30 <= rsi <= 50:
                score += wt; notes.append(f"RSI {rsi} bearish momentum")
            elif rsi > 60:
                score -= wt; notes.append(f"RSI {rsi} strong — weak bearish case")

    # --- MACD ---
    if macd_trend and macd_trend not in ("N/A",):
        wt = w("technical", "macd")
        bullish_structures = ("Put Credit Spread", "Call Debit Spread")
        bearish_structures = ("Call Credit Spread", "Put Debit Spread")
        neutral_structures = ("Iron Condor", "Calendar Spread")
        if recommended_structure in bullish_structures:
            if macd_trend == "Bullish":
                score += wt; notes.append("MACD Bullish confirms upside bias")
            else:
                score -= wt; notes.append("MACD Bearish conflicts with upside bias")
        elif recommended_structure in bearish_structures:
            if macd_trend == "Bearish":
                score += wt; notes.append("MACD Bearish confirms downside bias")
            else:
                score -= wt; notes.append("MACD Bullish conflicts with downside bias")
        elif recommended_structure in neutral_structures:
            notes.append(f"MACD {macd_trend} — watch for breakout")

    # --- IV Term Structure (technical edge for calendars/diagonals) ---
    if iv_term_shape is not None:
        wt       = w("technical", "vol_skew")   # reuses vol_skew budget until dedicated sub added
        cal_diag = recommended_structure in ("Calendar Spread", "Diagonal Spread")
        if cal_diag:
            if iv_term_shape == "Backwardation":
                score += wt; notes.append("IV backwardation — selling richer near-term vol adds edge to time spread")
            elif iv_term_shape == "Contango":
                score -= wt; notes.append("IV contango — no near-term vol premium; time spread has no vol-edge advantage")
            else:
                notes.append("Flat IV term structure — neutral vol edge for calendar/diagonal")

    # ── Flow signals ──────────────────────────────────────────────────────────

    # --- News sentiment ---
    if news_sentiment and news_sentiment not in ("Neutral", "N/A"):
        wt = w("flow", "news")
        bullish_structures = ("Put Credit Spread", "Call Debit Spread")
        bearish_structures = ("Call Credit Spread", "Put Debit Spread")
        if recommended_structure in bullish_structures:
            if news_sentiment == "Bullish":
                score += wt; notes.append("news sentiment Bullish — supports upside trade")
            elif news_sentiment == "Bearish":
                score -= wt; notes.append("news sentiment Bearish — headwind for upside trade")
        elif recommended_structure in bearish_structures:
            if news_sentiment == "Bearish":
                score += wt; notes.append("news sentiment Bearish — supports downside trade")
            elif news_sentiment == "Bullish":
                score -= wt; notes.append("news sentiment Bullish — headwind for downside trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            if news_sentiment == "Mixed":
                score += wt; notes.append("mixed news — consistent with range-bound expectation")
            elif news_sentiment in ("Bullish", "Bearish"):
                score -= wt; notes.append(f"news {news_sentiment} — directional bias adds breakout risk to neutral trade")

    # --- Put/Call Ratio ---
    if pcr_sentiment and pcr_sentiment not in ("Neutral", "N/A"):
        wt = w("flow", "pcr")
        bullish_structures = ("Put Credit Spread", "Call Debit Spread")
        bearish_structures = ("Call Credit Spread", "Put Debit Spread")
        if recommended_structure in bullish_structures:
            if pcr_sentiment == "Bullish":
                score += wt; notes.append(f"PCR {pcr} call-heavy OI — confirms upside bias")
            elif pcr_sentiment == "Bearish":
                score -= wt; notes.append(f"PCR {pcr} put-heavy OI — headwind for upside trade")
        elif recommended_structure in bearish_structures:
            if pcr_sentiment == "Bearish":
                score += wt; notes.append(f"PCR {pcr} put-heavy OI — confirms downside bias")
            elif pcr_sentiment == "Bullish":
                score -= wt; notes.append(f"PCR {pcr} call-heavy OI — headwind for downside trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            if pcr_sentiment == "Neutral":
                score += wt; notes.append(f"PCR {pcr} neutral OI balance — consistent with range-bound")
            elif pcr_sentiment in ("Bullish", "Bearish"):
                notes.append(f"PCR {pcr} {pcr_sentiment.lower()} — directional OI skew adds breakout risk")

    # --- Relative Volume ---
    if rel_volume is not None:
        wt = w("flow", "rel_volume")
        if rel_volume > 1.5 and trend != "Range-bound":
            score += wt; notes.append(f"Rel volume {rel_volume}x — elevated volume confirms move")
        elif rel_volume < 0.5:
            notes.append(f"Rel volume {rel_volume}x — thin volume, low conviction")

    # --- Analyst sentiment (buy/hold/sell consensus) ---
    if analyst_label and analyst_label not in ("N/A", "Neutral"):
        wt = w("flow", "analyst")
        bullish_structures = ("Put Credit Spread", "Call Debit Spread")
        bearish_structures = ("Call Credit Spread", "Put Debit Spread")
        if recommended_structure in bullish_structures:
            if analyst_label == "Bullish":
                score += wt; notes.append("Analyst consensus Bullish — supports upside trade")
            elif analyst_label == "Bearish":
                score -= wt; notes.append("Analyst consensus Bearish — headwind for upside trade")
        elif recommended_structure in bearish_structures:
            if analyst_label == "Bearish":
                score += wt; notes.append("Analyst consensus Bearish — confirms downside trade")
            elif analyst_label == "Bullish":
                score -= wt; notes.append("Analyst consensus Bullish — headwind for bearish trade")
        elif recommended_structure in ("Iron Condor", "Calendar Spread"):
            if analyst_label in ("Bullish", "Bearish"):
                score -= wt * 0.5
                notes.append(f"Analyst consensus {analyst_label} — directional bias adds breakout risk to neutral trade")

    # --- IV premium / discount over HV20 ---
    if iv_premium is not None:
        wt = w("technical", "iv_premium")
        if iv_premium > 0.03:        # IV at least 3 ppt above HV — options are rich
            if is_credit := recommended_structure in CREDIT_STRUCTURES:
                score += wt; notes.append(f"IV premium {iv_premium*100:+.1f}% over HV20 — selling rich options adds edge")
            else:
                score -= wt; notes.append(f"IV premium {iv_premium*100:+.1f}% over HV20 — buying expensive options reduces edge")
        elif iv_premium < -0.03:     # IV at least 3 ppt below HV — options are cheap
            if recommended_structure not in CREDIT_STRUCTURES:
                score += wt; notes.append(f"IV discount {iv_premium*100:+.1f}% vs HV20 — buying cheap options adds edge")
            else:
                score -= wt; notes.append(f"IV discount {iv_premium*100:+.1f}% vs HV20 — selling cheap options reduces edge")

    # --- Short interest (squeeze risk) ---
    if short_interest is not None:
        wt = w("flow", "short_interest")
        bullish_structures = ("Put Credit Spread", "Call Debit Spread")
        bearish_structures = ("Call Credit Spread", "Put Debit Spread")
        if short_interest > 20:
            if recommended_structure in bullish_structures:
                score += wt; notes.append(f"Short interest {short_interest:.1f}% — high short float, squeeze risk favors upside")
            elif recommended_structure in bearish_structures:
                score -= wt; notes.append(f"Short interest {short_interest:.1f}% — potential squeeze is headwind for bearish trade")
        elif short_interest < 3:
            if recommended_structure in bearish_structures:
                score += wt; notes.append(f"Short interest {short_interest:.1f}% — low short float supports steady downside")

    # --- Vol skew (put vs call IV imbalance) ---
    if vol_skew_pct is not None:
        wt = w("technical", "vol_skew")
        if vol_skew_pct > 5:
            # put IV > call IV — market pricing in more downside risk
            if recommended_structure in ("Put Credit Spread",):
                score -= wt; notes.append(f"Vol skew +{vol_skew_pct:.1f}% — elevated put IV suggests downside fear, caution for short put")
            elif recommended_structure in ("Put Debit Spread", "Call Credit Spread"):
                score += wt; notes.append(f"Vol skew +{vol_skew_pct:.1f}% — bearish skew, puts richly priced — supports bearish trade")
            elif recommended_structure in ("Iron Condor",):
                score += wt; notes.append(f"Vol skew +{vol_skew_pct:.1f}% — selling rich put IV in condor adds edge")
        elif vol_skew_pct < -5:
            # call IV > put IV — unusual, often pre-breakout
            if recommended_structure in ("Call Debit Spread",):
                score += wt; notes.append(f"Vol skew {vol_skew_pct:.1f}% — calls richer than puts, market pricing upside move")
            elif recommended_structure in ("Call Credit Spread",):
                score -= wt; notes.append(f"Vol skew {vol_skew_pct:.1f}% — elevated call IV, breakout risk for short call")

    rating = sc.score_to_rating(score, regime)
    return {"score": round(score, 3), "rating": rating, "notes": notes, "regime": regime}


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

    # Preliminary recommended structure (will be recalculated later if options available)
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

    iv_rank = get_iv_rank_proxy(hist, atm_iv, ticker=ticker)
    result["iv_rank_proxy"] = iv_rank
    iv_env = "High" if (iv_rank is not None and iv_rank >= p["iv_rank_high_threshold"]) else "Low"
    result["iv_env"] = iv_env

    recommended_structure = STRUCTURE_MATRIX[(iv_env, trend)]
    result["recommended_structure"] = recommended_structure

    signal = compute_signal_alignment(
        recommended_structure, trend, weekly_trend, rsi, macd_trend, news["sentiment"],
        adx=adx, rel_volume=rel_volume, pcr=flow["pcr"], pcr_sentiment=flow["pcr_sentiment"],
        ema200_position=ema200_position,
        iv_term_shape=iv_ts["shape"] if iv_ts else None,
        short_interest=short_interest,
        vol_skew_pct=vol_skew_data["skew_pct"] if vol_skew_data else None,
        analyst_label=analyst["label"],
        iv_premium=hv_data["iv_premium"] if hv_data else None,
        regime=regime,
    )
    result["regime"] = regime
    result["signal_score"] = signal["score"]
    result["signal_rating"] = signal["rating"]
    result["signal_notes"] = signal["notes"]

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
        cc_pop       = round((1.0 + abs(float(short_call_cc.get("delta") or 0))) * 100, 1)  # approx probability called away
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
