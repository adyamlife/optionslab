/**
 * Live Positions — Main Orchestrator
 *
 * Coordinates between Phase 2 modules:
 * - State management (live_positions_state.js)
 * - Service layer (live_positions_service.js)
 * - Analysis (live_positions_analysis.js)
 * - Modal dialogs (live_positions_modal.js)
 * - UI rendering (live_positions_ui.js)
 *
 * Phase 2 Refactoring: Orchestration-only main file
 */

// ── Page Initialization ────────────────────────────────────────────────────────

/**
 * Initialize Live Positions page
 */
function initLivePositions() {
  console.log("Initializing Live Positions page");

  // Add modal to DOM (once per page)
  const modal = document.getElementById("lp-action-modal");
  if (!modal) {
    const modalHtml = buildActionModal();
    document.body.insertAdjacentHTML("beforeend", modalHtml);
    setupModalHandlers();
  }

  // Load file list
  const fileListEl = document.getElementById("lp-file-list");
  if (fileListEl) {
    loadLivePositionFiles(fileListEl);
    setupFileListHandlers(fileListEl);
  }

  // Setup control buttons
  setupControlButtonHandlers();

  console.log("Live Positions page initialized");
}

// ── File List Event Handlers ───────────────────────────────────────────────────

/**
 * Set up event handlers for file list
 * @param {Element} fileListEl - File list container
 */
function setupFileListHandlers(fileListEl) {
  if (typeof eventManager !== 'undefined') {
    // Use eventManager for delegation
    eventManager.delegateTo(fileListEl, ".lp-analyse-btn", "click", (e, btn) => {
      const filename = btn.dataset.file;
      if (filename) {
        handleFileClick(filename);
      }
    });

    // Handle file row click
    eventManager.delegateTo(fileListEl, ".lp-file-row", "click", (e, row) => {
      // Highlight selected
      fileListEl.querySelectorAll(".lp-file-row").forEach(r =>
        r.classList.remove("lp-file-selected")
      );
      row.classList.add("lp-file-selected");
    });
  } else {
    // Fallback without eventManager
    fileListEl.addEventListener("click", (e) => {
      if (e.target.classList.contains("lp-analyse-btn")) {
        const filename = e.target.dataset.file;
        if (filename) handleFileClick(filename);
      }
    });
  }
}

/**
 * Handle file selection and analysis
 * @param {string} filename - File to analyze
 */
async function handleFileClick(filename) {
  const resultsEl = document.getElementById("lp-results");
  if (!resultsEl) return;

  // Show loading state
  resultsEl.innerHTML = `<p class="muted">Analysing ${escHtml(filename)}…</p>`;

  try {
    // Analyze file
    const data = await analysePositionFile(filename);

    // Update state
    setCurrentFilename(filename);
    setCurrentGroups(data.groups);
    setCurrentElement(resultsEl);

    // Show filter bar and refresh button now that data is loaded
    const filterBar = document.getElementById("lp-filter-bar");
    if (filterBar) filterBar.classList.remove("is-hidden");
    const refreshBtn = document.getElementById("lp-refresh-btn");
    if (refreshBtn) refreshBtn.classList.remove("is-hidden");

    // Render results
    renderPositionResults(data.groups, filename, resultsEl);

    console.log("Analysis complete:", filename);
  } catch (e) {
    console.error("Analysis failed:", e);
    resultsEl.innerHTML = `<p class="fail">Failed to analyse: ${escHtml(e.message || e)}</p>`;
  }
}

// ── Control Button Handlers ────────────────────────────────────────────────────

/**
 * Set up event handlers for control buttons
 */
