// Portfolio Upload — parse, display, analyse broker position exports

// ── Upload wiring ─────────────────────────────────────────────────────────────

const uploadZone  = document.getElementById("upload-zone");
const fileInput   = document.getElementById("file-input");
const statusEl    = document.getElementById("upload-status");
const resultsEl   = document.getElementById("upload-results");

function setStatus(msg, cls = "") {
  statusEl.textContent = msg;
  statusEl.className   = "status " + cls;
}

// Drag-and-drop
uploadZone.addEventListener("dragover", (e) => { e.preventDefault(); uploadZone.classList.add("drag-over"); });
uploadZone.addEventListener("dragleave", ()  => uploadZone.classList.remove("drag-over"));
uploadZone.addEventListener("drop", (e) => {
  e.preventDefault();
  uploadZone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});

// Click-to-browse
uploadZone.addEventListener("click", (e) => {
  if (e.target.tagName !== "LABEL") fileInput.click();
});
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) uploadFile(fileInput.files[0]);
});

async function uploadFile(file) {
  setStatus(`Uploading ${file.name}…`);
  resultsEl.innerHTML = "";

  const form = new FormData();
  form.append("file", file);

  try {
    const res  = await fetch("/api/upload-positions", { method: "POST", body: form });
    const data = await res.json();
    if (!data.ok) { setStatus("Upload failed: " + data.error, "error"); return; }
    setStatus(`Analysed: ${file.name}`, "ok");
    renderResults(data.groups);
  } catch (err) {
    setStatus("Network error: " + err, "error");
  }
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function fmt(v, prefix = "$", digits = 2) {
  if (v == null) return "—";
  const n = parseFloat(v);
  return (n < 0 ? "-" + prefix + Math.abs(n).toFixed(digits)
                :        prefix + n.toFixed(digits));
}

function fmtPct(v) { return v != null ? v.toFixed(1) + "%" : "—"; }

function pnlCls(v) { return v == null ? "na" : v > 0 ? "pass" : v < 0 ? "fail" : "na"; }

function renderHedge(hedge) {
  if (!hedge) return "";
  const isUrgent   = hedge.hedge_structure.includes("CRITICAL");
  const urgentCls  = isUrgent ? "hedge-urgent" : "hedge-normal";
  const origProfit = (hedge.combined_max_profit + hedge.hedge_cost_per_share).toFixed(2);
  const origLoss   = hedge.combined_max_loss != null
    ? (hedge.combined_max_loss - hedge.hedge_cost_per_share).toFixed(2) : "—";
  const combinedLoss = hedge.combined_max_loss != null
    ? `$${hedge.combined_max_loss.toFixed(2)} <em class="muted">(+$${hedge.hedge_cost_per_share.toFixed(2)})</em>`
    : `<span class="pass" title="${hedge.combined_max_loss_note ?? ""}">Defined ✓</span>`;
  const combinedDeltaCls =
    Math.abs(hedge.combined_delta) < 0.05 ? "pass" :
    Math.abs(hedge.combined_delta) < 0.20 ? "na"   : "warn";

  return `
    <details class="hedge-block ${urgentCls}" style="margin-top:0.75rem">
      <summary class="hedge-summary">
        <span class="hedge-icon">${isUrgent ? "⚠" : "🛡"}</span>
        <strong>Hedge Suggestion:</strong> ${hedge.hedge_structure}
        <span class="hedge-cost-pill">Est. ~$${hedge.hedge_cost_per_share.toFixed(2)}/share &nbsp;·&nbsp; ~$${hedge.hedge_cost_per_contract.toFixed(0)}/contract</span>
      </summary>
      <div class="hedge-body">
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
            <div class="hedge-row"><span>Max Loss</span><span class="warn">${combinedLoss}</span></div>
            <div class="hedge-row"><span>Net Delta</span><span class="${combinedDeltaCls}">${hedge.combined_delta >= 0 ? "+" : ""}${hedge.combined_delta.toFixed(3)}</span></div>
          </div>
        </div>
        ${hedge.combined_max_loss_note ? `<p class="hedge-protection-note hedge-urgent-note">✅ ${hedge.combined_max_loss_note}</p>` : ""}
        <p class="hedge-protection-note">ℹ️ ${hedge.protection_note}</p>
        <p class="hedge-cost-note muted">⚠ ${hedge.cost_note}</p>
      </div>
    </details>`;
}

function renderSpread(sp) {
  const pnlC     = pnlCls(sp.unrealized_pnl);
  const pnlPctC  = pnlCls(sp.pnl_pct);
  const popC     = sp.pop_est == null ? "na" : sp.pop_est >= 60 ? "pass" : sp.pop_est >= 40 ? "na" : "warn";
  const dteC     = sp.dte == null ? "na" : sp.dte <= 5 ? "fail" : sp.dte <= 14 ? "warn" : "na";
  const moveC    = sp.move_to_be_pct == null ? "na"
                 : sp.move_to_be_pct > 10 ? "fail"
                 : sp.move_to_be_pct > 3  ? "warn" : "pass";

  // Risk summary badge
  let riskLevel = "na", riskLabel = "OK";
  if (sp.dte != null && sp.dte <= 5)              { riskLevel = "fail"; riskLabel = "Expiring Soon"; }
  else if (sp.pop_est != null && sp.pop_est < 35) { riskLevel = "fail"; riskLabel = "Low POP"; }
  else if (sp.move_to_be_pct != null && sp.move_to_be_pct > 10) { riskLevel = "warn"; riskLabel = "Far from BE"; }
  else if (sp.unrealized_pnl != null && sp.unrealized_pnl < 0)  { riskLevel = "warn"; riskLabel = "Losing"; }
  else if (sp.pop_est != null && sp.pop_est >= 55) { riskLevel = "pass"; riskLabel = "On Track"; }

  const legRows = (sp.legs || []).map(leg => `
    <tr class="leg-row">
      <td class="muted" style="padding-left:1.5rem">${leg.raw ?? "—"}</td>
      <td class="muted">${leg.qty >= 0 ? "Long" : "Short"}</td>
      <td class="muted">${fmt(leg.mark)}</td>
      <td class="muted">${fmt(leg.mark_chg)}</td>
      <td class="muted">${fmt(leg.cost_value, "$", 2)}</td>
      <td class="muted">${fmt(leg.market_value, "$", 2)}</td>
    </tr>`).join("");

  return `
    <div class="pu-spread-card">
      <div class="pu-spread-header">
        <div>
          <span class="pu-spread-title">${sp.desc}</span>
          ${sp.structure ? `<span class="pu-structure-badge">${sp.structure}</span>` : ""}
        </div>
        <span class="pu-risk-badge ${riskLevel}">${riskLabel}</span>
      </div>

      <div class="pu-metrics">
        <div class="pu-metric">
          <div class="pu-metric-label">Unrealized P&amp;L</div>
          <div class="pu-metric-value ${pnlC}">${fmt(sp.unrealized_pnl)} <span class="muted">(${fmtPct(sp.pnl_pct)} of max)</span></div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Max Profit / Max Loss <em class="muted">(per share)</em></div>
          <div class="pu-metric-value">
            <span class="pass">${fmt(sp.max_profit_ps)}</span>
            <span class="muted"> / </span>
            <span class="fail">${fmt(sp.max_loss_ps)}</span>
            ${sp.width ? `<span class="muted"> (width $${sp.width.toFixed(2)})</span>` : ""}
          </div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">POP (market-implied)</div>
          <div class="pu-metric-value ${popC}">${fmtPct(sp.pop_est)}
            <span class="muted hint-inline" title="Estimated from current spread value vs spread width. Higher = more likely to profit at expiry.">?</span>
          </div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Breakeven / Move needed</div>
          <div class="pu-metric-value">
            ${fmt(sp.breakeven, "$", 3)}
            ${sp.move_to_be != null
              ? `<span class="${moveC}"> (${sp.move_to_be >= 0 ? "+" : ""}${sp.move_to_be.toFixed(3)} / ${sp.move_to_be_pct >= 0 ? "+" : ""}${fmtPct(sp.move_to_be_pct)})</span>`
              : ""}
          </div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Days to Expiry</div>
          <div class="pu-metric-value ${dteC}">${sp.dte != null ? sp.dte + "d" : "—"} <span class="muted">(${sp.expiry ?? "—"})</span></div>
        </div>
        <div class="pu-metric">
          <div class="pu-metric-label">Underlying / Change</div>
          <div class="pu-metric-value">
            ${fmt(sp.ul_price, "$", 3)}
            ${sp.ul_change != null ? `<span class="${sp.ul_change >= 0 ? "pass" : "fail"}"> (${sp.ul_change >= 0 ? "+" : ""}${sp.ul_change.toFixed(3)})</span>` : ""}
          </div>
        </div>
      </div>

      ${(sp.legs || []).length > 0 ? `
        <details class="legs-detail">
          <summary>Individual Legs</summary>
          <div class="table-scroll">
            <table class="journal-table">
              <thead><tr><th>Leg</th><th>Side</th><th>Mark</th><th>Mark Chg</th><th>Cost</th><th>Mkt Value</th></tr></thead>
              <tbody>${legRows}</tbody>
            </table>
          </div>
        </details>` : ""}

      ${renderHedge(sp.hedge)}
    </div>`;
}

function renderResults(groups) {
  if (!groups || !groups.length) {
    resultsEl.innerHTML = `<div class="panel"><p class="na">No positions found in the uploaded file.</p></div>`;
    return;
  }

  const html = groups.map(g => {
    if (!g.spreads || !g.spreads.length) return "";
    const spreads = g.spreads.map(renderSpread).join("");
    return `
      <section class="panel pu-group">
        <h3 class="pu-group-title">${g.name} ${g.ticker ? `<span class="pu-ticker-badge">${g.ticker}</span>` : ""}</h3>
        ${spreads}
      </section>`;
  }).join("");

  resultsEl.innerHTML = html || `<div class="panel"><p class="na">No spreads found.</p></div>`;
}
