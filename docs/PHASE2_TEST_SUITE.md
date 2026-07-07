# Phase 2 Testing Suite

## Browser Console Tests

Copy and paste each test section into the browser console (F12) after loading each page.

---

## TEST 1: Live Positions Page (`/positions`)

### Module Load Verification
```javascript
console.log("=== PHASE 2 TEST: LIVE POSITIONS ===\n");

// Test Phase 1 utilities
console.log("Phase 1 Utilities:");
console.log("  fmtMoney:", typeof fmtMoney === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  escHtml:", typeof escHtml === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  lpFmt:", typeof lpFmt === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  getStatusClass:", typeof getStatusClass === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 1 config
console.log("\nPhase 1 Config:");
console.log("  CSS object:", typeof CSS === 'object' ? "✅ LOADED" : "❌ MISSING");
console.log("  SELECTORS object:", typeof SELECTORS === 'object' ? "✅ LOADED" : "❌ MISSING");
console.log("  API object:", typeof API === 'object' ? "✅ LOADED" : "❌ MISSING");

// Test EventManager
console.log("\nPhase 1 EventManager:");
console.log("  eventManager:", typeof eventManager === 'object' ? "✅ LOADED" : "❌ MISSING");
if (typeof eventManager === 'object') {
  const stats = eventManager.getStats();
  console.log("  - Stats:", stats);
}

// Test Phase 2.1 State
console.log("\nPhase 2.1 State Module:");
console.log("  getFilter:", typeof getFilter === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  setFilter:", typeof setFilter === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  getCurrentGroups:", typeof getCurrentGroups === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  setCurrentGroups:", typeof setCurrentGroups === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 2.1 Service
console.log("\nPhase 2.1 Service Module:");
console.log("  loadLivePositionFiles:", typeof loadLivePositionFiles === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  analysePositionFile:", typeof analysePositionFile === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  fetchTickerAnalysis:", typeof fetchTickerAnalysis === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 2.1 Analysis
console.log("\nPhase 2.1 Analysis Module:");
console.log("  isOptionPosition:", typeof isOptionPosition === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  buildPositionMarketSignals:", typeof buildPositionMarketSignals === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  buildPositionFeedback:", typeof buildPositionFeedback === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 2.1 Modal
console.log("\nPhase 2.1 Modal Module:");
console.log("  buildActionModal:", typeof buildActionModal === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  openActionModal:", typeof openActionModal === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  setupModalHandlers:", typeof setupModalHandlers === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 2.1 UI
console.log("\nPhase 2.1 UI Module:");
console.log("  renderSpreadLP:", typeof renderSpreadLP === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  renderPositionResults:", typeof renderPositionResults === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  renderGroupCombined:", typeof renderGroupCombined === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test functionality
console.log("\n=== FUNCTIONALITY TEST ===");
console.log("File list element:", document.getElementById("lp-file-list") ? "✅ FOUND" : "❌ NOT FOUND");
console.log("Filter bar element:", document.getElementById("lp-filter-bar") ? "✅ FOUND" : "❌ NOT FOUND");
console.log("Results element:", document.getElementById("lp-results") ? "✅ FOUND" : "❌ NOT FOUND");

// Test utilities work
console.log("\n=== UTILITY FUNCTION TEST ===");
try {
  const money = fmtMoney(1234.56);
  console.log("fmtMoney(1234.56) =", money, money === "$1,234.56" ? "✅" : "❌");
  
  const html = escHtml("<script>");
  console.log("escHtml('<script>') =", html, html.includes("&lt;") ? "✅" : "❌");
  
  const cls = getStatusClass(5);
  console.log("getStatusClass(5) =", cls, cls === "pass" ? "✅" : "❌");
} catch (e) {
  console.error("Utility test error:", e);
}

console.log("\n=== LIVE POSITIONS PAGE TEST COMPLETE ===\n");
```

### Manual Functionality Test
1. [ ] File list loads and displays files
2. [ ] Can click a file to analyze
3. [ ] Analysis completes without errors
4. [ ] Filter buttons appear and work
5. [ ] Position cards display correctly
6. [ ] No console errors (F12 → Console tab)

---

## TEST 2: Live Suggestions Page (`/`)

