/**
 * Shared utility functions across Live Positions, Live Suggestions, and Paper Trades
 * Consolidates duplicated utility functions for DRY principle
 */

/**
 * Format currency value to $X,XXX.XX format
 */
function fmtMoney(v, digits = 2) {
  if (v == null) return "—";
  const isNeg = v < 0;
  const abs = Math.abs(v).toFixed(digits);
  const parts = abs.split(".");
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (isNeg ? "−" : "$") + parts.join(".");
}

/**
 * Format percentage value
 */
function fmtPercent(v, digits = 1) {
  return v != null ? v.toFixed(digits) + "%" : "—";
}

/**
 * Format number with specified decimal places
 * @param {number} v - Value to format
 * @param {number} digits - Decimal places
 * @returns {string} Formatted value or "—" if null
 */
function lpFmt(v, digits = 2) {
  if (v == null) return "—";
  return v.toFixed ? v.toFixed(digits) : String(v);
}

/**
 * Format percentage with 1 decimal
 */
function lpPct(v) {
  return v != null ? v.toFixed(1) + "%" : "—";
}

/**
 * Get CSS status class based on numeric value
 * @param {number} v - Value to classify
 * @returns {string} CSS class: 'pass' (>0), 'fail' (<0), 'na' (null/0)
 */
function getStatusClass(v) {
  return v == null ? "na" : v > 0 ? "pass" : v < 0 ? "fail" : "na";
}

/**
 * Legacy alias for getStatusClass (deprecated - use getStatusClass)
 */
function lpCls(v) {
  return getStatusClass(v);
}

/**
 * Legacy alias for getStatusClass (deprecated - use getStatusClass)
 */
function pctCls(v) {
  return getStatusClass(v);
}

/**
 * Format percentage string
 */
function pctStr(v) {
  if (v == null) return "—";
  const str = typeof v === "number" ? v.toFixed(2) : String(v);
  return str + "%";
}

/**
 * HTML escape string to prevent XSS
 */
function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Format timestamp as relative time (e.g., "2 hours ago")
 */
function timeAgo(dt) {
  if (!dt) return "—";
  const now = new Date();
  const then = typeof dt === "string" ? new Date(dt) : dt;
  const diffMs = now - then;
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;

  return then.toLocaleDateString();
}

/**
 * Deep clone an object (safe for JSON-serializable data)
 */
function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

/**
 * Check if value is null, undefined, or empty string
 */
function isEmpty(v) {
  return v == null || v === "";
}

/**
 * Safe JSON parse with fallback
 */
function safeJsonParse(str, fallback = {}) {
  try {
    return JSON.parse(str);
  } catch (e) {
    console.warn("JSON parse error:", e);
    return fallback;
  }
}

/**
 * Debounce function execution
 */
function debounce(fn, delay = 300) {
  let timeoutId = null;
  return function (...args) {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(() => fn(...args), delay);
  };
}

/**
 * Throttle function execution
 */
function throttle(fn, interval = 300) {
  let lastCall = 0;
  return function (...args) {
    const now = Date.now();
    if (now - lastCall >= interval) {
      lastCall = now;
      fn(...args);
    }
  };
}

/**
 * Wait for a condition to be true
 */
async function waitUntil(condition, maxWait = 5000, checkInterval = 100) {
  const startTime = Date.now();
  while (!condition()) {
    if (Date.now() - startTime > maxWait) {
      throw new Error("Timeout waiting for condition");
    }
    await new Promise(resolve => setTimeout(resolve, checkInterval));
  }
}
