# Phase 3: Implementation Guide

## Starting Point

You are here:
- ✅ Phase 1: Utilities, config, event-manager
- ✅ Phase 2: 11 modular files (state already in separate files)
- 🚀 Phase 3: Advanced patterns (NOW)

Your current state system:
- live_positions_state.js (115 lines - global state)
- live_state.js (195 lines - global state)

---

## Step 1: Create State Manager Library

### Create `web/static/js/lib/state-manager.js`

```javascript
/**
 * Centralized State Management System
 * Single source of truth for application state
 * Supports subscriptions, history, and time-travel debugging
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
   * @param {Function} callback - Called on state change
   * @param {string} path - Optional path to watch specific slice
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

  // ── Private Helpers ──

  _getByPath(obj, path) {
    return path.split('.').reduce((current, key) => current?.[key], obj);
  }

  _notifyObservers(updates) {
    // Notify all observers
    if (this.observers.has(null)) {
      this.observers.get(null).forEach(callback => {
        try {
          callback(this.state, updates);
        } catch (e) {
          console.error('Observer callback error:', e);
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
            console.error('Observer callback error:', e);
          }
        });
      }
    }
  }

  _pathAffected(path, updates) {
    return path.split('.')[0] in updates;
  }
}

// Create global instance
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

export default StateManager;
```

### Update State Modules

**In `live_positions_state.js`, replace getters/setters with:**

```javascript
/**
 * Get current filter
 */
function getFilter() {
  return window.StateManager?.getState('livePositions.filter') ?? 'all';
}

/**
 * Set filter
 */
function setFilter(filter) {
  if (filter === 'all' || filter === 'options') {
    window.StateManager?.setState({ 
      livePositions: { 
        ...window.StateManager.getState('livePositions'), 
        filter 
      } 
    });
    return true;
  }
  return false;
}

// ... repeat for all getter/setter pairs
```

---

## Step 2: Create Component Base Class

### Create `web/static/js/lib/component-base.js`

```javascript
/**
 * Base Component Class
 * Provides lifecycle hooks and state management integration
 */

class Component {
  constructor(element, props = {}) {
    if (!element) throw new Error('Component requires an element');
    this.element = element;
    this.props = props;
    this.state = {};
    this.subscriptions = [];
    this._mounted = false;
  }

  /**
   * Called when component is first mounted
   * Override in subclasses for setup logic
   */
  onMount() {}

  /**
   * Called before rendering (render prep)
   * Override to prepare render data
   */
  onBeforeRender() {}

  /**
   * Override to return HTML string
   */
  render() {
    return '';
  }

  /**
   * Called after rendering (event binding, etc)
   * Override for post-render setup
   */
  onAfterRender() {}

  /**
   * Called when state changes
   * Override to handle specific state changes
   */
  onStateChange(newState, updates) {}

  /**
   * Called before component unmounts
   * Override for cleanup
   */
  onUnmount() {}

  /**
   * Mount the component
   */
  mount() {
    try {
      this.onMount();
      this._render();
      this._subscribe();
      this._mounted = true;
      console.log(`[Component] Mounted: ${this.constructor.name}`);
    } catch (e) {
      console.error(`[Component] Mount failed: ${this.constructor.name}`, e);
      this._handleError(e);
    }
  }

  /**
   * Update component (re-render)
   */
  update() {
    if (!this._mounted) return;
    try {
      this._render();
      console.log(`[Component] Updated: ${this.constructor.name}`);
    } catch (e) {
      console.error(`[Component] Update failed: ${this.constructor.name}`, e);
      this._handleError(e);
    }
  }

  /**
   * Unmount the component
   */
  unmount() {
    try {
      this.onUnmount();
      this._unsubscribe();
      this.element.innerHTML = '';
      this._mounted = false;
      console.log(`[Component] Unmounted: ${this.constructor.name}`);
    } catch (e) {
      console.error(`[Component] Unmount failed: ${this.constructor.name}`, e);
    }
  }

  /**
   * Determine if component should re-render
   * Override for optimization
   */
  shouldUpdate(prevState, nextState) {
    return true; // Always update by default
  }

  /**
   * Subscribe to state changes
   * Override to watch specific paths
   */
  getStatePaths() {
    return [null]; // Watch all state by default
  }

  // ── Private Methods ──

  _render() {
    this.onBeforeRender();
    const html = this.render();
    this.element.innerHTML = html;
    this.onAfterRender();
  }

  _subscribe() {
    if (typeof window.StateManager === 'undefined') return;

    const paths = this.getStatePaths();
    paths.forEach(path => {
      const unsub = window.StateManager.subscribe((state, updates) => {
        if (this.shouldUpdate(this.state, state)) {
          this.state = JSON.parse(JSON.stringify(state));
          this.onStateChange(state, updates);
          this.update();
        }
      }, path);
      this.subscriptions.push(unsub);
    });
  }

  _unsubscribe() {
    this.subscriptions.forEach(unsub => unsub());
    this.subscriptions = [];
  }

  _handleError(error) {
    this.element.innerHTML = `
      <div class="component-error">
        <p class="error-message">Failed to load component</p>
        <details>
          <summary>Error details</summary>
          <pre>${escapeHtml(error.message)}</pre>
        </details>
      </div>
    `;
  }
}

export default Component;
```

---

## Step 3: Create Cache Manager

### Create `web/static/js/lib/cache-manager.js`

