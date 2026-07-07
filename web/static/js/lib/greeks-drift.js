/**
 * Greeks Drift Library — shared by Live Positions and Paper Trades
 *
 * Fetches and renders "since entry" drift for a held position's IV and
 * Greeks, using the exact held legs (not the rulebook's newly-suggested
 * structure that /api/analyze prices). Backed by a persistence layer
 * (scripts/position_snapshots.py) that records the first-seen pricing the
 * first time each position is loaded, so later loads can show real drift.
 */

/**
 * Fetch entry/current/drift for a held Live Position from the backend.
 * @param {Object} position - Position object (ticker, structure, strikes, expiry, ul_price)
 * @returns {Promise<{entry: Object, current: Object, drift: Object}>}
 */
async function fetchGreeksDrift(position) {
  const res = await fetch("/api/live-position-greeks-drift", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(position),
  });
  const data = await res.json();
  if (!data.ok) {
    throw new Error(data.error || "Failed to fetch Greeks drift");
  }
  return { entry: data.entry, current: data.current, drift: data.drift };
}

/**
 * Fetch entry/current/drift for a Paper Trade by its stable trade id
 * (the backend adapts the trade's stored strikes into the same shape
 * Live Positions uses, via scripts.position_snapshots.paper_trade_to_position_shape).
 * @param {string} tradeId
 * @returns {Promise<{entry: Object, current: Object, drift: Object}>}
 */
async function fetchGreeksDriftForTrade(tradeId) {
  const res = await fetch(`/api/paper-trades/greeks-drift/${encodeURIComponent(tradeId)}`);
  const data = await res.json();
  if (!data.ok) {
    throw new Error(data.error || "Failed to fetch Greeks drift");
  }
  return { entry: data.entry, current: data.current, drift: data.drift };
}

/**
 * Build the "Since Entry" drift card.
 * @param {Object} result - {entry, current, drift} from fetchGreeksDrift
 * @param {Function} escFn - HTML-escape function to use (defaults to escHtml)
 * @returns {string} HTML
 */
function buildGreeksDriftCard(result, escFn) {
  const esc = escFn || (typeof escHtml === "function" ? escHtml : (s) => s);
  if (!result) return "";
  const { entry, current, drift } = result;

  const DESCRIPTIONS = {
    "IV":    "Implied Volatility — market's expected price swing; higher = more expensive options",
    "Delta": "Directional exposure — how much the spread gains/loses per $1 move in the stock",
    "Theta": "Daily time decay — credit you collect (or pay) each day as expiry approaches",
    "Gamma": "Delta acceleration — how fast delta shifts as the stock moves; rises near expiry",
    "Vega":  "IV sensitivity — P&L impact per 1% change in implied volatility",
  };

  const row = (label, entryVal, currentVal, driftVal, unit, decimals) => {
    if (currentVal == null) return "";
    const driftCls = driftVal == null ? "na" : driftVal > 0 ? "pass" : driftVal < 0 ? "fail" : "na";
    const driftStr = driftVal == null ? "—" : `${driftVal > 0 ? "+" : ""}${driftVal.toFixed(decimals)}${unit}`;
    const desc = DESCRIPTIONS[label] || "";
    return `
      <div class="lp-drift-row" title="${esc(desc)}">
        <span class="lp-drift-label">${esc(label)}</span>
        <span class="lp-drift-desc">${esc(desc)}</span>
        <span class="lp-drift-entry">${entryVal != null ? entryVal.toFixed(decimals) + unit : "—"}</span>
        <span class="lp-drift-arrow">→</span>
        <span class="lp-drift-current">${currentVal.toFixed(decimals)}${unit}</span>
        <span class="lp-drift-change ${driftCls}">(${driftStr})</span>
      </div>`;
  };

  const rows = [
    row("IV", entry.iv, current.iv, drift.iv, "%", 1),
    row("Delta", entry.net_delta, current.net_delta, drift.net_delta, "", 3),
    row("Theta", entry.net_theta, current.net_theta, drift.net_theta, "", 4),
    row("Gamma", entry.net_gamma, current.net_gamma, drift.net_gamma, "", 5),
    row("Vega", entry.net_vega, current.net_vega, drift.net_vega, "", 4),
  ].filter(Boolean).join("");

  if (!rows) return "";

  const trackingSince = (entry.ts || "").slice(0, 10);
  const openedOn     = (entry.date_acquired || "").slice(0, 10);
  const headerText   = openedOn && openedOn !== trackingSince
    ? `Opened <span class="lp-drift-since">${esc(openedOn)}</span> &nbsp;·&nbsp; Greeks tracked from <span class="lp-drift-since">${esc(trackingSince)}</span>`
    : `Greeks tracked from <span class="lp-drift-since">${esc(trackingSince)}</span>`;

  return `
    <div class="lp-drift-card">
      <div class="lp-drift-header">${headerText}</div>
      ${rows}
    </div>
  `;
}
