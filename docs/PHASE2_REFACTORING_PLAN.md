# Phase 2: File Splitting & Modularization Plan

## Overview

**Goal:** Break down large monolithic files into focused, single-responsibility modules.

**Current State:**
- live_positions.js: 1478 lines (rendering + analysis + I/O + modals all mixed)
- live.js: 1101 lines (cards + sorting + market context mixed)
- paper_trades.js: 693 lines (might also benefit from splitting)

**Desired State:**
- Each module: 200-400 lines maximum
- Single responsibility per module
- Clear dependencies between modules
- Easier to test and maintain

---

## Live Positions Splitting Strategy

### Current Structure Analysis
live_positions.js contains 4 distinct concerns:

1. **File I/O & Loading** (lines 318-350, 371-393, 1469+)
   - loadLivePositionFiles()
   - analysePositionFile()
   - loadEtradePositions()

2. **Analysis & Feedback** (lines 19-57, 92-317)
   - fetchTickerAnalysis()
   - buildPositionMarketSignals()
   - buildPositionFeedback()
   - isOptionPosition()

3. **Rendering** (lines 639-862, 1043-1134, 1158-1371)
   - renderSpreadLP()
   - renderPositionResults()
   - renderGroupCombined()
   - renderCombinedPnlExplanation()

4. **Modals & Actions** (lines 1372-1468)
   - buildActionModal()
   - openActionModal()

### Proposed Split

```
live_positions.js (960 lines → 240 lines)
├── live_positions_service.js (File I/O)
├── live_positions_analysis.js (Feedback & signals)
├── live_positions_ui.js (Rendering)
├── live_positions_modal.js (Modals)
└── live_positions_state.js (Shared state)
```

#### Module 1: live_positions_service.js
**Purpose:** File I/O and data fetching
**~250 lines**

Functions:
- loadLivePositionFiles()
- analysePositionFile()
- loadEtradePositions()
- fetchTickerAnalysis() - move from analysis module

Dependencies:
- utils.js (escHtml, timeAgo)
- event-manager.js (for cleanup)

Exports:
```javascript
export { loadLivePositionFiles, analysePositionFile, loadEtradePositions, fetchTickerAnalysis }
```

#### Module 2: live_positions_analysis.js
**Purpose:** Market signals and feedback generation
**~350 lines**

Functions:
- buildPositionMarketSignals()
- buildPositionFeedback()
- isOptionPosition()
- Helper functions for analysis

Dependencies:
- utils.js (formatting functions)
- config.js (constants, thresholds)
- common.js (P&L calculations)

Exports:
```javascript
export { buildPositionMarketSignals, buildPositionFeedback, isOptionPosition }
```

#### Module 3: live_positions_ui.js
**Purpose:** All rendering and DOM manipulation
**~450 lines**

Functions:
- renderSpreadLP()
- renderPositionResults()
- renderGroupCombined()
- renderCombinedPnlExplanation()
- renderHedgeLP()
- renderPnlExplanation()
- _classifyPortfolio()
- _combinedNarrative()
- _pnlNarrative()
- _isItm()

Dependencies:
- utils.js (formatting)
- config.js (selectors, CSS classes)
- common.js (P&L analysis)
- live_positions_analysis.js (feedback)

Exports:
```javascript
export { renderSpreadLP, renderPositionResults, renderGroupCombined, renderCombinedPnlExplanation }
```

#### Module 4: live_positions_modal.js
**Purpose:** Action modal dialog management
**~150 lines**

Functions:
- buildActionModal()
- openActionModal()
- Modal state management

Dependencies:
- utils.js (escHtml)
- config.js (CSS, selectors)
- event-manager.js (event handling)

Exports:
```javascript
export { buildActionModal, openActionModal }
```

#### Module 5: live_positions_state.js
**Purpose:** Shared application state
**~80 lines**

State variables:
- _lpFilter
- _lpCurrentGroups
- _lpCurrentFilename
- _lpCurrentEl
- _lpCombinedMode
- _lpCurrentAction

