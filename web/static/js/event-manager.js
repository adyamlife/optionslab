/**
 * Centralized event management system
 * Consolidates event listener registration, delegation, and cleanup
 * Prevents event listener leaks and provides consistent event handling
 */

class EventManager {
  constructor() {
    this.listeners = new Map();  // Track all listeners for cleanup
    this.delegated = new Map();  // Track delegated listeners
  }

  /**
   * Register a direct event listener on a single element
   * @param {string|Element} selector - CSS selector or Element
   * @param {string} eventType - Event type (e.g., 'click', 'change')
   * @param {Function} handler - Event handler function
   * @param {Object} options - Event listener options (e.g., { once: true })
   * @returns {Function} Cleanup function to remove listener
   */
  register(selector, eventType, handler, options = {}) {
    const element = typeof selector === "string"
      ? document.querySelector(selector)
      : selector;

    if (!element) {
      console.warn(`EventManager: Element not found for selector: ${selector}`);
      return () => {};
    }

    const key = `${selector}-${eventType}-${handler.name || 'anonymous'}`;

    element.addEventListener(eventType, handler, options);

    // Store for cleanup
    if (!this.listeners.has(key)) {
      this.listeners.set(key, []);
    }
    this.listeners.get(key).push({ element, eventType, handler, options });

    // Return cleanup function
    return () => this.unregister(selector, eventType, handler);
  }

  /**
   * Register multiple event listeners
   * @param {Array<Object>} specs - Array of { selector, eventType, handler }
   * @returns {Function} Cleanup function for all listeners
   */
  registerMultiple(specs) {
    const cleanupFunctions = specs.map(spec =>
      this.register(spec.selector, spec.eventType, spec.handler, spec.options)
    );

    return () => cleanupFunctions.forEach(fn => fn());
  }

  /**
   * Unregister an event listener
   * @param {string|Element} selector - CSS selector or Element
   * @param {string} eventType - Event type
   * @param {Function} handler - Event handler function
   */
  unregister(selector, eventType, handler) {
    const element = typeof selector === "string"
      ? document.querySelector(selector)
      : selector;

    if (!element) return;

    element.removeEventListener(eventType, handler);

    const key = `${selector}-${eventType}-${handler.name || 'anonymous'}`;
    if (this.listeners.has(key)) {
      this.listeners.delete(key);
    }
  }

  /**
   * Event delegation: Register listener on parent, filter by selector
   * @param {string|Element} parentSelector - Parent element selector
   * @param {string} targetSelector - Target element selector for event filtering
   * @param {string} eventType - Event type
   * @param {Function} handler - Handler function, receives (event, targetElement)
   * @returns {Function} Cleanup function
   */
  delegateTo(parentSelector, targetSelector, eventType, handler) {
    const parent = typeof parentSelector === "string"
      ? document.querySelector(parentSelector)
      : parentSelector;

    if (!parent) {
      console.warn(`EventManager: Parent element not found for selector: ${parentSelector}`);
      return () => {};
    }

    const delegatedHandler = (event) => {
      const target = event.target.closest(targetSelector);
      if (target) {
        handler(event, target);
      }
    };

    const key = `delegated-${parentSelector}-${targetSelector}-${eventType}`;
    if (!this.delegated.has(key)) {
      this.delegated.set(key, []);
    }
    this.delegated.get(key).push({ parent, eventType, delegatedHandler });

    parent.addEventListener(eventType, delegatedHandler);

    return () => this.undelegateFrom(parentSelector, targetSelector, eventType);
  }

  /**
   * Remove delegated event listener
   */
  undelegateFrom(parentSelector, targetSelector, eventType) {
    const key = `delegated-${parentSelector}-${targetSelector}-${eventType}`;
    const listeners = this.delegated.get(key);

    if (listeners) {
      listeners.forEach(({ parent, eventType, delegatedHandler }) => {
        parent.removeEventListener(eventType, delegatedHandler);
      });
      this.delegated.delete(key);
    }
  }

  /**
   * Add click handler to elements
   * @param {string|Element} selector - CSS selector or Element
   * @param {Function} handler - Click handler
   */
  onClick(selector, handler) {
    return this.register(selector, 'click', handler);
  }

  /**
   * Add change handler to form elements
   * @param {string|Element} selector - CSS selector or Element
   * @param {Function} handler - Change handler
   */
  onChange(selector, handler) {
    return this.register(selector, 'change', handler);
  }

  /**
   * Add input handler to form elements
   * @param {string|Element} selector - CSS selector or Element
   * @param {Function} handler - Input handler
   */
  onInput(selector, handler) {
    return this.register(selector, 'input', handler);
  }

  /**
   * Add submit handler to forms
   * @param {string|Element} selector - CSS selector or Element
   * @param {Function} handler - Submit handler
   */
  onSubmit(selector, handler) {
    return this.register(selector, 'submit', (e) => {
      e.preventDefault();
      handler(e);
    });
  }

  /**
   * Clean up all registered listeners
   */
  cleanup() {
    // Cleanup direct listeners
    for (const [key, listeners] of this.listeners) {
      listeners.forEach(({ element, eventType, handler }) => {
        element.removeEventListener(eventType, handler);
      });
    }
    this.listeners.clear();

    // Cleanup delegated listeners
    for (const [key, listeners] of this.delegated) {
      listeners.forEach(({ parent, eventType, delegatedHandler }) => {
        parent.removeEventListener(eventType, delegatedHandler);
      });
    }
    this.delegated.clear();
  }

  /**
   * Get stats about registered listeners (for debugging)
   */
  getStats() {
    return {
      directListeners: this.listeners.size,
      delegatedListeners: this.delegated.size,
      totalListeners: this.listeners.size + this.delegated.size,
    };
  }
}

/**
 * Global instance of EventManager
 * Use this throughout the app for consistent event handling
 */
window.eventManager = new EventManager();

/**
 * Auto-cleanup on page unload
 */
window.addEventListener('beforeunload', () => {
  window.eventManager.cleanup();
});
