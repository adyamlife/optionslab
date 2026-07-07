/**
 * Strike Proximity Library
 *
 * Shared "distance to the strike that defines max loss" signal, used by both
 * Live Suggestions (a candidate you haven't entered) and Live Positions (a
 * structure you're already holding). Early warning that price is drifting
 * toward the danger zone, independent of breakeven (move_to_be_pct), which
 * tracks P&L=0 rather than the actual strike boundary.
 */

/**
 * Find the nearest "risk-defining" short strike to spot and classify the cushion.
 * @param {Array<number|null|undefined>} strikes - Candidate short strikes to consider
 * @param {number} spot - Current underlying price
 * @returns {{strike: number, distancePct: number, riskLevel: string, riskCls: string}|null}
 */
function computeStrikeProximity(strikes, spot) {
  if (spot == null || !strikes || !strikes.length) return null;

  let nearest = null, nearestDist = Infinity;
  for (const k of strikes) {
    if (k == null) continue;
    const distPct = Math.abs(spot - k) / spot * 100;
    if (distPct < nearestDist) { nearestDist = distPct; nearest = k; }
  }
  if (nearest == null) return null;

  let riskLevel, riskCls;
  if (nearestDist <= 2) { riskLevel = "Danger Zone"; riskCls = "fail"; }
  else if (nearestDist <= 5) { riskLevel = "Caution"; riskCls = "warn"; }
  else { riskLevel = "Safe"; riskCls = "pass"; }

  return { strike: nearest, distancePct: Math.round(nearestDist * 10) / 10, riskLevel, riskCls };
}

/**
 * Build a compact proximity badge for a card header.
 * @param {Array<number|null|undefined>} strikes
 * @param {number} spot
 * @param {Function} escFn - HTML-escape function to use (defaults to escHtml)
 * @returns {string} HTML, or "" if not assessable
 */
function buildStrikeProximityBadge(strikes, spot, escFn) {
  const esc = escFn || (typeof escHtml === "function" ? escHtml : (s) => s);
  const prox = computeStrikeProximity(strikes, spot);
  if (!prox) return "";
  return `<span class="strike-proximity-badge ${prox.riskCls}" title="Nearest risk strike: $${prox.strike}">${esc(prox.riskLevel)} — ${prox.distancePct}% to $${prox.strike}</span>`;
}

/**
 * Extract risk-defining short strikes from a rulebook candidate (Live Suggestions shape).
 * @param {Object} c - Candidate object from /api/analyze
 * @returns {Array<number>}
 */
function getCandidateShortStrikes(c) {
  const strikes = [];
  if (!c) return strikes;
  if (c.short_strike != null) strikes.push(c.short_strike);
  if (c.put_short_strike != null) strikes.push(c.put_short_strike);
  if (c.call_short_strike != null) strikes.push(c.call_short_strike);
  return strikes;
}

/**
 * Extract risk-defining short strikes from a Live Position spread object
 * (field names differ from candidates: put_short/call_short, no "_strike" suffix).
 * @param {Object} sp - Position object
 * @returns {Array<number>}
 */
function getPositionShortStrikes(sp) {
  const strikes = [];
  if (!sp) return strikes;
  if (sp.put_short != null) strikes.push(sp.put_short);
  if (sp.call_short != null) strikes.push(sp.call_short);
  if (!strikes.length) {
    // Simple 2-strike spread or single-leg position: watch both boundary strikes.
    if (sp.strike_hi != null) strikes.push(sp.strike_hi);
    if (sp.strike_lo != null) strikes.push(sp.strike_lo);
    if (sp.strike != null) strikes.push(sp.strike);
  }
  return strikes;
}
