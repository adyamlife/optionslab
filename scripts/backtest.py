"""
Approximate historical backtest of the rulebook's structure-matrix logic.

LIMITATIONS (read before trusting any numbers):
- No historical option chain data is available for free, so option premiums
  are SYNTHESIZED via Black-Scholes using trailing 30-day REALIZED volatility
  as a stand-in for IV. Real IV often differs from realized vol, especially
  around events - this backtest does not capture that.
- Earnings/event blackout is NOT applied (no historical earnings calendar).
- Strike increments are simplified to whole-dollar steps.
- Entries are simulated once per week (every 5 trading days), DTE ~ 7 calendar
  days, matching the live analyze.py defaults.
"""
import sys
import os
import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from config import rules
from config.structures import STRUCTURE_MATRIX  # single source of truth — was previously hand-copied here and had drifted out of sync with the live matrix (High IV + Uptrend pointed at the wrong structure)
from scripts.black_scholes import delta as bs_delta

RISK_FREE_RATE = 0.05
DTE = 7
TRADING_DAYS_FOR_DTE = 5  # ~1 trading week
WIDTH = 1.0  # $ width of each spread leg (matches live script's narrow sizing)


def bs_price(S, K, T, r, sigma, option_type):
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def strike_for_delta(S, target_delta, T, r, sigma, option_type):
    """Invert Black-Scholes delta to get the strike."""
    if option_type == "call":
        d1 = norm.ppf(target_delta)
    else:
        d1 = norm.ppf(1 - target_delta)  # put delta = N(d1) - 1, target_delta given as positive magnitude
    K = S * np.exp((r + 0.5 * sigma ** 2) * T - d1 * sigma * np.sqrt(T))
    return K


def classify_trend(closes, sma_short, sma_long, band_pct):
    price = closes.iloc[-1]
    s = closes.tail(sma_short).mean()
    l = closes.tail(sma_long).mean()
    if price > s * (1 + band_pct) and s > l:
        return "Uptrend"
    if price < s * (1 - band_pct) and s < l:
        return "Downtrend"
    return "Range-bound"


def realized_vol_series(closes, window=30):
    log_ret = np.log(closes / closes.shift(1))
    return (log_ret.rolling(window).std() * np.sqrt(252)).dropna()


