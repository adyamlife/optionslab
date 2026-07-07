/**
 * Live Positions State Management
 * Phase 3 Integration: Delegates to centralized StateManager
 * Maintains backwards compatibility with Phase 2 interface
 */

// ── State Getters (Delegate to StateManager) ────────────────────────────────────

/**
 * Get current filter setting
 */
function getFilter() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('livePositions.filter') ?? 'all'
    : 'all';
}

/**
 * Get current position groups
 */
function getCurrentGroups() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('livePositions.groups')
    : null;
}

/**
 * Get current filename
 */
function getCurrentFilename() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('livePositions.filename')
    : null;
}

/**
 * Get results DOM element
 */
function getCurrentElement() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('livePositions.element')
    : null;
}

/**
 * Get view mode
 */
function isCombinedMode() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('livePositions.combinedMode') ?? true
    : true;
}

/**
 * Get current action state
 */
function getCurrentAction() {
  return typeof window.StateManager !== 'undefined'
    ? window.StateManager.getState('livePositions.action')
    : null;
}

// ── State Setters (Delegate to StateManager) ────────────────────────────────────

/**
 * Update filter setting
 */
function setFilter(filter) {
  if (filter !== 'all' && filter !== 'options') {
    console.warn('Invalid filter:', filter);
    return false;
  }

  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('livePositions');
    window.StateManager.setState({
      livePositions: { ...current, filter }
    });
  }
  return true;
}

/**
 * Update position groups
 */
function setCurrentGroups(groups) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('livePositions');
    window.StateManager.setState({
      livePositions: { ...current, groups }
    });
  }
}

/**
 * Update filename
 */
function setCurrentFilename(filename) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('livePositions');
    window.StateManager.setState({
      livePositions: { ...current, filename }
    });
  }
}

/**
 * Update results element
 */
function setCurrentElement(el) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('livePositions');
    window.StateManager.setState({
      livePositions: { ...current, element: el }
    });
  }
}

/**
 * Update view mode
 */
function setCombinedMode(combined) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('livePositions');
    window.StateManager.setState({
      livePositions: { ...current, combinedMode: combined }
    });
  }
}

/**
 * Update action state
 */
function setCurrentAction(action) {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('livePositions');
    window.StateManager.setState({
      livePositions: { ...current, action }
    });
  }
}

// ── State Reset ────────────────────────────────────────────────────────────────

/**
 * Reset all state to initial values
 */
function resetState() {
  if (typeof window.StateManager !== 'undefined') {
    window.StateManager.setState({
      livePositions: {
        filter: 'all',
        groups: null,
        filename: null,
        element: null,
        combinedMode: true,
        action: null
      }
    });
  }
}

/**
 * Reset to default view state but keep filter
 */
function resetViewState() {
  if (typeof window.StateManager !== 'undefined') {
    const current = window.StateManager.getState('livePositions');
    window.StateManager.setState({
      livePositions: {
        ...current,
        groups: null,
        filename: null,
        element: null,
        combinedMode: true,
        action: null
      }
    });
  }
}

// ── State Batch Update ─────────────────────────────────────────────────────────

/**
 * Update multiple state variables at once
 */
function updateState(updates) {
  const result = {};

  if (typeof updates.filter !== 'undefined') {
    result.filter = setFilter(updates.filter);
  }
  if (typeof updates.groups !== 'undefined') {
    setCurrentGroups(updates.groups);
    result.groups = true;
  }
  if (typeof updates.filename !== 'undefined') {
    setCurrentFilename(updates.filename);
    result.filename = true;
  }
  if (typeof updates.element !== 'undefined') {
    setCurrentElement(updates.element);
    result.element = true;
  }
  if (typeof updates.combinedMode !== 'undefined') {
    setCombinedMode(updates.combinedMode);
    result.combinedMode = true;
  }
  if (typeof updates.action !== 'undefined') {
    setCurrentAction(updates.action);
    result.action = true;
  }

  return result;
}

// ── Helper Functions ───────────────────────────────────────────────────────────

/**
 * Get all state as object (for debugging)
 */
function getFullState() {
  if (typeof window.StateManager !== 'undefined') {
    return window.StateManager.getState('livePositions');
  }
  return null;
}

/**
 * Log state for debugging
 */
function logState() {
  console.log('Live Positions State:', getFullState());
}

console.log('[live_positions_state] Integrated with Phase 3 StateManager');
