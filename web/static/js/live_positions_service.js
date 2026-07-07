/**
 * Live Positions Service Layer
 * Handles file I/O, API calls, and data fetching
 * Phase 3 Integration: Uses CacheManager for smart caching
 */

// ── File Loading ───────────────────────────────────────────────────────────────

/**
 * Load list of position files from data/live_position/
 * @param {Element} el - DOM element to populate with file list
 */
async function loadLivePositionFiles(el) {
  if (!el) return;

  try {
    const apiUrl = typeof API !== 'undefined' ? API.LIVE_POSITION_FILES : "/api/live-position-files";
    const res = await fetch(apiUrl);
    const data = await res.json();

    if (!data.files || !data.files.length) {
      const emptyMsg = typeof UI !== 'undefined' ? UI.NO_FILES_MESSAGE : "No files found in data/live_position/. Copy a CSV/TSV export there to begin.";
      el.innerHTML = `<p class="lp-empty-message">${emptyMsg}</p>`;
      return;
    }

    const rows = data.files.map(f => {
      const dt = new Date(f.modified * 1000);
      const ago = timeAgo(dt);
      const kb = (f.size / 1024).toFixed(1);
      return `
        <div class="lp-file-row" data-file="${escHtml(f.name)}" title="Click to analyse ${escHtml(f.name)}">
          <span class="lp-file-icon">📄</span>
          <span class="lp-file-name">${escHtml(f.name)}</span>
          <span class="lp-file-meta">${kb} KB</span>
          <span class="lp-file-meta">${ago}</span>
          <span class="lp-file-meta muted">${dt.toLocaleString()}</span>
          <button class="lp-analyse-btn" data-file="${escHtml(f.name)}">Analyse</button>
        </div>`;
    }).join("");

    el.innerHTML = `<div class="lp-file-table">${rows}</div>`;
  } catch (e) {
    console.error("Failed to load files:", e);
    el.innerHTML = `<p class="fail">Failed to load files: ${escHtml(e.message || e)}</p>`;
  }
}

/**
 * Analyze a position file
 * Uses PerformanceMonitor to track execution time
 * @param {string} filename - File to analyze
 * @returns {Promise<Object>} Parsed position data
 */
async function analysePositionFile(filename) {
  // Track performance
  if (typeof window.PerformanceMonitor !== 'undefined') {
    window.PerformanceMonitor.mark(`analyze-file:${filename}`);
  }

  try {
    const apiUrl = typeof API !== 'undefined' ? API.LIVE_POSITION_ANALYZE : "/api/analyze-position-file";
    const res = await fetch(`${apiUrl}?file=${encodeURIComponent(filename)}`);

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }

    const data = await res.json();

    if (data.error) {
      throw new Error(data.error);
    }

    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure(`analyze-file:${filename}`);
    }

    return data;
  } catch (e) {
    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure(`analyze-file:${filename}`);
    }
    console.error(`Failed to analyze ${filename}:`, e);
    throw e;
  }
}

/**
 * Load positions from E*TRADE (admin only)
 * Uses CacheManager for smart caching (60 second TTL)
 * @returns {Promise<Object>} Position data from E*TRADE
 */
async function loadEtradePositions() {
  // Use CacheManager with longer TTL for E*TRADE data (60 seconds)
  if (typeof window.CacheManager !== 'undefined') {
    const cacheKey = 'etrade-positions';
    return window.CacheManager.get(cacheKey, () => _loadEtradePositionsImpl());
  }
  return _loadEtradePositionsImpl();
}

/**
 * Internal implementation - actual API call
 * @private
 */
async function _loadEtradePositionsImpl() {
  try {
    const res = await fetch("/api/etrade-positions");

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }

    const data = await res.json();

    if (data.error) {
      throw new Error(data.error);
    }

    return data;
  } catch (e) {
    console.error("Failed to load E*TRADE positions:", e);
    throw e;
  }
}

// ── Market Analysis Fetching ───────────────────────────────────────────────────
// fetchTickerAnalysis is provided by lib/ticker-analysis.js (shared with
// Paper Trades' per-ticker analysis fetching).

/**
 * Fetch market context data
 * Uses PerformanceMonitor to track execution time
 * @returns {Promise<Object>} Market context data
 */
async function fetchMarketContext() {
  // Track performance
  if (typeof window.PerformanceMonitor !== 'undefined') {
    window.PerformanceMonitor.mark('fetch-market-context');
  }

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

    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure('fetch-market-context');
    }

    return data;
  } catch (e) {
    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure('fetch-market-context');
    }
    console.error("Failed to fetch market context:", e);
    throw e;
  }
}

// ── Helper Functions ───────────────────────────────────────────────────────────

/**
 * Check if API is available
 * @returns {boolean}
 */
function isApiAvailable() {
  return typeof fetch !== 'undefined';
}

/**
 * Format API error message
 * @param {Error} error - Error object
 * @returns {string} Formatted error message
 */
function formatApiError(error) {
  if (error.message.includes("HTTP")) {
    return `Server error: ${error.message}`;
  }
  if (error.message.includes("Failed to fetch")) {
    return "Network error: Unable to reach server";
  }
  return error.message || "Unknown error";
}

console.log('[live_positions_service] Phase 3 Integration Active: CacheManager (ticker, etrade), PerformanceMonitor (analyze, market-context)');
