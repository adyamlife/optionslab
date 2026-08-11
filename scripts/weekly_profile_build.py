"""
Weekly ticker profile builder — runs Sunday 8 PM via Windows Task Scheduler.

Reads regime_training (structural backfill) + iv_history (live ATM IV) and
writes one row per ticker to ticker_profile_snapshots.

Key design decisions:
- Ubuntu 4-regime labels: Mean-reverting, Trending, Low-vol-squeeze, High-vol-breakout
- Bayesian shrinkage (k=15) prevents cliff-edge at p=0 or p=1 for sparse tickers
- Wilson score CI replaces naive normal approximation at boundaries
- VP ratio (hv20/forward_hv) = volatility persistence; IV/RV (atm_iv/future_hv5d) = separate
- Profile Quality Score weights: n=0.40, recency=0.25, uncertainty=0.20, span=0.15
- Regime diversity = Shannon entropy / log(4), 0 = monoculture, 1 = all four equally

Schedule: Task Scheduler → Action → python scripts/weekly_profile_build.py
"""
import sys
import logging
import datetime
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [weekly_profile_build] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Ubuntu authoritative regime labels and their short keys
REGIME_MAP = {
    "Mean-reverting":    "mr",
    "Trending":          "tr",
    "Low-vol-squeeze":   "lv",
    "High-vol-breakout": "hv",
}
REGIME_KEYS = list(REGIME_MAP.values())

BAYES_K = 15          # prior strength (configurable here, not hardcoded in math)
MIN_PROFILE_QUALITY = 0.10  # below this, skip writing — too sparse to be meaningful


def _wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a proportion. Handles n=0 and boundary cases."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _regime_diversity(counts: dict[str, int]) -> float:
    """Shannon entropy / log(4), where counts maps regime_key → n. Returns 0-1."""
    total = sum(counts.values())
    if total == 0:
        return 0.0
    entropy = 0.0
    for n in counts.values():
        if n > 0:
            p = n / total
            entropy -= p * math.log(p)
    max_entropy = math.log(4)
    return round(entropy / max_entropy, 4)


def _profile_quality(n_total: int, date_min, date_max, bayes_survival_by_regime: dict) -> tuple[float, float, float, float, float]:
    """
    Compute Profile Quality Score and its four components.
    Returns (n_score, recency_score, uncertainty_score, span_score, composite).
    """
    # n_score: 0 at n=0, 0.5 at n=30, 1.0 at n=100+
    n_score = min(1.0, n_total / 100.0) if n_total >= 30 else min(0.5, n_total / 60.0)

    # recency_score: how far back is the most recent data
    recency_score = 0.0
    if date_max is not None:
        try:
            dmax = datetime.date.fromisoformat(str(date_max)[:10])
            days_ago = (datetime.date.today() - dmax).days
            if days_ago <= 30:
                recency_score = 1.0
            elif days_ago <= 180:
                recency_score = max(0.0, 1.0 - (days_ago - 30) / 150.0)
            else:
                recency_score = 0.0
        except Exception:
            recency_score = 0.0

    # uncertainty_score: average CI width across regime cells with data
    ci_widths = []
    for key, survival in bayes_survival_by_regime.items():
        # uncertainty is lower (score higher) when we're confident
        # proxy: width of Wilson CI on the raw rate is stored separately;
        # here use 1 - (high - low) / 1.0 to turn width into a quality signal
        pass
    # Simpler version: based on n_total (CI width ∝ 1/sqrt(n))
    if n_total >= 100:
        uncertainty_score = 1.0
    elif n_total >= 15:
        uncertainty_score = round(math.sqrt(n_total / 100.0), 3)
    else:
        uncertainty_score = 0.0

    # span_score: how many calendar days of data
    span_score = 0.0
    if date_min is not None and date_max is not None:
        try:
            dmin = datetime.date.fromisoformat(str(date_min)[:10])
            dmax = datetime.date.fromisoformat(str(date_max)[:10])
            span_days = (dmax - dmin).days
            if span_days >= 365:
                span_score = 1.0
            elif span_days >= 90:
                span_score = round((span_days - 90) / 275.0, 3)
            else:
                span_score = 0.0
        except Exception:
            span_score = 0.0

    composite = round(
        0.40 * n_score
        + 0.25 * recency_score
        + 0.20 * uncertainty_score
        + 0.15 * span_score,
        4,
    )
    return n_score, recency_score, uncertainty_score, span_score, composite


