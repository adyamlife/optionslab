/**
 * Performance Monitor
 * Tracks and measures performance metrics
 *
 * Phase 3: Monitoring and observability
 */

class PerformanceMonitor {
  static metrics = new Map();
  static marks = new Map();

  /**
   * Start measuring (mark a point in time)
   */
  static mark(name) {
    performance.mark(`${name}-start`);
    this.marks.set(name, Date.now());
  }

  /**
   * Finish measuring and log duration
   */
  static measure(name) {
    performance.mark(`${name}-end`);

    try {
      performance.measure(name, `${name}-start`, `${name}-end`);
    } catch (e) {
      console.warn(`[PerformanceMonitor] Could not measure ${name}:`, e.message);
    }

    const entries = performance.getEntriesByName(name);
    let duration = 0;

    if (entries.length > 0) {
      duration = entries[entries.length - 1].duration;
    } else if (this.marks.has(name)) {
      duration = Date.now() - this.marks.get(name);
    }

    if (!this.metrics.has(name)) {
      this.metrics.set(name, []);
    }
    this.metrics.get(name).push(duration);

    console.log(`[PerformanceMonitor] ${name}: ${duration.toFixed(2)}ms`);

    this.marks.delete(name);
    return duration;
  }

  /**
   * Get metrics summary for a specific operation
   */
  static getMetric(name) {
    if (!this.metrics.has(name)) return null;

    const durations = this.metrics.get(name);
    const sum = durations.reduce((a, b) => a + b, 0);

    return {
      name,
      count: durations.length,
      avg: (sum / durations.length).toFixed(2) + 'ms',
      min: Math.min(...durations).toFixed(2) + 'ms',
      max: Math.max(...durations).toFixed(2) + 'ms',
      total: sum.toFixed(2) + 'ms'
    };
  }

  /**
   * Get metrics summary for all operations
   */
  static getSummary() {
    const summary = {};
    for (const [name, durations] of this.metrics) {
      const sum = durations.reduce((a, b) => a + b, 0);
      summary[name] = {
        count: durations.length,
        avg: (sum / durations.length).toFixed(2) + 'ms',
        min: Math.min(...durations).toFixed(2) + 'ms',
        max: Math.max(...durations).toFixed(2) + 'ms',
        total: sum.toFixed(2) + 'ms'
      };
    }
    return summary;
  }

  /**
   * Clear all metrics
   */
  static clear() {
    this.metrics.clear();
    this.marks.clear();
    console.log('[PerformanceMonitor] Cleared all metrics');
  }

  /**
   * Print formatted summary to console
   */
  static printSummary() {
    console.group('[PerformanceMonitor] Summary');
    const summary = this.getSummary();
    for (const [name, stats] of Object.entries(summary)) {
      console.log(`${name}:`, stats);
    }
    console.groupEnd();
  }

  /**
   * Get all collected metrics
   */
  static getAllMetrics() {
    return {
      metrics: this.metrics,
      summary: this.getSummary(),
      operationCount: this.metrics.size
    };
  }

  /**
   * Measure async operation
   */
  static async measureAsync(name, asyncFn) {
    this.mark(name);
    try {
      const result = await asyncFn();
      this.measure(name);
      return result;
    } catch (e) {
      this.measure(name);
      throw e;
    }
  }
}

window.PerformanceMonitor = PerformanceMonitor;

console.log('[PerformanceMonitor] Initialized with global instance');
