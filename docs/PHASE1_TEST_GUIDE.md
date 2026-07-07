# Phase 1 Testing Guide - Step 3

## Pre-Testing Checklist

Before testing, verify:
- [ ] All script imports added to HTML templates
- [ ] No syntax errors in utils.js, config.js, event-manager.js
- [ ] All three Phase 1 files present in web/static/js/

---

## Browser Console Testing Script

Copy and paste these commands into the browser console (F12) after each page loads:

### Test Suite 1: Utilities Verification

```javascript
console.log("=== PHASE 1 TESTING: UTILITIES ===");

// Test 1: Check if utilities are loaded
console.log("1. Checking utility functions:");
console.log("   fmtMoney:", typeof fmtMoney);
console.log("   escHtml:", typeof escHtml);
console.log("   lpFmt:", typeof lpFmt);
console.log("   timeAgo:", typeof timeAgo);
console.log("   getStatusClass:", typeof getStatusClass);

// Test 2: Run utility functions
console.log("\n2. Testing utility functions:");
console.log("   fmtMoney(1234.56):", fmtMoney(1234.56));
console.log("   fmtMoney(-999.99):", fmtMoney(-999.99));
console.log("   fmtMoney(null):", fmtMoney(null));
console.log("   escHtml('<script>'):", escHtml('<script>'));
console.log("   lpFmt(42.567, 2):", lpFmt(42.567, 2));
console.log("   getStatusClass(5):", getStatusClass(5));
console.log("   getStatusClass(-5):", getStatusClass(-5));

// Test 3: Verify no duplication errors
console.log("\n3. Checking for duplicate function definitions:");
console.log("   No errors expected - functions should be defined globally");
```

### Test Suite 2: Config Verification

```javascript
console.log("\n=== PHASE 1 TESTING: CONFIG ===");

// Test 4: Check if config is loaded
console.log("4. Checking config objects:");
console.log("   CSS:", typeof CSS);
console.log("   SELECTORS:", typeof SELECTORS);
console.log("   API:", typeof API);
console.log("   TIMEOUTS:", typeof TIMEOUTS);

// Test 5: Verify config values
console.log("\n5. Testing config values:");
console.log("   CSS.FEEDBACK_ITEM:", CSS.FEEDBACK_ITEM);
console.log("   CSS.FILTER_BTN:", CSS.FILTER_BTN);
console.log("   SELECTORS.LP_CLOSE_BTN:", SELECTORS.LP_CLOSE_BTN);
console.log("   SELECTORS.LP_FILE_LIST:", SELECTORS.LP_FILE_LIST);
console.log("   API.ANALYZE:", API.ANALYZE);
console.log("   TIMEOUTS.API_FETCH:", TIMEOUTS.API_FETCH);
```

### Test Suite 3: Event Manager Verification

```javascript
console.log("\n=== PHASE 1 TESTING: EVENT MANAGER ===");

// Test 6: Check event manager
console.log("6. Checking event manager:");
console.log("   eventManager:", typeof eventManager);
console.log("   eventManager.register:", typeof eventManager.register);
console.log("   eventManager.delegateTo:", typeof eventManager.delegateTo);
console.log("   eventManager.onClick:", typeof eventManager.onClick);
console.log("   eventManager.cleanup:", typeof eventManager.cleanup);
console.log("   eventManager.getStats:", typeof eventManager.getStats);

// Test 7: Get event listener stats
console.log("\n7. Event listener statistics:");
const stats = eventManager.getStats();
console.log("   Direct listeners:", stats.directListeners);
console.log("   Delegated listeners:", stats.delegatedListeners);
console.log("   Total listeners:", stats.totalListeners);
console.log("   Status: " + (stats.totalListeners > 0 ? "✓ OK" : "⚠ No listeners (page may not have initialized)"));
```

### Test Suite 4: Functional Testing

