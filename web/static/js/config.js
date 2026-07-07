/**
 * Centralized configuration for CSS classes, DOM selectors, and constants
 * Single source of truth to avoid magic strings scattered throughout the code
 */

/**
 * CSS Class Names
 * Named CSS_CLASSES (not CSS) to avoid shadowing the native browser
 * `window.CSS` namespace (CSS.escape, CSS.supports, etc.) — a global
 * `const CSS = {...}` here previously broke CSS.escape() everywhere.
 */
const CSS_CLASSES = {
  // Status classes
  STATUS_PASS: 'pass',
  STATUS_FAIL: 'fail',
  STATUS_WARN: 'warn',
  STATUS_NA: 'na',

  // Top 3 Trades
  TOP3_PANEL: 'top3-panel',
  TOP3_CARD_HDR: 'top3-card-hdr',
  TOP3_RANK: 'top3-rank',
  TOP3_TICKER: 'top3-ticker',
  TOP3_PRICE: 'top3-price',
  TOP3_BODY: 'top3-body',
  TOP3_ALT_PANEL: 'tc-alt-panel',
  TOP3_ALT_BTN: 'tc-alt-btn',
  TOP3_BIG_METRICS: 'top3-big-metrics',
  TOP3_METRIC: 'top3-metric',

  // Live Positions - Card and Container
  SPREAD_CARD: 'pu-spread-card',
  SPREAD_HEADER: 'pu-spread-header',
  SPREAD_TITLE: 'pu-spread-title',
  METRICS: 'pu-metrics',
  METRIC_LABEL: 'pu-metric-label',
  METRIC_VALUE: 'pu-metric-value',

  // Analysis and Feedback
  FEEDBACK: 'lp-feedback',
  FEEDBACK_ITEM: 'lp-feedback-item',
  FEEDBACK_SECTION: 'lp-feedback-section',
  ANALYSIS_PLACEHOLDER: 'lp-analysis-placeholder',
  MARKET_ANALYSIS: 'lp-market-analysis',
  ANALYSIS_ERROR: 'lp-analysis-error',
  MARKET_SIGNALS: 'lp-market-analysis',
  MARKET_SIGNALS_SUMMARY: 'lp-market-analysis-summary',
  MARKET_SIGNALS_GRID: 'lp-market-analysis-grid',

  // Filter and Controls
  FILTER_BAR: 'lp-filter-bar',
  FILTER_BTN: 'lp-filter-btn',
  FILTER_ACTIVE: 'lp-filter-active',
  VIEW_TOGGLE: 'lp-view-toggle',

  // File and Results
  FILE_LIST: 'lp-file-list',
  FILE_ROW: 'lp-file-row',
  FILE_SELECTED: 'lp-file-selected',
  RESULTS: 'lp-results',

  // Utility classes
  MUTED: 'muted',
  HINT: 'hint',
  BTN_PRIMARY: 'btn-primary',
  ML_AUTO: 'ml-auto',

  // Modal
  MODAL_OVERLAY: 'modal-overlay',
  MODAL_DIALOG: 'modal-dialog',
  MODAL_FIELDS: 'modal-fields',
  MODAL_ACTIONS: 'modal-actions',
  MODAL_LABEL: 'lp-modal-label',
  MODAL_LABEL_FULL: 'lp-modal-label-full',
  MODAL_INPUT_SHORT: 'lp-modal-input-short',
  MODAL_INPUT_FULL: 'lp-modal-input-full',
  MODAL_RESULT: 'lp-modal-result',

  // Text formatting
  LOADING_TEXT: 'lp-loading-text',
  EMPTY_MESSAGE: 'lp-empty-message',
  ERROR_TEXT: 'lp-error-text',

  // Details and tables
  LEG_RAW: 'lp-leg-raw',
  LEGS_DETAIL: 'legs-detail',
  JOURNAL_TABLE: 'journal-table',
  TABLE_SCROLL: 'table-scroll',
};

/**
 * DOM Selectors
 */
