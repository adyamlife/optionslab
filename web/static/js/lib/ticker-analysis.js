/**
 * Ticker Analysis Fetch Library
 *
 * Shared `/api/analyze` fetcher for a single ticker, usable from Live
 * Positions, Paper Trades, or anywhere else that needs the same per-ticker
 * rulebook analysis (trend, MACD, market bias, recommended structure, etc.)
 * that Live Suggestions computes for new trades.
 */

/**
 * Fetch live ticker analysis from the /api/analyze SSE stream.
 * Uses CacheManager for request dedup/caching when available (30s TTL),
 * falling back to a direct fetch otherwise.
 * @param {string} ticker - Ticker symbol to analyze
 * @returns {Promise<Object>} Analysis row (matches the shape used by
 *   lib/position-health.js: trend, macd_trend, candidates[], status, etc.)
 */
async function fetchTickerAnalysis(ticker) {
  if (typeof window.CacheManager !== 'undefined') {
    return window.CacheManager.get(
      `ticker-analysis:${ticker}`,
      () => _fetchTickerAnalysisImpl(ticker)
    );
  }
  return _fetchTickerAnalysisImpl(ticker);
}

/**
 * Internal implementation - actual API call (no caching)
 * @private
 */
async function _fetchTickerAnalysisImpl(ticker) {
  try {
    const apiUrl = typeof API !== 'undefined' ? API.ANALYZE : "/api/analyze";
    const res = await fetch(`${apiUrl}?tickers=${encodeURIComponent(ticker)}`);

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }

    // SSE stream - read events
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let fullData = "";
    let analysis = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      fullData += decoder.decode(value, { stream: true });
      const lines = fullData.split("\n");

      // Process complete lines
      for (let i = 0; i < lines.length - 1; i++) {
        const line = lines[i].trim();

        if (line.startsWith("data:")) {
          try {
            const jsonStr = line.substring(5).trim();
            if (jsonStr) {
              const data = JSON.parse(jsonStr);
              if (data.type === "ticker" && data.row && data.row.ticker === ticker) {
                analysis = data.row;
              }
            }
          } catch (e) {
            console.warn("Failed to parse SSE line:", line, e);
          }
        }
      }

      // Keep incomplete line in buffer
      fullData = lines[lines.length - 1];
    }

    if (!analysis) {
      throw new Error(`No analysis received for ${ticker}`);
    }

    return analysis;
  } catch (e) {
    console.error(`Failed to fetch analysis for ${ticker}:`, e);
    throw e;
  }
}
