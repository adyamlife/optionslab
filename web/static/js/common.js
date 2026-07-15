// Shared utilities — loaded on every page before page-specific scripts

// ── Expiration P&L engine ─────────────────────────────────────────────────────
// sp must have: structure, strike_lo/hi (spreads), strike (single), put_long/short/call_short/long (IC),
//               qty (positive always), cost_value (+debit / -credit for full position), ul_price

function _expiryIntrinsic(spy, sp) {
  const struct = sp.structure || sp.kind || "";
  const qty    = Math.abs(sp.qty || 1);
  // candidateToSpread passes mult:1 so table values stay per-share (matching cost_value scale)
  const mult   = sp.mult != null ? sp.mult : qty * 100;
  const lo     = sp.strike_lo, hi = sp.strike_hi, K = sp.strike;

  if (struct === "Call Debit Spread")  return Math.max(Math.min(spy - lo, hi - lo), 0) * mult;
  if (struct === "Put Debit Spread")   return Math.max(Math.min(hi - spy, hi - lo), 0) * mult;
  if (struct === "Risk Reversal") {
    // short put (strike_lo) + long call (strike_hi); net_credit stored in cost_value as negative
    const putPnl  = -Math.max(lo - spy, 0);   // short put: loss if spy < put_strike
    const callPnl =  Math.max(spy - hi, 0);   // long call: profit if spy > call_strike
    return (putPnl + callPnl) * mult;
  }
  if (struct === "Financed Long Call") {
    // put credit spread (short higher put, long lower put) + standalone long call
    const putPnl  = -Math.max(Math.min(sp.put_short - spy, sp.put_short - sp.put_long), 0);
    const callPnl =  Math.max(spy - sp.strike_hi, 0);   // strike_hi = call strike
    return (putPnl + callPnl) * mult;
  }
  if (struct === "Financed Long Put") {
    // call credit spread (short lower call, long higher call) + standalone long put
    const callPnl = -Math.max(Math.min(spy - sp.call_short, sp.call_long - sp.call_short), 0);
    const putPnl  =  Math.max(sp.strike_lo - spy, 0);   // strike_lo = put strike
    return (callPnl + putPnl) * mult;
  }
  if (struct === "Ratio Call Backspread") {
    // sell 1 near-ATM call (strike_lo), buy 2 OTM calls (strike_hi)
    const shortPnl = -Math.max(spy - sp.strike_lo, 0);
    const longPnl  =  2 * Math.max(spy - sp.strike_hi, 0);
    return (shortPnl + longPnl) * mult;
  }
  if (struct === "Ratio Put Backspread") {
    // sell 1 near-ATM put (strike_hi), buy 2 OTM puts (strike_lo)
    const shortPnl = -Math.max(sp.strike_hi - spy, 0);
    const longPnl  =  2 * Math.max(sp.strike_lo - spy, 0);
    return (shortPnl + longPnl) * mult;
  }
  if (struct === "Long Strangle") {
    // buy OTM put (strike_lo) + buy OTM call (strike_hi)
    return (Math.max(sp.strike_lo - spy, 0) + Math.max(spy - sp.strike_hi, 0)) * mult;
  }
  if (struct === "Bear Combo") {
    // Bear put spread + bear call spread (4 legs)
    // put_long = higher put (bought), put_short = lower put (sold)
    // call_short = lower call (sold, closer to ATM), call_long = higher call (bought, cap)
    const pL = sp.put_long, pS = sp.put_short, cS = sp.call_short, cH = sp.call_long;
    // Put spread (long pL put, short pS put): max(pL-spy,0) - max(pS-spy,0)
    const putPnl  = Math.max(pL - spy, 0) - Math.max(pS - spy, 0);
    // Call spread (short cS call, long cH call): -max(spy-cS,0) + max(spy-cH,0)
    const callPnl = -Math.max(spy - cS, 0) + Math.max(spy - cH, 0);
    return (putPnl + callPnl) * mult;
  }
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
    strike_lo = c.short_strike;  // lower put (sold)
    strike_hi = c.long_strike;   // higher put (bought)
  } else if (struct === "Call Debit Spread") {
    strike_lo = c.long_strike;
    strike_hi = c.short_strike;
  } else if (struct === "Risk Reversal") {
    // short_strike = put strike (sold), long_strike = call strike (bought)
    strike_lo = c.short_strike;   // put strike (loss side)
    strike_hi = c.long_strike;    // call strike (profit side)
  } else if (struct === "Bear Combo") {
    // 4-leg: BUY long_put + SELL short_put + SELL short_call + BUY long_call
    put_long   = c.long_put_strike;
    put_short  = c.short_put_strike;
    call_short = c.short_call_strike;
    call_long  = c.long_call_strike;
    strike_lo  = put_short;
    strike_hi  = call_long;
  } else if (struct === "Financed Long Call") {
    // put credit spread + standalone long call
    put_short  = c.short_put_strike;   // higher put (sold)
    put_long   = c.long_put_strike;    // lower put (bought, defines max loss)
    call_long  = c.call_strike;        // standalone long call (for price anchors)
    strike_lo  = c.long_put_strike;
    strike_hi  = c.call_strike;
  } else if (struct === "Financed Long Put") {
    // call credit spread + standalone long put
    call_short = c.short_call_strike;  // lower call (sold)
    call_long  = c.long_call_strike;   // higher call (bought, cap)
    put_long   = c.put_strike;         // standalone long put (for price anchors)
    strike_lo  = c.put_strike;
    strike_hi  = c.long_call_strike;
  } else if (struct === "Ratio Call Backspread") {
    strike_lo = c.short_strike;        // near-ATM call (1× sold)
    strike_hi = c.long_strike;         // OTM call (2× bought)
  } else if (struct === "Ratio Put Backspread") {
    strike_lo = c.long_strike;         // OTM put (2× bought, lower)
    strike_hi = c.short_strike;        // near-ATM put (1× sold, higher)
  } else if (struct === "Long Strangle") {
    strike_lo = c.short_strike;        // OTM put (lower)
    strike_hi = c.long_strike;         // OTM call (upper)
  } else if (struct === "Iron Condor") {
    put_long   = c.put_long_strike;
    put_short  = c.put_short_strike;
    call_short = c.call_short_strike;
    call_long  = c.call_long_strike;
    strike_lo  = put_long;
    strike_hi  = call_long;
  }

  // cost_value: for standard structures = credit received (neg) or debit paid (pos).
  // For financed/backspread/strangle structures, use the actual net cost directly.
  const unlimitedProfit = ["Financed Long Call", "Financed Long Put",
                           "Ratio Call Backspread", "Ratio Put Backspread", "Long Strangle"].includes(struct);
  let costValue;
  if (struct === "Financed Long Call")    costValue = c.flc_net_cost ?? 0;
  else if (struct === "Financed Long Put") costValue = c.flp_net_cost ?? 0;
  else if (struct === "Ratio Call Backspread") costValue = c.rbc_net_cost ?? 0;
  else if (struct === "Ratio Put Backspread")  costValue = c.rbp_net_cost ?? 0;
  else if (struct === "Long Strangle")    costValue = c.ls_total_debit ?? maxLoss;
  else costValue = isCredit ? -maxProfit : maxLoss;

  return {
    structure:  struct,
    kind:       "spread",
    qty:        1,
    mult:       1,   // per-share scale: cost_value is per-share, table shows per-share
    strike_lo, strike_hi,
    put_long, put_short, call_short, call_long,
    cost_value: costValue,
    ul_price:   row.spot ?? c.spot_at_entry ?? null,
    expiry:     c.expiry ?? row.expiry ?? null,
    ticker:     row.ticker ?? "",
    // pre-computed for narrative — null for unlimited-upside structures
    max_profit_ps: unlimitedProfit ? null : (c.max_profit != null ? c.max_profit / 100 : null),
    max_loss_ps:   c.max_loss != null ? c.max_loss / 100 : null,
    // Risk Reversal extras
    rr_net_credit:    c.rr_net_credit,
    rr_ref_up:        c.rr_ref_up,
    rr_ref_dn:        c.rr_ref_dn,
    rr_pnl_dn:        c.rr_pnl_dn,
    rr_true_max_loss: c.rr_true_max_loss,
    // Bear Combo extras
    bc_put_debit:   c.bc_put_debit,
    bc_call_credit: c.bc_call_credit,
    bc_net_cost:    c.bc_net_cost,
    bc_put_width:   c.bc_put_width,
    bc_call_width:  c.bc_call_width,
    bc_lower_be:    c.bc_lower_be,
    bc_upper_be:    c.bc_upper_be,
    // Financed Long Call extras
    flc_put_credit:  c.flc_put_credit,
    flc_call_debit:  c.flc_call_debit,
    flc_net_cost:    c.flc_net_cost,
    flc_put_width:   c.flc_put_width,
    flc_lower_be:    c.flc_lower_be,
    flc_upper_be:    c.flc_upper_be,
    // Financed Long Put extras
    flp_call_credit: c.flp_call_credit,
    flp_put_debit:   c.flp_put_debit,
    flp_net_cost:    c.flp_net_cost,
    flp_call_width:  c.flp_call_width,
    flp_upper_be:    c.flp_upper_be,
    flp_lower_be:    c.flp_lower_be,
    // Ratio Backspread extras
    rbc_net_cost:  c.rbc_net_cost,
    rbc_spread_w:  c.rbc_spread_w,
    rbc_upper_be:  c.rbc_upper_be,
    rbc_dead_be:   c.rbc_dead_be,
    rbc_max_loss:  c.rbc_max_loss,
    rbp_net_cost:  c.rbp_net_cost,
    rbp_spread_w:  c.rbp_spread_w,
    rbp_lower_be:  c.rbp_lower_be,
    rbp_dead_be:   c.rbp_dead_be,
    rbp_max_loss:  c.rbp_max_loss,
    // Long Strangle extras
    ls_call_k:      c.ls_call_k,
    ls_put_k:       c.ls_put_k,
    ls_call_debit:  c.ls_call_debit,
    ls_put_debit:   c.ls_put_debit,
    ls_total_debit: c.ls_total_debit,
    ls_call_be:     c.ls_call_be,
    ls_put_be:      c.ls_put_be,
    ls_fits_cap:    c.ls_fits_cap,
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

  if (struct === "Risk Reversal") {
    const netCred = sp.rr_net_credit;
    const credStr = netCred != null
      ? (netCred >= 0 ? `net credit $${netCred.toFixed(2)}` : `net debit $${Math.abs(netCred).toFixed(2)}`)
      : costLabel;
    lines.push(`<strong>Risk Reversal</strong> — ${qty} contract${qty > 1 ? "s" : ""}, ${credStr}.`);
    lines.push(`✅ Unlimited upside above $${sp.strike_hi?.toFixed(2)}. ❌ Large downside below put strike $${sp.strike_lo?.toFixed(2)} — uncapped (stock can go to zero).`);
    if (bes.length) lines.push(`Downside breakeven: <strong>$${bes[0].toFixed(2)}</strong>. Loss grows below this level.`);
    const trueMaxLoss = sp.rr_true_max_loss;
    if (trueMaxLoss != null) lines.push(`<strong>True max loss if stock → $0:</strong> <span class="fail">-$${trueMaxLoss.toFixed(2)}/sh (-$${(trueMaxLoss * 100).toFixed(0)}/contract)</span>. Add a protective put below to cap this.`);
    const refUp = sp.rr_ref_up;
    const refDn = sp.rr_ref_dn;
    const pnlDn = sp.rr_pnl_dn;
    if (maxP != null) lines.push(`P&L at +10% ($${refUp != null ? refUp.toFixed(2) : "?"}): <span class="pass">+$${maxP.toFixed(2)}</span>.`);
    if (pnlDn != null) lines.push(`P&L at put strike −20% ($${refDn != null ? refDn.toFixed(2) : "?"}): <span class="fail">$${pnlDn.toFixed(2)}</span> (loss accelerates below put strike $${sp.strike_lo?.toFixed(2)}).`);
  } else if (struct === "Financed Long Call") {
    const netCost = sp.flc_net_cost;
    const costStr = netCost != null
      ? (netCost < 0 ? `net credit $${Math.abs(netCost).toFixed(2)}` : `net debit $${netCost.toFixed(2)}`)
      : costLabel;
    lines.push(`<strong>Financed Long Call</strong> (put credit spread + long call) — ${qty} contract${qty > 1 ? "s" : ""}, ${costStr}.`);
    lines.push(`❌ Max loss <span class="fail">-$${maxL != null ? maxL.toFixed(2) : "?"}</span> if stock falls below $${sp.put_long?.toFixed(2)} — defined by the put spread.`);
    lines.push(`✅ Unlimited upside above $${sp.flc_upper_be?.toFixed(2)} (long call has no cap).`);
    if (sp.flc_put_credit != null && sp.flc_call_debit != null)
      lines.push(`Put spread credit $${sp.flc_put_credit.toFixed(2)} offsets call debit $${sp.flc_call_debit.toFixed(2)} — ${netCost < 0 ? "fully financed plus a credit" : "partially financed"}.`);
    if (sp.flc_lower_be != null) lines.push(`Put spread enters loss zone below $${sp.flc_lower_be.toFixed(2)}.`);
  } else if (struct === "Financed Long Put") {
    const netCost = sp.flp_net_cost;
    const costStr = netCost != null
      ? (netCost < 0 ? `net credit $${Math.abs(netCost).toFixed(2)}` : `net debit $${netCost.toFixed(2)}`)
      : costLabel;
    lines.push(`<strong>Financed Long Put</strong> (call credit spread + long put) — ${qty} contract${qty > 1 ? "s" : ""}, ${costStr}.`);
    lines.push(`❌ Max loss <span class="fail">-$${maxL != null ? maxL.toFixed(2) : "?"}</span> if stock rises above $${sp.call_long?.toFixed(2)} — defined by the call spread.`);
    lines.push(`✅ Unlimited downside profit below $${sp.flp_lower_be?.toFixed(2)} (long put, no floor above $0).`);
    if (sp.flp_call_credit != null && sp.flp_put_debit != null)
      lines.push(`Call spread credit $${sp.flp_call_credit.toFixed(2)} offsets put debit $${sp.flp_put_debit.toFixed(2)} — ${netCost < 0 ? "fully financed plus a credit" : "partially financed"}.`);
    if (sp.flp_upper_be != null) lines.push(`Call spread enters loss zone above $${sp.flp_upper_be.toFixed(2)}.`);
  } else if (struct === "Ratio Call Backspread") {
    const netCost = sp.rbc_net_cost;
    const costStr = netCost != null
      ? (netCost <= 0 ? `net credit $${Math.abs(netCost).toFixed(2)}` : `net debit $${netCost.toFixed(2)}`)
      : costLabel;
    lines.push(`<strong>Ratio Call Backspread</strong> (sell 1× $${sp.strike_lo?.toFixed(0)}C / buy 2× $${sp.strike_hi?.toFixed(0)}C) — ${costStr}.`);
    lines.push(`⚠ Dead zone: max loss <span class="fail">-$${maxL != null ? maxL.toFixed(2) : "?"}</span> if stock expires between $${sp.strike_lo?.toFixed(2)}–$${sp.strike_hi?.toFixed(2)}.`);
    lines.push(`✅ Unlimited upside above <strong>$${sp.rbc_upper_be?.toFixed(2)}</strong> (2× long call). ${netCost <= 0 ? `Credit $${Math.abs(netCost ?? 0).toFixed(2)} kept if stock stays below $${sp.strike_lo?.toFixed(2)}.` : ""}`);
    lines.push(`Best in Low IV expecting a large up move + IV expansion. Close early if stock drifts into the dead zone.`);
  } else if (struct === "Ratio Put Backspread") {
    const netCost = sp.rbp_net_cost;
    const costStr = netCost != null
      ? (netCost <= 0 ? `net credit $${Math.abs(netCost).toFixed(2)}` : `net debit $${netCost.toFixed(2)}`)
      : costLabel;
    lines.push(`<strong>Ratio Put Backspread</strong> (sell 1× $${sp.strike_hi?.toFixed(0)}P / buy 2× $${sp.strike_lo?.toFixed(0)}P) — ${costStr}.`);
    lines.push(`⚠ Dead zone: max loss <span class="fail">-$${maxL != null ? maxL.toFixed(2) : "?"}</span> if stock expires between $${sp.strike_lo?.toFixed(2)}–$${sp.strike_hi?.toFixed(2)}.`);
    lines.push(`✅ Unlimited downside profit below <strong>$${sp.rbp_lower_be?.toFixed(2)}</strong> (2× long put). ${netCost <= 0 ? `Credit $${Math.abs(netCost ?? 0).toFixed(2)} kept if stock stays above $${sp.strike_hi?.toFixed(2)}.` : ""}`);
    lines.push(`Best in Low IV expecting a large down move or crash + IV expansion. Close early if stock drifts into the dead zone.`);
  } else if (struct === "Long Strangle") {
    lines.push(`<strong>Long Strangle</strong> — buy $${sp.ls_put_k?.toFixed(0)}P + $${sp.ls_call_k?.toFixed(0)}C, total debit $${sp.ls_total_debit?.toFixed(2)}/sh ($${((sp.ls_total_debit ?? 0) * 100).toFixed(0)}/contract).`);
    lines.push(`✅ Profits if stock moves more than $${sp.ls_total_debit?.toFixed(2)} from either strike by expiry.`);
    if (sp.ls_call_be != null && sp.ls_put_be != null)
      lines.push(`Upper BE: <strong>$${sp.ls_call_be.toFixed(2)}</strong> / Lower BE: <strong>$${sp.ls_put_be.toFixed(2)}</strong>.`);
    if (sp.ls_fits_cap === false)
      lines.push(`<span class="warn">⚠ Debit exceeds risk limit — informational only. Suitable for accounts with larger capital allocation.</span>`);
    else
      lines.push(`Best entered when IV is low — benefits from both a large move AND vol expansion (long vega on both legs).`);
  } else if (struct === "Bear Combo") {
    const netCost = sp.bc_net_cost;
    const costStr = netCost != null
      ? (netCost < 0 ? `net credit $${Math.abs(netCost).toFixed(2)}` : `net debit $${netCost.toFixed(2)}`)
      : costLabel;
    lines.push(`<strong>Bear Combo</strong> (bear put spread + bear call spread) — ${qty} contract${qty > 1 ? "s" : ""}, ${costStr}.`);
    lines.push(`✅ Max profit <span class="pass">+$${maxP != null ? maxP.toFixed(2) : "?"}</span> if stock falls below $${sp.put_short?.toFixed(2)} at expiry.`);
    lines.push(`❌ Max loss <span class="fail">-$${maxL != null ? maxL.toFixed(2) : "?"}</span> if stock rises above $${sp.call_long?.toFixed(2)} — all 4 legs defined, no naked exposure.`);
    const lowerBe = sp.bc_lower_be, upperBe = sp.bc_upper_be;
    if (lowerBe != null && upperBe != null)
      lines.push(`Profit zone: stock below <strong>$${lowerBe.toFixed(2)}</strong>. Loss zone: stock above <strong>$${upperBe.toFixed(2)}</strong>.`);
    if (sp.bc_put_debit != null && sp.bc_call_credit != null)
      lines.push(`Put spread debit $${sp.bc_put_debit.toFixed(2)}, call spread credit $${sp.bc_call_credit.toFixed(2)} — call premium offsets put cost.`);
  } else {
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
  }

  if (sp.ul_price != null && bes.length) {
    const ul = sp.ul_price;
    const profitable =
      struct === "Risk Reversal"         ? ul > bes[0]
      : struct === "Bear Combo"          ? (bes.length >= 1 ? ul < bes[0] : false)
      : struct === "Financed Long Call"  ? (bes.length >= 2 ? ul > bes[1] : bes.length === 1 ? ul > bes[0] : false)
      : struct === "Financed Long Put"   ? (bes.length >= 1 ? ul < bes[0] : false)
      : struct === "Ratio Call Backspread" ? (bes.length >= 2 ? (ul < bes[0] || ul > bes[1]) : ul > (bes[0] ?? Infinity))
      : struct === "Ratio Put Backspread"  ? (bes.length >= 2 ? (ul > bes[1] || ul < bes[0]) : ul < (bes[0] ?? -Infinity))
      : struct === "Long Strangle"       ? (bes.length >= 2 ? (ul < bes[0] || ul > bes[1]) : false)
      : bes.length === 1
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

// ── Shared ML / Bias info modal ───────────────────────────────────────────────

function _ensureInfoModal() {
  if (document.getElementById("lp-info-modal")) return;
  const el = document.createElement("div");
  el.id = "lp-info-modal";
  el.className = "lp-modal-overlay";
  el.style.display = "none";
  el.innerHTML = `
    <div class="lp-modal-dialog" style="max-width:520px">
      <div class="lp-modal-header">
        <span id="lp-info-modal-title" class="lp-modal-title"></span>
        <button class="lp-modal-close" id="lp-info-modal-close">&times;</button>
      </div>
      <div id="lp-info-modal-body" class="lp-modal-body" style="padding:1rem 1.2rem 1.2rem"></div>
    </div>`;
  document.body.appendChild(el);
  el.addEventListener("click", e => { if (e.target === el) _closeInfoModal(); });
  document.getElementById("lp-info-modal-close").addEventListener("click", _closeInfoModal);
  document.addEventListener("keydown", e => { if (e.key === "Escape") _closeInfoModal(); });
}

function _closeInfoModal() {
  const m = document.getElementById("lp-info-modal");
  if (m) m.style.display = "none";
}

function _openInfoModal(type, detail) {
  _ensureInfoModal();
  const title = document.getElementById("lp-info-modal-title");
  const body  = document.getElementById("lp-info-modal-body");
  title.textContent = detail.title || "Details";

  if (type === "bias") {
    const reasons = (detail.reasons || []).map(r => `<li>${r}</li>`).join("");
    const actionHtml = detail.action
      ? `<div class="lp-tracking-action" style="margin-top:.8rem"><strong>Suggested action:</strong> ${detail.action}</div>`
      : "";
    const noteHtml = detail.note
      ? `<p class="lp-tracking-note" style="margin-top:.8rem">${detail.note}</p>`
      : "";
    body.innerHTML = `<ul class="lp-tracking-reasons" style="margin:0">${reasons || "<li>No signal data available.</li>"}</ul>${actionHtml}${noteHtml}`;
  } else if (type === "ml") {
    const rowsHtml = (detail.rows || []).map(({ signal, val, vCls, desc }) => `
      <div class="lp-ml-modal-row">
        <div class="lp-ml-modal-header-row">
          <span class="lp-ml-signal">${signal}</span>
          <span class="lp-ml-val${vCls ? " " + vCls : ""}">${val}</span>
        </div>
        <p class="lp-ml-modal-desc">${desc}</p>
      </div>`).join("");
    body.innerHTML = `<div class="lp-ml-modal-rows">${rowsHtml}</div>`;
  } else if (type === "metric-group") {
    const rowsHtml = (detail.rows || []).map(({ label, desc }) => `
      <div class="lp-ml-modal-row">
        <div class="lp-ml-modal-header-row">
          <span class="lp-ml-signal">${label}</span>
        </div>
        <p class="lp-ml-modal-desc">${desc}</p>
      </div>`).join("");
    body.innerHTML = `<div class="lp-ml-modal-rows">${rowsHtml}</div>`;
  } else if (type === "metric") {
    body.innerHTML = `<p class="lp-ml-modal-desc" style="margin:0">${detail.desc || ""}</p>`;
  }

  document.getElementById("lp-info-modal").style.display = "flex";
}

// Global delegated click handler — works for dynamically injected cards
document.addEventListener("click", e => {
  const btn = e.target.closest(".lp-info-btn");
  if (!btn) return;
  e.stopPropagation();
  const type = btn.dataset.infoType;
  let detail;
  try { detail = JSON.parse(btn.dataset.info); } catch { return; }
  _openInfoModal(type, detail);
});