const SELECTORS = {
  // Live Positions page
  LP_FILE_LIST: '#lp-file-list',
  LP_RESULTS: '#lp-results',
  LP_FILTER_BAR: '#lp-filter-bar',
  LP_CLOSE_BTN: '#lp-close-results',
  LP_VIEW_INDIVIDUAL: '#lp-view-individual',
  LP_VIEW_COMBINED: '#lp-view-combined',

  // Live Suggestions page
  TC_RESULTS: '#tc-results',
  TC_HEADER: '.tc-header',
  TC_SORT_BAR: '#tc-sort-bar',
  TC_ALTS_LABEL: '.tc-alts-label',
  TC_ALT_PANEL: '.tc-alt-panel',

  // Common
  BODY: 'body',
  CONTAINER: '.container',
  PANEL: '.panel',

  // Data attributes
  DATA_POSITION_KEY: '[data-position-key]',
  DATA_TICKER: '[data-ticker]',
  DATA_FILTER: '[data-filter]',
  DATA_ALT: '[data-alt]',
  DATA_TOP3: '[data-top3]',
  DATA_ANALYSIS_ID: '[data-analysis-id]',
};

/**
 * Event Names and Types
 */
const EVENTS = {
  CLICK: 'click',
  CHANGE: 'change',
  INPUT: 'input',
  SUBMIT: 'submit',
  CLOSE: 'close',
  FILTER: 'filter',
  SELECT: 'select',
};

/**
 * API Endpoints
 */
const API = {
  ANALYZE: '/api/analyze',
  LIVE_POSITION_FILES: '/api/live-position-files',
  LIVE_POSITION_ANALYZE: '/api/analyze-position-file',
  MARKET_CONTEXT: '/api/market-context',
};

/**
 * Timeout Values (in milliseconds)
 */
const TIMEOUTS = {
  API_FETCH: 30000,
  ANALYSIS_FETCH: 60000,
  DEBOUNCE: 300,
  THROTTLE: 500,
};

/**
 * UI Constants
 */
const UI = {
  MAX_ANALYSIS_RETRIES: 3,
  ANALYSIS_PLACEHOLDER_TEXT: 'Loading market analysis…',
  NO_FILES_MESSAGE: 'No files found in data/live_position/. Copy a CSV/TSV export there to begin.',
  EMPTY_RESULTS_MESSAGE: 'No results. Run a scan first.',
};

/**
 * Numeric Thresholds
 */
const THRESHOLDS = {
  ANN_GAIN_HIGH: 50,      // Ann. Gain >= 50% = green
  ANN_GAIN_MID: 20,       // Ann. Gain 20-50% = default
  POP_GOOD: 65,           // POP >= 65% = good
  POP_FAIR: 50,           // POP 50-65% = fair
  ADX_STRONG_TREND: 25,   // ADX >= 25 = strong trend
  ADX_WEAK_TREND: 20,     // ADX < 20 = weak/choppy
  RSI_OVERBOUGHT: 70,
  RSI_OVERSOLD: 30,
};

/**
 * Column Help Text
 */
const COLUMN_HELP = {
  'Max Profit': 'Maximum profit per share if trade expires profitably',
  'Max Loss': 'Maximum loss per share if trade expires at worst case',
  'POP%': 'Probability of Profit at expiration',
  'EV': 'Expected Value = (POP × Max Profit) - ((1 - POP) × Max Loss)',
  'Take-Profit': 'Target price to close the trade at 50% of max profit',
  'DTE': 'Days to Expiration',
  'Ann. Gain': 'Annualized return = (Max Profit / Max Loss) × (365 / DTE)',
};

/**
 * Default Configuration
 */
const DEFAULTS = {
  LIVE_POSITIONS_VIEW: 'combined', // 'combined' or 'individual'
  FILTER: 'all',                    // 'all' or 'options'
  SORT_BY: 'pop',                   // default sort column
  SORT_DIR: 'desc',                 // 'asc' or 'desc'
};
