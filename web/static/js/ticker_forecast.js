const runBtn  = document.getElementById("tf-run-btn");
const statusEl = document.getElementById("tf-status");
const thead   = document.getElementById("tf-thead");
const tbody   = document.getElementById("tf-tbody");

const SIG_CLASS = {
  "Strong":    "tf-sig-strong",
  "Moderate":  "tf-sig-moderate",
  "Neutral":   "tf-sig-neutral",
  "Weak":      "tf-sig-weak",
  "Conflicted":"tf-sig-conflicted",
};

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function sigBadge(rating) {
  if (!rating) return `<span class="muted">—</span>`;
  const cls = SIG_CLASS[rating] || "tf-sig-neutral";
  return `<span class="tf-sig ${cls}">${esc(rating)}</span>`;
}

function weekHeader(weeks) {
  return weeks.map(w => {
    const d = w.expiry.slice(5);
    return `<th class="tf-week">
      <div>${d}</div>
      <div class="tf-week-dte">DTE ${w.dte}</div>
    </th>`;
  }).join("");
}

function weekCell(week, spot) {
  if (!week || week.p10 == null) {
    return `<td class="tf-no-data">—</td>`;
  }
  const { p10, p50, p90 } = week;
  const range = p90 - p10;
  if (range <= 0) return `<td class="tf-no-data">—</td>`;

  const pct = v => Math.max(0, Math.min(100, (v - p10) / range * 100));
  const p50pct  = pct(p50);
  const spotPct = spot ? pct(spot) : null;

  const dir = !spot ? "" : p50 > spot * 1.002 ? "up" : p50 < spot * 0.998 ? "down" : "";

  const spotMarker = spotPct != null
    ? `<div class="tf-range-spot" style="left:calc(${spotPct.toFixed(1)}% - 1px)"></div>`
    : "";

  const label      = `$${p50.toFixed(1)}`;
  const rangeLabel = `${p10.toFixed(0)} – ${p90.toFixed(0)}`;

  return `<td class="tf-week">
    <div class="tf-week-inner">
      <div class="tf-week-p50 ${dir}">${label}</div>
      <div class="tf-range-bar">
        <div class="tf-range-fill" style="left:0;width:100%"></div>
        <div class="tf-range-mid" style="left:calc(${p50pct.toFixed(1)}% - 0.5px)"></div>
        ${spotMarker}
      </div>
      <div class="tf-week-range-label">${rangeLabel}</div>
    </div>
  </td>`;
}

let _lastExpiries = [];

function buildRows(data) {
  const expiries = data.expiries;
  _lastExpiries = expiries;

  const firstRow = data.tickers.find(t => t.weeks.length > 0);
  if (firstRow) {
    thead.innerHTML = `<tr>
      <th>Ticker</th><th>Price</th><th>Signal</th><th>Structure</th>
      ${weekHeader(firstRow.weeks)}
    </tr>`;
  }

  tbody.innerHTML = data.tickers.map(row => {
    const spot     = row.spot;
    const spotStr  = spot != null ? `$${spot.toFixed(2)}` : "—";
    const snapDate = row.snap_date ? `<div class="tf-snap-date">${row.snap_date}</div>` : "";

    const weekMap = {};
    (row.weeks || []).forEach(w => { weekMap[w.expiry] = w; });

    const weekCells = expiries.map(exp => weekCell(weekMap[exp], spot)).join("");

    return `<tr data-ticker="${esc(row.ticker)}">
      <td>
        <span class="tf-ticker">${esc(row.ticker)}</span>
        <button class="tf-refresh-btn" data-ticker="${esc(row.ticker)}" title="Refresh live forecast">↺</button>
      </td>
      <td><span class="tf-price">${spotStr}</span>${snapDate}</td>
      <td>${sigBadge(row.signal_rating)}</td>
      <td><span class="tf-struct">${esc(row.recommended_structure ?? "—")}</span></td>
      ${weekCells}
    </tr>`;
  }).join("");
}

function showSkeleton(n) {
  tbody.innerHTML = Array.from({length: n}, () => `
    <tr class="tf-skeleton">
      <td><span class="tf-ticker" style="background:var(--bg-subtle);color:transparent;border-radius:4px">XXXX</span></td>
      <td><span style="background:var(--bg-subtle);color:transparent;border-radius:4px">$000.00</span></td>
      <td><span class="tf-sig tf-sig-neutral" style="opacity:.3">——</span></td>
      <td><span class="tf-struct" style="background:var(--bg-subtle);color:transparent;border-radius:4px">——————</span></td>
      <td colspan="8" class="tf-no-data">loading…</td>
    </tr>`).join("");
}

async function refreshRow(ticker, btn) {
  btn.disabled = true;
  btn.textContent = "…";
  const tr = btn.closest("tr");

  try {
    const res  = await fetch(`/api/ticker-forecast?tickers=${encodeURIComponent(ticker)}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unknown error");

    const row = data.tickers[0];
    if (!row) throw new Error("no data returned");

    const expiries = data.expiries.length ? data.expiries : _lastExpiries;
    const spot     = row.spot;
    const spotStr  = spot != null ? `$${spot.toFixed(2)}` : "—";
    const snapDate = row.snap_date ? `<div class="tf-snap-date">${row.snap_date}</div>` : "";

    tr.cells[1].innerHTML = `<span class="tf-price">${spotStr}</span>${snapDate}`;

    const weekMap = {};
    (row.weeks || []).forEach(w => { weekMap[w.expiry] = w; });

    const tmp = document.createElement("tbody");
    tmp.innerHTML = `<tr>${expiries.map(exp => weekCell(weekMap[exp], spot)).join("")}</tr>`;
    const newCells = Array.from(tmp.firstChild.cells);
    expiries.forEach((exp, i) => {
      const old = tr.cells[4 + i];
      if (old && newCells[i]) old.replaceWith(newCells[i]);
    });
  } catch (e) {
    console.error("refresh failed:", e);
  }

  btn.disabled    = false;
  btn.textContent = "↺";
}

tbody.addEventListener("click", e => {
  const btn = e.target.closest(".tf-refresh-btn");
  if (btn && !btn.disabled) refreshRow(btn.dataset.ticker, btn);
});

runBtn.addEventListener("click", async () => {
  runBtn.disabled = true;
  statusEl.textContent = "Running… (may take 30–60s for all tickers)";
  showSkeleton(20);

  const t0 = Date.now();
  try {
    const res  = await fetch("/api/ticker-forecast");
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "unknown error");
    buildRows(data);
    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    statusEl.textContent = `${data.tickers.length} tickers · ${data.n_sims} sims/run · loaded in ${elapsed}s`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="12" class="tf-no-data" style="color:var(--fail)">Error: ${esc(e.message)}</td></tr>`;
    statusEl.textContent = "Failed.";
  }
  runBtn.disabled = false;
});
