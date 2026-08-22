-- ============================================================
-- Financial Results Data — DuckDB Schema
-- Four tables capturing earnings fundamentals, IV timeline,
-- NLP signals, and corporate events for 83-ticker universe.
--
-- Design principles:
--   1. NULL means "not available / not collected" — never zero.
--      Zero has a real financial meaning; NULL signals absence.
--   2. has_historical_iv flag gates all IV-derived features.
--      Train on NULL rows; add IV signal progressively as
--      prospective collection accumulates (v2, Aug/Sep 2026+).
--   3. point_in_time_verified flags each feature group. Only
--      verified=TRUE rows enter training. Prevents look-ahead.
--   4. NLP deltas (quarter-over-quarter change) are the primary
--      signal, not raw absolute scores.
-- ============================================================


-- ── Table 1: earnings_fundamentals ──────────────────────────
-- One row per (ticker, earnings_date).
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS earnings_fundamentals (
    -- Keys
    ticker              VARCHAR NOT NULL,
    earnings_date       DATE    NOT NULL,   -- actual release date (confirmed, not estimate)
    fiscal_quarter      VARCHAR,            -- e.g. 'Q2-2025' (FY calendar)
    release_timing      VARCHAR,            -- 'BMO' | 'AMC' | 'unknown'

    -- ── Income statement actuals ────────────────────────────
    revenue_actual      DOUBLE,             -- USD millions
    revenue_estimate    DOUBLE,             -- consensus at earnings_date - 1 day
    revenue_surprise    DOUBLE,             -- (actual - estimate) / |estimate|, fraction
    eps_actual          DOUBLE,             -- USD per share (diluted)
    eps_estimate        DOUBLE,             -- consensus
    eps_surprise        DOUBLE,             -- fraction
    ebitda              DOUBLE,             -- USD millions
    ebit                DOUBLE,             -- USD millions
    net_income          DOUBLE,             -- USD millions

    -- ── Cash flow & balance sheet ───────────────────────────
    free_cash_flow      DOUBLE,             -- USD millions (quarterly)
    operating_cash_flow DOUBLE,             -- USD millions
    cash_and_equiv      DOUBLE,             -- USD millions (balance sheet date)
    total_debt          DOUBLE,             -- USD millions
    net_debt            DOUBLE,             -- total_debt - cash_and_equiv

    -- ── Margins (fractions, 0–1) ────────────────────────────
    gross_margin        DOUBLE,
    operating_margin    DOUBLE,
    net_margin          DOUBLE,
    fcf_margin          DOUBLE,             -- fcf / revenue

    -- ── Guidance ────────────────────────────────────────────
    guidance_direction  VARCHAR,            -- 'raise' | 'lower' | 'maintain' | 'none'
    guidance_revenue_mid DOUBLE,            -- midpoint of next-quarter revenue guide, USD M
    guidance_eps_mid    DOUBLE,             -- midpoint of next-quarter EPS guide

    -- ── Historical beat / miss rates (trailing 8 quarters) ─
    -- Computed at insertion time from prior rows for this ticker.
    -- NULL for the oldest rows where < 4 prior events exist.
    eps_beat_rate_8q    DOUBLE,             -- fraction of last N quarters beat EPS (N up to 8)
    rev_beat_rate_8q    DOUBLE,
    avg_eps_surprise_8q DOUBLE,             -- mean eps_surprise over last N quarters

    -- ── Estimate revisions — NOT collected (no free point-in-time source) ──
    -- eps_rev_1w / eps_rev_4w omitted: consensus snapshots at historical dates
    -- require a paid provider (FactSet, Bloomberg). Adding them later would
    -- require marking point_in_time_verified = FALSE, so they're excluded.

    -- ── Valuation multiples at earnings_date ────────────────
    -- Computed from stored financials + yfinance historical price.
    -- NOT from yfinance.info (that is live-only; using it for historical
    -- dates is look-ahead leakage).
    trailing_pe         DOUBLE,             -- price / ttm_eps (computed)
    forward_pe          DOUBLE,             -- price / forward_eps_consensus (approximate)
    ev_ebitda           DOUBLE,             -- (mktcap + net_debt) / ttm_ebitda (computed)
    price_to_sales      DOUBLE,             -- mktcap / ttm_revenue (computed)
    fcf_yield           DOUBLE,             -- ttm_fcf_per_share / price (fraction)
    peg_ratio           DOUBLE,             -- forward_pe / eps_growth_est; NULL if unavailable

    -- ── Valuation percentiles (primary ML features) ─────────
    -- Percentile vs own prior history (0–1). NULL until ≥ 6 prior quarters.
    -- These are point-in-time correct: computed only from rows with
    -- earnings_date < current row's earnings_date.
    forward_pe_5y_pct   DOUBLE,             -- e.g. 0.82 = 82nd pct of own prior distribution
    ev_ebitda_5y_pct    DOUBLE,
    ps_5y_pct           DOUBLE,

    -- Percentile vs sector peers on same earnings_date (0–1)
    forward_pe_sector_pct   DOUBLE,
    ev_ebitda_sector_pct    DOUBLE,

    -- ── Implied vs realized IV ratio distribution (8 quarters) ─
    -- NULL when has_historical_iv = 0. Never 0.
    median_impl_real_ratio_8q  DOUBLE,
    p25_impl_real_ratio_8q     DOUBLE,
    p75_impl_real_ratio_8q     DOUBLE,

    -- ── Post-earnings price returns ─────────────────────────
    -- Return from close on earnings_date forward. Backfillable from history().
    ret_1d              DOUBLE,             -- fraction
    ret_3d              DOUBLE,
    ret_5d              DOUBLE,
    ret_10d             DOUBLE,
    drift_30d           DOUBLE,             -- ret_30d excluding the initial gap (ret_1d)

    -- ── Point-in-time integrity ──────────────────────────────
    -- TRUE  = all features for this row were collected from data that was
    --         genuinely available before earnings_date (safe for training).
    -- FALSE = at least one feature used a non-point-in-time source
    --         (e.g., yfinance.info valuation fields, or a forward consensus
    --         figure from today's API pulled for a historical date).
    -- Only rows with point_in_time_verified = TRUE enter training.
    point_in_time_verified  BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Metadata ────────────────────────────────────────────
    source              VARCHAR,            -- 'yfinance' | 'fmp' | 'sec_edgar' | 'mixed'
    created_at          TIMESTAMP DEFAULT current_timestamp,
    updated_at          TIMESTAMP DEFAULT current_timestamp,

    PRIMARY KEY (ticker, earnings_date)
);


-- ── Table 2: earnings_iv_timeline ───────────────────────────
-- Multiple rows per (ticker, earnings_date).
-- ATM IV snapshot at fixed offsets from earnings date.
-- days_offset: negative = before earnings, positive = after.
-- Standard offsets: -30, -14, -7, -3, -1, +1
--
-- IV AVAILABILITY DESIGN:
--   has_historical_iv = 0 for all backfilled rows (no free historical IV).
--   All IV fields are NULL — never 0. Zero ATM IV has no financial meaning
--   and would corrupt the model's learned relationship.
--   When prospective collection begins (Aug/Sep 2026+):
--     has_historical_iv = 1
--     atm_iv, iv_rank_52w, etc. populated with real observations.
--   Use has_historical_iv as a feature mask in the ML pipeline.
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS earnings_iv_timeline (
    ticker              VARCHAR  NOT NULL,
    earnings_date       DATE     NOT NULL,
    days_offset         INTEGER  NOT NULL,   -- trading days from earnings_date

    snapshot_date       DATE     NOT NULL,   -- actual calendar date of this snapshot

    -- IV availability gate — 0 for all historical rows, 1 once prospective
    has_historical_iv   INTEGER  NOT NULL DEFAULT 0,  -- 0 | 1

    -- IV fields: NULL when has_historical_iv = 0. Do NOT substitute 0.
    atm_iv              DOUBLE,             -- ATM 30-day IV, annualized fraction. NULL if no IV data.
    iv_rank_52w         DOUBLE,             -- IV rank 0–100 at snapshot_date. NULL if no IV data.
    expected_move_pct   DOUBLE,             -- 0.68 × atm_iv × √(dte/252). NULL if atm_iv NULL.
    implied_move_pct    DOUBLE,             -- straddle-derived expected move. NULL if no options data.

    -- Post-earnings only (days_offset = +1)
    iv_crush_pct        DOUBLE,             -- (iv[-1] - iv[+1]) / iv[-1]. NULL if no IV data.

    -- Backfillable regardless of IV availability
    realized_move_pct   DOUBLE,             -- abs(ret_1d) at days_offset=+1. Populated for all rows.

    source              VARCHAR,            -- 'prospective_collection' | 'internal_iv_history' | 'manual'
    created_at          TIMESTAMP DEFAULT current_timestamp,

    PRIMARY KEY (ticker, earnings_date, days_offset)
);


-- ── Table 3: earnings_nlp_signals ───────────────────────────
-- One row per (ticker, earnings_date).
-- Loughran-McDonald word-frequency scoring of SEC 8-K filings.
--
-- V1 approach (no ML model required):
--   - Pull 8-K text from SEC EDGAR EFTS (free, full 2yr history)
--   - Count words against Loughran-McDonald dictionary (6 categories)
--   - Normalize by total_words → _pct fields
--   - Compute quarter-over-quarter delta → _delta fields
--   - Score per section where section boundaries are detectable
--
-- Delta fields are the primary ML feature. A jump in negative_pct
-- or uncertainty_pct from one quarter to the next is more predictive
-- than the absolute level.
--
-- SECTION SCORING:
--   Score separately for each identifiable section of the 8-K.
--   Sections vary by company; extract what's present. Financial
--   statement tables and boilerplate contaminate whole-doc scoring.
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS earnings_nlp_signals (
    ticker              VARCHAR NOT NULL,
    earnings_date       DATE    NOT NULL,

    -- ── Whole-document counts (fallback if sections not parseable) ──
    total_words             INTEGER,
    positive_count          INTEGER,
    negative_count          INTEGER,
    uncertainty_count       INTEGER,
    litigious_count         INTEGER,
    constraining_count      INTEGER,

    -- Normalized (count / total_words)
    positive_pct            DOUBLE,
    negative_pct            DOUBLE,
    uncertainty_pct         DOUBLE,
    litigious_pct           DOUBLE,
    constraining_pct        DOUBLE,

    -- Quarter-over-quarter delta (current_pct - prior_quarter_pct).
    -- NULL for the first observation of a ticker (no prior to diff against).
    -- These are the primary ML features.
    positive_delta          DOUBLE,
    negative_delta          DOUBLE,
    uncertainty_delta       DOUBLE,
    litigious_delta         DOUBLE,
    constraining_delta      DOUBLE,

    -- ── Section-level scores (NULL if section not found in filing) ──
    -- earnings_release: press release / Exhibit 99.1
    release_negative_pct    DOUBLE,
    release_uncertainty_pct DOUBLE,
    release_positive_pct    DOUBLE,
    release_negative_delta  DOUBLE,
    release_uncertainty_delta DOUBLE,

    -- management_commentary: MD&A or "Results of Operations" section
    mda_negative_pct        DOUBLE,
    mda_uncertainty_pct     DOUBLE,
    mda_positive_pct        DOUBLE,
    mda_negative_delta      DOUBLE,
    mda_uncertainty_delta   DOUBLE,

    -- guidance: forward-looking statements section (when separable)
    guidance_negative_pct   DOUBLE,
    guidance_uncertainty_pct DOUBLE,
    guidance_positive_pct   DOUBLE,
    guidance_negative_delta DOUBLE,
    guidance_uncertainty_delta DOUBLE,

    -- ── Metadata ────────────────────────────────────────────
    filing_url          VARCHAR,            -- SEC EDGAR accession URL for auditability
    lm_dict_version     VARCHAR,            -- e.g. '2024' (LM dictionary release year)
    sections_found      VARCHAR,            -- JSON list of sections successfully parsed
    created_at          TIMESTAMP DEFAULT current_timestamp,

    PRIMARY KEY (ticker, earnings_date)
);


-- ── Table 4: corporate_events ────────────────────────────────
-- Independent of earnings cycle.
-- event_type vocabulary:
--   'buyback_announce' | 'buyback_completion'
--   'dividend_initiate' | 'dividend_increase' | 'dividend_cut' | 'dividend_special'
--   'spinoff_announce' | 'spinoff_complete'
--   'merger_announce' | 'acquisition_announce' | 'acquisition_close'
--   'secondary_offering' | 'stock_split' | 'reverse_split'
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS corporate_events (
    id                  INTEGER PRIMARY KEY,
    ticker              VARCHAR NOT NULL,
    event_date          DATE    NOT NULL,
    event_type          VARCHAR NOT NULL,
    magnitude_pct       DOUBLE,             -- event size as % of market cap
    magnitude_usd_m     DOUBLE,             -- USD millions (deal size, buyback authorization)
    description         VARCHAR,
    source              VARCHAR,
    created_at          TIMESTAMP DEFAULT current_timestamp
);


-- ── Indexes ──────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_ef_ticker      ON earnings_fundamentals (ticker);
CREATE INDEX IF NOT EXISTS idx_ef_date        ON earnings_fundamentals (earnings_date);
CREATE INDEX IF NOT EXISTS idx_ef_pit         ON earnings_fundamentals (point_in_time_verified);
CREATE INDEX IF NOT EXISTS idx_eit_ticker     ON earnings_iv_timeline  (ticker, earnings_date);
CREATE INDEX IF NOT EXISTS idx_eit_iv_avail   ON earnings_iv_timeline  (has_historical_iv);
CREATE INDEX IF NOT EXISTS idx_nlp_ticker     ON earnings_nlp_signals  (ticker);
CREATE INDEX IF NOT EXISTS idx_ce_ticker_date ON corporate_events       (ticker, event_date);
