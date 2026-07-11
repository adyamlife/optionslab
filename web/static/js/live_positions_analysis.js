/**
 * Live Positions Analysis Module
 * Generates market signals and feedback for positions
 * Phase 2 Refactoring: Analysis isolation
 */

// ── Position Classification ────────────────────────────────────────────────────

/**
 * Check if a position is an option position (vs stock-only)
 * @param {Object} sp - Position object with dte and expiry
 * @returns {boolean}
 */
function isOptionPosition(sp) {
  return sp.dte != null && sp.expiry != null;
}

// ── Market Signals Building ────────────────────────────────────────────────────
// getRecommendedCandidate and buildPositionTrackingFeedback are provided by
// lib/position-health.js (shared with Live Suggestions' candidate handling).

/**
 * Build market analysis signals grid from analysis data
 * @param {Object} analysis - Analysis data from /api/analyze
 * @returns {string} HTML for market signals
 */
function _buildMlSection(ml) {
  if (!ml) return "";
  if (!ml.ok) return `<div class="lp-ml-section"><div class="lp-ml-section-title">ML Analysis</div><p class="muted" style="font-size:.8rem;margin:0">Unavailable: ${ml.error || "unknown error"}</p></div>`;

  const rows = [];
  const cls = (v, good, bad) => v >= good ? "pass" : v <= bad ? "fail" : "";

  if (ml.regime) {
    const rCls = ml.regime === "Uptrend" ? "pass" : ml.regime === "Downtrend" ? "fail" : "warn";
    const probaStr = ml.regime_proba
      ? Object.entries(ml.regime_proba).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${k} ${(v*100).toFixed(0)}%`).join(" · ")
      : "";
    const impl = ml.regime === "Uptrend"
      ? "Bull put spreads, covered calls, and call debit spreads align with trend."
      : ml.regime === "Downtrend"
      ? "Bear call spreads and put debit spreads align with trend."
      : "Range-bound — iron condors may fit. Watch for breakout.";
    rows.push(["ML Regime", ml.regime, rCls, `${impl}${probaStr ? " (" + probaStr + ")" : ""}`]);
  }

  if (ml.expected_move_pct != null) {
    const em = (ml.expected_move_pct * 100).toFixed(1);
    const fvol = ml.expected_vol != null ? ` (forecast vol: ${(ml.expected_vol*100).toFixed(1)}% ann.)` : "";
    rows.push(["ML Move ±", `±${em}%`, "", `ML predicts ±${em}% over the next 10 days${fvol}. Check whether your strikes are safely beyond this range.`]);
  }

  if (ml.iv_direction) {
    const ivCls = ml.iv_direction === "Expanding" ? "warn" : "pass";
    const prob = ml.iv_expanding_prob != null ? ` (${(ml.iv_expanding_prob*100).toFixed(0)}% confidence)` : "";
    const desc = ml.iv_direction === "Expanding"
      ? `IV rank likely rising${prob}. For credit positions, rising IV expands the spread against you — consider tightening stops or reducing size.`
      : `IV rank likely falling${prob}. Vol contraction helps credit positions; watch for reduced extrinsic value on debit legs.`;
    rows.push(["ML IV Dir", ml.iv_direction, ivCls, desc]);
  }

  if (ml.p_up != null) {
    const pUp = ml.p_up * 100;
    const pCls = cls(pUp, 60, 40);
    const dir = pUp >= 55 ? "Bullish lean" : pUp <= 45 ? "Bearish lean" : "No strong directional lean";
    const desc = pUp >= 60
      ? "Model sees strong upside probability — bullish structures supported."
      : pUp <= 40
      ? "Model sees strong downside probability — review bullish positions for risk."
      : "Directional signal is mixed. Position sizing caution advised.";
    rows.push(["ML P(↑)", `${pUp.toFixed(0)}%`, pCls, `${dir}. ${desc}`]);
  }

  if (ml.meta_score != null) {
    const m   = ml.meta_score;
    const mCls = cls(m, 65, 35);
    const pd  = ml.pred_dist;
    const agr = pd ? pd.model_agreement : null;
    const agrCls = agr === "High" ? "pass" : agr === "Low" ? "fail" : "warn";
    const agrStr = agr ? ` <span class="${agrCls}" style="font-size:.75rem;font-weight:600">${agr} agr.</span>` : "";
    const confStr = pd && pd.confidence != null ? ` Confidence ${(pd.confidence*100).toFixed(0)}%.` : "";
    const desc = m >= 65
      ? "Strong bullish consensus across all 5 ML models."
      : m <= 35
      ? "Strong bearish consensus across all 5 ML models — review long-delta exposure."
      : "Models disagree — no strong composite signal. Trade defensively.";
    rows.push(["ML Meta", `${m.toFixed(0)}/100${agrStr}`, mCls, `Composite of all models (0–100).${confStr} ${desc}`]);
  }

  if (ml.pred_dist) {
    const pd = ml.pred_dist;
    if (pd.p10_pnl != null && pd.p90_pnl != null) {
      const p10Cls = pd.p10_pnl >= 0 ? "pass" : "fail";
      const p90Cls = pd.p90_pnl >= 0 ? "pass" : "fail";
      const evStr  = pd.ev_per_share != null ? ` EV ${pd.ev_per_share >= 0 ? "+" : ""}$${pd.ev_per_share.toFixed(2)}/sh.` : "";
      const srcStr = pd.vol_source ? ` (${pd.vol_source})` : "";
      rows.push(["MC P10/P90",
        `<span class="pass">${pd.p10_pnl >= 0 ? "+" : ""}$${pd.p10_pnl.toFixed(2)}</span> – <span class="${p90Cls}">+$${pd.p90_pnl.toFixed(2)}</span>`,
        "",
        `Monte Carlo P&L range per share${srcStr}.${evStr} Tighter range = more predictable outcome.`
      ]);
    }
  }

  if (ml.anomaly_score != null) {
    const a = ml.anomaly_score;
    const aCls = ml.is_anomaly ? (a <= 20 ? "fail" : "warn") : "";
    const flags = (ml.anomaly_flags || []).slice(0, 3).join(", ") || "multi-feature outlier";
    const desc = ml.is_anomaly
      ? `Unusual conditions detected: ${flags}. ML confidence is reduced — consider reducing position size or widening stops.`
      : "Conditions are within normal historical range. ML models are reliable.";
    rows.push(["ML Anomaly", `${a.toFixed(0)}${ml.is_anomaly ? " ⚠" : ""}/100`, aCls, desc]);
  }

  if (!rows.length) return "";

  const rowsHtml = rows.map(([signal, val, vCls, desc]) =>
    `<div class="lp-ml-row">
      <span class="lp-ml-signal">${signal}</span>
      <span class="lp-ml-val${vCls ? " " + vCls : ""}">${escHtml(val)}</span>
      <span class="lp-ml-desc">${desc}</span>
    </div>`).join("");

  return `<div class="lp-ml-section">
    <div class="lp-ml-section-title">ML Analysis</div>
    ${rowsHtml}
  </div>`;
}

function buildPositionMarketSignals(analysis) {
  if (!analysis) return "";

  const data = analysis || {};
  const rec = getRecommendedCandidate(data);
  const trend = data.trend || "N/A";
  const rsi = data.rsi || "N/A";
  const macd = data.macd_trend || "N/A";
  const adx = data.adx || "N/A";
  const relVol = data.rel_volume != null ? data.rel_volume.toFixed(2) : "N/A";
  const ivEnv = data.iv_env || "N/A";
  const pcr = data.pcr != null ? data.pcr.toFixed(2) : "N/A";
  const pop = rec && rec.pop != null ? rec.pop.toFixed(1) : "N/A";

  const mlSection = _buildMlSection(data.ml);

  return `
    <div class="lp-market-signals">
      <div class="lp-market-analysis-summary">
        <strong>Market Signals</strong>
      </div>
      <div class="lp-market-analysis-grid">
        <div><label>Trend:</label> <span>${escHtml(trend)}</span></div>
        <div><label>RSI:</label> <span>${rsi}</span></div>
        <div><label>MACD:</label> <span>${escHtml(macd)}</span></div>
        <div><label>ADX:</label> <span>${adx}</span></div>
        <div><label>Rel Vol:</label> <span>${relVol}x</span></div>
        <div><label>IV Env:</label> <span>${escHtml(ivEnv)}</span></div>
        <div><label>PCR:</label> <span>${pcr}</span></div>
        <div><label>POP%:</label> <span>${pop}%</span></div>
      </div>
      ${mlSection}
    </div>
  `;
}

// ── Feedback Building ──────────────────────────────────────────────────────────

/**
 * Build feedback/commentary for a position based on analysis
 * @param {Object} sp - Position object
 * @param {Object} analysis - Analysis data
 * @returns {string} HTML for feedback
 */
function buildPositionFeedback(sp, analysis) {
  if (!analysis) return "";

  // Note: analysis.status may be "SKIP - earnings in Xd (within blackout
  // window)" — that gate blocks *new* trade entry suggestions, but it
  // shouldn't suppress feedback for a position already held, so it's
  // intentionally ignored here.
  const rec = getRecommendedCandidate(analysis);
  const feedbackItems = [];

  // Probability of Profit (lives on the recommended candidate, not the row)
  if (rec && rec.pop != null) {
    const pop = rec.pop;
    const popCls = pop >= 65 ? "pass" : pop >= 50 ? "na" : "fail";
    feedbackItems.push({
      type: "pop",
      title: "Probability of Profit",
      value: `${pop.toFixed(1)}%`,
      className: popCls,
      details: pop >= 65 ? "Favorable odds" : pop >= 50 ? "Fair odds" : "Challenging odds"
    });
  }

  // Annualized Gain
  if (sp.max_profit_ps != null && sp.max_loss_ps != null && sp.dte != null) {
    const annGain = (sp.max_profit_ps / sp.max_loss_ps) * (365 / sp.dte) * 100;
    const annGainCls = annGain >= 50 ? "pass" : annGain >= 20 ? "na" : "fail";
    feedbackItems.push({
      type: "ann_gain",
      title: "Annualized Gain %",
      value: `${annGain.toFixed(1)}%`,
      className: annGainCls,
      details: annGain >= 50 ? "Excellent return" : annGain >= 20 ? "Moderate return" : "Below target"
    });
  }

  // Market Bias (lives on the recommended candidate, not the row)
  if (rec && rec.market_bias) {
    const bias = rec.market_bias;
    const biasCls = bias.score >= 0.5 ? "pass" : bias.score <= -0.5 ? "fail" : "na";
    feedbackItems.push({
      type: "bias",
      title: "Market Bias",
      value: bias.label || "Neutral",
      className: biasCls,
      details: (bias.notes || []).join(" · ")
    });
  }

  // Trend Alignment
  if (analysis.trend) {
    const trend = analysis.trend;
    const trendCls = trend.includes("Uptrend") ? "pass" : trend.includes("Downtrend") ? "fail" : "na";
    feedbackItems.push({
      type: "trend",
      title: "Trend (Daily)",
      value: trend,
      className: trendCls,
      details: ""
    });
  }

  // IV Environment
  if (analysis.iv_env) {
    const ivCls = analysis.iv_env === "High" ? "pass" : "na";
    feedbackItems.push({
      type: "iv",
      title: "IV Environment",
      value: analysis.iv_env,
      className: ivCls,
      details: ""
    });
  }

  // Risk Assessment
  if (sp.max_loss_ps != null) {
    const maxLoss = Math.abs(sp.max_loss_ps);
    const maxLossCls = maxLoss <= 1.0 ? "pass" : maxLoss <= 2.0 ? "na" : "fail";
    feedbackItems.push({
      type: "risk",
      title: "Max Loss per Share",
      value: `$${maxLoss.toFixed(2)}`,
      className: maxLossCls,
      details: maxLoss <= 1.0 ? "Acceptable risk" : maxLoss <= 2.0 ? "Moderate risk" : "High risk"
    });
  }

  // Build HTML
  if (feedbackItems.length === 0) {
    return `<div class="lp-feedback"><p class="muted">No feedback available</p></div>`;
  }

  const feedbackHtml = feedbackItems.map(item => {
    return `
      <div class="lp-feedback-section ${item.className}">
        <div class="lp-feedback-title">${escHtml(item.title)}</div>
        <div class="lp-feedback-value">${escHtml(item.value)}</div>
        ${item.details ? `<div class="lp-feedback-details">${escHtml(item.details)}</div>` : ""}
      </div>
    `;
  }).join("");

  return `<div class="lp-feedback">${feedbackHtml}</div>`;
}

// ── Feedback Filtering ─────────────────────────────────────────────────────────

/**
 * Check if position should display feedback
 * Only shows feedback for option positions, not stock-only
 * @param {Object} sp - Position object
 * @returns {boolean}
 */
function shouldShowFeedback(sp) {
  return isOptionPosition(sp);
}

/**
 * Filter analysis array to only include option positions
 * @param {Array} spreads - Position array
 * @returns {Array} Filtered positions
 */
function filterOptionPositions(spreads) {
  return (spreads || []).filter(sp => isOptionPosition(sp));
}

// ── Confidence Scoring ─────────────────────────────────────────────────────────

/**
 * Calculate confidence score for feedback (0-100)
 * Based on data completeness
 * @param {Object} analysis - Analysis data
 * @returns {number} Confidence score
 */
function calculateConfidenceScore(analysis) {
  if (!analysis) return 0;

  let score = 0;
  const maxScore = 100;
  const factors = {
    pop: 20,
    trend_daily: 15,
    rsi: 10,
    macd_trend: 10,
    adx: 10,
    iv_env: 10,
    market_bias: 15,
  };

  for (const [key, points] of Object.entries(factors)) {
    if (analysis[key] != null) {
      score += points;
    }
  }

  return Math.min(score, maxScore);
}

// ── Helper Text Generation ────────────────────────────────────────────────────

/**
 * Generate trading recommendation text based on analysis
 * @param {Object} sp - Position
 * @param {Object} analysis - Analysis data
 * @returns {string} Recommendation text
 */
function generateRecommendation(sp, analysis) {
  if (!analysis) return "Insufficient data for recommendation";

  const pop = analysis.pop || 0;
  const trend = analysis.trend_daily || "";
  const bias = analysis.market_bias?.score || 0;

  const recommendations = [];

  if (pop >= 65 && bias > 0) {
    recommendations.push("Strong setup: High POP with favorable bias");
  } else if (pop >= 65) {
    recommendations.push("High probability but watch market direction");
  } else if (pop >= 50) {
    recommendations.push("Fair setup, monitor closely");
  } else {
    recommendations.push("Lower probability, consider alternatives");
  }

  if (trend.includes("Range")) {
    recommendations.push("Range-bound market suggests neutral strategy");
  }

  return recommendations.join(" • ");
}
