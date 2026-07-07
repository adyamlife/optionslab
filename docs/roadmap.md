# Options Strategy Lab — Roadmap

## Implemented (as of 2026-06-16)

| Feature | Where |
|---|---|
| Structure matrix (IV × Trend → structure) | `scripts/analyze.py` |
| Best-EV strike/width optimizer per structure | `scripts/analyze.py` |
| ADX trend-strength signal | `scripts/data_fetch.get_adx` |
| Relative volume (20-day avg) | `scripts/data_fetch.get_relative_volume` |
| Put/Call Ratio (PCR) from live option OI | `scripts/data_fetch.get_options_flow` |
| Unusual activity flag (volume > 3× OI) | `scripts/data_fetch.get_options_flow` |
| OI change vs prior run (delta OI) | `scripts/data_fetch.get_options_flow` — `data/oi_cache.json` |
| Signal alignment scoring (RSI, MACD, weekly trend, news, ADX, RelVol, PCR) | `scripts/analyze.compute_signal_alignment` |
| News sentiment (keyword-based, yfinance) | `scripts/data_fetch.get_news_sentiment` |
| IV rank proxy (VIX percentile for ETFs; IV/HV ratio for stocks) | `scripts/data_fetch.get_iv_rank_proxy` |
| After-hours bid/ask fill (lastPrice + BS IV recompute) | `scripts/data_fetch._fill_bid_ask` |
| Earnings date live fetch + cache | `scripts/data_fetch.get_next_earnings_date` — `data/earnings_cache.json` |
| Top 3 trades ranked by signal + EV + capital | `web/app.py:build_top_trades` |
| AI assessment via Groq (primary) / Gemini (fallback) | `scripts/ai_assessment.py` |
| Tabbed UI per ticker, recommended tab bolded | `web/static/js/live.js` |
| Take-profit target (50% of max profit) | `config/settings.toml [management]` |
| Single config file for all parameters | `config/settings.toml` |

---

## Pending / In Progress

### #5 — Greeks Display (ATM Delta, Theta) in UI
**Effort:** Low | **Data:** Already computed, not surfaced  
Show ATM delta and estimated daily theta decay per structure in the tab panel.
This tells you: "This Put Credit Spread has delta -0.20 (low directional risk) and earns ~$0.05 theta/day."
Note: Portfolio-level Greeks (net delta across all positions) requires open-positions tracking — separate feature below.

### #12 — Hard Risk Limit Enforcement
**Effort:** Low | **Data:** Already in `settings.toml [risk_limits]`  
Currently `max_open_positions`, `max_daily_loss_pct`, etc. are stored in config but not enforced.
Next step: build a simple `data/positions.json` trade log; on each run check if limits are breached and show a warning banner in the UI before displaying suggestions.
Parameters already in config: `max_open_positions=5`, `max_daily_loss_pct=3%`, `max_weekly_loss_pct=6%`, `max_position_pct=5%`, `max_sector_pct=20%`.

### Trade Journal / Position Tracker
**Effort:** Medium  
Log entered trades to `data/positions.json` (ticker, structure, expiry, credit/debit, max_profit, max_loss, entry_date).
Enables: Greeks aggregation, risk-limit enforcement, Kelly Criterion sizing (once win rate is established after ~30 trades).

---

## Good-To-Have (Free Data, Medium Effort)

### Diagonal Spread Support
The long leg uses a later expiry than the short leg (unlike Calendar where both legs use the same strike).
A Call Diagonal = BUY ITM call (back month) / SELL OTM call (front month).
Useful in Low-IV uptrend when you want a cheaper long-delta position.
Implementation: extend `pick_back_expiry` logic; add "Call Diagonal" / "Put Diagonal" to `ALL_STRUCTURES`.

### EMA Stack (20/50/200) in Trend Signal
Current trend uses SMA 20/50. Adding EMA 200 gives a "market regime" context:
- Price above EMA 200: macro uptrend → favor bullish structures
- Price below EMA 200: macro downtrend → favor bearish structures
Low implementation effort — just add to `get_trend()` return dict as `ema200_side`.

### IV Term Structure (Contango / Backwardation)
Compare front-month ATM IV vs back-month ATM IV.
- Contango (front < back): normal; Calendar favored (buying cheap back-month vol)
- Backwardation (front > back): crisis/event; Calendar unfavorable; Iron Condor risky
Implementation: compute in `analyze_ticker` using front and back chains already fetched for Calendar.