### Module Load Verification
```javascript
console.log("=== PHASE 2 TEST: LIVE SUGGESTIONS ===\n");

// Test Phase 1 utilities
console.log("Phase 1 Utilities:");
console.log("  fmtMoney:", typeof fmtMoney === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  escHtml:", typeof escHtml === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  getStatusClass:", typeof getStatusClass === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 1 config
console.log("\nPhase 1 Config:");
console.log("  CSS object:", typeof CSS === 'object' ? "✅ LOADED" : "❌ MISSING");
console.log("  SELECTORS object:", typeof SELECTORS === 'object' ? "✅ LOADED" : "❌ MISSING");

// Test EventManager
console.log("\nPhase 1 EventManager:");
console.log("  eventManager:", typeof eventManager === 'object' ? "✅ LOADED" : "❌ MISSING");
if (typeof eventManager === 'object') {
  const stats = eventManager.getStats();
  console.log("  - Listener stats:", stats);
}

// Test Phase 2.2 State
console.log("\nPhase 2.2 State Module:");
console.log("  getSortKey:", typeof getSortKey === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  setSortKey:", typeof setSortKey === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  getLiveData:", typeof getLiveData === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  setLiveData:", typeof setLiveData === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  getMarketContext:", typeof getMarketContext === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 2.2 Sorting
console.log("\nPhase 2.2 Sorting Module:");
console.log("  SORT_BUTTONS:", typeof SORT_BUTTONS !== 'undefined' ? "✅ LOADED" : "❌ MISSING");
console.log("  sortValue:", typeof sortValue === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  sortRows:", typeof sortRows === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  getSortedRows:", typeof getSortedRows === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  renderSortBar:", typeof renderSortBar === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 2.2 Cards
console.log("\nPhase 2.2 Cards Module:");
console.log("  greeksBlock:", typeof greeksBlock === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  describeDirection:", typeof describeDirection === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  marketBiasBadge:", typeof marketBiasBadge === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  priceBadge:", typeof priceBadge === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  renderTopTrades:", typeof renderTopTrades === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  buildTickerCard:", typeof buildTickerCard === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  renderTickerSection:", typeof renderTickerSection === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test Phase 2.2 Market Context
console.log("\nPhase 2.2 Market Context Module:");
console.log("  renderMarketContext:", typeof renderMarketContext === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  loadMarketContext:", typeof loadMarketContext === 'function' ? "✅ LOADED" : "❌ MISSING");
console.log("  isMarketContextEnabled:", typeof isMarketContextEnabled === 'function' ? "✅ LOADED" : "❌ MISSING");

// Test functionality
console.log("\n=== FUNCTIONALITY TEST ===");
console.log("Live panel element:", document.getElementById("live-panel") ? "✅ FOUND" : "❌ NOT FOUND");
console.log("Live form element:", document.getElementById("live-form") ? "✅ FOUND" : "❌ NOT FOUND");
console.log("Live results element:", document.getElementById("live-results") ? "✅ FOUND" : "❌ NOT FOUND");
console.log("Watchlist element:", document.getElementById("wl-chips") ? "✅ FOUND" : "❌ NOT FOUND");

// Test utilities work
console.log("\n=== UTILITY FUNCTION TEST ===");
try {
  const money = fmtMoney(-999.99);
  console.log("fmtMoney(-999.99) =", money, money.includes("$") ? "✅" : "❌");
  
  const dir = describeDirection(0.7);
  console.log("describeDirection(0.7) =", dir, dir.includes("Bullish") ? "✅" : "❌");
} catch (e) {
  console.error("Utility test error:", e);
}

console.log("\n=== LIVE SUGGESTIONS PAGE TEST COMPLETE ===\n");
```

### Manual Functionality Test
1. [ ] Watchlist section loads
2. [ ] Can add/remove tickers from watchlist
3. [ ] Can click "Run Live Analysis" button
4. [ ] Analysis starts and shows loading
5. [ ] Sort bar appears with buttons
6. [ ] Ticker cards display correctly
7. [ ] Market context panel loads (if enabled)
8. [ ] No console errors (F12 → Console tab)

---

## EXPECTED RESULTS

### If ALL tests show ✅
```
Phase 1: ✅ All utilities loaded
Phase 2.1: ✅ All live_positions modules loaded
Phase 2.2: ✅ All live_suggestions modules loaded
EventManager: ✅ Tracking listeners
DOM: ✅ All elements found
Functions: ✅ All working correctly
```

### If you see ❌
Check:
1. Browser console (F12) for error messages
2. Network tab (F12) - verify .js files load with 200 status
3. Script load order in HTML (order matters!)
4. No typos in script src paths

---

## QUICK VISUAL CHECKS

### Live Positions Page
- [ ] Title appears
- [ ] File list loads quickly
- [ ] Files display with dates and sizes
- [ ] Can select files
- [ ] Analysis button works

### Live Suggestions Page
- [ ] Title appears
- [ ] Watchlist section shows
- [ ] Form loads
- [ ] Can input data
- [ ] Run button is clickable

---

## SUCCESS CRITERIA

**Phase 2 is successful when:**

✅ All 17 Phase 1 + 2 utilities/modules load
✅ No JavaScript errors in console
✅ All .js files return 200 status in Network tab
✅ Both pages function as before (but with modular code)
✅ No visual regressions
✅ EventManager tracking listeners
✅ All 11 new modules are in memory

---

## DEBUGGING TIPS

If a module doesn't load:

1. **Check Network tab (F12)**
   - Right-click → Inspect → Network tab
   - Reload page
   - Look for .js files
   - Any red (errors)? Note which file

2. **Check Console (F12)**
   - F12 → Console tab
   - Any errors? Copy the error message

3. **Verify Script Order**
   - Open page source (Ctrl+U)
   - Check script tags in order
   - State modules must load BEFORE main orchestrator

4. **Clear Cache**
   - Hard refresh: Ctrl+Shift+R
   - Or F12 → Settings → Disable cache (while devtools open)
