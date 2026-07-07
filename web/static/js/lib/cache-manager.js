/**
 * Cache Manager
 * Handles request deduplication and TTL-based caching
 *
 * Phase 3: Performance optimization through smart caching
 */

class CacheManager {
  constructor(options = {}) {
    this.cache = new Map();
    this.ttl = options.ttl || 30000; // 30 seconds default
    this.pendingRequests = new Map();
    this.stats = {
      hits: 0,
      misses: 0,
      pending: 0
    };
  }

  /**
   * Get or fetch data
   * Deduplicates simultaneous requests
   * Caches results for TTL duration
   *
   * @param {string} key - Cache key
   * @param {Function} fetcher - Async function that returns data
   * @returns {Promise} Resolved data
   */
  async get(key, fetcher) {
    // Return cached data if fresh
    if (this.cache.has(key)) {
      const { data, expiry } = this.cache.get(key);
      if (Date.now() < expiry) {
        this.stats.hits++;
        console.log(`[Cache] HIT: ${key}`);
        return data;
      }
      this.cache.delete(key);
    }

    // Return pending request if one exists (deduplication)
    if (this.pendingRequests.has(key)) {
      this.stats.pending++;
      console.log(`[Cache] PENDING: ${key}`);
      return this.pendingRequests.get(key);
    }

    // Fetch and cache
    this.stats.misses++;
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
   * Invalidate by pattern (e.g., 'analysis:*')
   */
  invalidatePattern(pattern) {
    const regex = new RegExp('^' + pattern.replace('*', '.*') + '$');
    let count = 0;
    for (const key of this.cache.keys()) {
      if (regex.test(key)) {
        this.cache.delete(key);
        count++;
      }
    }
    console.log(`[Cache] INVALIDATED ${count} entries matching ${pattern}`);
  }

  /**
   * Clear entire cache
   */
  clear() {
    this.cache.clear();
    this.pendingRequests.clear();
    console.log('[Cache] CLEARED');
  }

  /**
   * Get cache statistics
   */
  getStats() {
    const total = this.stats.hits + this.stats.misses;
    const hitRate = total > 0 ? ((this.stats.hits / total) * 100).toFixed(1) : 0;

    return {
      size: this.cache.size,
      pendingRequests: this.pendingRequests.size,
      hits: this.stats.hits,
      misses: this.stats.misses,
      hitRate: `${hitRate}%`,
      entries: Array.from(this.cache.keys()),
      ttl: this.ttl
    };
  }

  /**
   * Reset statistics
   */
  resetStats() {
    this.stats = { hits: 0, misses: 0, pending: 0 };
  }
}

// Create global instance
window.CacheManager = new CacheManager();

console.log('[CacheManager] Initialized with global instance (30s TTL)');
