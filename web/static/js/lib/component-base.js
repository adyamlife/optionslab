/**
 * Base Component Class
 * Provides lifecycle hooks and state management integration
 *
 * Phase 3: Foundation for component-based architecture
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
   * Override for optimization (e.g., shouldUpdate pattern)
   */
  shouldUpdate(prevState, nextState) {
    return true; // Always update by default
  }

  /**
   * Subscribe to state changes
   * Override to watch specific paths (e.g., ['livePositions', 'liveSuggestions.data'])
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
    if (typeof window.StateManager === 'undefined') {
      console.warn(`[Component] StateManager not available: ${this.constructor.name}`);
      return;
    }

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
    const errorHtml = `
      <div class="component-error" style="padding: 1rem; background: #fee; border: 1px solid #f99; border-radius: 4px;">
        <p style="color: #c00; font-weight: bold; margin: 0 0 0.5rem 0;">Failed to load component</p>
        <details style="font-size: 0.85rem; color: #666;">
          <summary style="cursor: pointer;">Error details</summary>
          <pre style="margin-top: 0.5rem; overflow: auto; background: #f5f5f5; padding: 0.5rem; border-radius: 2px;">${this._escapeHtml(error.message || String(error))}</pre>
        </details>
      </div>
    `;
    this.element.innerHTML = errorHtml;
  }

  _escapeHtml(text) {
    const map = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    };
    return String(text).replace(/[&<>"']/g, m => map[m]);
  }
}

window.Component = Component;

console.log('[Component] Base class loaded');