### Backtest Win-Rate Display on Live Page
The backtest engine already exists (`scripts/backtest.py`).
Add a lightweight "historical win rate" sidebar on the live results page showing the 1-year backtest win rate for the recommended structure per ticker.
Implementation: run backtest per ticker (expensive), or pre-cache results nightly.

### Volume-Weighted Average Price (VWAP) as Intraday Support/Resistance
VWAP is available intraday via yfinance (`interval="5m"` or `"1h"`).
Price above VWAP intraday = bullish bias; below = bearish.
Useful for confirming same-day entries, not for end-of-day scans.

---

## Requires Paid Data (~$30–100/month)

### Dark Pool Prints / Block Trades
**Provider:** Unusual Whales (~$50/mo), BlackBoxStocks (~$100/mo), Tradytics (~$30/mo)  
Large institutional block trades (>$1M notional) in options often precede directional moves.
Signal: if dark-pool call volume in XYZ > 10× average, bullish institutional positioning.
Integration: REST API → add `dark_pool_flow` to signal alignment.

### Real-Time Options Flow (Sweeps)
**Provider:** Unusual Whales, FlowAlgo  
Sweep orders = options bought across multiple exchanges immediately (urgency signal from institutions).
Much stronger than PCR alone — tells you direction AND conviction.
Integration: add `sweeps_bullish` / `sweeps_bearish` count to signal alignment.

### Level 2 / Order Book Data
**Provider:** Polygon.io (~$29/mo for options L2)  
Bid/ask depth at each strike tells you real liquidity, not just OI.
Current fill-price estimates (using bid×0.98 / ask×1.02) would be replaced by actual mid-market fills.

### Social Sentiment (Reddit/Twitter/StockTwits)
**Provider:** Quiver Quant (~$20/mo), StockTwits API (some free tier)  
Retail sentiment — especially useful for meme stocks (MSTR, SOUN, APLD, etc.) in the watchlist.
Can catch retail-driven gamma squeezes before they happen.

### Sector/Industry Classification
**Provider:** Polygon.io or Quiver Quant  
Currently no sector tracking. Needed for `max_sector_pct` enforcement.
Free alternative: hardcode a sector map for the ~50 watchlist tickers (one-time, low maintenance).

---

## Institutional-Grade (Significant Build Effort, May Need Paid Data)

### ML Probability of Profit Model (XGBoost/LightGBM)
Replaces BSM-delta-based POP with a trained model using features:
IV Rank, Delta, Gamma, Theta, Vega, OI, PCR, VIX, DTE, earnings proximity.
**Prerequisite:** 6+ months of live trade log (positions.json) with actual outcomes to train on.
Without historical outcome data, ML just overfits to theory.

### Monte Carlo Simulation
Run 10,000 price paths per trade using GBM (Geometric Brownian Motion) with current HV.
Outputs: 95th-percentile worst loss, probability of touching the short strike, expected profit at expiry.
For defined-risk spreads, the value is marginal (max loss is already known).
More useful for: Calendar Spreads (path-dependent), Jade Lizard (undefined risk).

### Kelly Criterion Position Sizing
`f = (b×p - q) / b` where b = reward/risk ratio, p = win probability, q = loss probability.
Use Half-Kelly for safety: `f/2`.
**Prerequisite:** Reliable win rate from at minimum 30–50 live trades (positions.json).
Current approach (fixed % of capital) is safer until win rate is established.

### Delta-Neutral Hedging
Buy/sell underlying shares to neutralize portfolio delta.
**Prerequisite:** Margin account; ability to short shares; position tracker.
Not practical for a $1,000 account — requires significant capital for the hedge shares.

### Portfolio-Level Hedge (SPY Puts)
Long SPY puts as a macro hedge against a broad market crash wiping out the portfolio.
Cost: ~$2–5/contract/month for 5-10% OTM SPY puts.
At $1,000 account size, the hedge cost would consume most of the premium income.
Revisit when account reaches ~$5,000+.

### Market Regime via Hidden Markov Model (HMM) / Bayesian Switching
Classify market into Bullish/Bearish/Sideways/High-Vol regimes using HMM on price returns.
**vs. current approach:** Current SMA + ADX trend classification captures ~80% of the same signal.
HMM adds: smooth regime transitions, uncertainty quantification, handles choppy-to-trending shifts.
Implementation: `hmmlearn` library; needs 2–5 years of daily returns to fit.

---

## Notes

- API keys: always in `config/secrets.toml` — never commit to git.
- All free signals now in production: RSI, MACD, ADX, RelVol, PCR, OI change, unusual activity, news, weekly trend, IV rank.
- Next milestone: Trade Journal (positions.json) unlocks Kelly sizing, Greeks dashboard, and ML POP model.