```javascript
/**
 * Cache Manager
 * Handles request deduplication and TTL-based caching
 */

class CacheManager {
  constructor(options = {}) {
    this.cache = new Map();
    this.ttl = options.ttl || 30000; // 30 seconds default
    this.pendingRequests = new Map();
  }

  /**
   * Get or fetch data
   * Deduplicates simultaneous requests
   * Caches results for TTL duration
   */
  async get(key, fetcher) {
    // Return cached data if fresh
    if (this.cache.has(key)) {
      const { data, expiry } = this.cache.get(key);
      if (Date.now() < expiry) {
        console.log(`[Cache] HIT: ${key}`);
        return data;
      }
      this.cache.delete(key);
    }

    // Return pending request if one exists
    if (this.pendingRequests.has(key)) {
      console.log(`[Cache] PENDING: ${key}`);
      return this.pendingRequests.get(key);
    }

    // Fetch and cache
    console.log(`[Cache] MISS: ${key}`);
    const promise = fetcher()
      .then(data => {
        this.cache.set(key, {
          data,
          expiry: Date.now() + this.ttl
        });
        this.pendingRequests.delete(key);
        return data;
      })
      .catch(error => {
        this.pendingRequests.delete(key);
        throw error;
      });

    this.pendingRequests.set(key, promise);
    return promise;
  }

  /**
   * Invalidate cache entry
   */
  invalidate(key) {
    this.cache.delete(key);
    console.log(`[Cache] INVALIDATED: ${key}`);
  }

  /**
   * Clear entire cache
   */
  clear() {
    this.cache.clear();
    this.pendingRequests.clear();
    console.log(`[Cache] CLEARED`);
  }

  /**
   * Get cache stats
   */
  getStats() {
    return {
      size: this.cache.size,
      pendingRequests: this.pendingRequests.size,
      entries: Array.from(this.cache.keys())
    };
  }
}

window.CacheManager = new CacheManager();

export default CacheManager;
```

---

## Step 4: Migrate Existing Code

### Update `live_positions_service.js`

```javascript
/**
 * Wrap API calls with cache
 */
async function fetchTickerAnalysis(ticker) {
  return window.CacheManager.get(
    `analysis:${ticker}`,
    () => _fetchTickerAnalysisImpl(ticker)
  );
}

async function _fetchTickerAnalysisImpl(ticker) {
  // Original implementation here
  // ...
}
```

### Update `live_positions.js`

```javascript
/**
 * Convert to use StateManager
 */
function handleFileClick(filename) {
  // Old: setCurrentFilename(filename)
  // New:
  window.StateManager.setState({
    livePositions: {
      ...window.StateManager.getState('livePositions'),
      filename: filename
    }
  });
}
```

---

## Step 5: Create Monitoring

### Create `web/static/js/lib/performance-monitor.js`

```javascript
class PerformanceMonitor {
  static metrics = new Map();

  /**
   * Start measuring
   */
  static mark(name) {
    performance.mark(`${name}-start`);
  }

  /**
   * Finish measuring and log
   */
  static measure(name) {
    performance.mark(`${name}-end`);
    performance.measure(name, `${name}-start`, `${name}-end`);
    
    const entries = performance.getEntriesByName(name);
    const duration = entries[entries.length - 1].duration;
    
    if (!this.metrics.has(name)) {
      this.metrics.set(name, []);
    }
    this.metrics.get(name).push(duration);
    
    return duration;
  }

  /**
   * Get metrics summary
   */
  static getSummary() {
    const summary = {};
    for (const [name, durations] of this.metrics) {
      summary[name] = {
        count: durations.length,
        avg: (durations.reduce((a, b) => a + b, 0) / durations.length).toFixed(2) + 'ms',
        min: Math.min(...durations).toFixed(2) + 'ms',
        max: Math.max(...durations).toFixed(2) + 'ms'
      };
    }
    return summary;
  }
}

window.PerformanceMonitor = PerformanceMonitor;

export default PerformanceMonitor;
```

---

## Next Steps

1. **Create the 4 library files** above
2. **Update existing state modules** to use StateManager
3. **Update service modules** to use CacheManager
4. **Test in browser console:**

```javascript
// Test StateManager
StateManager.setState({ livePositions: { filter: 'options' } });
console.log(StateManager.getState('livePositions.filter')); // 'options'

// Test CacheManager
CacheManager.get('test:key', () => Promise.resolve('data'));
console.log(CacheManager.getStats());

// Test PerformanceMonitor
PerformanceMonitor.mark('operation');
await someOperation();
const duration = PerformanceMonitor.measure('operation');
console.log(PerformanceMonitor.getSummary());
```

---

## Before/After Comparison

### Before (Phase 2)
```javascript
// Global state scattered
let _lpFilter = "all";
let _lpCurrentGroups = null;

function getFilter() { return _lpFilter; }
function setFilter(f) { _lpFilter = f; }
```

### After (Phase 3)
```javascript
// Centralized state
StateManager.setState({ livePositions: { filter: 'options' } });
const filter = StateManager.getState('livePositions.filter');

// Subscription
StateManager.subscribe((state, updates) => {
  console.log('State changed:', updates);
});
```

---

## 🎯 Success Checklist

- [ ] StateManager working globally
- [ ] All state modules refactored to use StateManager
- [ ] CacheManager reducing API calls
- [ ] Performance improvements measurable
- [ ] No functionality regression
- [ ] Console shows proper logging
- [ ] Browser tests passing

Next: Implement Component lifecycle pattern
