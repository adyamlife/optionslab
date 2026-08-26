"""
F2b: Seed ticker_competitors table for all tracked tickers.

Groups tickers by GICS industry (yfinance info['industry']) and inserts
each ticker's same-industry peers as direct competitors (weight=1.0).
Tickers with no industry info or in a unique industry get no competitors.

Run:
  python -m scripts.seed_ticker_competitors
  python -m scripts.seed_ticker_competitors --dry-run
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.db import connect, ensure_financial_results_tables

log = logging.getLogger(__name__)

_EDGAR_HEADERS = {
    "User-Agent": "optionlab-research admin@honerfit.com",
}
_EDGAR_SLEEP = 0.12   # EDGAR rate limit: 10 req/s; 0.12s gives headroom


def _fetch_industry_map(tickers: list[str]) -> dict[str, dict]:
    """
    Fetch {ticker: {industry, sector}} via SEC EDGAR submissions API.

    Uses SIC codes (4-digit Standard Industrial Classification) to group
    tickers into industry peers. EDGAR is a public government API with no
    auth, no crumbs, and a generous 10 req/s rate limit — immune to the
    IP-level blocks Yahoo Finance imposes after heavy yfinance backfills.

    Two calls total before the per-ticker loop:
      1. company_tickers.json  → ticker → CIK map (one request)
      2. CIK{n}.json per ticker → sic + sicDescription (86 requests × 0.12s ≈ 10s)
    """
    import json as _json
    import urllib.request

    result: dict[str, dict] = {}
    cik_url = "https://www.sec.gov/files/company_tickers.json"
    log.info("Loading EDGAR ticker→CIK map …")
    try:
        req = urllib.request.Request(cik_url, headers=_EDGAR_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            cik_data = _json.loads(resp.read())
    except Exception as exc:
        log.error("EDGAR company_tickers.json fetch failed: %s", exc)
        return {t: {"industry": "Unknown", "sector": "Unknown"} for t in tickers}

    ticker_to_cik: dict[str, str] = {}
    for entry in cik_data.values():
        sym = entry.get("ticker", "").upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        ticker_to_cik[sym] = cik
    log.info("CIK map loaded (%d entries)", len(ticker_to_cik))

    # ── Step 2: fetch SIC per ticker ──────────────────────────────────────
    log.info("Fetching SIC codes from EDGAR for %d tickers …", len(tickers))
    for ticker in tickers:
        cik = ticker_to_cik.get(ticker)
        if not cik:
            log.warning("  %s: not in EDGAR CIK map — skipping", ticker)
            result[ticker] = {"industry": "Unknown", "sector": "Unknown"}
            continue

        sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            req = urllib.request.Request(sub_url, headers=_EDGAR_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                sub = _json.loads(resp.read())
            sic      = str(sub.get("sic", "") or "")
            sic_desc = sub.get("sicDescription", "") or "Unknown"
            result[ticker] = {"industry": sic_desc, "sector": sic_desc, "sic": sic}
            log.info("  %s: SIC %s — %s", ticker, sic, sic_desc)
        except Exception as exc:
            log.warning("  %s: EDGAR fetch failed (%s) — Unknown", ticker, exc)
            result[ticker] = {"industry": "Unknown", "sector": "Unknown"}

        time.sleep(_EDGAR_SLEEP)

    return result


def _build_pairs(industry_map: dict[str, dict]) -> list[tuple]:
    """
    Build (ticker, competitor, relationship_type, weight) pairs.
    Same industry → weight 1.0. Tickers with Unknown industry are excluded.
    """
    by_industry: dict[str, list[str]] = {}
    for ticker, info in industry_map.items():
        ind = info["industry"]
        if ind and ind != "Unknown":
            by_industry.setdefault(ind, []).append(ticker)

    pairs = []
    for industry, members in by_industry.items():
        if len(members) < 2:
            continue  # sole member in its industry — no competitors
        for ticker in members:
            for competitor in members:
                if ticker == competitor:
                    continue
                pairs.append((ticker, competitor, "same_industry", 1.0))

    return pairs


def run(dry_run: bool = False) -> None:
    ensure_financial_results_tables()

    _NO_EVENTS = frozenset({
        "SPY", "QQQ", "TQQQ", "IWM",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "^VIX", "VIX",
    })
    with connect() as con:
        tickers = [
            r[0] for r in con.execute(
                "SELECT DISTINCT ticker FROM regime_training ORDER BY ticker"
            ).fetchall()
            if r[0] not in _NO_EVENTS
        ]

    if not tickers:
        log.error("No tickers found in regime_training")
        return

    log.info("Fetching industry info for %d tickers", len(tickers))
    industry_map = _fetch_industry_map(tickers)

    pairs = _build_pairs(industry_map)
    log.info("Built %d competitor pairs across %d tickers", len(pairs), len(tickers))

    # Summary by industry
    by_ind: dict[str, list[str]] = {}
    for ticker, info in industry_map.items():
        ind = info["industry"]
        if ind != "Unknown":
            by_ind.setdefault(ind, []).append(ticker)
    print("\nIndustry groups:")
    for ind, members in sorted(by_ind.items(), key=lambda x: -len(x[1])):
        print(f"  {ind:45s} → {', '.join(sorted(members))}")

    if dry_run:
        print(f"\n[dry-run] Would insert {len(pairs)} rows into ticker_competitors")
        return

    if not pairs:
        log.error("No competitor pairs built — ticker_competitors table unchanged")
        return

    sql = (
        "INSERT OR REPLACE INTO ticker_competitors "
        "(ticker, competitor, relationship_type, weight) VALUES (?, ?, ?, ?)"
    )
    with connect() as con:
        con.executemany(sql, pairs)
        con.commit()

    print(f"\nDone. Inserted {len(pairs)} competitor pairs.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Seed ticker_competitors from yfinance industry info")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
