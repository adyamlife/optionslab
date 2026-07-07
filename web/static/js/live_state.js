/**
 * Live Suggestions State Management
 * Phase 3 Integration: Delegates to centralized StateManager
 * Maintains backwards compatibility with Phase 2 interface
 */

// ── Sort State Getters (Delegate to StateManager) ────────────────────────────────

/**
 * Get current sort key
 * @returns {string} Sort column name
 */
function getSortKey() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('liveSuggestions.sortKey') ?? 'signal'
    : 'signal';
}

/**
 * Get current sort direction
 * @returns {number} -1 for desc, 1 for asc
 */
function getSortDirection() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('liveSuggestions.sortDir') ?? -1
    : -1;
}

/**
 * Get sort state as object
 * @returns {Object} { key, direction }
 */
function getSortState() {
  return { key: getSortKey(), direction: getSortDirection() };
}

// ── Sort State Setters (Delegate to StateManager) ────────────────────────────────

/**
 * Set sort column
 * @param {string} key - Column name
 */
function setSortKey(key) {
  const validKeys = ['signal', 'ev', 'profit', 'pop', 'capital', 'ticker', 'unusual', 'bias'];
  if (!validKeys.includes(key)) {
    console.warn('Invalid sort key:', key);
    return false;
  }

  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, sortKey: key }
    });
  }
  return true;
}

/**
 * Toggle sort direction
 */
function toggleSortDirection() {
  const dir = getSortDirection();
  const newDir = dir === -1 ? 1 : -1;
  setSortDirection(newDir);
  return newDir;
}

/**
 * Set sort direction
 * @param {number} dir - -1 or 1
 */
function setSortDirection(dir) {
  if (dir !== -1 && dir !== 1) return false;

  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, sortDir: dir }
    });
  }
  return true;
}

/**
 * Update sort state
 * @param {string} key - Sort column
 * @param {number} dir - Sort direction
 */
function setSortState(key, dir) {
  const keyOk = setSortKey(key);
  const dirOk = setSortDirection(dir);
  return keyOk && dirOk;
}

// ── Data State Getters (Delegate to StateManager) ───────────────────────────────

/**
 * Get current live analysis data
 * @returns {Object|null} Analysis results
 */
function getLiveData() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('liveSuggestions.data')
    : null;
}

/**
 * Get live data rows
 * @returns {Array} Array of ticker analysis objects
 */
function getLiveRows() {
  const data = getLiveData();
  return data?.rows || [];
}

/**
 * Get live data grouped by status
 * @returns {Object} Grouped results
 */
function getLiveGrouped() {
  const data = getLiveData();
  return data?.grouped || {};
}

/**
 * Get last error
 * @returns {string|null} Error message
 */
function getLiveError() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('liveSuggestions.error')
    : null;
}

/**
 * Check if scan in progress
 * @returns {boolean}
 */
function isScanning() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('liveSuggestions.scanning') ?? false
    : false;
}

// ── Data State Setters (Delegate to StateManager) ───────────────────────────────

/**
 * Set live analysis data
 * @param {Object} data - Analysis results
 */
function setLiveData(data) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, data, error: null }
    });
  }
}

/**
 * Set error state
 * @param {string|Error} error - Error message or Error object
 */
function setLiveError(error) {
  const msg = typeof error === 'string' ? error : error.message || String(error);
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, error: msg, data: null }
    });
  }
}

/**
 * Set scanning flag
 * @param {boolean} scanning - Is scanning
 */
function setScanning(scanning) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, scanning }
    });
  }
}

/**
 * Clear live data
 */
function clearLiveData() {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, data: null, error: null, scanning: false }
    });
  }
}

// ── Market Context State (Delegate to StateManager) ────────────────────────────

/**
 * Get market context
 * @returns {Object|null}
 */
function getMarketContext() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('liveSuggestions.marketContext')
    : null;
}

/**
 * Set market context
 * @param {Object} data - Market context data
 */
function setMarketContext(data) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, marketContext: data }
    });
  }
}

/**
 * Check if market context loading
 * @returns {boolean}
 */
function isMarketContextLoading() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('liveSuggestions.marketContextLoading') ?? false
    : false;
}

/**
 * Set market context loading
 * @param {boolean} loading
 */
function setMarketContextLoading(loading) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('liveSuggestions');
    window.StateManager.setState({
      liveSuggestions: { ...current, marketContextLoading: loading }
    });
  }
}

// ── Batch State Update ─────────────────────────────────────────────────────────

/**
 * Update multiple state variables
 * @param {Object} updates - Updates object
 */
function updateLiveState(updates) {
  const result = {};

  if (updates.sortKey) {
    result.sortKey = setSortKey(updates.sortKey);
  }
  if (typeof updates.sortDir !== 'undefined') {
    result.sortDir = setSortDirection(updates.sortDir);
  }
  if (updates.data) {
    setLiveData(updates.data);
    result.data = true;
  }
  if (updates.error) {
    setLiveError(updates.error);
    result.error = true;
  }
  if (typeof updates.scanning !== 'undefined') {
    setScanning(updates.scanning);
    result.scanning = true;
  }

  return result;
}

// ── Debug Helpers ──────────────────────────────────────────────────────────────

/**
 * Get full state for debugging
 * @returns {Object}
 */
function getFullLiveState() {
  if (typeof window.StateManager !== 'undefined') {
    return window.StateManager.getState('liveSuggestions');
  }
  return null;
}

/**
 * Log state to console
 */
function logLiveState() {
  console.log('Live Suggestions State:', getFullLiveState());
}

console.log('[live_state] Integrated with Phase 3 StateManager');
