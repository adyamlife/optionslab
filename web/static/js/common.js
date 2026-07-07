// Shared utilities — loaded on every page before page-specific scripts

// ── Expiration P&L engine ─────────────────────────────────────────────────────
// sp must have: structure, strike_lo/hi (spreads), strike (single), put_long/short/call_short/long (IC),
//               qty (positive always), cost_value (+debit / -credit for full position), ul_price

function _expiryIntrinsic(spy, sp) {
  const struct = sp.structure || sp.kind || "";
  const qty    = Math.abs(sp.qty || 1);
  const mult   = qty * 100;
  const lo     = sp.strike_lo, hi = sp.strike_hi, K = sp.strike;

  if (struct === "Call Debit Spread")  return Math.max(Math.min(spy - lo, hi - lo), 0) * mult;
  if (struct === "Put Debit Spread")   return Math.max(Math.min(hi - spy, hi - lo), 0) * mult;
  if (struct === "Call Credit Spread") return -Math.max(Math.min(spy - lo, hi - lo), 0) * mult;
  if (struct === "Put Credit Spread")  return -Math.max(Math.min(hi - spy, hi - lo), 0) * mult;
  if (struct === "Iron Condor") {
    const pL = sp.put_long, pS = sp.put_short, cS = sp.call_short, cH = sp.call_long;
    const putLoss  = pL != null ? Math.max(Math.min(pS - spy, pS - pL), 0) * mult : 0;
    const callLoss = cS != null ? Math.max(Math.min(spy - cS, cH - cS), 0) * mult : 0;
    return -(putLoss + callLoss);
  }
  if (struct === "Long Call"  || (struct === "Single Leg" && sp.opt_type === "Call" && (sp.qty || 1) > 0))
    return Math.max(spy - K, 0) * mult;
  if (struct === "Long Put"   || (struct === "Single Leg" && sp.opt_type === "Put"  && (sp.qty || 1) > 0))
    return Math.max(K - spy, 0) * mult;
  if (struct === "Short Call" || (struct === "Single Leg" && sp.opt_type === "Call" && (sp.qty || 1) < 0))
    return -Math.max(spy - K, 0) * mult;
  if (struct === "Short Put"  || (struct === "Single Leg" && sp.opt_type === "Put"  && (sp.qty || 1) < 0))
    return -Math.max(K - spy, 0) * mult;
  return 0;
}

function pnlAtExpiry(spy, sp) {
  return _expiryIntrinsic(spy, sp) - (sp.cost_value ?? 0);
}

function generatePriceLevels(sp) {
  const ul      = sp.ul_price;
  const anchors = new Set();
  const add = (v) => { if (v != null && isFinite(v)) anchors.add(Math.round(v * 2) / 2); };

  [sp.strike, sp.strike_lo, sp.strike_hi,
   sp.put_long, sp.put_short, sp.call_short, sp.call_long,
   sp.breakeven, sp.lower_be, sp.upper_be, ul].forEach(add);

  const center = ul ?? sp.strike ?? ((sp.strike_lo != null && sp.strike_hi != null) ? (sp.strike_lo + sp.strike_hi) / 2 : null);
  if (center == null) return [];
  const span = Math.max((sp.strike_hi ?? center) - (sp.strike_lo ?? center), 20, center * 0.08);
  const step = Math.max(Math.ceil(span / 4), 1);
  for (let i = -6; i <= 6; i++) anchors.add(Math.round((center + i * step) * 2) / 2);

  return Array.from(anchors).sort((a, b) => a - b);
}

function _findBreakevens(sp, levels) {
  const bes = [];
  for (let i = 0; i < levels.length - 1; i++) {
    const p0 = pnlAtExpiry(levels[i],   sp);
    const p1 = pnlAtExpiry(levels[i+1], sp);
    if (p0 * p1 <= 0 && p0 !== p1) {
      const be = levels[i] + (levels[i+1] - levels[i]) * (-p0 / (p1 - p0));
      bes.push(Math.round(be * 100) / 100);
    }
  }
  return bes;
}

// ── Hedge P&L helpers ─────────────────────────────────────────────────────────

