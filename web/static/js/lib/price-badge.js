/**
 * Price Badge Library
 *
 * Shared "current price + today's change" badge, colored red/green/neutral.
 * Used by Live Suggestions (row from /api/analyze), Live Positions (the
 * position's own underlying price/change), and Paper Trades (the fetched
 * /api/analyze row for that ticker).
 */

/**
 * Build a price badge from a {spot, price_change, price_change_pct}-shaped
 * object. All three pages' data sources are adapted to this shape before
 * calling — see buildPriceBadgeFromPosition() for the Live Positions case.
 * @param {Object} row - Object with spot/price_change/price_change_pct
 * @returns {string} HTML, or "" if no spot price available
 */
function buildPriceBadge(row) {
  if (!row || row.spot == null) return "";

  const spot = row.spot;
  const chg  = row.price_change;
  const pct  = row.price_change_pct;

  const sign  = chg >= 0 ? "+" : "";
  const cls   = chg > 0 ? "price-up" : chg < 0 ? "price-dn" : "price-flat";
  const arrow = chg > 0 ? "▲" : chg < 0 ? "▼" : "●";
  const chgStr = (chg != null && pct != null)
    ? ` <span class="price-chg">${arrow} ${sign}$${Math.abs(chg).toFixed(2)} (${sign}${pct.toFixed(2)}%)</span>`
    : "";

  return ` <span class="price-badge ${cls}">$${spot.toFixed(2)}${chgStr}</span>`;
}

/**
 * Adapt a Live Positions spread object (ul_price/ul_change, no stored
 * percentage) into the {spot, price_change, price_change_pct} shape and
 * build the badge. Percentage is derived from today's change vs. the
 * implied previous close (ul_price - ul_change).
 * @param {Object} sp - Position object with ul_price/ul_change
 * @returns {string} HTML, or "" if no underlying price available
 */
function buildPriceBadgeFromPosition(sp) {
  if (!sp || sp.ul_price == null) return "";

  const spot = sp.ul_price;
  const chg  = sp.ul_change ?? null;
  const prevClose = chg != null ? spot - chg : null;
  const pct = (chg != null && prevClose) ? (chg / prevClose) * 100 : null;

  return buildPriceBadge({ spot, price_change: chg, price_change_pct: pct });
}