Exports:
```javascript
export { getState, setState, updateFilter, getCurrentGroups, setCurrentGroups }
```

#### Main Module: live_positions.js (refactored)
**Purpose:** Orchestration and initialization
**~150 lines**

Imports all other modules and:
- Sets up page initialization
- Coordinates between modules
- Handles page lifecycle
- Manages event listeners

Structure:
```javascript
import { loadLivePositionFiles, analysePositionFile } from './live_positions_service.js';
import { buildPositionFeedback } from './live_positions_analysis.js';
import { renderPositionResults } from './live_positions_ui.js';
import { buildActionModal } from './live_positions_modal.js';
import { getState, setState } from './live_positions_state.js';

// Page initialization
function initLivePositions() { /* ... */ }

// Event setup
function setupEventHandlers() { /* ... */ }

// Page lifecycle
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    initLivePositions();
    setupEventHandlers();
  });
} else {
  initLivePositions();
  setupEventHandlers();
}
```

---

## Live Suggestions (live.js) Splitting Strategy

### Current Structure Analysis
live.js contains 3 distinct concerns:

1. **Card Rendering** (lines 354-711)
   - renderTopTrades()
   - buildTickerCard()
   - renderTickerSection()

2. **Sorting & Filtering** (lines 101-159)
   - sortValue()
   - sortedRows()
   - renderSortBar()

3. **Market Context** (lines 782-833+)
   - renderMarketContext()
   - loadMarketContext()

### Proposed Split

```
live.js (1101 lines → 220 lines)
├── live_cards.js (Card rendering)
├── live_sorting.js (Sort logic)
├── live_market_context.js (Market data)
└── live_state.js (Shared state)
```

#### Module 1: live_cards.js
**Purpose:** Card rendering and display
**~350 lines**

Functions:
- renderTopTrades()
- buildTickerCard()
- renderTickerSection()
- buildTradeSection()
- Helper functions (marketBiasBadge, priceBadge, etc.)

Dependencies:
- utils.js (formatting)
- config.js (CSS classes)
- common.js (P&L, Greeks)

#### Module 2: live_sorting.js
**Purpose:** Sorting and filtering logic
**~150 lines**

Functions:
- sortValue()
- sortedRows()
- renderSortBar()

State:
- _sortKey
- _sortDir

Dependencies:
- config.js
- event-manager.js

#### Module 3: live_market_context.js
**Purpose:** Market context panel
**~180 lines**

Functions:
- renderMarketContext()
- loadMarketContext()
- Helper formatting functions

Dependencies:
- utils.js
- config.js

#### Main Module: live.js (refactored)
**Purpose:** Orchestration
**~120 lines**

Imports and coordinates the above modules.

---

## Implementation Sequence

### Phase 2.1: Live Positions Splitting (Priority 1)

1. **Create live_positions_state.js**
   - Define state variables
   - Create getter/setter functions
   - Ensures single source of truth

2. **Create live_positions_service.js**
   - Extract file I/O functions
   - Update imports to use Phase 1 modules
   - Add error handling

3. **Create live_positions_analysis.js**
   - Extract analysis functions
   - Add dependencies on common.js for P&L
   - Export feedback builders

4. **Create live_positions_modal.js**
   - Extract modal functions
   - Use event-manager for modal interactions
   - Keep modal state separate

5. **Create live_positions_ui.js**
   - Extract all rendering functions
   - Import from analysis module for feedback
   - Use config constants for CSS classes

6. **Refactor main live_positions.js**
   - Import all modules
   - Keep only initialization and orchestration
   - Clean up event handlers

7. **Update HTML template**
   - Add script imports for new modules
   - Keep correct import order (state first, then services, etc.)

### Phase 2.2: Live Suggestions Splitting (Priority 2)

1. **Create live_state.js**
   - _sortKey, _sortDir
   - _liveData
   - Other shared state

2. **Create live_sorting.js**
   - Sort logic
   - Event handlers for sort buttons

3. **Create live_cards.js**
   - Card rendering
   - All display functions

