# Phase 1 Refactoring - Integration Guide

## Three New Modules Created

### 1. **utils.js** - Consolidated Utility Functions
**Location:** `web/static/js/utils.js`

**Functions exported:**
- `fmtMoney(v, digits)` - Format currency
- `fmtPercent(v, digits)` - Format percentage
- `lpFmt(v, digits)` - Format number with decimals
- `lpPct(v)` - Format percentage with 1 decimal
- `getStatusClass(v)` - Get pass/fail/na class
- `lpCls(v)` - Legacy alias (deprecated)
- `pctCls(v)` - Legacy alias (deprecated)
- `pctStr(v)` - Format percent string
- `escHtml(s)` - HTML escape for XSS prevention
- `timeAgo(dt)` - Format relative time
- `deepClone(obj)` - Deep copy object
- `isEmpty(v)` - Check if empty
- `safeJsonParse(str, fallback)` - Safe JSON parse
- `debounce(fn, delay)` - Debounce function
- `throttle(fn, interval)` - Throttle function
- `waitUntil(condition, maxWait, checkInterval)` - Wait for condition

---

### 2. **config.js** - Centralized Configuration
**Location:** `web/static/js/config.js`

**Exports:**
- `CSS` - All CSS class names
- `SELECTORS` - All DOM selectors
- `EVENTS` - Event type constants
- `API` - API endpoint URLs
- `TIMEOUTS` - Timeout values
- `UI` - UI text constants
- `THRESHOLDS` - Numeric thresholds
- `COLUMN_HELP` - Tooltip text
- `DEFAULTS` - Default settings

---

### 3. **event-manager.js** - Centralized Event Handling
**Location:** `web/static/js/event-manager.js`

**Methods:**
- `register(selector, eventType, handler, options)` - Direct listener
- `registerMultiple(specs)` - Multiple listeners
- `delegateTo(parentSelector, targetSelector, eventType, handler)` - Event delegation
- `onClick(selector, handler)` - Click handler
- `onChange(selector, handler)` - Change handler
- `onInput(selector, handler)` - Input handler
- `onSubmit(selector, handler)` - Form submit
- `cleanup()` - Remove all listeners
- `getStats()` - Debug stats

**Global instance:** `window.eventManager`

---

## Integration Steps

### Step 1: Add Script Tags to HTML

Add to `web/templates/live_positions.html`:
```html
<script src="/static/js/utils.js"></script>
<script src="/static/js/config.js"></script>
<script src="/static/js/event-manager.js"></script>
<script src="/static/js/live_positions.js"></script>
```

Add to `web/templates/live.html`:
```html
<script src="/static/js/utils.js"></script>
<script src="/static/js/config.js"></script>
<script src="/static/js/event-manager.js"></script>
<script src="/static/js/live.js"></script>
```

---

### Step 2: Usage Examples in live_positions.js

#### ❌ OLD CODE:
```javascript
// Duplicate function definition
function fmtMoney(v, digits = 2) { /* ... */ }

// Magic string selectors
document.getElementById("lp-close-results").addEventListener("click", () => { /* ... */ });

// Repeated HTML escape
const safe = String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
```

#### ✅ NEW CODE:
```javascript
// Import utilities
import { fmtMoney, escHtml } from './utils.js';
import { SELECTORS, CSS } from './config.js';

// Use config constants
eventManager.onClick(SELECTORS.LP_CLOSE_BTN, () => { /* ... */ });

// Use utility function
const safe = escHtml(s);
```

---

### Step 3: Before/After Refactoring Examples

#### Example 1: Replace duplicated fmtMoney
**Files affected:** `live_positions.js`, `live.js`, `paper_trades.js`

```javascript
// ❌ BEFORE (in live.js:129)
function fmtMoney(v) {
  if (v == null) return "—";
  const isNeg = v < 0;
  const abs = Math.abs(v).toFixed(2);
  const parts = abs.split(".");
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (isNeg ? "−" : "$") + parts.join(".");
}

// ✅ AFTER
import { fmtMoney } from './utils.js';
// Function is now centralized in utils.js
```

#### Example 2: Replace magic CSS class strings
**Before:**
```javascript
const html = `<div class="lp-feedback-item ${s.type}">`;
```

**After:**
```javascript
import { CSS } from './config.js';
const html = `<div class="${CSS.FEEDBACK_ITEM} ${s.type}">`;
```

#### Example 3: Replace direct event listeners
**Before:**
```javascript
document.getElementById("lp-close-results").addEventListener("click", () => {
  el.innerHTML = "";
});

document.getElementById("lp-filter-btn").addEventListener("click", (e) => {
  const filter = e.target.dataset.filter;
  // ...
});
```

**After:**
```javascript
import { SELECTORS } from './config.js';

// Direct click
eventManager.onClick(SELECTORS.LP_CLOSE_BTN, () => {
  el.innerHTML = "";
});

// Delegated click
eventManager.delegateTo(SELECTORS.LP_FILTER_BAR, '[data-filter]', 'click', (e, btn) => {
  const filter = btn.dataset.filter;
  // ...
});
```

---

## Files to Update

### Priority 1 (Core changes)
- [ ] `web/static/js/live_positions.js` - Remove duplicated functions, import from utils/config
- [ ] `web/static/js/live.js` - Remove duplicated functions, use event manager
- [ ] `web/static/js/paper_trades.js` - Use shared utilities

### Priority 2 (Template changes)
- [ ] `web/templates/live_positions.html` - Add script imports
- [ ] `web/templates/live.html` - Add script imports

### Priority 3 (Cleanup)
- [ ] `web/static/js/common.js` - Remove redundant definitions
- [ ] Remove inline event listeners from HTML

---

## Checklist for Each File Update

When updating `live_positions.js`, `live.js`, etc.:

- [ ] Add import statements at top
- [ ] Remove duplicate utility function definitions
- [ ] Replace `getElementById` + `addEventListener` with `eventManager`
- [ ] Replace magic CSS class strings with `CSS.XXX` constants
- [ ] Replace magic selector strings with `SELECTORS.XXX` constants
- [ ] Replace string HTML escaping with `escHtml()`
- [ ] Update `renderActionModal()` to use event manager
- [ ] Test that all functionality still works

---

## Testing

After refactoring each file:

1. **Browser Console Check:**
   ```javascript
   // Should show all registered listeners
   window.eventManager.getStats()
   ```

2. **Functionality Test:**
   - Load Live Positions - file loading works
   - Filter positions - filters work
   - Click action buttons - modal opens
   - Close modal - modal closes

3. **Memory Leak Check:**
   - Navigate away and back
   - Check `window.eventManager.getStats()` - should reset on page load

---

## Benefits of Phase 1 Refactoring

✅ **DRY Principle** - No more duplicated functions  
✅ **Single Source of Truth** - CSS classes and selectors in one place  
✅ **Easier Maintenance** - Change a selector once, applies everywhere  
✅ **Memory Efficiency** - Centralized event cleanup prevents leaks  
✅ **Better Debugging** - Can inspect all listeners via `eventManager.getStats()`  
✅ **Consistency** - Same patterns used across all pages  

---

## Next Steps (Phase 2 & 3)

After Phase 1 is complete:
- **Phase 2:** Split large files into focused modules
- **Phase 3:** Implement template system, state management
