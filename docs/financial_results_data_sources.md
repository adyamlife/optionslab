# Financial Results — Data Source Matrix

**Universe**: 83 tickers | **Backfill window**: 2 years (8 quarters per ticker, one yfinance pull)  
**Two-phase model**: v1 trains now on fundamentals + NLP; v2 adds IV features prospectively.

---

## Design Principles

**NULL means absent, never zero.**  
`atm_iv = NULL` → IV data not collected.  
`atm_iv = 0` → stock had zero implied vol. These are completely different. Use NULL.

**`has_historical_iv` gates all IV-derived features.**  
All backfilled rows: `has_historical_iv = 0`, IV fields = NULL. Once prospective collection begins (Aug/Sep 2026), new rows get `has_historical_iv = 1` and real IV values. The model learns from both populations; the flag is a feature that tells the model which regime it's in.

**`point_in_time_verified` prevents look-ahead leakage.**  
Historical earnings analysis is contaminated whenever you pull today's API value (e.g., `yfinance.info['forwardPE']`) and attribute it to an earnings event 12 months ago. That estimate did not exist then. Only rows where every feature was derived from data genuinely available before `earnings_date` get `point_in_time_verified = TRUE`. Only TRUE rows enter training.

---

## Phase Roadmap

### v1 — Historical (now, 2yr backfill)
Train on: financials, earnings surprises, guidance, post-earnings price moves, valuation percentiles, market regime (JOIN to training_snapshots), NLP (Loughran-McDonald).

Trains: earnings jump distribution, trade-win model, earnings-aware MC, calibration, ranking, preliminary Kelly adjustment.

### v2 — Prospective (Aug/Sep 2026+)
Accumulate: ATM IV buildup → implied earnings move → IV crush → realized move.

Adds: P(IV expansion), P(IV crush), implied/realized move ratio. The ratio `realized_earnings_move / implied_earnings_move` is the key feature for premium-selling structures.

---

## Table 1 — earnings_fundamentals

| Field | Source | point_in_time_verified? | 2yr Backfill? | Notes |
|---|---|---|---|---|
| earnings_date | yfinance `.earnings_dates` | ✅ TRUE | ✅ | ~8 quarters per call |
| release_timing (BMO/AMC) | yfinance `.earnings_dates['Hour']` | ✅ TRUE | ⚠️ | Often 'TAS'; fallback manual |
| revenue_actual | yfinance `.quarterly_income_stmt['Total Revenue']` | ✅ TRUE | ✅ | |
| revenue_estimate | yfinance `.earnings_history` does not have revenue est. FMP free tier approximate | ⚠️ FALSE | ⚠️ | Not point-in-time; today's consensus for a past date. Flag FALSE. |
| revenue_surprise | computed: (actual - estimate) / abs(estimate) | inherits estimate flag | — | |
| eps_actual | yfinance `.earnings_history['epsActual']` | ✅ TRUE | ✅ | |
| eps_estimate | yfinance `.earnings_history['epsEstimate']` | ✅ TRUE | ✅ | yfinance stores the pre-event estimate |
| eps_surprise | yfinance `.earnings_history['surprisePercent']` | ✅ TRUE | ✅ | Pre-computed by Yahoo |
| ebitda, ebit, net_income | yfinance `.quarterly_income_stmt` | ✅ TRUE | ✅ | Actuals; no leakage |
| free_cash_flow, operating_cash_flow | yfinance `.quarterly_cashflow` | ✅ TRUE | ✅ | Confirmed present for AAPL |
| cash_and_equiv, total_debt, net_debt | yfinance `.quarterly_balance_sheet` | ✅ TRUE | ✅ | |
| margins (gross, operating, net, fcf) | computed from above | ✅ TRUE | ✅ | |
| guidance_direction | SEC 8-K NLP (v1 rule-based) | ✅ TRUE | ⚠️ | Extraction quality varies by company |
| guidance_revenue_mid, guidance_eps_mid | SEC 8-K NLP (regex on numeric ranges) | ✅ TRUE | ⚠️ | Patchy; many companies don't give specific numbers |
| eps_beat_rate_8q, rev_beat_rate_8q | computed rolling from prior rows | ✅ TRUE | ✅ | Correct: uses only past data |
| avg_eps_surprise_8q | computed rolling from prior rows | ✅ TRUE | ✅ | |
| eps_rev_1w, eps_rev_4w | **OMITTED** | — | ❌ | Requires point-in-time consensus history (FactSet/Bloomberg, paid). Cannot reconstruct free. |
| trailing_pe | computed: price_at_earnings_date / ttm_eps | ✅ TRUE | ✅ | Use yfinance `.history()` for price, ttm_eps from stored actuals |
| forward_pe | computed: price / forward_eps_consensus | ⚠️ FALSE | ⚠️ | Forward consensus at historical dates not available free. Flag FALSE; exclude from v1 training. |
| ev_ebitda | computed: (mktcap + net_debt) / ttm_ebitda | ✅ TRUE | ✅ | All inputs backfillable |
| price_to_sales | computed: mktcap / ttm_revenue | ✅ TRUE | ✅ | |
| fcf_yield | computed: ttm_fcf_per_share / price | ✅ TRUE | ✅ | |
| peg_ratio | **OMITTED from v1** | — | ❌ | Needs forward EPS growth rate (point-in-time); no free source |
| forward_pe_5y_pct | rolling percentile of own prior trailing_pe rows | ✅ TRUE | ✅ | NULL for first 6 rows (< 6 prior quarters) |
| ev_ebitda_5y_pct | same | ✅ TRUE | ✅ | |
| ps_5y_pct | same | ✅ TRUE | ✅ | |
| forward_pe_sector_pct, ev_ebitda_sector_pct | cross-sectional rank on earnings_date vs sector | ✅ TRUE | ✅ | Rank among peers with data for same quarter |
| median_impl_real_ratio_8q (and p25, p75) | computed from earnings_iv_timeline realized_move_pct + atm_iv | ✅ TRUE when has_historical_iv=1 | ❌ historical, ✅ prospective | NULL for all v1 rows. Populate in v2. |
| ret_1d, ret_3d, ret_5d, ret_10d, drift_30d | yfinance `.history()` | ✅ TRUE | ✅ | Forward price from earnings_date; no leakage |
| point_in_time_verified | set per-row at insertion | — | — | FALSE for any row using today's API as historical proxy |

