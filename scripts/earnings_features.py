"""
F5: Training JOIN layer — earnings fundamentals + NLP signals.

Provides load_earnings_join(df) which left-joins earnings context
to any training DataFrame that has (ticker, date) columns.

JOIN semantics (temporal integrity):
  For each (ticker, date) row in the training data, finds the most
  recent earnings event where earnings_available_date <= date.
  This enforces the point-in-time constraint: earnings data used in
  training was genuinely available before the decision date.

  earnings_available_date:
    BMO earnings → same day as earnings_date
    AMC/unknown  → next trading day after earnings_date

  Strict ≤ means same-day BMO reporters ARE included (available that
  morning before market open). AMC reporters are excluded same-day
  (results not available until after close).

Competitor signal JOIN:
  For each training row, also aggregates signals from the ticker's
  direct same-industry competitors using ticker_competitors + earnings_fundamentals.
  Aggregates: competitor_beat_rate_14d, competitor_avg_eps_surprise_14d,
  days_since_last_competitor_earnings.

Usage:
  from scripts.earnings_features import load_earnings_join, EARNINGS_FEATURE_COLS

  df = load_labeled_data()        # regime_training or training_snapshots
  df = load_earnings_join(df)     # adds earnings_* and comp_* columns
  # NaN means no earnings event available before this date (correct, not imputed)
"""
from __future__ import annotations

import logging
from pathlib import Path
import sys

import pandas as pd
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

log = logging.getLogger(__name__)

# ── Feature column lists (used by training scripts for NUMERIC_FEATURES) ───────

# Core earnings fundamentals (NULL = no earnings event available before this date)
EARNINGS_FUNDAMENTALS_COLS = [
    # EPS signals
    "ef_eps_surprise",
    "ef_eps_surprise_vs_sector",
    "ef_eps_surprise_vs_market",
    "ef_eps_beat_rate_8q",
    "ef_rev_beat_rate_8q",
    "ef_avg_eps_surprise_8q",
    # Margins
    "ef_gross_margin",
    "ef_operating_margin",
    "ef_net_margin",
    "ef_fcf_margin",
    # Valuation (point-in-time own history)
    "ef_ev_ebitda_5y_pct",
    "ef_ps_5y_pct",
    # Sector momentum
    "ef_sector_beat_rate_14d",
    "ef_sector_avg_eps_surprise_14d",
    "ef_sector_avg_post_earnings_return_14d",
    # Post-earnings price move (prior event — how stock historically reacted)
    "ef_abnormal_return_1d",
    "ef_abnormal_return_3d",
    "ef_abnormal_return_5d",
    # Staleness
    "ef_days_since_earnings",
]

# NLP signals from earnings press releases (requires F3 backfill)
EARNINGS_NLP_COLS = [
    "nlp_negative_delta",
    "nlp_uncertainty_delta",
    "nlp_management_sentiment",
    "nlp_guidance_sentiment",
    "nlp_management_sentiment_delta",
    "nlp_guidance_sentiment_delta",
    "nlp_risk_sentiment",
]

# Competitor signals (requires F2b seed)
COMPETITOR_COLS = [
    "comp_beat_rate_14d",
    "comp_avg_eps_surprise_14d",
    "comp_days_since_last_earnings",
]

# All earnings feature columns (add these to NUMERIC_FEATURES in training scripts)
EARNINGS_FEATURE_COLS = (
    EARNINGS_FUNDAMENTALS_COLS
    + EARNINGS_NLP_COLS
    + COMPETITOR_COLS
)


# ── JOIN implementation ────────────────────────────────────────────────────────

