/**
 * Live Suggestions Market Context Module
 * Handles market context panel rendering and data fetching
 * Phase 2 Refactoring: Market context isolation
 */

// ── Market Context Rendering ───────────────────────────────────────────────────

/**
 * Render market context panel
 * @param {Object} data - Market context data
 * @returns {string} HTML for market context
 */
function renderMarketContext(data) {
  if (!data) return "";

  const el = document.getElementById("mkt-ctx-body");
  if (!el) return "";

  // Update age label
  const ageEl = document.getElementById("mkt-ctx-age");
  if (ageEl && data.fetched_at) {
    const mins = Math.round((Date.now() / 1000 - data.fetched_at) / 60);
    ageEl.textContent = mins < 1 ? "just fetched" : `fetched ${mins}m ago`;
  }

  // ── Futures row ────────────────────────────────────────────────────────────
  const futuresHtml = (data.futures || []).map((f, idx) => {
    const name = f.label || f.name || f.symbol || `Index ${idx + 1}`;
    const pct = f.change_pct ?? 0;
    const price = f.price ?? 0;
    // Calculate absolute change from percentage
    const chg = price * (pct / 100);
    const chgCls = chg > 0 ? "price-up" : chg < 0 ? "price-dn" : "price-flat";
    const arrow = chg > 0 ? "▲" : chg < 0 ? "▼" : "●";
    const chgStr = `${arrow} ${chg > 0 ? "+" : ""}${chg.toFixed(2)} (${pct > 0 ? "+" : ""}${pct.toFixed(2)}%)`;

    return `
      <div class="mkt-future-chip">
        <span class="mkt-chip-label">${escHtml(name)}</span>
        <span class="mkt-chip-price">${price.toFixed(2)}</span>
        <span class="mkt-chip-pct ${chgCls}">${chgStr}</span>
      </div>
    `;
  }).join("");

  // ── VIX row ────────────────────────────────────────────────────────────────
  const vixHtml = (data.vix != null && typeof data.vix === 'number') ? `
    <div class="mkt-vix-chip">
      <span class="mkt-vix-label">VIX</span>
      <span class="mkt-vix-value">${data.vix.toFixed(2)}</span>
      <span class="mkt-vix-pct">${data.vix > 20 ? "Elevated" : data.vix > 15 ? "Moderate" : "Low"}</span>
    </div>
  ` : "";

  // ── Sector performance ─────────────────────────────────────────────────────
  const sectorHtml = (data.sectors || []).map((s, idx) => {
    const name = s.label || s.name || s.sector || s.symbol || `Sector ${idx + 1}`;
    const chg = s.change_pct || 0;
    const cls = chg > 0 ? "pass" : chg < 0 ? "fail" : "na";
    const barWidth = Math.min(Math.abs(chg) * 5, 100); // Scale for visual width
    return `
      <div class="mkt-sector-row">
        <span class="mkt-sector-label">${escHtml(name)}</span>
        <div class="mkt-sector-bar-wrap">
          <div class="mkt-sector-bar ${cls}" style="width: ${barWidth}%"></div>
        </div>
        <span class="mkt-sector-pct ${cls}">${chg > 0 ? "+" : ""}${chg.toFixed(2)}%</span>
      </div>
    `;
  }).join("");

  // ── Economic indicators ────────────────────────────────────────────────────
  const indicatorsHtml = (data.indicators || []).map(ind => {
    return `
      <div class="mkt-indicator">
        <span class="mkt-indicator-label">${escHtml(ind.name)}</span>
        <span class="mkt-indicator-value">${escHtml(ind.value)}</span>
      </div>
    `;
  }).join("");

  // ── Sentiment ──────────────────────────────────────────────────────────────
  const sentimentHtml = data.market_sentiment ? `
    <div class="mkt-sentiment">
      <span class="mkt-sentiment-label">Market Sentiment</span>
      <span class="mkt-sentiment-value">${escHtml(data.market_sentiment)}</span>
    </div>
  ` : "";

  // Assemble HTML
  const html = `
    <div>
      <h3>Futures & VIX</h3>
      <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;">
        ${futuresHtml}
        ${vixHtml}
      </div>
    </div>

    ${sectorHtml ? `
      <div>
        <h3>Sector Performance</h3>
        <div class="mkt-ctx-sectors">
          ${sectorHtml}
        </div>
      </div>
    ` : ""}

    ${indicatorsHtml ? `
      <div class="mkt-indicators">
        <h3>Economic Indicators</h3>
        ${indicatorsHtml}
      </div>
    ` : ""}

    ${sentimentHtml}
  `;

  el.innerHTML = html;
  return html;
}

// ── Market Context Fetching ───────────────────────────────────────────────────

/**
 * Fetch market context data from API
 * @returns {Promise<Object>} Market context data
 */
async function fetchMarketContext() {
  try {
    const apiUrl = typeof API !== 'undefined' ? API.MARKET_CONTEXT : "/api/market-context";
    const res = await fetch(apiUrl);

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }

    const data = await res.json();

    if (data.error) {
      throw new Error(data.error);
    }

    return data;
  } catch (e) {
    console.error("Failed to fetch market context:", e);
    throw e;
  }
}

/**
 * Load and render market context
 */
async function loadMarketContext() {
  const panel = document.getElementById("mkt-ctx-panel");
  if (!panel) return;

  try {
    // Update state
    if (typeof setMarketContextLoading === "function") {
      setMarketContextLoading(true);
    }

    // Fetch data
    const data = await fetchMarketContext();

    // Store in state
    if (typeof setMarketContext === "function") {
      setMarketContext(data);
    }

    // Render
    renderMarketContext(data);

    console.log("Market context loaded successfully");
  } catch (e) {
    console.error("Market context load failed:", e);
    const el = document.getElementById("mkt-ctx-body");
    if (el) {
      el.innerHTML = `<p class="fail">Failed to load: ${escHtml(e.message || e)}</p>`;
    }
  } finally {
    if (typeof setMarketContextLoading === "function") {
      setMarketContextLoading(false);
    }
  }
}

/**
 * Set up market context auto-refresh
 * @param {number} intervalMs - Refresh interval in milliseconds
 */
function setupMarketContextAutoRefresh(intervalMs = 300000) {
  // Load initially
  loadMarketContext();

  // Refresh periodically
  setInterval(() => {
    loadMarketContext();
  }, intervalMs);
}

/**
 * Set up market context refresh button
 * @param {Element} refreshBtn - Refresh button element
 */
function setupMarketContextRefresh(refreshBtn) {
  if (!refreshBtn) return;

  if (typeof eventManager !== 'undefined') {
    eventManager.onClick(refreshBtn, async () => {
      refreshBtn.disabled = true;
      try {
        await loadMarketContext();
      } finally {
        refreshBtn.disabled = false;
      }
    });
  } else {
    refreshBtn.addEventListener("click", async () => {
      refreshBtn.disabled = true;
      try {
        await loadMarketContext();
      } finally {
        refreshBtn.disabled = false;
      }
    });
  }
}

// ── Helper Wrappers ───────────────────────────────────────────────────────────

// escHtml is provided by utils.js

/**
 * Check if market context feature is enabled
 * @returns {boolean}
 */
function isMarketContextEnabled() {
  return typeof document.getElementById === "function" && !!document.getElementById("mkt-ctx-panel");
}