4. **Create live_market_context.js**
   - Market context panel logic
   - Data loading

5. **Refactor main live.js**
   - Import all modules
   - Keep only orchestration

6. **Update HTML template**
   - Add script imports

---

## Dependency Graph

### Live Positions Dependency Flow
```
utils.js, config.js, event-manager.js, common.js
                    ↓
        live_positions_state.js
        ↓           ↓           ↓
   service.js   analysis.js   modal.js
        ↓           ↓           ↓
    (all flow into)→ ui.js
                    ↓
            live_positions.js (orchestrator)
```

### Live Suggestions Dependency Flow
```
utils.js, config.js, event-manager.js, common.js
                    ↓
        live_state.js
        ↓       ↓           ↓
   sorting.js  cards.js  market_context.js
        ↓       ↓           ↓
    (all flow into)→ live.js (orchestrator)
```

---

## Benefits of Phase 2

✅ **Smaller Files** - Easier to read and understand
✅ **Single Responsibility** - Each module has one job
✅ **Better Testing** - Easier to unit test smaller functions
✅ **Reusability** - Can import individual modules elsewhere
✅ **Parallel Development** - Multiple developers can work on different modules
✅ **Easier Debugging** - Narrower scope to debug
✅ **Performance** - Can lazy-load modules if needed
✅ **Maintainability** - Clear separation of concerns

---

## Testing Strategy for Phase 2

After each module creation:

1. **Syntax Check**
   - No console errors
   - All imports resolve

2. **Functionality Check**
   - Page-specific tests pass
   - Events fire correctly
   - Data renders properly

3. **Integration Check**
   - All modules work together
   - State updates propagate correctly
   - No missing dependencies

---

## HTML Template Updates

### live_positions.html
```html
{% block scripts %}
  <!-- Phase 1 Modules -->
  <script src="/static/js/utils.js"></script>
  <script src="/static/js/config.js"></script>
  <script src="/static/js/event-manager.js"></script>

  <!-- Phase 2 Modules (live_positions) -->
  <script src="/static/js/live_positions_state.js"></script>
  <script src="/static/js/live_positions_service.js"></script>
  <script src="/static/js/live_positions_analysis.js"></script>
  <script src="/static/js/live_positions_modal.js"></script>
  <script src="/static/js/live_positions_ui.js"></script>

  <!-- Main Orchestrator -->
  <script src="/static/js/live_positions.js"></script>
{% endblock %}
```

### index.html (Live Suggestions)
```html
{% block scripts %}
  <!-- Phase 1 Modules -->
  <script src="/static/js/utils.js"></script>
  <script src="/static/js/config.js"></script>
  <script src="/static/js/event-manager.js"></script>

  <!-- Phase 2 Modules (live) -->
  <script src="/static/js/live_state.js"></script>
  <script src="/static/js/live_sorting.js"></script>
  <script src="/static/js/live_cards.js"></script>
  <script src="/static/js/live_market_context.js"></script>

  <!-- Main Orchestrator -->
  <script src="/static/js/live.js"></script>
  <script src="/static/js/watchlist_editor.js"></script>
{% endblock %}
```

---

## Rollback Plan

If issues occur during Phase 2:

1. Keep original files as `.backup` versions
2. If critical error occurs, revert to Phase 1 state
3. No data loss - only code reorganization
4. Can retry specific module split

---

## Next Steps

Ready to begin Phase 2.1: Live Positions Splitting

Start with:
1. Create live_positions_state.js
2. Create live_positions_service.js
3. (Continue with remaining modules)

---

## Success Metrics

Phase 2 is successful when:
- [x] All large files split into <400 line modules
- [x] Each module has single responsibility
- [x] All tests pass
- [x] No functionality lost
- [x] Page performance unchanged or improved
- [x] Code is easier to understand

---

## Estimated Time

- Phase 2.1 (Live Positions): ~2-3 hours
- Phase 2.2 (Live Suggestions): ~1-2 hours
- Testing: ~1 hour
- **Total Phase 2: ~4-6 hours**