def _build_profiles(con_rt, con_iv) -> list[dict]:
    """
    Build one profile dict per ticker from regime_training + iv_history.
    con_rt: connection to the DB with regime_training
    con_iv: connection to the DB with iv_history (may be the same)
    """
    import pandas as pd

    today_str = datetime.date.today().isoformat()

    # Load regime_training — structural backfill, all tickers
    log.info("Loading regime_training...")
    df = con_rt.execute(
        "SELECT ticker, date, regime_label, hv20, forward_hv, forward_return "
        "FROM regime_training "
        "WHERE regime_label IS NOT NULL AND hv20 IS NOT NULL AND forward_hv IS NOT NULL"
    ).fetchdf()
    if df.empty:
        log.warning("regime_training is empty — nothing to build")
        return []

    # Only use known Ubuntu regime labels
    df = df[df["regime_label"].isin(REGIME_MAP.keys())].copy()
    log.info("  %d rows, %d tickers after label filter", len(df), df["ticker"].nunique())

    # Containment: |forward_return| < hv20 * sqrt(5/252) → within 5-day 1-sigma band
    sigma_5d = (5.0 / 252.0) ** 0.5
    df["contained"] = (df["forward_return"].abs() < df["hv20"] * sigma_5d).astype(int)

    # VP ratio per row
    df["vp_ratio"] = df["hv20"] / df["forward_hv"].replace(0, float("nan"))

    # Universe priors (one per regime, used in Bayesian shrinkage)
    universe_priors: dict[str, float] = {}
    for label in REGIME_MAP:
        sub = df[df["regime_label"] == label]
        universe_priors[REGIME_MAP[label]] = float(sub["contained"].mean()) if len(sub) > 0 else 0.5
    log.info("Universe priors: %s", {k: round(v, 3) for k, v in universe_priors.items()})

    # Load iv_history for IV/RV
    try:
        iv_df = con_iv.execute(
            "SELECT ticker, collected_date, atm_iv FROM iv_history "
            "WHERE atm_iv IS NOT NULL AND atm_iv > 0"
        ).fetchdf()
        has_iv_history = not iv_df.empty
    except Exception:
        iv_df = None
        has_iv_history = False

    # Load training_snapshots for future_hv5d (to pair with atm_iv for IV/RV)
    try:
        snap_df = con_rt.execute(
            "SELECT ticker, collected_at, atm_iv, future_hv5d "
            "FROM training_snapshots "
            "WHERE atm_iv IS NOT NULL AND future_hv5d IS NOT NULL "
            "  AND atm_iv > 0 AND future_hv5d > 0"
        ).fetchdf()
        has_snap_ivrv = not snap_df.empty
    except Exception:
        snap_df = None
        has_snap_ivrv = False

    profiles = []
    for ticker, grp in df.groupby("ticker"):
        # Per-regime stats
        regime_stats: dict[str, dict] = {}
        for label, key in REGIME_MAP.items():
            sub = grp[grp["regime_label"] == label]
            n = len(sub)
            if n == 0:
                regime_stats[key] = {"n": 0, "raw_rate": None, "vp_ratio": None}
                continue
            raw_rate = float(sub["contained"].mean())
            prior = universe_priors[key]
            # Bayesian posterior
            bayes = (n * raw_rate + BAYES_K * prior) / (n + BAYES_K)
            vp = float(sub["vp_ratio"].dropna().mean()) if sub["vp_ratio"].notna().any() else None
            regime_stats[key] = {
                "n":        n,
                "raw_rate": round(raw_rate, 4),
                "bayes":    round(bayes, 4),
                "vp_ratio": round(vp, 4) if vp is not None else None,
            }

        n_total = len(grp)
        n_counts = {k: regime_stats[k]["n"] for k in REGIME_KEYS}
        diversity = _regime_diversity(n_counts)

        date_min = grp["date"].min() if not grp["date"].isna().all() else None
        date_max = grp["date"].max() if not grp["date"].isna().all() else None

        # Wilson CI on the regime with the most data (dominant regime)
        dominant_key = max(n_counts, key=lambda k: n_counts[k])
        dom_stats = regime_stats[dominant_key]
        dom_n = dom_stats["n"]
        dom_successes = round(dom_stats["raw_rate"] * dom_n) if dom_stats["raw_rate"] is not None else 0
        lo95, hi95 = _wilson_ci(dom_successes, dom_n)

        # IV/RV from training_snapshots (live data, not backfill)
        iv_rv_ratio = None
        iv_rv_n = 0
        if has_snap_ivrv and snap_df is not None:
            t_snap = snap_df[snap_df["ticker"] == ticker].copy()
            if len(t_snap) >= 5:
                t_snap["iv_rv"] = t_snap["atm_iv"] / t_snap["future_hv5d"]
                t_snap = t_snap[t_snap["iv_rv"].between(0.1, 10.0)]
                if len(t_snap) >= 3:
                    iv_rv_ratio = round(float(t_snap["iv_rv"].mean()), 4)
                    iv_rv_n = len(t_snap)

        # Profile quality
        bayes_by_regime = {k: regime_stats[k]["bayes"] for k in REGIME_KEYS if regime_stats[k].get("bayes") is not None}
        pq_n, pq_rec, pq_unc, pq_span, pq_composite = _profile_quality(
            n_total, date_min, date_max, bayes_by_regime
        )

        if pq_composite < MIN_PROFILE_QUALITY:
            log.debug("Skip %s — profile_quality=%.3f below floor", ticker, pq_composite)
            continue

        def _g(key, field):
            return regime_stats[key].get(field)

        profiles.append({
            "ticker":               ticker,
            "profile_date":         today_str,
            "containment_mr":       _g("mr", "raw_rate"),
            "containment_tr":       _g("tr", "raw_rate"),
            "containment_lv":       _g("lv", "raw_rate"),
            "containment_hv":       _g("hv", "raw_rate"),
            "n_mr":                 _g("mr", "n"),
            "n_tr":                 _g("tr", "n"),
            "n_lv":                 _g("lv", "n"),
            "n_hv":                 _g("hv", "n"),
            "bayes_survival_mr":    _g("mr", "bayes"),
            "bayes_survival_tr":    _g("tr", "bayes"),
            "bayes_survival_lv":    _g("lv", "bayes"),
            "bayes_survival_hv":    _g("hv", "bayes"),
            "survival_lo95":        round(lo95, 4),
            "survival_hi95":        round(hi95, 4),
            "vp_ratio_mr":          _g("mr", "vp_ratio"),
            "vp_ratio_tr":          _g("tr", "vp_ratio"),
            "vp_ratio_lv":          _g("lv", "vp_ratio"),
            "vp_ratio_hv":          _g("hv", "vp_ratio"),
            "iv_rv_ratio":          iv_rv_ratio,
            "iv_rv_n":              iv_rv_n,
            "pq_n_score":           round(pq_n, 4),
            "pq_recency_score":     round(pq_rec, 4),
            "pq_uncertainty_score": round(pq_unc, 4),
            "pq_span_score":        round(pq_span, 4),
            "profile_quality":      pq_composite,
            "regime_diversity":     diversity,
            "n_total":              n_total,
            "date_min":             str(date_min)[:10] if date_min is not None else None,
            "date_max":             str(date_max)[:10] if date_max is not None else None,
        })

    return profiles


