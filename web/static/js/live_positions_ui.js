/**
 * Live Positions UI Module
 * Handles all rendering and DOM manipulation
 * Phase 2 Refactoring: UI isolation
 */

// ── Position Card Rendering ────────────────────────────────────────────────────

/**
 * Build a deterministic, collision-free analysis placeholder ID from a
 * ticker + position description, so the renderer and the async analysis
 * fetcher can independently compute the same ID without needing to share
 * state or rely on document order.
 * @param {string} ticker - Ticker symbol
 * @param {string} desc - Position description (unique per spread)
 * @returns {string}
 */
function makeAnalysisId(ticker, desc) {
  const slug = `${ticker}-${desc || ""}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `analysis-${slug}`;
}

/**
 * Render a single position spread card
 * @param {Object} sp - Position spread data
 * @param {string} ticker - Ticker symbol
 * @returns {string} HTML for position card
 */
function renderSpreadLP(sp, ticker) {
  if (!sp) return "";

  const pnlC = lpCls(sp.unrealized_pnl);
  const popC = sp.pop_est == null ? "na" : sp.pop_est >= 60 ? "pass" : sp.pop_est >= 40 ? "na" : "warn";
  const dteC = sp.dte == null ? "na" : sp.dte <= 5 ? "fail" : sp.dte <= 14 ? "warn" : "na";
  const moveC = sp.move_to_be_pct == null ? "na"
    : Math.abs(sp.move_to_be_pct) > 10 ? "fail"
      : Math.abs(sp.move_to_be_pct) > 3 ? "warn" : "pass";

  // Risk label
  let riskCls = "na", riskLabel = "—";
  if (sp.max_loss_ps != null) {
    if (sp.max_loss_ps <= -0.50) { riskCls = "pass"; riskLabel = "Low"; }
    else if (sp.max_loss_ps <= -1.00) { riskCls = "na"; riskLabel = "Med"; }
    else { riskCls = "fail"; riskLabel = "High"; }
  }

  // Annualized gain
  let annGainHtml = "";
  if (sp.max_profit_ps != null && sp.max_loss_ps != null && sp.dte != null && sp.dte > 0) {
    const annGain = (sp.max_profit_ps / Math.abs(sp.max_loss_ps)) * (365 / sp.dte) * 100;
    const annGainCls = annGain >= 50 ? "pass" : annGain >= 20 ? "na" : "fail";
    annGainHtml = `<div class="pu-metric"><div class="pu-metric-label">Ann. Gain %</div><div class="pu-metric-value ${annGainCls}">${annGain.toFixed(1)}%</div></div>`;
  }

  const analysisId = makeAnalysisId(ticker, sp.desc);
  const verdictSlot = isOptionPosition(sp)
    ? `<span class="pu-verdict-badge pu-verdict-loading" data-verdict-id="${analysisId}">···</span>`
    : "";
  const priceBadgeHtml = buildPriceBadgeFromPosition(sp);
  const proximityBadge = buildStrikeProximityBadge(getPositionShortStrikes(sp), sp.ul_price);

  return `
    <div class="pu-spread-card" data-ticker="${escHtml(ticker)}">
      <div class="pu-spread-header">
        <h3 class="pu-spread-title">${escHtml(sp.desc || "Position")}</h3>
        ${priceBadgeHtml}
        ${proximityBadge}
        ${verdictSlot}
      </div>

      <div class="pu-metrics">
        <div class="pu-metric">
          <div class="pu-metric-label">P&L</div>
          <div class="pu-metric-value ${pnlC}">${fmtMoney(sp.unrealized_pnl)}</div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">POP</div>
          <div class="pu-metric-value ${popC}">${sp.pop_est != null ? sp.pop_est.toFixed(1) + "%" : "—"}</div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">DTE</div>
          <div class="pu-metric-value ${dteC}">${sp.dte || "—"}</div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Move to BE</div>
          <div class="pu-metric-value ${moveC}">${sp.move_to_be_pct != null ? sp.move_to_be_pct.toFixed(1) + "%" : "—"}</div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Risk</div>
          <div class="pu-metric-value ${riskCls}">${riskLabel}</div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Max Profit</div>
          <div class="pu-metric-value pass">${fmtMoney(sp.max_profit_ps)}</div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Max Loss</div>
          <div class="pu-metric-value fail">${fmtMoney(sp.max_loss_ps)}</div>
        </div>
        ${annGainHtml}
      </div>

      ${renderPnlExplanation(sp)}

      ${buildHedgeBlock(sp.hedge, sp.structure, sp.hedge_exact, sp)}

      ${isOptionPosition(sp) ? `
      <div class="lp-greeks-drift-placeholder" data-analysis-id="${analysisId}"></div>
      ` : ""}

      <div class="lp-analysis-placeholder" data-analysis-id="${analysisId}">
        <p class="lp-loading-text">Loading market analysis…</p>
      </div>
    </div>
  `;
}

// ── P&L Explanation Rendering ─────────────────────────────────────────────────
// renderPnlExplanation and renderHedgePnlAnalysis are provided by common.js

// ── Hedge Rendering ───────────────────────────────────────────────────────────
// buildHedgeBlock is provided by lib/hedge-block.js (shared with Live Suggestions)

// ── Position Results Rendering ─────────────────────────────────────────────────

/**
 * Render all position results (main container)
 * @param {Array} groups - Grouped position data
 * @param {string} filename - Current file name
 * @param {Element} el - DOM element to populate
 */
function renderPositionResults(groups, filename, el) {
  if (!el || !groups || !groups.length) {
    if (el) el.innerHTML = `<p class="lp-empty-message">No positions found.</p>`;
    return;
  }

  const isCombined = isCombinedMode ? isCombinedMode() : true;
  const filter = getFilter ? getFilter() : "all";

  let html = "";

  if (isCombined) {
    // Combined view - show groups
    for (const g of groups) {
      html += renderGroupCombined(g, filter);
    }
  } else {
    // Individual view - show all spreads
    for (const g of groups) {
      const spreads = g.spreads || [];
      for (const sp of spreads) {
        // Skip based on filter
        if (filter === "options" && !isOptionPosition(sp)) continue;

        html += renderSpreadLP(sp, g.ticker);
      }
    }
  }

  el.innerHTML = html || `<p class="lp-empty-message">No positions match this filter.</p>`;

  // Setup background analysis loading
  setupPositionAnalysis(groups, el);
}

/**
 * Set up background analysis fetching for positions
 * @param {Array} groups - Position groups
 * @param {Element} el - Results container
 */
function setupPositionAnalysis(groups, el) {
  if (!el) return;

  // Background async IIFE to fetch analysis
  (async () => {
    for (const g of groups) {
      const spreads = g.spreads || [];
      for (const sp of spreads) {
        // Skip stock-only positions
        if (!isOptionPosition(sp)) continue;

        const analysisId = makeAnalysisId(g.ticker, sp.desc);
        const analysisPlaceholder = el.querySelector(`.lp-analysis-placeholder[data-analysis-id="${analysisId}"]`);
        const verdictBadge = el.querySelector(`.pu-verdict-badge[data-verdict-id="${analysisId}"]`);
        const driftPlaceholder = el.querySelector(`.lp-greeks-drift-placeholder[data-analysis-id="${analysisId}"]`);
        if (!analysisPlaceholder) continue;

        try {
          const positionKey = `${g.ticker}-${sp.desc}`;
          console.log(`[FETCH] Analysis for ${positionKey}`);

          // Fetch analysis
          const analysis = await fetchTickerAnalysis(g.ticker);
          console.log(`[SUCCESS] Got analysis for ${g.ticker}`, analysis);

          // How close price already is to the strike that defines max loss —
          // a fact computed client-side, fed to the backend for scoring below
          const proximity = computeStrikeProximity(getPositionShortStrikes(sp), sp.ul_price);

          // Score the held position server-side (single source of truth —
          // see scripts/decision_provider.py) using the analysis row already
          // fetched above plus this position's own facts.
          const decision = await fetchDecision(analysis, {
            structure: sp.structure,
            pnl_pct: sp.pnl_pct,
            dte: sp.dte,
            move_to_be_pct: sp.move_to_be_pct,
            proximity: proximity ? {
              strike: proximity.strike,
              distance_pct: proximity.distancePct,
              risk_level: proximity.riskLevel,
            } : null,
          });

          // Build position health tracking, feedback, and signals
          const trackingHtml = buildPositionTrackingFeedback(decision);
          const feedbackHtml = buildPositionFeedback(sp, analysis);
          const marketSignalsHtml = buildPositionMarketSignals(analysis);

          // Update header verdict badge first (most important info, shown without expanding)
          if (verdictBadge) {
            verdictBadge.outerHTML = buildVerdictBadge(decision) ||
              `<span class="pu-verdict-badge na">N/A</span>`;
          }

          // Update placeholder
          analysisPlaceholder.innerHTML = `<div class="lp-analysis-panels">${trackingHtml}${feedbackHtml}${marketSignalsHtml}</div>`;
          console.log(`[UPDATE] Analysis rendered for ${positionKey}`);
        } catch (e) {
          console.error(`[ERROR] Failed to fetch analysis for ${g.ticker}:`, e);
          if (verdictBadge) verdictBadge.outerHTML = `<span class="pu-verdict-badge na">N/A</span>`;
          const errorHtml = `<div class="lp-analysis-error"><p class="lp-error-text">⚠️ Error loading analysis</p></div>`;
          analysisPlaceholder.innerHTML = errorHtml;
        }

        // Greeks drift since entry — independent fetch, doesn't block the analysis above
        if (driftPlaceholder) {
          try {
            const driftResult = await fetchGreeksDrift({ ...sp, ticker: g.ticker });
            driftPlaceholder.innerHTML = buildGreeksDriftCard(driftResult);
          } catch (e) {
            console.warn(`[Greeks Drift] Unavailable for ${g.ticker}:`, e.message);
            driftPlaceholder.innerHTML = "";
          }
        }
      }
    }
  })();
}

// ── Combined View Rendering ───────────────────────────────────────────────────

/**
 * Render combined view for a group
 * @param {Object} g - Position group
 * @param {string} filter - "all" or "options" (matches the filter bar)
 * @returns {string} HTML for group
 */
function renderGroupCombined(g, filter) {
  if (!g || !g.spreads || !g.spreads.length) return "";

  const visibleSpreads = g.spreads.filter(sp =>
    filter !== "options" || isOptionPosition(sp)
  );
  if (!visibleSpreads.length) return "";

  const groupHtml = visibleSpreads.map(sp => `
    <div class="pu-group-item">
      ${renderSpreadLP(sp, g.ticker)}
    </div>
  `).join("");

  return `
    <div class="pu-group" data-ticker="${escHtml(g.ticker)}">
      <h3 class="pu-group-title">${escHtml(g.ticker)}</h3>
      ${groupHtml}
      ${renderCombinedPnlExplanation(g)}
    </div>
  `;
}

/**
 * Render combined P&L explanation for a group
 * @param {Object} g - Group data
 * @returns {string} HTML for combined P&L
 */
function renderCombinedPnlExplanation(g) {
  if (!g || !g.combined_pnl) return "";

  const pnlCls = getStatusClass ? getStatusClass(g.combined_pnl) : (g.combined_pnl > 0 ? "pass" : "fail");

  return `
    <details class="combined-pnl-block">
      <summary class="combined-pnl-summary">
        <strong>Portfolio P&L</strong>
        <span class="${pnlCls}">${fmtMoney(g.combined_pnl)}</span>
      </summary>
      <div class="combined-pnl-body">
        <p>Combined P&L for ${escHtml(g.ticker)} across all positions</p>
        <div class="pnl-detail">
          <div>Total Profit/Loss: <span class="${pnlCls}">${fmtMoney(g.combined_pnl)}</span></div>
        </div>
      </div>
    </details>
  `;
}

// ── Helper Functions ───────────────────────────────────────────────────────────

/**
 * Check if combined mode is enabled
 * Wrapper for state module
 * @returns {boolean}
 */
function isCombinedMode() {
  return typeof window.StateManager !== 'undefined'
    ? (window.StateManager.getState('livePositions.combinedMode') ?? true)
    : true;
}

/**
 * Get current filter setting
 * Wrapper for state module
 * @returns {string}
 */
function getFilter() {
  return typeof window.StateManager !== 'undefined'
    ? (window.StateManager.getState('livePositions.filter') ?? "all")
    : "all";
}

// isOptionPosition, buildPositionFeedback, buildPositionMarketSignals are provided by live_positions_analysis.js
// fetchTickerAnalysis is provided by live_positions_service.js

// All utility functions (fmtMoney, escHtml, getStatusClass, lpPct) are provided by utils.js
// No local wrappers needed - they conflict with global scope

/**
 * Get status class (legacy name)
 * Wrapper for utils module
 * @param {number} v - Value
 * @returns {string}
 */
function lpCls(v) {
  return getStatusClass(v);
}
