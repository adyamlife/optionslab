"""
Ticker forecast collector — run after every morning / afternoon scan.

Collects MC price-distribution forecasts for all tracked tickers × the next
8 weekly expiry dates and writes them to ticker_forecast_log in DuckDB.

Only stores a row when all five quantiles (p10..p90) are non-null.
Rows with the same (ticker, forecast_date, expiry_date, model_version) are
silently ignored (UNIQUE index enforces idempotency).

Run:
    python -m scripts.ticker_forecast_collector
    python -m scripts.ticker_forecast_collector --n-sims 1000
"""
from __future__ import annotations

import argparse
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ticker_forecast_collector")

N_EXPIRIES   = 8   # weekly expiries to forecast
DEFAULT_SIMS = 500


def _next_fridays(n: int, from_date: date) -> list[date]:
    """Return the next n Fridays on or after from_date."""
    fridays: list[date] = []
    d = from_date
    while len(fridays) < n:
        if d.weekday() == 4:  # Friday
            fridays.append(d)
            d += timedelta(days=7)
        else:
            days_until_friday = (4 - d.weekday()) % 7 or 7
            d += timedelta(days=days_until_friday)
    return fridays


def _get_signal_map(tickers: list[str], con) -> dict[str, dict]:
    from scripts.db import SNAPSHOTS_TABLE
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    rows = con.execute(f"""
        SELECT ticker, atm_iv, spot, collected_at
        FROM {SNAPSHOTS_TABLE}
        WHERE ticker IN ({placeholders})
          AND atm_iv IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY collected_at DESC) = 1
    """, tickers).fetchall()
    return {r[0]: {"atm_iv": r[1], "spot": r[2]} for r in rows}


def _run_mc_one(ticker: str, spot: float, iv: float, dte: int,
                n_sims: int) -> tuple[dict | None, str]:
    import numpy as np
    from scripts.monte_carlo import simulate_paths, _distribution_version, _load_garch_art
    from config.rules import RISK_FREE_RATE
    try:
        S_T, _, _, vol_source = simulate_paths(ticker, spot, iv, dte, RISK_FREE_RATE, n_sims=n_sims)
        if S_T is None:
            return None, ""
        pcts = np.percentile(S_T, [10, 25, 50, 75, 90])
        art = _load_garch_art(ticker)
        model_version = _distribution_version(vol_source, art)
        return {
            "p10": round(float(pcts[0]), 4),
            "p25": round(float(pcts[1]), 4),
            "p50": round(float(pcts[2]), 4),
            "p75": round(float(pcts[3]), 4),
            "p90": round(float(pcts[4]), 4),
        }, model_version
    except Exception as e:
        log.debug("MC failed %s dte=%d: %s", ticker, dte, e)
        return None, ""


def collect(n_sims: int = DEFAULT_SIMS) -> None:
    from scripts.db import connect, ensure_forecast_tables, ensure_snapshot_tables
    from config.watchlist import WATCHLIST as TICKERS

    ensure_snapshot_tables()
    ensure_forecast_tables()

    today       = date.today()
    now_iso     = datetime.now(timezone.utc).isoformat()
    expiries    = _next_fridays(N_EXPIRIES, today + timedelta(days=1))

    with connect(read_only=True) as rcon:
        sig_map = _get_signal_map(list(TICKERS), rcon)

    log.info("Tickers with IV snapshot: %d / %d", len(sig_map), len(list(TICKERS)))

    rows: list[dict] = []
    for ticker, sig in sig_map.items():
        spot = sig.get("spot")
        iv   = sig.get("atm_iv")
        if not spot or not iv:
            continue
        for exp in expiries:
            dte = (exp - today).days
            if dte < 1:
                continue
            mc, model_version = _run_mc_one(ticker, float(spot), float(iv), dte, n_sims)
            if mc is None:
                continue
            rows.append({
                "id":            str(uuid.uuid4()),
                "ticker":        ticker,
                "forecast_date": str(today),
                "scan_timestamp": now_iso,
                "expiry_date":   str(exp),
                "dte":           dte,
                "spot":          round(float(spot), 4),
                "atm_iv":        round(float(iv), 6),
                "model_version": model_version,
                **mc,
            })

    if not rows:
        log.warning("No rows to insert — check IV snapshot availability")
        return

    log.info("Inserting %d forecast rows", len(rows))
    with connect() as wcon:
        for r in rows:
            try:
                wcon.execute("""
                    INSERT INTO ticker_forecast_log
                        (id, ticker, forecast_date, scan_timestamp, expiry_date,
                         dte, spot, atm_iv, model_version,
                         p10, p25, p50, p75, p90)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    r["id"], r["ticker"], r["forecast_date"], r["scan_timestamp"],
                    r["expiry_date"], r["dte"], r["spot"], r["atm_iv"],
                    r["model_version"],
                    r["p10"], r["p25"], r["p50"], r["p75"], r["p90"],
                ])
            except Exception as e:
                if "Constraint Error" in str(e) or "UNIQUE" in str(e).upper():
                    log.debug("Duplicate skipped: %s %s %s", r["ticker"], r["forecast_date"], r["expiry_date"])
                else:
                    log.warning("Insert failed for %s: %s", r["ticker"], e)
        wcon.commit()

    log.info("Done — %d rows written for forecast_date=%s", len(rows), today)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect ticker MC forecasts into DuckDB.")
    parser.add_argument("--n-sims", type=int, default=DEFAULT_SIMS)
    args = parser.parse_args()
    collect(n_sims=args.n_sims)


if __name__ == "__main__":
    main()
