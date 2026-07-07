/**
 * Hedge Block Library
 *
 * Shared hedge-suggestion renderer, used by both Live Suggestions (a new
 * candidate trade) and Live Positions (a position you already hold).
 *
 * Takes a spread-shaped `sp` object directly (NOT candidate+row) — this is
 * the same shape candidateToSpread() in common.js produces for Suggestions,
 * and the shape Live Positions' own analyze_spread output already comes in
 * natively (structure, strike_lo/hi, max_profit_ps, max_loss_ps, ul_price,
 * expiry, ticker). See common.js's candidateToSpread() comment — it already
 * documents that renderHedgePnlAnalysis() works with both sources.
 *
 * @param {Object} hedge - Estimated hedge suggestion (hedge_structure, rationale, etc.)
 * @param {string} primaryStructure - The primary trade's structure (for Jade Lizard urgency styling)
 * @param {Object} hedgeExact - Live-priced exact hedge trade, if available
 * @param {Object} sp - Spread-shaped object for the primary position/candidate
 * @returns {string} HTML, or "" if no hedge data
 */
function buildHedgeBlock(hedge, primaryStructure, hedgeExact, sp) {
  if (!hedge && !hedgeExact) return "";

  const isJade     = primaryStructure === "Jade Lizard";
  const urgencyCls = isJade ? "hedge-urgent" : "hedge-normal";

  // Build the combined position+hedge P&L analysis when we have enough data
  const pnlAnalysisHtml = (sp && hedgeExact && !hedgeExact.error)
    ? renderHedgePnlAnalysis(sp, hedgeExact)
    : "";

  // ── Exact trade (live data preferred) ────────────────────────────────────
  if (hedgeExact && !hedgeExact.error) {
    const ex     = hedgeExact;
    const costPs = ex.cost_per_share || 0;
    const total  = ex.total_cost || 0;
    const verb   = ex.hedge_side === 'sell' ? 'Sell' : 'Buy';

    let legRows = "";
    if (ex.type === "two_leg" && ex.legs) {
      legRows = ex.legs.map(l => `
        <div class="hedge-exact-leg">
          <span class="hedge-exact-action">${verb} ${ex.contracts}x</span>
          <span class="hedge-exact-strike">$${l.strike.toFixed(2)} ${l.option_type}</span>
          <span class="hedge-exact-expiry">${l.expiry}</span>
          <span class="hedge-exact-mark">@ <strong>$${l.mark.toFixed(3)}</strong></span>
          <span class="muted">(bid $${l.bid.toFixed(3)} / ask $${l.ask.toFixed(3)})</span>
          ${l.iv ? `<span class="muted">IV ${l.iv.toFixed(1)}%</span>` : ""}
        </div>`).join("");
    } else {
      legRows = `
        <div class="hedge-exact-leg">
          <span class="hedge-exact-action">${verb} ${ex.contracts}x</span>
          <span class="hedge-exact-strike">$${(ex.strike||0).toFixed(2)} ${ex.option_type||""}</span>
          <span class="hedge-exact-expiry">${ex.expiry_used||""}</span>
          <span class="hedge-exact-mark">@ <strong>$${(ex.mark||0).toFixed(3)}</strong></span>
          <span class="muted">(bid $${(ex.bid||0).toFixed(3)} / ask $${(ex.ask||0).toFixed(3)})</span>
          ${ex.iv ? `<span class="muted">IV ${ex.iv.toFixed(1)}%</span>` : ""}
          ${ex.volume ? `<span class="muted">Vol ${ex.volume.toLocaleString()}</span>` : ""}
        </div>`;
    }

    const mp  = ex.primary_max_profit_ps;
    const ml  = ex.primary_max_loss_ps;
    const cmp = ex.combined_max_profit_ps;
    const cml = ex.combined_max_loss_ps;
    const hasPnl = mp != null || ml != null;
    const pnlHtml = hasPnl ? `
      <div class="hedge-comparison">
        <div class="hedge-col">
          <div class="hedge-col-title">Without hedge</div>
          ${mp != null ? `<div class="hedge-row"><span>Max Profit</span><span class="pass">$${mp.toFixed(2)}</span></div>` : ""}
          ${ml != null ? `<div class="hedge-row"><span>Max Loss</span><span class="fail">${ml != null ? "$"+ml.toFixed(2) : "Unlimited"}</span></div>` : ""}
        </div>
        <div class="hedge-arrow">+ hedge</div>
        <div class="hedge-col hedge-col-combined">
          <div class="hedge-col-title">Combined</div>
          ${cmp != null ? `<div class="hedge-row"><span>Max Profit</span><span class="pass">$${cmp.toFixed(2)} <em class="muted">(−$${costPs.toFixed(3)})</em></span></div>` : ""}
          ${cml != null
            ? `<div class="hedge-row"><span>Max Loss</span><span class="warn">$${cml.toFixed(2)} <em class="muted">(+$${costPs.toFixed(3)})</em></span></div>`
            : (ml == null ? `<div class="hedge-row"><span>Max Loss</span><span class="pass">Defined ✓</span></div>` : "")}
          <div class="hedge-row"><span>Hedge cost</span><span class="na">$${total.toFixed(2)} total</span></div>
        </div>
      </div>` : "";

    return `
      <details class="hedge-block ${urgencyCls}" open>
        <summary class="hedge-summary">
          <span class="hedge-icon">${isJade ? "⚠" : "🛡"}</span>
          <strong>${isJade ? "⚠ CRITICAL Hedge:" : "Hedge Trade (live):"}</strong>
          <span class="hedge-cost-pill">$${total.toFixed(2)} total &nbsp;·&nbsp; $${costPs.toFixed(3)}/sh</span>
        </summary>
        <div class="hedge-body">
          <p class="hedge-rationale">${ex.rationale}</p>
          <div class="hedge-exact-box">
            <div class="hedge-exact-label">Exact trade — live market data</div>
            ${legRows}
          </div>
          ${pnlHtml}
          ${pnlAnalysisHtml}
          ${hedge && hedge.protection_note ? `<p class="hedge-protection-note">ℹ️ ${hedge.protection_note}</p>` : ""}
        </div>
      </details>`;
  }

  // ── Fallback: estimated hedge ─────────────────────────────────────────────
  if (!hedge) return "";
  const combinedDeltaCls =
    Math.abs(hedge.combined_delta) < 0.05 ? "pass" :
    Math.abs(hedge.combined_delta) < 0.20 ? "na" : "warn";
  const origProfit = (hedge.combined_max_profit + hedge.hedge_cost_per_share).toFixed(2);
  const origLoss   = (hedge.combined_max_loss   - hedge.hedge_cost_per_share).toFixed(2);
  const errNote = (hedgeExact && hedgeExact.error)
    ? `<p class="hedge-cost-note warn">Live data unavailable: ${hedgeExact.error}. Showing estimates.</p>` : "";

  return `
    <details class="hedge-block ${urgencyCls}" open>
      <summary class="hedge-summary">
        <span class="hedge-icon">${isJade ? "⚠" : "🛡"}</span>
        <strong>Hedge Suggestion:</strong> ${hedge.hedge_structure}
        <span class="hedge-cost-pill">Est. ~$${hedge.hedge_cost_per_share.toFixed(2)}/share &nbsp;·&nbsp; ~$${hedge.hedge_cost_per_contract.toFixed(0)}/contract</span>
      </summary>
      <div class="hedge-body">
        ${errNote}
        <p class="hedge-rationale">${hedge.rationale}</p>
        <p class="hedge-details-line"><strong>Hedge trade:</strong> ${hedge.hedge_details}</p>
        <div class="hedge-comparison">
          <div class="hedge-col">
            <div class="hedge-col-title">Primary Only</div>
            <div class="hedge-row"><span>Max Profit</span><span class="pass">$${origProfit}</span></div>
            <div class="hedge-row"><span>Max Loss</span><span class="fail">$${origLoss}</span></div>
          </div>
          <div class="hedge-arrow">+ hedge</div>
          <div class="hedge-col hedge-col-combined">
            <div class="hedge-col-title">Combined</div>
            <div class="hedge-row"><span>Max Profit</span><span class="pass">$${hedge.combined_max_profit.toFixed(2)} <em class="muted">(−$${hedge.hedge_cost_per_share.toFixed(2)})</em></span></div>
            <div class="hedge-row">
              <span>Max Loss</span>
              ${hedge.combined_max_loss != null
                ? `<span class="warn">$${hedge.combined_max_loss.toFixed(2)} <em class="muted">(+$${hedge.hedge_cost_per_share.toFixed(2)})</em></span>`
                : `<span class="pass" title="${hedge.combined_max_loss_note ?? ""}">Defined ✓</span>`}
            </div>
            <div class="hedge-row"><span>Net Delta</span><span class="${combinedDeltaCls}">${hedge.combined_delta >= 0 ? "+" : ""}${hedge.combined_delta.toFixed(3)}</span></div>
          </div>
        </div>
        ${hedge.combined_max_loss_note ? `<p class="hedge-protection-note hedge-urgent-note">✅ ${hedge.combined_max_loss_note}</p>` : ""}
        <p class="hedge-protection-note">ℹ️ ${hedge.protection_note}</p>
        <p class="hedge-cost-note muted">⚠ ${hedge.cost_note}</p>
        ${pnlAnalysisHtml}
      </div>
    </details>`;
}