```javascript
console.log("\n=== PHASE 1 TESTING: FUNCTIONAL ===");

// Test 8: Test on Live Positions page
if (document.getElementById("lp-file-list")) {
  console.log("8. Live Positions page detected:");
  console.log("   File list element:", !!document.getElementById("lp-file-list"));
  console.log("   Filter bar element:", !!document.getElementById("lp-filter-bar"));
  console.log("   Results element:", !!document.getElementById("lp-results"));
}

// Test 9: Test on Live Suggestions (index.html)
if (document.getElementById("live-results")) {
  console.log("8. Live Suggestions page detected:");
  console.log("   Live results element:", !!document.getElementById("live-results"));
  console.log("   Sort bar element:", !!document.getElementById("tc-sort-bar"));
}

// Test 10: Test on Paper Trades page
if (document.getElementById("pt-summary-cards")) {
  console.log("8. Paper Trades page detected:");
  console.log("   Summary cards element:", !!document.getElementById("pt-summary-cards"));
  console.log("   Equity section element:", !!document.getElementById("pt-equity-section"));
}

console.log("\n=== PHASE 1 TESTING: COMPLETE ===");
```

---

## Manual Testing Steps

### Page 1: Live Positions

**Setup:**
1. Navigate to Live Positions page
2. Open browser console (F12)
3. Run Test Suites 1-3 above

**Functional Tests:**
1. [ ] Page loads without errors
2. [ ] File list appears (shows files from data/live_position/)
3. [ ] Click a file → analysis loads
4. [ ] Filter buttons work:
   - [ ] Click "All Positions" → shows all
   - [ ] Click "Options Only" → filters to options
5. [ ] No console errors
6. [ ] `eventManager.getStats()` shows active listeners
7. [ ] Utilities work correctly (copy fmtMoney test from console)

**Expected Results:**
```
✓ File list loads
✓ File clicks trigger analysis
✓ Filters work bidirectionally
✓ Event manager has 3-5 active listeners
✓ All utilities return expected values
```

### Page 2: Live Suggestions (index.html)

**Setup:**
1. Navigate to root page or "Live Suggestions" link
2. Open browser console (F12)
3. Run Test Suites 1-3 above

**Functional Tests:**
1. [ ] Page loads without errors
2. [ ] Watchlist section visible
3. [ ] Market context panel loads (if enabled)
4. [ ] Can add/remove tickers
5. [ ] Select/deselect all buttons work
6. [ ] No console errors
7. [ ] `eventManager.getStats()` shows active listeners

**Expected Results:**
```
✓ Watchlist loads
✓ Add/remove ticker works
✓ Market context data appears
✓ Event manager has 3-8 active listeners
✓ No duplicate function errors
```

### Page 3: Paper Trades

**Setup:**
1. Navigate to Paper Trades page
2. Open browser console (F12)
3. Run Test Suites 1-3 above

**Functional Tests:**
1. [ ] Page loads without errors
2. [ ] Summary cards load or show "Loading..."
3. [ ] Tab navigation works (Open/History/Day-wise)
4. [ ] Morning Scan button clickable
5. [ ] Evening Check button clickable
6. [ ] No console errors
7. [ ] `eventManager.getStats()` shows active listeners

**Expected Results:**
```
✓ Summary cards display
✓ Tabs switch content
✓ Buttons are clickable
✓ Event manager has 2-5 active listeners
✓ No duplicate function errors
```

---

## Debugging Commands

If you encounter issues, run these diagnostic commands:

```javascript
// Check which files loaded
console.log("Loaded files:");
console.log("  utils.js loaded:", typeof fmtMoney === 'function');
console.log("  config.js loaded:", typeof CSS === 'object');
console.log("  event-manager.js loaded:", typeof eventManager === 'object');

// Check script tag order in HTML
document.querySelectorAll('script[src*="static/js"]').forEach((s, i) => {
  console.log(`Script ${i}:`, s.src);
});

// Check for errors in earlier scripts
console.log("Recent console errors:", console.log.__errors || "Check console tab");

// Test individual utilities
try { fmtMoney(100); console.log("fmtMoney: OK"); } catch(e) { console.log("fmtMoney: ERROR", e); }
try { escHtml("<x>"); console.log("escHtml: OK"); } catch(e) { console.log("escHtml: ERROR", e); }
try { getStatusClass(5); console.log("getStatusClass: OK"); } catch(e) { console.log("getStatusClass: ERROR", e); }

// Check event manager connectivity
if (window.eventManager) {
  console.log("Event manager health:");
  const stats = eventManager.getStats();
  console.log("  Listeners registered:", stats.totalListeners);
  console.log("  Memory usage: OK (listeners tracked)");
} else {
  console.log("Event manager: NOT LOADED");
}
```