function hedgePnlAtExpiry(spy, hedgeExact) {
  if (!hedgeExact || hedgeExact.error) return 0;
  const contracts = hedgeExact.contracts || 1;
  const mult      = contracts * 100;
  const totalCost = hedgeExact.total_cost || 0;

  if (hedgeExact.type === "two_leg" && (hedgeExact.legs || []).length === 2) {
    const legs    = hedgeExact.legs;
    const strikes = legs.map(l => l.strike).sort((a, b) => a - b);
    const lo = strikes[0], hi = strikes[1];
    const optType = (legs[0].option_type || "").toLowerCase();
    const intrinsic = optType === "put"
      ? Math.max(Math.min(hi - spy, hi - lo), 0) * mult
      : Math.max(Math.min(spy - lo, hi - lo), 0) * mult;
    return intrinsic - totalCost;
  }

  const strike  = hedgeExact.strike;
  const optType = (hedgeExact.option_type || "").toLowerCase();
  if (strike == null) return -totalCost;
  const intrinsic = optType === "put"  ? Math.max(strike - spy, 0) * mult
                  : optType === "call" ? Math.max(spy - strike, 0) * mult : 0;
  return intrinsic - totalCost;
}

// Convert a live-suggestion candidate into a spread-like object for pnlAtExpiry
function candidateToSpread(c, row) {
  const isCredit  = c.is_credit ?? true;
  const maxProfit = c.max_profit ?? 0;   // total dollars
  const maxLoss   = c.max_loss   ?? 0;
  const struct    = c.structure  ?? "";

  let strike_lo, strike_hi, put_long, put_short, call_short, call_long;

  if (struct === "Put Credit Spread") {
    strike_lo = c.long_strike;    // lower, long put
    strike_hi = c.short_strike;   // higher, short put
  } else if (struct === "Call Credit Spread") {
    strike_lo = c.short_strike;   // lower, short call
    strike_hi = c.long_strike;    // higher, long call
  } else if (struct === "Put Debit Spread") {
    strike_lo = c.long_strike;
    strike_hi = c.short_strike;
  } else if (struct === "Call Debit Spread") {
    strike_lo = c.long_strike;
    strike_hi = c.short_strike;
  } else if (struct === "Iron Condor") {
    put_long   = c.put_long_strike;
    put_short  = c.put_short_strike;
    call_short = c.call_short_strike;
    call_long  = c.call_long_strike;
    strike_lo  = put_long;
    strike_hi  = call_long;
  }

  // cost_value: negative = credit received, positive = debit paid
  const costValue = isCredit ? -maxProfit : maxLoss;

  return {
    structure:  struct,
    kind:       "spread",
    qty:        1,
    strike_lo, strike_hi,
    put_long, put_short, call_short, call_long,
    cost_value: costValue,
    ul_price:   row.spot ?? c.spot_at_entry ?? null,
    expiry:     c.expiry ?? row.expiry ?? null,
    ticker:     row.ticker ?? "",
    // pre-computed for narrative
    max_profit_ps: maxProfit / 100,
    max_loss_ps:   maxLoss   / 100,
  };
}

// ── Shared hedge+position P&L renderer ───────────────────────────────────────
// Works on both live-suggestions (where sp comes from candidateToSpread) and
// live-positions (where sp comes from analyze_spread output).