def run_backtest(ticker, period="3y", credit_min_pct=None, width=None, dte=None):
    if credit_min_pct is None:
        credit_min_pct = rules.CREDIT_MIN_CREDIT_PCT_OF_WIDTH
    width = WIDTH if width is None else width
    dte = DTE if dte is None else dte
    import yfinance as yf
    hist = yf.Ticker(ticker).history(period=period)
    closes = hist["Close"]

    rv_full = realized_vol_series(closes, 30)

    min_i = 252 + 30  # need 1yr of realized-vol history + the 30d window itself
    max_i = len(closes) - TRADING_DAYS_FOR_DTE - 1

    trades = []
    skipped_no_trade = 0
    skipped_filter = 0

    for i in range(min_i, max_i, TRADING_DAYS_FOR_DTE):
        S = closes.iloc[i]
        window_closes = closes.iloc[: i + 1]

        trend = classify_trend(window_closes, rules.SMA_SHORT, rules.SMA_LONG, rules.TREND_BAND_PCT)

        sigma = rv_full.loc[:closes.index[i]].iloc[-1]
        rv_hist = rv_full.loc[:closes.index[i]].tail(252)
        iv_rank = (rv_hist < sigma).mean() * 100
        iv_env = "High" if iv_rank >= rules.IV_RANK_HIGH_THRESHOLD else "Low"

        structure = STRUCTURE_MATRIX[(iv_env, trend)]
        if structure == "No Trade":
            skipped_no_trade += 1
            continue

        T = dte / 365.0
        S_T = closes.iloc[i + TRADING_DAYS_FOR_DTE]
        entry_date = closes.index[i].date()

        mid_delta = sum(rules.CREDIT_SHORT_DELTA_RANGE) / 2
        debit_long_delta = sum(rules.DEBIT_LONG_DELTA_RANGE) / 2
        debit_short_delta = sum(rules.DEBIT_SHORT_DELTA_RANGE) / 2

        if structure in ("Put Credit Spread", "Iron Condor"):
            k_short = round(strike_for_delta(S, mid_delta, T, RISK_FREE_RATE, sigma, "put"))
            k_long = k_short - width
            credit_put = (bs_price(S, k_short, T, RISK_FREE_RATE, sigma, "put")
                           - bs_price(S, k_long, T, RISK_FREE_RATE, sigma, "put"))
        if structure in ("Call Credit Spread", "Iron Condor"):
            k_short_c = round(strike_for_delta(S, mid_delta, T, RISK_FREE_RATE, sigma, "call"))
            k_long_c = k_short_c + width
            credit_call = (bs_price(S, k_short_c, T, RISK_FREE_RATE, sigma, "call")
                            - bs_price(S, k_long_c, T, RISK_FREE_RATE, sigma, "call"))

        flags = []

        if structure == "Put Credit Spread":
            if credit_put / width * 100 < credit_min_pct * 100:
                skipped_filter += 1
                continue
            loss = max(0.0, min(k_short - S_T, width))
            pnl = credit_put - loss
            max_risk = width - credit_put
            details = f"SELL {k_short:.0f}P / BUY {k_long:.0f}P  (credit ${credit_put:.2f})"
            if credit_put <= 0:
                flags.append("negative/zero credit")
            if pnl == -max_risk:
                flags.append("max loss")
            elif pnl == credit_put:
                flags.append("max profit")

        elif structure == "Call Credit Spread":
            if credit_call / width * 100 < credit_min_pct * 100:
                skipped_filter += 1
                continue
            loss = max(0.0, min(S_T - k_short_c, width))
            pnl = credit_call - loss
            max_risk = width - credit_call
            details = f"SELL {k_short_c:.0f}C / BUY {k_long_c:.0f}C  (credit ${credit_call:.2f})"
            if credit_call <= 0:
                flags.append("negative/zero credit")
            if pnl == -max_risk:
                flags.append("max loss")
            elif pnl == credit_call:
                flags.append("max profit")

        elif structure == "Iron Condor":
            pct_put = credit_put / width * 100
            pct_call = credit_call / width * 100
            if pct_put < credit_min_pct * 100 or pct_call < credit_min_pct * 100:
                skipped_filter += 1
                continue
            loss_put = max(0.0, min(k_short - S_T, width))
            loss_call = max(0.0, min(S_T - k_short_c, width))
            total_credit = credit_put + credit_call
            pnl = total_credit - loss_put - loss_call
            max_risk = width - total_credit
            details = (f"SELL {k_short:.0f}P/BUY {k_long:.0f}P + "
                       f"SELL {k_short_c:.0f}C/BUY {k_long_c:.0f}C  (credit ${total_credit:.2f})")
            if total_credit <= 0:
                flags.append("negative/zero credit")
            if loss_put > 0 and loss_call > 0:
                flags.append("both sides breached")
            if pnl == -max_risk:
                flags.append("max loss")
            elif pnl == total_credit:
                flags.append("max profit")

        elif structure == "Cash Secured Put":
            # Single short put, no spread — capital at risk is the strike
            # itself (minus premium), not a defined width like the spreads.
            k_short_csp = round(strike_for_delta(S, mid_delta, T, RISK_FREE_RATE, sigma, "put"))
            credit_csp = bs_price(S, k_short_csp, T, RISK_FREE_RATE, sigma, "put")
            if credit_csp / k_short_csp * 100 < credit_min_pct * 100:
                skipped_filter += 1
                continue
            loss = max(0.0, k_short_csp - S_T)
            pnl = credit_csp - loss
            max_risk = k_short_csp - credit_csp
            details = f"SELL {k_short_csp:.0f}P (cash-secured, credit ${credit_csp:.2f})"
            if credit_csp <= 0:
                flags.append("negative/zero credit")
            if pnl == -max_risk:
                flags.append("max loss")
            elif pnl == credit_csp:
                flags.append("max profit")

        elif structure == "Call Debit Spread":
            k_buy = round(strike_for_delta(S, debit_long_delta, T, RISK_FREE_RATE, sigma, "call"))
            k_sell = round(strike_for_delta(S, debit_short_delta, T, RISK_FREE_RATE, sigma, "call"))
            if k_sell <= k_buy:
                k_sell = k_buy + width  # fallback if delta-implied strikes collapse
            spread_width = k_sell - k_buy
            debit = (bs_price(S, k_buy, T, RISK_FREE_RATE, sigma, "call")
                     - bs_price(S, k_sell, T, RISK_FREE_RATE, sigma, "call"))
            payoff = max(0.0, min(S_T - k_buy, spread_width))
            pnl = payoff - debit
            max_risk = debit
            details = f"BUY {k_buy:.0f}C / SELL {k_sell:.0f}C  (debit ${debit:.2f}, width ${spread_width:.0f})"
            if debit <= 0:
                flags.append("negative/zero debit")
            if pnl == -max_risk:
                flags.append("max loss")
            elif payoff == spread_width:
                flags.append("max profit")

        elif structure == "Put Debit Spread":
            k_buy = round(strike_for_delta(S, debit_long_delta, T, RISK_FREE_RATE, sigma, "put"))
            k_sell = round(strike_for_delta(S, debit_short_delta, T, RISK_FREE_RATE, sigma, "put"))
            if k_sell >= k_buy:
                k_sell = k_buy - width  # fallback if delta-implied strikes collapse
            spread_width = k_buy - k_sell
            debit = (bs_price(S, k_buy, T, RISK_FREE_RATE, sigma, "put")
                     - bs_price(S, k_sell, T, RISK_FREE_RATE, sigma, "put"))
            payoff = max(0.0, min(k_buy - S_T, spread_width))
            pnl = payoff - debit
            max_risk = debit
            details = f"BUY {k_buy:.0f}P / SELL {k_sell:.0f}P  (debit ${debit:.2f}, width ${spread_width:.0f})"
            if debit <= 0:
                flags.append("negative/zero debit")
            if pnl == -max_risk:
                flags.append("max loss")
            elif payoff == spread_width:
                flags.append("max profit")

        trades.append({
            "date": entry_date, "structure": structure, "iv_env": iv_env,
            "trend": trend, "S": round(S, 2), "S_T": round(S_T, 2),
            "details": details,
            "pnl": round(pnl, 4), "max_risk": round(max_risk, 4),
            "win": pnl > 0,
            "flags": ", ".join(flags) if flags else "-",
        })

    return pd.DataFrame(trades), skipped_no_trade, skipped_filter


