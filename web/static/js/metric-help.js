/**
 * metric-help.js — Reusable metric tooltip component
 *
 * Include this script once in any page that shows financial metrics.
 * It adds a global MetricHelp object with:
 *   - METRIC_HELP / ML_HELP dictionaries (extendable per-page)
 *   - MetricHelp.btn(key, dict?)  → HTML string for the ? button
 *   - MetricHelp.extend(dict)     → merge extra entries into METRIC_HELP
 *
 * Tooltip appears on click, closes on outside-click or Escape.
 * No external dependencies.
 */

(function (global) {
  'use strict';

  // ── Metric definitions ────────────────────────────────────────────────────
  // Each entry: { desc: string, calc: string }
  // desc  — plain-English explanation shown in the popup
  // calc  — window/source line shown in the ⏱ footer

  const METRIC_HELP = {
    'IV Rank': {
      desc: 'Where current implied volatility sits within its 52-week range. 0% = historically cheapest, 100% = most expensive. Higher values favour premium selling.',
      calc: '52-week IV range',
    },
    'HV20': {
      desc: 'Historical (realised) volatility — annualised std dev of daily log-returns. Shows how much the stock has actually moved, not what the market expects.',
      calc: '20 trading days (~1 month)',
    },
    'ATM IV': {
      desc: 'Implied volatility of the nearest at-the-money option. What the market is pricing in for future moves. Compare with HV20 via IV Premium.',
      calc: 'Nearest expiry options chain, ATM strike',
    },
    'IV Premium': {
      desc: 'ATM IV minus HV20. Positive = options are rich vs realised vol (good for selling). Negative = vol is cheap (good for buying).',
      calc: 'ATM IV − HV20',
    },
    'RSI': {
      desc: 'Relative Strength Index: momentum oscillator 0–100. >70 overbought, <30 oversold, 40–60 neutral momentum.',
      calc: '14 trading days, close-to-close',
    },
    'ADX': {
      desc: 'Average Directional Index: trend strength 0–50+. <20 = choppy/range-bound, >25 = strong trend. Direction-neutral — does not indicate up or down.',
      calc: '14 trading days, ATR-smoothed',
    },
    'Beta vs SPY': {
      desc: 'How much this stock moves relative to SPY. Beta 1.5 = 1.5× SPY\'s daily move on average. Used for hedge sizing and correlation risk.',
      calc: '60 trading days (~3 months)',
    },
    'Rel. Volume': {
      desc: 'Today\'s volume ÷ N-day average. >1.5 = elevated activity, <0.5 = thin market. Avoid illiquid strikes on thin-volume days.',
      calc: '20-day average daily volume',
    },
    'Trend': {
      desc: 'Bull / Bear / Range-bound: classified from close price vs short and long SMA with a deadband to avoid noise at the boundary.',
      calc: '20-day & 50-day SMA with 0.5% band',
    },
    'Earn. Days': {
      desc: 'Calendar days until the next earnings announcement. Trades spanning earnings carry binary event risk — most structures add an earnings blackout.',
      calc: 'Next confirmed earnings date from yfinance',
    },
    'Signal Score': {
      desc: 'Composite rulebook score (0–100) combining IV environment, trend alignment, earnings proximity, and structure suitability. Higher = stronger setup.',
      calc: 'Weighted sum of rulebook gates',
    },
    'POP': {
      desc: 'Probability of Profit: statistical likelihood of the trade expiring in the black based on implied volatility and strike distance.',
      calc: 'Black-Scholes derived from ATM IV',
    },
    'Max Profit': {
      desc: 'Maximum possible gain per share if the trade expires at its best-case scenario (credit spread: full premium kept; debit spread: full width captured).',
      calc: 'Structure-specific at expiry',
    },
    'Max Loss': {
      desc: 'Maximum possible loss per share (credit spread: width minus premium; debit spread: premium paid). Always defined — no undefined-risk structures.',
      calc: 'Structure-specific at expiry',
    },
    'Capital at Risk': {
      desc: 'Dollar amount locked up as margin or cost basis for this position. For credit spreads = spread width × 100 × contracts minus premium received.',
      calc: 'Per-trade margin calculation',
    },
    'DTE': {
      desc: 'Days To Expiry — calendar days remaining until the option expires. Theta decay accelerates under ~21 DTE.',
      calc: 'Expiry date − today',
    },
    'Theta': {
      desc: 'Daily time-decay dollar value per share. Negative for long options (cost), positive for short premium positions (income).',
      calc: 'Black-Scholes, current price and IV',
    },
    'Delta': {
      desc: 'Net directional exposure per share. +0.30 means the position gains ~$0.30 for each $1 the stock rises. Closer to 0 = more market-neutral.',
      calc: 'Sum of leg deltas, current price and IV',
    },
    'Ann. Gain': {
      desc: 'Annualised return on risk: (Max Profit ÷ Max Loss) × (365 ÷ DTE) × 100. Normalises different expiry lengths so positions can be compared on the same scale. ≥50% is strong, ≥20% acceptable.',
      calc: 'Max Profit / |Max Loss| × 365 / DTE',
    },
    'Move to BE': {
      desc: 'How far the underlying must move (as % of current price) to reach the break-even point. Negative = stock must fall, positive = must rise. Smaller absolute value = less room for error.',
      calc: '(Break-even strike − current price) / current price',
    },
    'IV Skew': {
      desc: 'Volatility skew: the difference between OTM put IV and OTM call IV. Negative skew (puts more expensive) is normal — reflects downside demand. A strongly negative skew signals fear; near-zero skew is unusual and may indicate complacency.',
      calc: 'OTM put IV − OTM call IV, nearest expiry',
    },
    'IV Term': {
      desc: 'Term structure of implied volatility: Contango = near-term IV < far-term IV (normal, market calm). Backwardation = near-term IV > far-term IV (elevated near-term fear or event risk). Backwardation often precedes vol crush after the event.',
      calc: 'Front expiry ATM IV vs next expiry ATM IV',
    },
    'Trend W': {
      desc: 'Weekly (longer-timeframe) trend: Bull = price above both 10-week and 20-week SMA. Bear = price below both. Range-bound = mixed. Use to confirm or counter the daily trend signal.',
      calc: '10-week & 20-week SMA with 0.5% band',
    },
    'EMA200': {
      desc: 'Price position relative to the 200-day Exponential Moving Average — the standard institutional trend filter. Above = long-term uptrend (institutions are net buyers). Below = long-term downtrend.',
      calc: '200-day EMA of closing prices',
    },
    'MACD': {
      desc: 'MACD trend signal: Bullish = MACD line crossed above the signal line (momentum turning positive). Bearish = MACD below signal (momentum turning negative). Used to confirm trend direction entries.',
      calc: 'EMA(12) − EMA(26) vs signal line EMA(9)',
    },
    'PCR': {
      desc: 'Put/Call Ratio: total put open interest ÷ total call open interest. >1.2 = put-heavy (bearish hedging or fear). <0.7 = call-heavy (bullish positioning). Contrarian reading: extreme PCR can signal a reversal.',
      calc: 'Total put OI / total call OI, front expiry',
    },
    'News': {
      desc: 'Sentiment of recent news headlines — Bullish, Bearish, or Neutral. Scanned from the last 5–10 headlines using a financial keyword model. Use as a directional bias signal, not a trade trigger.',
      calc: 'yfinance news feed, keyword sentiment model',
    },
    'Sector ETF': {
      desc: 'The sector ETF for this ticker (e.g. XLK for tech, XLV for healthcare) and its current daily trend. A sector in a strong uptrend provides a tailwind; a falling sector is a headwind even for individual bullish setups.',
      calc: 'GICS sector mapping → ETF daily close trend',
    },
    'Ex-Div': {
      desc: 'Ex-dividend date — the date by which you must own the stock to receive the next dividend. Options positions that span the ex-div date may be affected by early assignment risk (especially short calls).',
      calc: 'Next declared ex-dividend date from yfinance',
    },
  };

  const ML_HELP = {
    'Regime': {
      desc: 'Market regime label predicted by the Regime Classifier (Bull, Bear, Neutral). Trained on RSI, ADX, HV20, MACD trend, VIX, and SPY relative strength.',
      calc: 'XGBoost classifier · updated each morning scan',
    },
    'p(Win)': {
      desc: 'Probability of a winning trade outcome from the POP model. Requires at least 14 days of past-expiry labeled trade snapshots to have training data.',
      calc: 'XGBoost classifier · needs labeled history (14-day expiry lag)',
    },
    'Confidence': {
      desc: 'Composite trade confidence combining regime class probabilities and POP model output into a single 0–100% signal.',
      calc: 'Composite of regime probs + POP model · each scan',
    },
    'Pred. Return': {
      desc: 'Expected forward return magnitude from the Return Regressor. Not directional — use alongside Trend and Regime for direction context.',
      calc: 'XGBoost regressor · target = 14-day forward return',
    },
    'Pred. Vol': {
      desc: 'Predicted forward realised volatility from the Volatility Forecast model. Higher values suggest wider expected price swings ahead.',
      calc: 'XGBoost regressor · target = 14-day forward HV',
    },
    'Signal Rating': {
      desc: 'Overall trade signal quality combining ML regime confidence, POP probability, and rulebook score into a letter grade (A–D).',
      calc: 'Composite · updated each morning scan',
    },
  };

  // ── Tooltip engine ────────────────────────────────────────────────────────

  let _activeTip = null;

  function _close() {
    if (_activeTip) { _activeTip.remove(); _activeTip = null; }
  }

  function _open(key, dict, anchorEl) {
    _close();
    const info = dict[key];
    if (!info) return;

    const tip = document.createElement('div');
    tip.className = 'mh-tip';
    tip.innerHTML =
      `<strong>${_esc(key)}</strong>` +
      `<p>${_esc(info.desc)}</p>` +
      `<div class="mh-calc">⏱ ${_esc(info.calc)}</div>`;
    document.body.appendChild(tip);

    // Position below anchor, clamp to viewport
    const r = anchorEl.getBoundingClientRect();
    const tipW = tip.offsetWidth;
    let left = r.left + window.scrollX;
    if (left + tipW > window.innerWidth - 12) left = window.innerWidth - tipW - 12;
    tip.style.left = Math.max(8, left) + 'px';
    tip.style.top  = (r.bottom + window.scrollY + 6) + 'px';

    _activeTip = tip;
  }

  function _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Global click delegation — handles all .mh-btn clicks and outside-close
  document.addEventListener('click', function (e) {
    if (e.target.classList && e.target.classList.contains('mh-btn')) {
      const key  = e.target.dataset.mhKey;
      const map  = e.target.dataset.mhMap === 'ml' ? ML_HELP : METRIC_HELP;
      // Toggle: clicking the same open tip closes it
      if (_activeTip && _activeTip.dataset.mhKey === key) { _close(); }
      else { _open(key, map, e.target); if (_activeTip) _activeTip.dataset.mhKey = key; }
      e.stopPropagation();
      return;
    }
    if (_activeTip && !_activeTip.contains(e.target)) _close();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') _close();
  });

  // ── Public API ────────────────────────────────────────────────────────────

  /**
   * Returns an HTML string for the ? button.
   * @param {string} key   - Metric name (must exist in dict)
   * @param {object} [dict] - Defaults to METRIC_HELP; pass ML_HELP for ML fields
   */
  function btn(key, dict) {
    const map = dict || METRIC_HELP;
    const mapName = map === ML_HELP ? 'ml' : 'metric';
    if (!map[key]) return '';
    return `<button class="mh-btn" type="button" aria-label="Help for ${_esc(key)}" ` +
           `data-mh-key="${_esc(key)}" data-mh-map="${mapName}">?</button>`;
  }

  /**
   * Merge additional entries into METRIC_HELP (call before rendering tables).
   * Useful for page-specific metrics not in the shared dictionary.
   */
  function extend(extraDict) {
    Object.assign(METRIC_HELP, extraDict || {});
  }

  /**
   * Merge additional entries into ML_HELP.
   */
  function extendML(extraDict) {
    Object.assign(ML_HELP, extraDict || {});
  }

  global.MetricHelp = { METRIC_HELP, ML_HELP, btn, extend, extendML };

}(window));
