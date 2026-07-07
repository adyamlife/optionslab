// Phase 3 Simple Test Script - Copy and paste this into console
// No special characters that cause paste issues

console.log('PHASE 3 TEST 1: Live Positions Page');
console.log('====================================');

const tests = {};

// Phase 1 Utilities
tests.phase1 = {
  fmtMoney: typeof fmtMoney === 'function',
  escHtml: typeof escHtml === 'function',
  getStatusClass: typeof getStatusClass === 'function'
};

// Phase 3 Libraries
tests.phase3 = {
  StateManager: typeof window.StateManager !== 'undefined',
  CacheManager: typeof window.CacheManager !== 'undefined',
  PerformanceMonitor: typeof window.PerformanceMonitor !== 'undefined',
  Component: typeof window.Component === 'function'
};

// Phase 2 State Module
tests.state = {
  getFilter: typeof getFilter === 'function',
  setFilter: typeof setFilter === 'function',
  getCurrentGroups: typeof getCurrentGroups === 'function',
  getFullState: typeof getFullState === 'function'
};

// Phase 2 Service Module
tests.service = {
  loadLivePositionFiles: typeof loadLivePositionFiles === 'function',
  analysePositionFile: typeof analysePositionFile === 'function',
  fetchTickerAnalysis: typeof fetchTickerAnalysis === 'function',
  loadEtradePositions: typeof loadEtradePositions === 'function'
};

// Phase 2 Analysis Module
tests.analysis = {
  isOptionPosition: typeof isOptionPosition === 'function',
  buildPositionMarketSignals: typeof buildPositionMarketSignals === 'function',
  buildPositionFeedback: typeof buildPositionFeedback === 'function'
};

// Phase 2 Modal Module
tests.modal = {
  buildActionModal: typeof buildActionModal === 'function',
  openActionModal: typeof openActionModal === 'function'
};

// Phase 2 UI Module
tests.ui = {
  renderSpreadLP: typeof renderSpreadLP === 'function',
  renderPositionResults: typeof renderPositionResults === 'function'
};

// Print results
const printSection = (name, results) => {
  console.log('');
  console.log(name);
  for (const [key, result] of Object.entries(results)) {
    const mark = result ? 'PASS' : 'FAIL';
    console.log('  ' + key + ': ' + mark);
  }
};

printSection('Phase 1 Utilities', tests.phase1);
printSection('Phase 3 Libraries', tests.phase3);
printSection('Phase 2 State Module', tests.state);
printSection('Phase 2 Service Module', tests.service);
printSection('Phase 2 Analysis Module', tests.analysis);
printSection('Phase 2 Modal Module', tests.modal);
printSection('Phase 2 UI Module', tests.ui);

// StateManager Verification
console.log('');
console.log('StateManager State Verification:');
try {
  const lpState = window.StateManager.getState('livePositions');
  console.log('  State loaded: ' + (lpState ? 'PASS' : 'FAIL'));
  console.log('  Filter: ' + (lpState?.filter));
  console.log('  Combined mode: ' + (lpState?.combinedMode));

  // Test mutation
  const orig = lpState.filter;
  window.StateManager.setState({
    livePositions: { ...lpState, filter: 'options' }
  });
  const changed = window.StateManager.getState('livePositions.filter') === 'options';
  console.log('  State mutation works: ' + (changed ? 'PASS' : 'FAIL'));

  // Restore
  window.StateManager.setState({
    livePositions: { ...lpState, filter: orig }
  });
} catch (e) {
  console.error('  ERROR: ' + e.message);
}

// CacheManager Verification
console.log('');
console.log('CacheManager Status:');
try {
  const stats = window.CacheManager.getStats();
  console.log('  Cache size: ' + stats.size);
  console.log('  Hit rate: ' + stats.hitRate);
  console.log('  Working: PASS');
} catch (e) {
  console.error('  ERROR: ' + e.message);
}

// PerformanceMonitor Verification
console.log('');
console.log('PerformanceMonitor Status:');
try {
  window.PerformanceMonitor.mark('test');
  for (let i = 0; i < 1000000; i++) Math.sqrt(i);
  window.PerformanceMonitor.measure('test');
  const summary = window.PerformanceMonitor.getSummary();
  console.log('  Metrics collected: ' + Object.keys(summary).length);
  console.log('  Working: PASS');
} catch (e) {
  console.error('  ERROR: ' + e.message);
}

// Summary
const allPass = Object.values(tests).every(section =>
  Object.values(section).every(v => v === true)
);

console.log('');
console.log('====================================');
console.log(allPass ? 'ALL TESTS PASSED!' : 'SOME TESTS FAILED');
console.log('====================================');
