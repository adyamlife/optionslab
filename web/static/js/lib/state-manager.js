/**
 * Centralized State Management System
 * Single source of truth for application state
 * Supports subscriptions, history, and time-travel debugging
 *
 * Phase 3: Core state management for advanced patterns
 */

class StateManager {
  constructor(initialState = {}) {
    this.state = initialState;
    this.observers = new Map();
    this.history = [JSON.parse(JSON.stringify(initialState))];
    this.historyIndex = 0;
  }

  /**
   * Get current state or a specific slice
   * @param {string} path - Dot notation path (e.g., 'livePositions.filter')
   * @returns {any} State value or entire state
   */
  getState(path) {
    if (!path) return JSON.parse(JSON.stringify(this.state));
    return this._getByPath(this.state, path);
  }

  /**
   * Set state (shallow merge)
   * @param {Object} updates - Partial state updates
   * @returns {Object} Updated state
   */
  setState(updates) {
    const prev = JSON.parse(JSON.stringify(this.state));
    this.state = { ...this.state, ...updates };

    // Add to history
    this.history = this.history.slice(0, this.historyIndex + 1);
    this.history.push(JSON.parse(JSON.stringify(this.state)));
    this.historyIndex++;

    // Notify observers
    this._notifyObservers(updates);

    return this.state;
  }

  /**
   * Subscribe to state changes
   * @param {Function} callback - Called on state change (state, updates) => {}
   * @param {string} path - Optional path to watch specific slice (null = watch all)
   * @returns {Function} Unsubscribe function
   */
  subscribe(callback, path = null) {
    const id = Math.random().toString(36).substr(2, 9);
    if (!this.observers.has(path)) {
      this.observers.set(path, new Map());
    }
    this.observers.get(path).set(id, callback);

    return () => {
      this.observers.get(path).delete(id);
    };
  }

  /**
   * Undo to previous state
   */
  undo() {
    if (this.historyIndex > 0) {
      this.historyIndex--;
      this.state = JSON.parse(JSON.stringify(this.history[this.historyIndex]));
      this._notifyObservers(this.state);
    }
  }

  /**
   * Redo to next state
   */
  redo() {
    if (this.historyIndex < this.history.length - 1) {
      this.historyIndex++;
      this.state = JSON.parse(JSON.stringify(this.history[this.historyIndex]));
      this._notifyObservers(this.state);
    }
  }

  /**
   * Get state history for debugging
   */
  getHistory() {
    return {
      history: this.history,
      currentIndex: this.historyIndex,
      current: this.state
    };
  }

  /**
   * Reset to initial state
   */
  reset(initialState) {
    this.state = JSON.parse(JSON.stringify(initialState));
    this.history = [JSON.parse(JSON.stringify(initialState))];
    this.historyIndex = 0;
    this._notifyObservers(this.state);
  }

  // ── Private Helpers ──

  _getByPath(obj, path) {
    return path.split('.').reduce((current, key) => current?.[key], obj);
  }

  _notifyObservers(updates) {
    // Notify all observers (path = null)
    if (this.observers.has(null)) {
      this.observers.get(null).forEach(callback => {
        try {
          callback(this.state, updates);
        } catch (e) {
          console.error('[StateManager] Observer callback error:', e);
        }
      });
    }

    // Notify path-specific observers
    for (const [path, callbacks] of this.observers) {
      if (path !== null && this._pathAffected(path, updates)) {
        callbacks.forEach(callback => {
          try {
            callback(this._getByPath(this.state, path), updates);
          } catch (e) {
            console.error('[StateManager] Observer callback error:', e);
          }
        });
      }
    }
  }

  _pathAffected(path, updates) {
    return path.split('.')[0] in updates;
  }
}

// Create global instance with initial state
window.StateManager = new StateManager({
  livePositions: {
    filter: 'all',
    groups: null,
    filename: null,
    element: null,
    combinedMode: true,
    action: null
  },
  liveSuggestions: {
    sortKey: 'signal',
    sortDir: -1,
    data: null,
    error: null,
    scanning: false,
    marketContext: null
  }
});

console.log('[StateManager] Initialized with global instance');
