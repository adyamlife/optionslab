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


_HV_WINDOWS = [5, 10, 15, 20, 25, 30, 60]


def _fetch_atm_iv(ticker: str) -> dict | None:
    """
    Fetch spot, ATM IV, and realized-vol for all HV windows (5–60 days).
    Returns dict with atm_iv, hv5/hv10/.../hv60, iv_rank_52w, spot — or None.
    """
    try:
        import yfinance as yf
        import numpy as np

        tk = yf.Ticker(ticker)

        # Fetch enough history for the widest window (HV60 needs 61+ returns)
        # "6mo" gives ~125 trading days — sufficient for all windows
        hist = tk.history(period="6mo")
        if hist.empty or len(hist) < _HV_WINDOWS[-1] + 1:
            return None
        spot = float(hist["Close"].iloc[-1])

        # Realized vol for each window (annualised)
        rets = hist["Close"].pct_change().dropna()
        hv = {}
        for w in _HV_WINDOWS:
            if len(rets) >= w:
                hv[w] = round(float(rets.iloc[-w:].std() * (252 ** 0.5)), 6)
            else:
                hv[w] = None
        hv20 = hv.get(20)

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

        # IV rank 52-week proxy: where does today's ATM IV sit vs rolling HV20
        # over the past year. True IV rank requires 252 days of stored atm_iv rows;
        # once that data accumulates, switch to querying iv_history directly.
        hist_1y = tk.history(period="1y")
        iv_rank_52w = None
        if hv20 is not None and len(hist_1y) >= 50:
            rets_1y = hist_1y["Close"].pct_change().dropna()
            rolling_hv = [
                float(rets_1y.iloc[max(0, i - 20):i].std() * (252 ** 0.5))
                for i in range(20, len(rets_1y) + 1)
            ]
            if rolling_hv:
                lo, hi = min(rolling_hv), max(rolling_hv)
                if hi > lo:
                    iv_rank_52w = round((hv20 - lo) / (hi - lo) * 100, 1)

        return {
            "atm_iv":      round(atm_iv, 6),
            "hv5":         hv.get(5),
            "hv10":        hv.get(10),
            "hv15":        hv.get(15),
            "hv20":        hv20,
            "hv25":        hv.get(25),
            "hv30":        hv.get(30),
            "hv60":        hv.get(60),
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
                    "(ticker, collected_date, atm_iv, "
                    " hv5, hv10, hv15, hv20, hv25, hv30, hv60, "
                    " iv_rank_52w, spot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [ticker, today.isoformat(),
                     row["atm_iv"],
                     row["hv5"], row["hv10"], row["hv15"], row["hv20"],
                     row["hv25"], row["hv30"], row["hv60"],
                     row["iv_rank_52w"], row["spot"]],
                )
                collected += 1
            except Exception as exc:
                log.warning("DB insert failed for %s: %s", ticker, exc)
                skipped += 1
        con.commit()

    log.info(
        "IV done — collected=%d skipped=%d date=%s",
        collected, skipped, today,
    )

    # OI daily summary — requires today's close OI snapshot to already be in oi_changes
    try:
        from scripts.data_archive import compute_oi_daily_summary
        compute_oi_daily_summary(tickers=tickers)
    except Exception as exc:
        log.warning("OI daily summary failed: %s", exc)


if __name__ == "__main__":
    from scripts.run_log import record
    record("daily_iv_collect", run)