---

## Table 2 — earnings_iv_timeline

| Field | has_historical_iv | Backfill? | Notes |
|---|---|---|---|
| has_historical_iv | 0 for all historical rows | — | Set to 1 only when prospective collection captures real IV |
| atm_iv (all offsets) | 0 → NULL, never 0 | ❌ historical | No free historical options data. Prospective from Aug/Sep 2026. |
| iv_rank_52w | 0 → NULL | ❌ historical | |
| expected_move_pct, implied_move_pct | 0 → NULL | ❌ historical | |
| iv_crush_pct | 0 → NULL | ❌ historical | Needs both pre and post IV snapshots |
| realized_move_pct | always populated regardless of IV | ✅ | abs(ret_1d) from history(). Backfillable for all 8q. |

**No substitution rule**: When `has_historical_iv = 0`, all IV fields are NULL in the database and passed as NaN to the model. XGBoost handles NaN natively (splits route NaN down the "missing" branch). Do not impute, do not fill zero.

---

## Table 3 — earnings_nlp_signals

### Loughran-McDonald v1 Approach

Source: SEC EDGAR full-text search (`efts.sec.gov`) — free, no rate limit if polite (1 req/sec, User-Agent header required).

Scope: All 8-K filings for 83 tickers over 2 years = ~498 filings. Feasible in one collection run (~2-3 hours including parsing).

| Field group | Source | Backfill? | Notes |
|---|---|---|---|
| *_count (raw word counts) | 8-K text + LM dictionary | ✅ | LM dictionary free download: loughranmcdonald.com |
| *_pct (normalized) | computed: count / total_words | ✅ | |
| *_delta (quarter-over-quarter change) | computed from prior row | ✅ | NULL for first observation per ticker |
| Section scores (release_, mda_, guidance_) | 8-K section parsing | ⚠️ | Sections vary by company; some 8-Ks are unseparated. Score whole doc as fallback. |
| filing_url | SEC EDGAR accession number | ✅ | Store for auditability / re-scoring |

### Section parsing strategy
8-K structure varies. Parse in order of reliability:
1. **Exhibit 99.1** (earnings press release) — almost always present, cleanest signal
2. **Item 2** (MD&A) — present in 10-Q/10-K, not in pure 8-K; may require separate 10-Q pull
3. **Item 1A** (Risk Factors) — present in annual filings; too static for earnings events
4. **Forward-looking statements section** — often a legal disclaimer block; filter carefully
5. Whole-document fallback if section boundaries undetectable

### Delta is the primary feature
`negative_delta = current_negative_pct - prior_quarter_negative_pct`

Example:
```
Q1 2025: negative_pct = 1.8%, uncertainty_pct = 0.9%
Q2 2025: negative_pct = 3.1%, uncertainty_pct = 1.7%
  => negative_delta   = +1.3%   (language deteriorating)
  => uncertainty_delta = +0.8%  (management hedging more)
```

The delta captures regime change in management tone, not baseline style differences between companies (which the absolute score conflates with genuine signal).

---

## Table 4 — corporate_events

| Event type | Source | Backfill? | Notes |
|---|---|---|---|
| Dividends (initiate, increase, cut, special) | yfinance `.dividends` | ✅ | Full time series per ticker |
| Stock splits, reverse splits | yfinance `.splits` | ✅ | |
| Buyback announcements | SEC 8-K Item 8.01 + Form SC TO-I | ⚠️ | Requires 8-K scraping; lower priority for v1 |
| M&A / spinoffs | SEC 8-K + news | ❌ for v1 | Complex; omit from v1 |

---

## Collection Summary

| Run | Effort | API calls | Time |
|---|---|---|---|
| yfinance backfill (fundamentals + returns) | One script | 83 × 5 calls = 415 | ~30 min with 2s sleep |
| yfinance corporate events | One script | 83 × 2 calls = 166 | ~10 min |
| SEC EDGAR 8-K collection + LM scoring | One script | ~498 HTTP GETs | ~2-3 hrs |
| Prospective IV collection (ongoing) | Cron job at each earnings event | ~5 calls per event | Ongoing |

## Point-in-time Flag Summary

| Feature group | point_in_time_verified |
|---|---|
| EPS/revenue actuals from quarterly financials | TRUE |
| EPS estimate + surprise from yfinance earnings_history | TRUE (Yahoo stores pre-event snapshot) |
| Revenue estimate from FMP (current consensus) | FALSE — exclude from training |
| forward_pe from yfinance.info | FALSE — live value, not historical |
| Valuation computed from stored price + stored actuals | TRUE |
| Valuation percentiles (rolling from prior rows) | TRUE |
| Post-earnings returns | TRUE (future of earnings_date, not leakage into features) |
| NLP scores from 8-K filed on earnings_date | TRUE |
| IV features when has_historical_iv = 1 | TRUE |