function renderHedgePnlAnalysis(sp, hedgeExact) {
  if (!hedgeExact || hedgeExact.error) return "";
  const hasStrikes = sp.strike != null || sp.strike_lo != null;
  if (!hasStrikes) return "";

  // Expand price range to include hedge strikes
  const hedgeStrikes = (hedgeExact.legs || []).map(l => l.strike).filter(Boolean);
  if (hedgeExact.strike) hedgeStrikes.push(hedgeExact.strike);

  const expandedSp = {
    ...sp,
    strike_lo: Math.min(sp.strike_lo ?? sp.strike ?? Infinity,  ...hedgeStrikes),
    strike_hi: Math.max(sp.strike_hi ?? sp.strike ?? -Infinity, ...hedgeStrikes),
  };
  const levels = generatePriceLevels(expandedSp);
  if (!levels.length) return "";

  const ul = sp.ul_price;

  const series = levels.map(spy => {
    const pos   = pnlAtExpiry(spy, sp);
    const hedge = hedgePnlAtExpiry(spy, hedgeExact);
    return { spy, pos, hedge, combined: pos + hedge };
  });

  // Combined breakevens
  const combinedBes = [];
  for (let i = 0; i < series.length - 1; i++) {
    const p0 = series[i].combined, p1 = series[i+1].combined;
    if (p0 * p1 <= 0 && p0 !== p1) {
      const be = series[i].spy + (series[i+1].spy - series[i].spy) * (-p0 / (p1 - p0));
      combinedBes.push(Math.round(be * 100) / 100);
    }
  }

  const posMaxLoss      = Math.min(...series.map(r => r.pos));
  const combinedMaxLoss = Math.min(...series.map(r => r.combined));
  const posMaxGain      = Math.max(...series.map(r => r.pos));
  const combinedMaxGain = Math.max(...series.map(r => r.combined));
  const lossImprove     = combinedMaxLoss - posMaxLoss;
  const costOfHedge     = hedgeExact.total_cost || 0;

  // Helper — works even if escHtml not defined (live.js doesn't have it)
  const _esc = typeof escHtml === "function" ? escHtml : s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

  const fmtPnl = v => `${v >= 0 ? "+" : ""}$${Math.abs(v).toFixed(2)}`;
  const cls    = v => v > 0.01 ? "pass" : v < -0.01 ? "fail" : "na";

  const rows = series.map(({ spy, pos, hedge, combined }) => {
    const isCur = ul != null && Math.abs(spy - ul) < 0.26;
    const isBe  = combinedBes.some(b => Math.abs(spy - b) < 0.26);
    const rowCls = isCur ? "pnl-row-current" : isBe ? "pnl-row-be" : "";
    return `<tr class="${rowCls}">
      <td>$${spy.toFixed(2)}${isCur ? " ◀" : ""}</td>
      <td class="${cls(pos)}">${fmtPnl(pos)}</td>
      <td class="pass">${fmtPnl(hedge)}</td>
      <td class="${cls(combined)}"><strong>${fmtPnl(combined)}</strong></td>
    </tr>`;
  }).join("");

  const lossText = lossImprove > 0
    ? `Hedge <span class="pass">reduces max loss by $${lossImprove.toFixed(2)}</span> (from <span class="fail">$${Math.abs(posMaxLoss).toFixed(2)}</span> → <span class="warn">$${Math.abs(combinedMaxLoss).toFixed(2)}</span>).`
    : `Hedge adds protection cost; max loss similar in modelled range.`;
  const gainText = combinedMaxGain < posMaxGain
    ? `Max gain trimmed from <span class="pass">+$${posMaxGain.toFixed(2)}</span> to <span class="pass">+$${combinedMaxGain.toFixed(2)}</span> after hedge cost.`
    : `Max gain: <span class="pass">+$${combinedMaxGain.toFixed(2)}</span>.`;
  const beText = combinedBes.length
    ? `Combined breakeven${combinedBes.length > 1 ? "s" : ""}: ${combinedBes.map(b => `<strong>$${b.toFixed(2)}</strong>`).join(" &amp; ")}.`
    : "";
  const curCtx = ul != null ? (() => {
    const cur = series.reduce((b, r) => Math.abs(r.spy - ul) < Math.abs(b.spy - ul) ? r : b);
    return `At today's price <strong>$${ul.toFixed(2)}</strong>: without hedge <span class="${cls(cur.pos)}">${fmtPnl(cur.pos)}</span> → with hedge <span class="${cls(cur.combined)}"><strong>${fmtPnl(cur.combined)}</strong></span>.`;
  })() : "";

  const narrative = `
    <p class="pnl-narr-line">Hedge costs <strong>$${costOfHedge.toFixed(2)}</strong> total. ${lossText}</p>
    <p class="pnl-narr-line">${gainText}</p>
    ${beText ? `<p class="pnl-narr-line">${beText}</p>` : ""}
    ${curCtx ? `<p class="pnl-narr-line">${curCtx}</p>` : ""}
    <p class="pnl-narr-line muted pnl-note-small">Position P&amp;L + Hedge P&amp;L = Combined at expiry.</p>`;

  return `
    <details class="pnl-explain-block pnl-hedge-combined-block">
      <summary class="pnl-explain-summary">📊 Trade + Hedge Expiration P&amp;L</summary>
      <div class="pnl-explain-body">
        <div class="pnl-explain-cols">
          <div class="pnl-narrative">${narrative}</div>
          <div class="pnl-table-wrap">
            <table class="pnl-table">
              <thead>
                <tr>
                  <th>${_esc(sp.ticker || "")} at expiry</th>
                  <th title="Trade P&amp;L at expiry">Trade</th>
                  <th class="pass" title="Hedge leg P&amp;L">Hedge</th>
                  <th title="Net combined">Combined</th>
                </tr>
              </thead>
              <tbody>${rows}</tbody>
            </table>
            ${combinedBes.length
              ? `<p class="pnl-be-note">BE: ${combinedBes.map(b => `<strong>$${b.toFixed(2)}</strong>`).join(" &amp; ")}</p>`
              : ""}
          </div>
        </div>
      </div>
    </details>`;
}

// ── Standalone trade expiration P&L (no hedge) ────────────────────────────────
// Shared by live.js and live_positions.js. Requires candidateToSpread for live suggestions.