function setupControlButtonHandlers() {
  if (typeof eventManager !== 'undefined') {
    // Close results button
    eventManager.onClick("#lp-close-results", () => {
      const el = document.getElementById("lp-results");
      if (el) el.innerHTML = "";

      // Reset state
      resetViewState();

      // Hide filter bar and refresh button until data is loaded again
      const filterBar = document.getElementById("lp-filter-bar");
      if (filterBar) filterBar.classList.add("is-hidden");
      const refreshBtn = document.getElementById("lp-refresh-btn");
      if (refreshBtn) refreshBtn.classList.add("is-hidden");

      // Deselect files
      document.querySelectorAll(".lp-file-row").forEach(r =>
        r.classList.remove("lp-file-selected")
      );
    });

    // View mode buttons
    eventManager.onClick("#lp-view-combined", () => {
      setCombinedMode(true);
      const groups = getCurrentGroups();
      const el = getCurrentElement();
      if (groups && el) {
        renderPositionResults(groups, getCurrentFilename(), el);
      }
    });

    eventManager.onClick("#lp-view-individual", () => {
      setCombinedMode(false);
      const groups = getCurrentGroups();
      const el = getCurrentElement();
      if (groups && el) {
        renderPositionResults(groups, getCurrentFilename(), el);
      }
    });

    // Filter buttons
    eventManager.delegateTo("#lp-filter-bar", ".lp-filter-btn", "click", (e, btn) => {
      const filterVal = btn.dataset.filter;
      setFilter(filterVal);

      // Update button states
      document.querySelectorAll(".lp-filter-btn").forEach(b => {
        b.classList.toggle("lp-filter-active", b.dataset.filter === filterVal);
      });

      // Re-render with new filter
      const groups = getCurrentGroups();
      const el = getCurrentElement();
      if (groups && el) {
        renderPositionResults(groups, getCurrentFilename(), el);
      }
    });

    // E*TRADE load button
    eventManager.onClick("#lp-etrade-btn", async () => {
      await handleEtradeLoad();
    });

    // Refresh file list button
    eventManager.onClick("#lp-refresh-btn", async () => {
      const fileListEl = document.getElementById("lp-file-list");
      if (fileListEl) {
        loadLivePositionFiles(fileListEl);
        setupFileListHandlers(fileListEl);
      }
    });
  } else {
    // Fallback without eventManager
    setupControlButtonHandlersFallback();
  }
}

/**
 * Fallback control handler setup (if eventManager not available)
 */
function setupControlButtonHandlersFallback() {
  const closeBtn = document.getElementById("lp-close-results");
  const viewCombined = document.getElementById("lp-view-combined");
  const viewIndividual = document.getElementById("lp-view-individual");
  const refreshBtn = document.getElementById("lp-refresh-btn");
  const etradeBtn = document.getElementById("lp-etrade-btn");

  closeBtn?.addEventListener("click", () => {
    const el = document.getElementById("lp-results");
    if (el) el.innerHTML = "";
    resetViewState();
  });

  viewCombined?.addEventListener("click", () => {
    setCombinedMode(true);
    const groups = getCurrentGroups();
    const el = getCurrentElement();
    if (groups && el) {
      renderPositionResults(groups, getCurrentFilename(), el);
    }
  });

  viewIndividual?.addEventListener("click", () => {
    setCombinedMode(false);
    const groups = getCurrentGroups();
    const el = getCurrentElement();
    if (groups && el) {
      renderPositionResults(groups, getCurrentFilename(), el);
    }
  });

  refreshBtn?.addEventListener("click", async () => {
    const fileListEl = document.getElementById("lp-file-list");
    if (fileListEl) {
      loadLivePositionFiles(fileListEl);
    }
  });

  etradeBtn?.addEventListener("click", async () => {
    await handleEtradeLoad();
  });
}

// ── E*TRADE Integration ────────────────────────────────────────────────────────

/**
 * Handle E*TRADE position loading
 */
async function handleEtradeLoad() {
  const btn = document.getElementById("lp-etrade-btn");
  const status = document.getElementById("lp-etrade-status");

  if (!btn || !status) return;

  btn.disabled = true;
  status.textContent = "Loading positions from E*TRADE…";
  status.className = "muted";

  try {
    const data = await loadEtradePositions();

    // Update state
    setCurrentGroups(data.groups);
    setCurrentFilename("E*TRADE (live)");

    // Render results
    const resultsEl = document.getElementById("lp-results");
    if (resultsEl) {
      setCurrentElement(resultsEl);

      const filterBar = document.getElementById("lp-filter-bar");
      if (filterBar) filterBar.classList.remove("is-hidden");
      const refreshBtn = document.getElementById("lp-refresh-btn");
      if (refreshBtn) refreshBtn.classList.remove("is-hidden");

      renderPositionResults(data.groups, "E*TRADE (live)", resultsEl);
    }

    status.textContent = "✓ Loaded successfully";
    status.className = "pass";
  } catch (e) {
    console.error("E*TRADE load failed:", e);
    status.textContent = `Error: ${e.message || e}`;
    status.className = "fail";
  } finally {
    btn.disabled = false;
  }
}

// ── Page Lifecycle ────────────────────────────────────────────────────────────

/**
 * Initialize on page load
 */
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initLivePositions);
} else {
  initLivePositions();
}

/**
 * Cleanup on page unload
 */
window.addEventListener('beforeunload', () => {
  if (typeof eventManager !== 'undefined') {
    eventManager.cleanup();
  }
  resetState();
});
