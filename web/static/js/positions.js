// Trade Journal — position CRUD and portfolio risk display

let _pendingLogData = null;
let _pendingCloseId = null;

// ── API helpers ───────────────────────────────────────────────────────────────

async function fetchPositions() {
  const res = await fetch("/api/positions");
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`/api/positions ${res.status}: ${text.substring(0, 120)}`);
  }
  return res.json();
}

async function apiAddPosition(data) {
  const res = await fetch("/api/positions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return res.json();
}

async function apiClosePosition(id, closeValue) {
  const res = await fetch(`/api/positions/${id}/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ close_value: closeValue }),
  });
  return res.json();
}

async function apiExpirePosition(id) {
  const res = await fetch(`/api/positions/${id}/expire`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return res.json();
}

// ── Risk banner (shown above live results) ────────────────────────────────────

function renderRiskBanner(warnings) {
  const el = document.getElementById("risk-banner");
  if (!el) return;
  if (!warnings || !warnings.length) {
    el.innerHTML = "";
    return;
  }
  const items = warnings.map((w) => {
    const cls = w.level === "danger" ? "risk-danger" : "risk-warn";
    return `<div class="${cls}">⚠ ${w.message}</div>`;
  }).join("");
  el.innerHTML = `<div class="risk-banner">${items}</div>`;
}

// ── Portfolio summary bar ─────────────────────────────────────────────────────

function renderJournalSummary(summary) {
  const el = document.getElementById("journal-summary");
  if (!el) return;
  const pnlCls = summary.realized_pnl >= 0 ? "pass" : "fail";
  const deployPct = summary.pct_deployed;
  const deployBarCls = deployPct > 80 ? "deploy-bar-danger" : deployPct > 60 ? "deploy-bar-warn" : "deploy-bar-ok";
  el.innerHTML = `
    <div class="journal-summary-row">
      <div class="jsumm-stat"><div class="jsumm-label">Open Trades</div><div class="jsumm-value">${summary.open_count}</div></div>
      <div class="jsumm-stat"><div class="jsumm-label">Capital Deployed</div><div class="jsumm-value">$${summary.capital_deployed.toFixed(0)} <span class="muted">(${deployPct}%)</span></div></div>
      <div class="jsumm-stat"><div class="jsumm-label">Available</div><div class="jsumm-value">$${summary.capital_available.toFixed(0)}</div></div>
      <div class="jsumm-stat"><div class="jsumm-label">Realized P&amp;L</div><div class="jsumm-value ${pnlCls}">${summary.realized_pnl >= 0 ? "+" : ""}$${summary.realized_pnl.toFixed(2)}</div></div>
      <div class="jsumm-stat"><div class="jsumm-label">Win Rate</div><div class="jsumm-value">${summary.win_rate != null ? summary.win_rate + "%" : "—"} <span class="muted">(${summary.closed_count} closed)</span></div></div>
    </div>
    <div class="deploy-bar-track" title="${deployPct}% of capital deployed">
      <div class="deploy-bar-fill ${deployBarCls}" style="width:${Math.min(deployPct,100)}%"></div>
    </div>`;
}

// ── Open positions table ──────────────────────────────────────────────────────

function renderOpenPositions(positions) {
  const el = document.getElementById("journal-open");
  if (!el) return;
  const open = positions.filter((p) => p.status === "open");
  if (!open.length) {
    el.innerHTML = `<p class="na" style="margin-top:1rem">No open positions. Use "Log Trade" on any recommended candidate above.</p>`;
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  const rows = open.map((p) => {
    const expDate = new Date(p.expiry);
    const dteLeft = Math.ceil((expDate - new Date()) / 86400000);
    const dteCls  = dteLeft <= 5 ? "fail" : dteLeft <= 10 ? "warn" : "";
    const dCls    = (p.net_delta || 0) > 0.1 ? "pass" : (p.net_delta || 0) < -0.1 ? "fail" : "na";
    const thCls   = (p.net_theta || 0) > 0 ? "pass" : "warn";
    const typeTag = p.is_credit
      ? `<span class="badge-credit">Credit</span>`
      : `<span class="badge-debit">Debit</span>`;
    return `
      <tr>
        <td><strong>${p.ticker}</strong></td>
        <td>${p.structure} ${typeTag}</td>
        <td>${p.entry_date}</td>
        <td>${p.expiry} <span class="${dteCls}">(${dteLeft}d)</span></td>
        <td>${p.contracts}</td>
        <td>$${(p.entry_value || 0).toFixed(2)}</td>
        <td>${p.max_profit != null ? "$" + p.max_profit.toFixed(2) : "—"}</td>
        <td>${p.capital_required != null ? "$" + p.capital_required.toFixed(0) : "—"}</td>
        <td><span class="${dCls}">${p.net_delta != null ? (p.net_delta >= 0 ? "+" : "") + p.net_delta : "—"}</span></td>
        <td><span class="${thCls}">${p.net_theta != null ? (p.net_theta >= 0 ? "+" : "") + p.net_theta.toFixed(3) : "—"}</span></td>
        <td>
          <button class="btn-close-pos" data-id="${p.id}"
            data-info="${p.ticker} ${p.structure} | Entry $${(p.entry_value||0).toFixed(2)} | Max Profit $${(p.max_profit||0).toFixed(2)} | ${p.is_credit ? 'Credit spread' : 'Debit spread'}">
            Close
          </button>
        </td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <h3 style="margin-top:1.25rem">Open Positions</h3>
    <div class="table-scroll">
      <table class="journal-table">
        <thead><tr>
          <th>Ticker</th><th>Structure</th><th>Entry</th><th>Expiry</th>
          <th>Qty</th><th>Entry $</th><th>Max Profit</th><th>Capital</th>
          <th title="Net position delta: positive = bullish, negative = bearish">Δ</th>
          <th title="Daily theta ($ per share per day). Positive = earning time decay.">Θ/day</th>
          <th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ── Closed positions table ────────────────────────────────────────────────────

function renderClosedPositions(positions) {
  const el = document.getElementById("journal-closed");
  if (!el) return;
  const closed = positions.filter((p) => p.status === "closed").slice().reverse();
  if (!closed.length) {
    el.innerHTML = "";
    return;
  }
  const rows = closed.map((p) => {
    const pnl    = p.close_pnl || 0;
    const pnlCls = pnl > 0 ? "pass" : pnl < 0 ? "fail" : "na";
    const typeTag = p.is_credit
      ? `<span class="badge-credit">Credit</span>`
      : `<span class="badge-debit">Debit</span>`;
    return `
      <tr>
        <td><strong>${p.ticker}</strong></td>
        <td>${p.structure} ${typeTag}</td>
        <td>${p.entry_date}</td>
        <td>${p.close_date || "—"}</td>
        <td>${p.contracts}</td>
        <td>$${(p.entry_value || 0).toFixed(2)}</td>
        <td>${p.close_value != null ? "$" + p.close_value.toFixed(2) : "—"}</td>
        <td class="${pnlCls}">${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}</td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <h3 style="margin-top:1.25rem">Closed Positions</h3>
    <div class="table-scroll">
      <table class="journal-table">
        <thead><tr>
          <th>Ticker</th><th>Structure</th><th>Entry</th><th>Closed</th>
          <th>Qty</th><th>Entry $</th><th>Close $</th><th>P&amp;L</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ── Full journal refresh ──────────────────────────────────────────────────────

async function refreshJournal() {
  try {
    const data = await fetchPositions();
    renderRiskBanner(data.warnings);
    renderJournalSummary(data.summary);
    renderOpenPositions(data.positions);
    renderClosedPositions(data.positions);
  } catch (e) {
    console.error("Journal load failed:", e);
  }
}

// ── Log Trade modal (called from live.js) ─────────────────────────────────────

function openLogModal(tradeData) {
  _pendingLogData = tradeData;
  document.getElementById("log-modal-title").textContent =
    `Log Trade: ${tradeData.ticker} — ${tradeData.structure}`;
  document.getElementById("log-modal-details").textContent = tradeData.details || "";
  document.getElementById("log-contracts").value = 1;
  document.getElementById("log-modal").style.display = "flex";
}

function closeLogModal() {
  _pendingLogData = null;
  document.getElementById("log-modal").style.display = "none";
}

// ── Close Position modal ──────────────────────────────────────────────────────

function openCloseModal(posId, infoText) {
  _pendingCloseId = posId;
  document.getElementById("close-modal-info").textContent = infoText;
  document.getElementById("close-value-input").value = "";
  document.getElementById("close-modal").style.display = "flex";
}

function closeCloseModal() {
  _pendingCloseId = null;
  document.getElementById("close-modal").style.display = "none";
}

// ── Event wiring ──────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  refreshJournal();

  // Log-trade modal confirm
  document.getElementById("log-modal-confirm").addEventListener("click", async () => {
    if (!_pendingLogData) return;
    const contracts = parseInt(document.getElementById("log-contracts").value, 10) || 1;
    const payload = { ..._pendingLogData, contracts };
    const result = await apiAddPosition(payload);
    if (result.ok) {
      closeLogModal();
      refreshJournal();
    } else {
      alert("Failed to log trade: " + result.error);
    }
  });
  document.getElementById("log-modal-cancel").addEventListener("click", closeLogModal);

  // Close-position modal
  document.getElementById("close-modal-confirm").addEventListener("click", async () => {
    if (!_pendingCloseId) return;
    const val = parseFloat(document.getElementById("close-value-input").value);
    if (isNaN(val) || val < 0) { alert("Enter a valid close price (≥ 0)."); return; }
    await apiClosePosition(_pendingCloseId, val);
    closeCloseModal();
    refreshJournal();
  });
  document.getElementById("close-modal-expire").addEventListener("click", async () => {
    if (!_pendingCloseId) return;
    await apiExpirePosition(_pendingCloseId);
    closeCloseModal();
    refreshJournal();
  });
  document.getElementById("close-modal-cancel").addEventListener("click", closeCloseModal);

  // Close-position button delegation (open positions table)
  document.getElementById("journal-open").addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-close-pos");
    if (!btn) return;
    openCloseModal(btn.dataset.id, btn.dataset.info);
  });

  // Close modal on overlay click
  document.getElementById("log-modal").addEventListener("click", (e) => {
    if (e.target === document.getElementById("log-modal")) closeLogModal();
  });
  document.getElementById("close-modal").addEventListener("click", (e) => {
    if (e.target === document.getElementById("close-modal")) closeCloseModal();
  });
});