def load_earnings_join(df: pd.DataFrame) -> pd.DataFrame:
    """
    Left-join earnings features to df (must have 'ticker' and 'date' columns).
    All added columns are prefixed: ef_*, nlp_*, comp_*.
    Missing rows → NaN (do NOT impute — absence is informative).
    """
    if df.empty:
        for col in EARNINGS_FEATURE_COLS:
            df[col] = np.nan
        return df

    from scripts.db import connect

    # Ensure date column is a date string for SQL
    df = df.copy()
    df["_date_str"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    try:
        with connect() as con:
            # Register the training DataFrame as a DuckDB view
            con.register("_training", df[["ticker", "_date_str"]].rename(columns={"_date_str": "date"}))

            ef_rows = con.execute("""
                SELECT
                    t.ticker,
                    t.date                                  AS training_date,
                    ef.eps_surprise                         AS ef_eps_surprise,
                    ef.eps_surprise_vs_sector               AS ef_eps_surprise_vs_sector,
                    ef.eps_surprise_vs_market               AS ef_eps_surprise_vs_market,
                    ef.eps_beat_rate_8q                     AS ef_eps_beat_rate_8q,
                    ef.rev_beat_rate_8q                     AS ef_rev_beat_rate_8q,
                    ef.avg_eps_surprise_8q                  AS ef_avg_eps_surprise_8q,
                    ef.gross_margin                         AS ef_gross_margin,
                    ef.operating_margin                     AS ef_operating_margin,
                    ef.net_margin                           AS ef_net_margin,
                    ef.fcf_margin                           AS ef_fcf_margin,
                    ef.ev_ebitda_5y_pct                     AS ef_ev_ebitda_5y_pct,
                    ef.ps_5y_pct                            AS ef_ps_5y_pct,
                    ef.sector_beat_rate_14d                 AS ef_sector_beat_rate_14d,
                    ef.sector_avg_eps_surprise_14d          AS ef_sector_avg_eps_surprise_14d,
                    ef.sector_avg_post_earnings_return_14d  AS ef_sector_avg_post_earnings_return_14d,
                    ef.abnormal_return_1d                   AS ef_abnormal_return_1d,
                    ef.abnormal_return_3d                   AS ef_abnormal_return_3d,
                    ef.abnormal_return_5d                   AS ef_abnormal_return_5d,
                    DATEDIFF('day',
                        CAST(ef.earnings_available_date AS DATE),
                        CAST(t.date AS DATE)
                    )                                       AS ef_days_since_earnings
                FROM _training t
                ASOF JOIN earnings_fundamentals ef
                    ON  ef.ticker = t.ticker
                    AND CAST(ef.earnings_available_date AS DATE) <= CAST(t.date AS DATE)
            """).df()

            nlp_rows = con.execute("""
                SELECT
                    t.ticker,
                    t.date                                  AS training_date,
                    nlp.negative_delta                      AS nlp_negative_delta,
                    nlp.uncertainty_delta                   AS nlp_uncertainty_delta,
                    nlp.management_sentiment                AS nlp_management_sentiment,
                    nlp.guidance_sentiment                  AS nlp_guidance_sentiment,
                    nlp.management_sentiment_delta          AS nlp_management_sentiment_delta,
                    nlp.guidance_sentiment_delta            AS nlp_guidance_sentiment_delta,
                    nlp.risk_sentiment                      AS nlp_risk_sentiment
                FROM _training t
                ASOF JOIN earnings_nlp_signals nlp
                    ON  nlp.ticker = t.ticker
                    AND CAST(nlp.earnings_date AS DATE) <= CAST(t.date AS DATE)
                WHERE nlp.nlp_scoring_method != 'no_filing'
            """).df()

            comp_rows = con.execute("""
                SELECT
                    t.ticker,
                    t.date                                                  AS training_date,
                    AVG(CASE WHEN ef.eps_surprise > 0 THEN 1.0 ELSE 0.0 END)
                        FILTER (WHERE DATEDIFF('day', CAST(ef.earnings_available_date AS DATE),
                                               CAST(t.date AS DATE)) <= 14)
                                                                            AS comp_beat_rate_14d,
                    AVG(ef.eps_surprise)
                        FILTER (WHERE DATEDIFF('day', CAST(ef.earnings_available_date AS DATE),
                                               CAST(t.date AS DATE)) <= 14)
                                                                            AS comp_avg_eps_surprise_14d,
                    MIN(DATEDIFF('day', CAST(ef.earnings_available_date AS DATE),
                                        CAST(t.date AS DATE)))              AS comp_days_since_last_earnings
                FROM _training t
                JOIN ticker_competitors tc
                    ON tc.ticker = t.ticker
                JOIN earnings_fundamentals ef
                    ON  ef.ticker = tc.competitor
                    AND CAST(ef.earnings_available_date AS DATE) <= CAST(t.date AS DATE)
                GROUP BY t.ticker, t.date
            """).df()

            con.unregister("_training")

    except Exception as exc:
        log.warning("Earnings JOIN failed: %s — returning df without earnings features", exc)
        for col in EARNINGS_FEATURE_COLS:
            df[col] = np.nan
        return df.drop(columns=["_date_str"], errors="ignore")

    # Merge back onto df
    df = df.rename(columns={"_date_str": "_merge_date"})
    merge_keys = {"ticker": "ticker", "_merge_date": "training_date"}

    for feat_df in [ef_rows, nlp_rows, comp_rows]:
        if feat_df.empty:
            continue
        feat_df["training_date"] = feat_df["training_date"].astype(str)
        df["_merge_date"] = df["_merge_date"].astype(str) if "_merge_date" in df.columns else df["date"].astype(str)
        df = df.merge(
            feat_df,
            left_on=["ticker", "_merge_date"],
            right_on=["ticker", "training_date"],
            how="left",
        ).drop(columns=["training_date"], errors="ignore")

    # Ensure all expected columns exist (fill missing with NaN)
    for col in EARNINGS_FEATURE_COLS:
        if col not in df.columns:
            df[col] = np.nan

    df = df.drop(columns=["_merge_date"], errors="ignore")

    n_joined = df["ef_eps_surprise"].notna().sum()
    n_nlp    = df["nlp_negative_delta"].notna().sum() if "nlp_negative_delta" in df.columns else 0
    log.info(
        "Earnings JOIN: %d/%d rows have ef data, %d/%d have NLP data",
        n_joined, len(df), n_nlp, len(df),
    )
    return df


def get_live_earnings_context(ticker: str, as_of: str | None = None) -> dict:
    """
    Return the most recent earnings features for ticker as of a given date.
    Used at inference time by the predictor. Returns {} if no data available.
    """
    from scripts.db import connect
    from datetime import date as _date

    as_of = as_of or _date.today().isoformat()

    try:
        with connect() as con:
            row = con.execute(
                """
                SELECT
                    eps_surprise, eps_surprise_vs_sector, eps_surprise_vs_market,
                    eps_beat_rate_8q, rev_beat_rate_8q, avg_eps_surprise_8q,
                    gross_margin, operating_margin, net_margin, fcf_margin,
                    ev_ebitda_5y_pct, ps_5y_pct,
                    sector_beat_rate_14d, sector_avg_eps_surprise_14d,
                    sector_avg_post_earnings_return_14d,
                    abnormal_return_1d, abnormal_return_3d, abnormal_return_5d,
                    earnings_available_date
                FROM earnings_fundamentals
                WHERE ticker = ?
                  AND earnings_available_date <= ?
                ORDER BY earnings_date DESC
                LIMIT 1
                """,
                [ticker, as_of],
            ).fetchone()
    except Exception:
        return {}

    if not row:
        return {}

    cols = [
        "eps_surprise", "eps_surprise_vs_sector", "eps_surprise_vs_market",
        "eps_beat_rate_8q", "rev_beat_rate_8q", "avg_eps_surprise_8q",
        "gross_margin", "operating_margin", "net_margin", "fcf_margin",
        "ev_ebitda_5y_pct", "ps_5y_pct",
        "sector_beat_rate_14d", "sector_avg_eps_surprise_14d",
        "sector_avg_post_earnings_return_14d",
        "abnormal_return_1d", "abnormal_return_3d", "abnormal_return_5d",
        "earnings_available_date",
    ]
    d = dict(zip(cols, row))
    from datetime import date as _date2
    avail = d.pop("earnings_available_date")
    try:
        days_since = (_date2.fromisoformat(as_of) - _date2.fromisoformat(str(avail))).days
    except Exception:
        days_since = None
    d["days_since_earnings"] = days_since
    return {"ef_" + k: v for k, v in d.items()}
