// Watchlist selector — session only, nothing saved to disk.
// On page load all tickers from settings.toml are shown.
// User toggles which ones to include in the current run (max 50).
// Extra tickers can be added temporarily via the search box.

const WL_MAX = 50;
let _wlAll      = [];   // full list from server (settings.toml)
let _wlSelected = new Set();  // currently selected for this run
let _wlExtra    = [];   // tickers added this session not in _wlAll

// ── Tiny helpers ──────────────────────────────────────────────────────────────

function wlEsc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function wlSetStatus(elId, msg, cls) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = msg;
  el.className   = "wl-search-status " + (cls ?? "");
}

function wlUpdateCount() {
  const n     = _wlSelected.size;
  const badge = document.getElementById("wl-count");
  badge.textContent = `${n} / ${WL_MAX} selected`;
  badge.className   = "wl-count-badge" + (n >= WL_MAX ? " wl-count-max" : "");
}

// ── Render chips ──────────────────────────────────────────────────────────────

function wlRenderChips() {
  const container = document.getElementById("wl-chips");
  const all = [..._wlAll, ..._wlExtra.filter(t => !_wlAll.includes(t))];

  container.innerHTML = all.map(ticker => {
    const sel   = _wlSelected.has(ticker);
    const extra = !_wlAll.includes(ticker);
    return `
      <span class="wl-chip ${sel ? "wl-chip-on" : "wl-chip-off"} ${extra ? "wl-chip-extra" : ""}"
            data-ticker="${wlEsc(ticker)}" title="${sel ? "Click to deselect" : "Click to select"}">
        ${wlEsc(ticker)}
        ${extra ? `<button class="wl-chip-remove" data-ticker="${wlEsc(ticker)}" title="Remove">&times;</button>` : ""}
      </span>`;
  }).join("");

  wlUpdateCount();
}

// ── Select / deselect all ─────────────────────────────────────────────────────

function wlSelectAll() {
  const all = [..._wlAll, ..._wlExtra.filter(t => !_wlAll.includes(t))];
  const toAdd = all.slice(0, WL_MAX);
  _wlSelected = new Set(toAdd);
  wlRenderChips();
}

function wlDeselectAll() {
  _wlSelected.clear();
  wlRenderChips();
}

// ── Load from server-injected variable ───────────────────────────────────────

function wlLoad() {
  try {
    _wlAll      = window.__WATCHLIST__ || [];
    _wlSelected = new Set(_wlAll);
    wlRenderChips();
  } catch(e) {
    document.getElementById("wl-chips").textContent = "Failed to load: " + e;
  }
}

// ── Add extra ticker temporarily ──────────────────────────────────────────────

async function wlAddTicker(raw) {
  const ticker = raw.trim().toUpperCase();
  if (!ticker) return;

  if (_wlAll.includes(ticker) || _wlExtra.includes(ticker)) {
    // Already in list — just select it
    if (_wlSelected.size < WL_MAX) _wlSelected.add(ticker);
    wlRenderChips();
    document.getElementById("wl-search").value = "";
    wlSetStatus("wl-search-status", `${ticker} is already in the list — toggled on`, "wl-ok");
    return;
  }
  if (_wlSelected.size >= WL_MAX) {
    wlSetStatus("wl-search-status", `Max ${WL_MAX} tickers selected`, "wl-err");
    return;
  }

  wlSetStatus("wl-search-status", `Validating ${ticker}…`, "wl-loading");
  document.getElementById("wl-add-btn").disabled = true;

  try {
    const res  = await fetch(`/api/ticker-validate?q=${encodeURIComponent(ticker)}`);
    const data = await res.json();
    if (!data.ok) {
      wlSetStatus("wl-search-status", `${ticker}: ${data.error}`, "wl-err");
      return;
    }
    _wlExtra.push(ticker);
    _wlSelected.add(ticker);
    wlRenderChips();
    document.getElementById("wl-search").value = "";
    const nameStr = data.name ? ` — ${data.name}` : "";
    wlSetStatus("wl-search-status", `✓ ${ticker}${nameStr}  $${data.price} added`, "wl-ok");
    // Scroll the new chip into view and re-focus input for quick multi-add
    const newChip = document.querySelector(`.wl-chip[data-ticker="${ticker}"]`);
    if (newChip) newChip.scrollIntoView({ behavior: "smooth", block: "nearest" });
    document.getElementById("wl-search").focus();
  } catch(e) {
    wlSetStatus("wl-search-status", "Error: " + e, "wl-err");
  } finally {
    document.getElementById("wl-add-btn").disabled = false;
  }
}

// ── Expose selected tickers for live.js to include in the API call ────────────

window.wlGetSelected = () => [..._wlSelected];

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  wlLoad();

  // Toggle expand/collapse
  document.getElementById("wl-toggle-btn").addEventListener("click", () => {
    const body = document.getElementById("wl-body");
    const btn  = document.getElementById("wl-toggle-btn");
    const open = body.style.display === "none";
    body.style.display = open ? "" : "none";
    btn.innerHTML = open ? "&#9650;" : "&#9660;";
  });

  // Chip click = toggle selected
  document.getElementById("wl-chips").addEventListener("click", e => {
    // Remove button on extra chips
    const removeBtn = e.target.closest(".wl-chip-remove");
    if (removeBtn) {
      const ticker = removeBtn.dataset.ticker;
      _wlExtra    = _wlExtra.filter(t => t !== ticker);
      _wlSelected.delete(ticker);
      wlRenderChips();
      return;
    }
    // Toggle chip
    const chip = e.target.closest(".wl-chip");
    if (!chip) return;
    const ticker = chip.dataset.ticker;
    if (_wlSelected.has(ticker)) {
      _wlSelected.delete(ticker);
    } else {
      if (_wlSelected.size >= WL_MAX) {
        wlSetStatus("wl-search-status", `Max ${WL_MAX} tickers selected`, "wl-err");
        return;
      }
      _wlSelected.add(ticker);
    }
    wlRenderChips();
    wlSetStatus("wl-search-status", "");
  });

  // Select all / deselect all buttons
  document.getElementById("wl-select-all").addEventListener("click", wlSelectAll);
  document.getElementById("wl-deselect-all").addEventListener("click", wlDeselectAll);

  // Add ticker
  document.getElementById("wl-add-btn").addEventListener("click", () =>
    wlAddTicker(document.getElementById("wl-search").value));

  document.getElementById("wl-search").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); wlAddTicker(e.target.value); }
  });

  document.getElementById("wl-search").addEventListener("input", () =>
    wlSetStatus("wl-search-status", ""));
});
