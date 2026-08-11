"""
Daily ATM IV collector — runs at 4:15 PM Mon-Fri via Windows Task Scheduler.

Fetches spot, ATM IV, HV20, and IV rank for every primary watchlist ticker
and appends one row per ticker to iv_history. Skips weekends automatically.

Schedule: Task Scheduler → Action → python scripts/daily_iv_collect.py
"""
import sys
import logging
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daily_iv_collect] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _load_watchlist() -> list[str]:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    cfg = tomllib.loads((ROOT / "config" / "settings.toml").read_text(encoding="utf-8"))
    return cfg.get("watchlist", [])


def _fetch_atm_iv(ticker: str) -> dict | None:
    """
    Use yfinance to fetch spot and the nearest-expiry ATM call/put mid-IV.
    Returns dict with atm_iv, hv20, iv_rank_52w, spot — or None on failure.
    """
    try:
        import yfinance as yf
        import numpy as np

        tk = yf.Ticker(ticker)

        # Spot price
        hist = tk.history(period="1d")
        if hist.empty:
            return None
        spot = float(hist["Close"].iloc[-1])

        # HV20: 20-day realised vol annualised
        hist_60 = tk.history(period="60d")
        if len(hist_60) >= 21:
            rets = hist_60["Close"].pct_change().dropna()
            hv20 = float(rets.iloc[-20:].std() * (252 ** 0.5))
        else:
            hv20 = None

        # Nearest expiry options chain
        expiries = tk.options
        if not expiries:
            return None
        nearest = expiries[0]
        chain = tk.option_chain(nearest)
        calls = chain.calls
        puts  = chain.puts

        # ATM strike = closest to spot
        if calls.empty or puts.empty:
            return None
        atm_strike = min(calls["strike"].tolist(), key=lambda k: abs(k - spot))

        call_row = calls[calls["strike"] == atm_strike]
        put_row  = puts[puts["strike"] == atm_strike]

        ivs = []
        for df_row in (call_row, put_row):
            if not df_row.empty:
                iv = df_row["impliedVolatility"].iloc[0]
                if iv and iv > 0:
                    ivs.append(float(iv))
        if not ivs:
            return None
        atm_iv = float(np.mean(ivs))

        # IV rank 52-week: where does today's IV sit vs past year
        hist_1y = tk.history(period="1y")
        iv_rank_52w = None
        if hv20 is not None and len(hist_1y) >= 50:
            # Proxy: use HV-based rolling realised vol percentile as IV rank proxy
            # (true IV rank requires a year of daily IV snapshots we don't have yet)
            rets_1y = hist_1y["Close"].pct_change().dropna()
            rolling_hv = [
                float(rets_1y.iloc[max(0, i - 20):i].std() * (252 ** 0.5))
                for i in range(20, len(rets_1y) + 1)
            ]
            if rolling_hv:
                lo = min(rolling_hv)
                hi = max(rolling_hv)
                if hi > lo:
                    iv_rank_52w = round((hv20 - lo) / (hi - lo) * 100, 1)

        return {
            "atm_iv":      round(atm_iv, 6),
            "hv20":        round(hv20, 6) if hv20 is not None else None,
            "iv_rank_52w": iv_rank_52w,
            "spot":        round(spot, 4),
        }
    except Exception as exc:
        log.debug("_fetch_atm_iv %s failed: %s", ticker, exc)
        return None


def run() -> None:
    today = datetime.date.today()
    if today.weekday() >= 5:   # Saturday=5, Sunday=6
        log.info("Weekend — skipping collection")
        return

    from scripts.db import ensure_iv_history_table, connect

    ensure_iv_history_table()
    tickers = _load_watchlist()
    log.info("Collecting ATM IV for %d tickers on %s", len(tickers), today)

    collected, skipped = 0, 0
    with connect() as con:
        for ticker in tickers:
            row = _fetch_atm_iv(ticker)
            if row is None:
                skipped += 1
                log.debug("Skip %s — no data returned", ticker)
                continue
            try:
                con.execute(
                    "INSERT OR REPLACE INTO iv_history "
                    "(ticker, collected_date, atm_iv, hv20, iv_rank_52w, spot) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [ticker, today.isoformat(),
                     row["atm_iv"], row["hv20"], row["iv_rank_52w"], row["spot"]],
                )
                collected += 1
            except Exception as exc:
                log.warning("DB insert failed for %s: %s", ticker, exc)
                skipped += 1
        con.commit()

    log.info(
        "Done — collected=%d skipped=%d date=%s",
        collected, skipped, today,
    )


if __name__ == "__main__":
    from scripts.run_log import record
    record("daily_iv_collect", run)
