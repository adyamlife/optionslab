function mispriceCls(vp) {
  if (vp >  1.5) return "mp-expensive";
  if (vp < -1.5) return "mp-cheap";
  return "mp-neutral";
}
function sign(v) { return v > 0 ? "+" : ""; }

function renderTopTable(rows) {
  const tbody = document.getElementById("mp-top-body");
  tbody.innerHTML = "";
  rows.forEach(r => {
    const cls = mispriceCls(r.mispricing);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.expiry}</td>
      <td>${r.dte}d</td>
      <td>${r.strike.toFixed(2)}</td>
      <td>${(r.moneyness * 100).toFixed(1)}%</td>
      <td>${r.market_iv.toFixed(1)}%</td>
      <td>${r.model_iv.toFixed(1)}%</td>
      <td class="${cls}">${sign(r.mispricing)}${r.mispricing.toFixed(1)} vp</td>
      <td class="${cls}">${sign(r.misprice_pct)}${r.misprice_pct.toFixed(1)}%</td>
    `;
    tbody.appendChild(tr);
  });
  document.getElementById("mp-top-section").style.display = "";
}

function renderSlices(slices, spot) {
  const container = document.getElementById("mp-slices");
  container.innerHTML = "";
  slices.forEach(sl => {
    const p = sl.params;
    const section = document.createElement("section");
    section.className = "card";
    section.style.marginBottom = "1rem";

    const paramStr = `a=${p.a.toFixed(4)} b=${p.b.toFixed(4)} ρ=${p.rho.toFixed(3)} m=${p.m.toFixed(3)} σ=${p.sigma.toFixed(3)}`;

    let rows = "";
    sl.strikes.forEach(r => {
      const cls = mispriceCls(r.mispricing);
      const atmFlag = Math.abs(r.moneyness - 1) < 0.03
        ? ' <span class="muted" style="font-size:.75rem">ATM</span>' : "";
      rows += `<tr>
        <td>${r.strike.toFixed(2)}${atmFlag}</td>
        <td>${(r.moneyness * 100).toFixed(1)}%</td>
        <td>${r.market_iv.toFixed(1)}%</td>
        <td>${r.model_iv.toFixed(1)}%</td>
        <td class="${cls}">${sign(r.mispricing)}${r.mispricing.toFixed(1)}</td>
        <td class="${cls}">${sign(r.misprice_pct)}${r.misprice_pct.toFixed(1)}%</td>
      </tr>`;
    });

    section.innerHTML = `
      <div class="slice-hdr">
        <h4>${sl.expiry} <span class="muted" style="font-weight:400;font-size:.85rem">${sl.dte}d · ${sl.n_points} strikes · RMSE ${sl.rmse.toFixed(2)} vp</span></h4>
        <div class="param-pills">
          ${paramStr.split(" ").map(s => `<span class="param-pill">${s}</span>`).join("")}
        </div>
      </div>
      <table class="sched-table">
        <thead><tr>
          <th>Strike</th><th>Moneyness</th><th>Market IV</th><th>Model IV</th>
          <th>Misprice (vp)</th><th>% of Model</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
    container.appendChild(section);
  });
}

// ── Load available tickers for autocomplete ───────────────────────────────────
let _availableTickers = [];

async function loadAvailableTickers() {
  try {
    const r = await fetch("/api/mispricing/tickers");
    const d = await r.json();
    if (!d.ok || !d.tickers.length) return;
    _availableTickers = d.tickers;
    const dl = document.getElementById("mp-ticker-list");
    dl.innerHTML = d.tickers.map(t => `<option value="${t}">`).join("");
  } catch(e) { /* silent — autocomplete is non-critical */ }
}

function showNoDataPanel(ticker) {
  const panel = document.getElementById("mp-no-data");
  document.getElementById("mp-no-data-title").textContent =
    `No chain snapshots found for "${ticker}"`;
  panel.style.display = "";

  const chipsEl   = document.getElementById("mp-ticker-chips");
  const chipsWrap = document.getElementById("mp-available-tickers");
  if (_availableTickers.length) {
    chipsEl.innerHTML = _availableTickers.map(t =>
      `<span style="cursor:pointer;background:var(--clr-bg);border:1px solid var(--clr-border);
       border-radius:12px;padding:.1rem .55rem;font-size:.8rem"
       onclick="document.getElementById('mp-ticker').value='${t}';runAnalysis()">${t}</span>`
    ).join("");
    chipsWrap.style.display = "";
  } else {
    chipsWrap.style.display = "none";
  }
}

// ── Main analysis ─────────────────────────────────────────────────────────────
async function runAnalysis() {
  const ticker  = document.getElementById("mp-ticker").value.trim().toUpperCase();
  const optType = document.getElementById("mp-opt-type").value;
  const maxDte  = document.getElementById("mp-max-dte").value;

  if (!ticker) { document.getElementById("mp-ticker").focus(); return; }

  const btn    = document.getElementById("mp-run-btn");
  const status = document.getElementById("mp-status");
  const errEl  = document.getElementById("mp-error");
  const noData = document.getElementById("mp-no-data");

  btn.disabled = true;
  btn.textContent = "Fitting…";
  status.textContent = "Running SVI fit…";
  errEl.style.display = "none";
  noData.style.display = "none";
  document.getElementById("mp-top-section").style.display = "none";
  document.getElementById("mp-slices").innerHTML = "";
  document.getElementById("mp-meta").style.display = "none";

  try {
    const r = await fetch(`/api/mispricing/${encodeURIComponent(ticker)}?opt_type=${optType}&max_dte=${maxDte}`);
    const d = await r.json();

    if (!d.ok) {
      const isNoData = d.error && d.error.toLowerCase().includes("no chain snapshots");
      if (isNoData) {
        showNoDataPanel(ticker);
      } else {
        errEl.textContent = d.error;
        errEl.style.display = "";
      }
      status.textContent = "";
    } else {
      if (d.top_mispriced?.length) renderTopTable(d.top_mispriced);
      renderSlices(d.slices, d.spot);
      const meta = document.getElementById("mp-meta");
      meta.textContent = `${ticker} · spot $${d.spot.toFixed(2)} · as of ${d.as_of} · source: ${d.source} · ${d.slices.length} expiry slice(s) fitted`;
      meta.style.display = "";
      status.textContent = `Done — ${d.slices.length} slices`;
    }
  } catch(e) {
    errEl.textContent = "Request failed: " + e.message;
    errEl.style.display = "";
    status.textContent = "";
  }

  btn.disabled = false;
  btn.textContent = "Analyze";
}

document.getElementById("mp-run-btn").addEventListener("click", runAnalysis);
document.getElementById("mp-ticker").addEventListener("keydown", e => {
  if (e.key === "Enter") runAnalysis();
});
document.getElementById("mp-ticker").addEventListener("input", e => {
  e.target.value = e.target.value.toUpperCase();
});

loadAvailableTickers();