def summarize(df, ticker, skipped_no_trade, skipped_filter, width=WIDTH, dte=DTE):
    print(f"\n=== {ticker} backtest (weekly entries, {dte}D, width=${width}) ===")
    print(f"Trades taken: {len(df)} | Skipped (No Trade per matrix): {skipped_no_trade} | "
          f"Skipped (failed credit filter): {skipped_filter}")
    if df.empty:
        print("No trades taken.")
        return
    win_rate = df["win"].mean() * 100
    avg_win = df.loc[df["win"], "pnl"].mean()
    avg_loss = df.loc[~df["win"], "pnl"].mean()
    expectancy = df["pnl"].mean()
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Avg win: ${avg_win:.3f} | Avg loss: ${avg_loss:.3f}")
    print(f"Expectancy per trade: ${expectancy:.3f} (per $1 of width)")
    print("\nBy structure:")
    print(df.groupby("structure").agg(
        n=("pnl", "size"), win_rate=("win", "mean"), avg_pnl=("pnl", "mean")
    ).round(3))

    print("\nTop 3 trades by P&L:")
    print(df.sort_values("pnl", ascending=False).head(3)[
        ["date", "structure", "details", "pnl", "win", "flags"]
    ].to_string(index=False))


if __name__ == "__main__":
    # Usage: python -m scripts.backtest [TICKER] [WIDTH] [CREDIT_MIN_PCT]
    # CREDIT_MIN_PCT is a fraction, e.g. 0.10 for 10% (rulebook default is 0.25)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "QQQ"
    width = float(sys.argv[2]) if len(sys.argv) > 2 else WIDTH
    credit_min_pct = float(sys.argv[3]) if len(sys.argv) > 3 else None

    df, skip_nt, skip_f = run_backtest(ticker, credit_min_pct=credit_min_pct, width=width)
    summarize(df, ticker, skip_nt, skip_f, width=width)
    if credit_min_pct is not None:
        print(f"(credit filter threshold used: {credit_min_pct*100:.0f}% of width)")
    out_dir = os.path.join(os.path.dirname(__file__), "..", "output")
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, f"backtest_{ticker}.csv"), index=False)