def run() -> None:
    from scripts.db import (
        connect,
        ensure_iv_history_table,
        ensure_ticker_profile_snapshots_table,
    )

    ensure_iv_history_table()
    ensure_ticker_profile_snapshots_table()

    with connect() as con:
        profiles = _build_profiles(con, con)

    if not profiles:
        log.warning("No profiles built — check regime_training has data")
        return

    log.info("Writing %d ticker profiles...", len(profiles))
    written = 0
    with connect() as con:
        for p in profiles:
            cols = list(p.keys())
            placeholders = ", ".join("?" * len(cols))
            col_names = ", ".join(cols)
            vals = [p[c] for c in cols]
            try:
                con.execute(
                    f"INSERT OR REPLACE INTO ticker_profile_snapshots "
                    f"({col_names}) VALUES ({placeholders})",
                    vals,
                )
                written += 1
            except Exception as exc:
                log.warning("Failed to write profile for %s: %s", p.get("ticker"), exc)
        con.commit()

    log.info(
        "Done — %d/%d profiles written for %s",
        written, len(profiles), datetime.date.today(),
    )

    # Summary: top and bottom quality profiles
    sorted_profiles = sorted(profiles, key=lambda x: x["profile_quality"], reverse=True)
    log.info("Top 5 by quality: %s",
             [(p["ticker"], p["profile_quality"], p["n_total"]) for p in sorted_profiles[:5]])
    log.info("Bottom 5 by quality: %s",
             [(p["ticker"], p["profile_quality"], p["n_total"]) for p in sorted_profiles[-5:]])


if __name__ == "__main__":
    from scripts.run_log import record
    record("weekly_profile_build", run)