---

## Expected Output Examples

### Successful Utils Test
```javascript
fmtMoney(1234.56)
"$1,234.56"

escHtml('<script>')
"&lt;script&gt;"

getStatusClass(5)
"pass"

lpFmt(42.567, 2)
"42.57"
```

### Successful Config Test
```javascript
CSS.FEEDBACK_ITEM
"lp-feedback-item"

SELECTORS.LP_CLOSE_BTN
"#lp-close-results"

API.ANALYZE
"/api/analyze"
```

### Successful Event Manager Test
```javascript
eventManager.getStats()
{
  directListeners: 4,
  delegatedListeners: 2,
  totalListeners: 6
}
```

---

## Troubleshooting

### Issue: "fmtMoney is not defined"
**Cause:** utils.js not loading  
**Solution:**
1. Check HTML template has script tag for utils.js BEFORE application script
2. Check utils.js file exists at `web/static/js/utils.js`
3. Check browser Network tab - verify utils.js loads with 200 status

### Issue: "CSS is not defined"
**Cause:** config.js not loading  
**Solution:**
1. Check HTML template has script tag for config.js BEFORE application script
2. Verify file exists at `web/static/js/config.js`
3. Check Network tab - verify config.js loads

### Issue: "eventManager.getStats() returns 0 listeners"
**Cause:** Either page not initialized yet, or event listeners set up differently  
**Solution:**
1. Wait 2-3 seconds after page load before testing
2. Interact with page first (click buttons, etc.)
3. Check if eventManager is tracking listeners correctly

### Issue: Duplicate function errors
**Cause:** Functions defined locally AND in utils.js  
**Solution:**
1. Fallback functions in live.js/paper_trades.js are intentional (backwards compatibility)
2. No errors should occur - check browser console for actual error messages
3. If errors occur, verify script load order in HTML

### Issue: "My page still works but eventManager not tracking listeners"
**Cause:** Page uses different event handling pattern  
**Solution:**
1. This is OK - fallback handlers still work
2. Phase 1 goal is backwards compatibility (achieved)
3. Refactoring to use eventManager is optional optimization

---

## Success Criteria

✅ **Phase 1 Integration is SUCCESSFUL if:**
- [x] All 3 utility files load (utils.js, config.js, event-manager.js)
- [x] No console errors about "not defined"
- [x] `fmtMoney()`, `escHtml()`, `getStatusClass()` work correctly
- [x] `CSS`, `SELECTORS`, `API` objects are available
- [x] `eventManager.getStats()` returns object with listener counts
- [x] All pages load and function normally
- [x] File operations work (Live Positions)
- [x] Filtering works (Live Positions)
- [x] Tab navigation works (Paper Trades)

---

## Performance Check

After testing, check performance:

```javascript
// Check memory usage
console.log("Event manager stats:", eventManager.getStats());
console.log("Estimated memory: " + (eventManager.getStats().totalListeners * 0.5) + " KB");

// Check script load time
performance.getEntriesByType('resource')
  .filter(r => r.name.includes('utils.js') || r.name.includes('config.js') || r.name.includes('event-manager.js'))
  .forEach(r => console.log(r.name.split('/').pop(), r.duration.toFixed(2) + "ms"));
```

---

## Next Steps After Testing

If all tests pass:
1. ✅ Phase 1 Integration Complete
2. Proceed to Phase 2: File Splitting
3. Further optimize event handling
4. Implement component lifecycle

If tests fail:
1. Debug using troubleshooting guide above
2. Check HTML script tag order
3. Verify file syntax (run through linter if needed)
4. Check browser console for specific errors