function _pnlNarrative(sp, bes, levels) {
  const struct    = sp.structure || sp.kind || "position";
  const cost      = sp.cost_value ?? 0;
  const qty       = Math.abs(sp.qty || 1);
  const costLabel = cost >= 0
    ? `paid $${cost.toFixed(2)} debit`
    : `received $${Math.abs(cost).toFixed(2)} credit`;
  const maxP = sp.max_profit_ps != null ? sp.max_profit_ps * qty * 100 : null;
  const maxL = sp.max_loss_ps   != null ? sp.max_loss_ps   * qty * 100 : null;

  const lines = [];
  lines.push(`<strong>${struct}</strong> — ${qty} contract${qty > 1 ? "s" : ""}, ${costLabel}.`);

  if (bes.length === 1)
    lines.push(`Breakeven at expiry: <strong>$${bes[0].toFixed(2)}</strong>.`);
  else if (bes.length === 2)
    lines.push(`Breakevens: <strong>$${bes[0].toFixed(2)}</strong> (down) and <strong>$${bes[1].toFixed(2)}</strong> (up).`);

  if (maxP != null && maxL != null)
    lines.push(`Max gain <span class="pass">+$${maxP.toFixed(2)}</span> / Max loss <span class="fail">-$${maxL.toFixed(2)}</span>.`);
  else if (maxL != null)
    lines.push(`Max loss: <span class="fail">-$${maxL.toFixed(2)}</span>. Gain is open-ended.`);

  if (struct === "Call Credit Spread")
    lines.push(`Full credit kept if stock stays below $${sp.strike_lo}. Loss grows above breakeven.`);
  else if (struct === "Put Credit Spread")
    lines.push(`Full credit kept if stock stays above $${sp.strike_hi}. Loss grows below breakeven.`);
  else if (struct === "Iron Condor")
    lines.push(`Profit zone: $${bes[0]?.toFixed(2)} – $${bes[1]?.toFixed(2)}.`);
  else if (struct === "Call Debit Spread")
    lines.push(`Profit if stock rises above $${bes[bes.length-1]?.toFixed(2)}.`);
  else if (struct === "Put Debit Spread")
    lines.push(`Profit if stock falls below $${bes[0]?.toFixed(2)}.`);

  if (sp.ul_price != null && bes.length) {
    const ul = sp.ul_price;
    const profitable = bes.length === 1
      ? (struct.includes("Call") ? ul > bes[0] : ul < bes[0])
      : (ul > bes[0] && ul < bes[1]);
    lines.push(`Current $${ul.toFixed(2)} — <span class="${profitable ? "pass" : "fail"}">${profitable ? "in profit zone ✓" : "outside profit zone"}</span>.`);
  }
  return lines.map(l => `<p class="pnl-narr-line">${l}</p>`).join("");
}

function renderPnlExplanation(sp) {
  const struct = sp.structure || sp.kind || "";
  if (!struct) return "";
  const hasStrikes = sp.strike != null || sp.strike_lo != null;
  if (!hasStrikes) return "";

  const levels = generatePriceLevels(sp);
  if (!levels.length) return "";

  const bes = _findBreakevens(sp, levels);
  const ul  = sp.ul_price;

  const rows = levels.map(spy => {
    const pnl    = pnlAtExpiry(spy, sp);
    const cls    = pnl > 0.01 ? "pass" : pnl < -0.01 ? "fail" : "na";
    const isCur  = ul != null && Math.abs(spy - ul) < 0.26;
    const isBe   = bes.some(b => Math.abs(spy - b) < 0.26);
    const rowCls = isCur ? "pnl-row-current" : isBe ? "pnl-row-be" : "";
    const sign   = pnl >= 0 ? "+" : "";
    return `<tr class="${rowCls}">
      <td>$${spy.toFixed(2)}${isCur ? " ◀" : ""}</td>
      <td class="${cls}">${sign}$${pnl.toFixed(2)}</td>
    </tr>`;
  }).join("");

  const narrative = _pnlNarrative(sp, bes, levels);

  return `
    <details class="pnl-explain-block">
      <summary class="pnl-explain-summary">📊 Expiration P&amp;L</summary>
      <div class="pnl-explain-body">
        <div class="pnl-explain-cols">
          <div class="pnl-narrative">${narrative}</div>
          <div class="pnl-table-wrap">
            <table class="pnl-table">
              <thead><tr><th>Price at expiry</th><th>P&amp;L</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
            ${bes.length ? `<p class="pnl-be-note">BE: ${bes.map(b => `<strong>$${b.toFixed(2)}</strong>`).join(" &amp; ")}</p>` : ""}
          </div>
        </div>
      </div>
    </details>`;
}
