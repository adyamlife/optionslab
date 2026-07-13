/**
 * Position Health Library — pure renderer
 *
 * All decision/scoring logic lives server-side in scripts/decision_provider.py
 * (single source of truth for Live Suggestions, Live Positions, and Paper
 * Trades alike — see that module's docstring). This file only fetches a
 * decision from /api/decision and renders it; it does not score anything.
 */

/**
 * Get the rulebook-recommended candidate from an analysis row.
 * pop/market_bias/ev/etc. live on the candidate, not on the row itself.
 * @param {Object} analysis - Analysis row from /api/analyze
 * @returns {Object|null}
 */
function getRecommendedCandidate(analysis) {
  if (!analysis || !Array.isArray(analysis.candidates)) return null;
  return analysis.candidates.find(c => c.recommended) || null;
}

/**
 * Fetch a decision for a HELD position from the backend (Live Positions /
 * Paper Trades only — Live Suggestions' candidates already carry their
 * decision inline on candidate.decision, computed server-side in the same
 * /api/analyze pass, with no extra round trip needed).
 * @param {Object} row - Analysis row from /api/analyze (trend, rsi, macd_trend, etc.)
 * @param {Object} position - {structure, pnl_pct, dte, move_to_be_pct, proximity}
 * @returns {Promise<{verdict, verdict_cls, score, bias, reasons, action}>}
 */
async function fetchDecision(row, position) {
  const res = await fetch("/api/decision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ row, position }),
  });
  const data = await res.json();
  if (!data.ok) {
    throw new Error(data.error || "Failed to fetch decision");
  }
  return data.decision;
}

/**
 * Build a compact verdict pill for a card header (e.g. "STRONG"),
 * surfacing the most important signal before anything else loads.
 * @param {Object} decision - Result of fetchDecision() or candidate.decision
 * @param {Function} [escFn] - HTML-escape function to use (defaults to escHtml)
 * @returns {string} HTML, or "" if no decision available
 */
function buildVerdictBadge(decision, escFn) {
  const esc = escFn || (typeof escHtml === "function" ? escHtml : (s) => s);
  if (!decision) return "";
  return `<span class="pu-verdict-badge ${decision.verdict_cls}">${esc(decision.verdict)}</span>`;
}

/**
 * Build the full "is this position on track?" feedback card with reasoning
 * and a suggested action, for a HELD position.
 * @param {Object} decision - Result of fetchDecision()
 * @param {Function} [escFn] - HTML-escape function to use (defaults to escHtml)
 * @returns {string} HTML
 */
function buildPositionTrackingFeedback(decision, escFn) {
  const esc = escFn || (typeof escHtml === "function" ? escHtml : (s) => s);
  if (!decision) return "";

  const { verdict, verdict_cls, bias, reasons, action } = decision;

  // Encode detail for the modal — stored as JSON in a data attribute
  const detail = {
    title: `Position Bias: ${bias}`,
    reasons: reasons || [],
    action: action || "",
    note: "Based on the same alignment engine Live Suggestions uses for new candidates, evaluated against the structure you actually hold.",
  };
  const detailJson = esc(JSON.stringify(detail));

  const summaryLine = (reasons || []).length ? esc(reasons[0]) : "Not enough signal data to assess.";

  return `
    <div class="lp-tracking-card ${verdict_cls}">
      <div class="lp-tracking-header">
        <span class="lp-tracking-verdict ${verdict_cls}">${esc(verdict)}</span>
        <span class="lp-tracking-bias">${esc(bias)}</span>
        <button class="lp-info-btn" data-info-type="bias" data-info='${detailJson}' title="Show full reasoning">?</button>
      </div>
      <p class="lp-tracking-summary">${summaryLine}${(reasons || []).length > 1 ? ` <span class="muted lp-more-hint">+${reasons.length - 1} more</span>` : ""}</p>
      ${action ? `<div class="lp-tracking-action"><strong>→</strong> ${esc(action)}</div>` : ""}
    </div>
  `;
}
