"""
Data Flywheel Archive — Tier 0

Four archive jobs scheduled automatically via APScheduler:

  T0-A  archive_intraday_bars()      — yesterday's 5m bars per watchlist ticker   (4:30 PM ET)
  T0-B  archive_vix_term_structure() — VIX / VIX3M / VIX6M / contango ratio       (4:30 PM ET)
  T0-C  archive_oi_snapshot(time)    — per-strike OI for front 2 expiries          (9:45 AM + 3:55 PM ET)
  T0-D  archive_earnings_iv()        — IV run-up for tickers with earnings < 30d   (4:30 PM ET)

label_earnings_outcomes() runs at evening_check to fill post_crush_pct once the
earnings date passes.

E*TRADE auth failures never halt collection; yfinance is the fallback for every job.
All data written to data/ml_training.duckdb via scripts/db ensure_archive_tables().
"""
import logging
from datetime import date, datetime, timedelta

import yfinance as yf

from config.watchlist import WATCHLIST_ALL  # archive jobs monitor both tiers

log = logging.getLogger(__name__)


# ── T0-A: Intraday bars ────────────────────────────────────────────────────────

def archive_intraday_bars(tickers: list[str] | None = None) -> dict:
    """
    Fetch yesterday's 5-minute OHLCV bars for all watchlist tickers.
    yfinance only keeps 7 days of 5m data — archive daily or it's gone.
    """
    from scripts.db import ensure_archive_tables, _insert_intraday_bars
    ensure_archive_tables()

    targets  = tickers or WATCHLIST_ALL
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    collected, errors = [], []

    for ticker in targets:
        try:
            hist = yf.Ticker(ticker).history(period="2d", interval="5m")
            if hist.empty:
                errors.append({"ticker": ticker, "error": "no data"})
                continue

            rows = []
            for ts, row in hist.iterrows():
                row_date = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                if row_date != yesterday:
                    continue
                rows.append({
                    "ticker": ticker,
                    "date":   row_date,
                    "time":   str(ts)[11:16],          # "HH:MM"
                    "open":   round(float(row["Open"]),  4),
                    "high":   round(float(row["High"]),  4),
                    "low":    round(float(row["Low"]),   4),
                    "close":  round(float(row["Close"]), 4),
                    "volume": int(row["Volume"]),
                    "vwap":   None,                    # yfinance doesn't expose VWAP at 5m
                })

            if not rows:
                errors.append({"ticker": ticker, "error": f"no bars for {yesterday}"})
                continue

            _insert_intraday_bars(rows)
            collected.append(ticker)
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})
            log.warning(f"T0-A intraday bars failed for {ticker}: {e}")

    log.info(f"T0-A intraday bars: {len(collected)} tickers archived for {yesterday}, {len(errors)} errors")
    return {"collected": len(collected), "date": yesterday, "errors": errors}


# ── T0-B: VIX term structure ───────────────────────────────────────────────────

def archive_vix_term_structure() -> dict:
    """
    Archive VIX (30d), VIX3M (93d), VIX6M (180d) and contango_ratio once daily.
    Contango ratio < 1 = backwardation (near-term fear spike) — key regime signal.
    """
    from scripts.db import ensure_archive_tables, _insert_vix_term_structure
    ensure_archive_tables()

    today = date.today().isoformat()
    try:
        data  = yf.download(["^VIX", "^VIX3M", "^VIX6M"], period="2d",
                             auto_adjust=True, progress=False)
        close = data["Close"] if "Close" in data.columns else data

        def _last(sym):
            try:
                col = close[sym].dropna()
                return float(col.iloc[-1]) if not col.empty else None
            except Exception:
                return None

        vix   = _last("^VIX")
        vix3m = _last("^VIX3M")
        vix6m = _last("^VIX6M")

        contango_ratio = round(vix / vix3m, 4) if vix and vix3m and vix3m > 0 else None
        term_slope     = round(vix3m / vix6m, 4) if vix3m and vix6m and vix6m > 0 else None

        record = {
            "date":           today,
            "vix":            round(vix,   2) if vix   else None,
            "vix_3m":         round(vix3m, 2) if vix3m else None,
            "vix_6m":         round(vix6m, 2) if vix6m else None,
            "contango_ratio": contango_ratio,
            "term_slope":     term_slope,
        }
        _insert_vix_term_structure(record)
        log.info(f"T0-B VIX term structure: VIX={vix:.1f}, VIX3M={vix3m:.1f}, contango={contango_ratio}")
        return {"ok": True, "date": today, **record}
    except Exception as e:
        log.warning(f"T0-B VIX term structure failed: {e}")
        return {"ok": False, "error": str(e)}


# ── T0-C: OI snapshot ─────────────────────────────────────────────────────────

def archive_oi_snapshot(time_of_day: str = "close",
                        tickers: list[str] | None = None) -> dict:
    """
    Snapshot per-strike OI for the front 2 expiries of each watchlist ticker.
    Call twice daily: time_of_day="open" at 9:45 AM ET, "close" at 3:55 PM ET.
    OI delta is derived in query (oi_close - oi_open for same date/ticker/strike).

    E*TRADE is preferred (provides gamma per strike for GEX computation);
    yfinance is the fallback (OI and IV only, no Greeks).
    """
    from scripts.db import ensure_archive_tables, _insert_oi_snapshot
    ensure_archive_tables()

    targets = tickers or WATCHLIST_ALL
    today   = date.today().isoformat()
    now     = datetime.now().isoformat()
    collected, errors = [], []

    for ticker in targets:
        try:
            rows = _fetch_oi_rows(ticker, today, now, time_of_day)
            if not rows:
                errors.append({"ticker": ticker, "error": "no chain data"})
                continue
            _insert_oi_snapshot(rows)
            collected.append(ticker)
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})
            log.warning(f"T0-C OI snapshot ({time_of_day}) failed for {ticker}: {e}")

    log.info(f"T0-C OI snapshot ({time_of_day}): {len(collected)} tickers, {len(errors)} errors")
    return {"collected": len(collected), "time_of_day": time_of_day, "errors": errors}


def _fetch_oi_rows(ticker: str, today: str, now: str,
                   time_of_day: str) -> list[dict]:
    """Try E*TRADE for OI + gamma; fall back to yfinance for OI only."""
    try:
        from scripts import etrade_client as et
        if et.is_authenticated():
            rows = _fetch_oi_etrade(ticker, today, now, time_of_day)
            if rows:
                return rows
    except Exception:
        pass
    return _fetch_oi_yfinance(ticker, today, now, time_of_day)


def _fetch_oi_etrade(ticker: str, today: str, now: str,
                     time_of_day: str) -> list[dict]:
    from scripts import etrade_client as et
    from scripts.data_fetch import pick_expiry
    tkr_obj = yf.Ticker(ticker)

    # Front 2 expiries within 60 DTE
    expiries = []
    for lo, hi in [(0, 21), (21, 60)]:
        exp, _ = pick_expiry(tkr_obj, min_dte=lo, max_dte=hi)
        if exp and exp not in expiries:
            expiries.append(exp)

    rows = []
    for expiry in expiries:
        try:
            calls_df, puts_df = et.get_option_chain(ticker, expiry)
            for opt_type, df in (("call", calls_df), ("put", puts_df)):
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    rows.append({
                        "ticker":       ticker,
                        "date":         today,
                        "time_of_day":  time_of_day,
                        "collected_at": now,
                        "expiry":       expiry,
                        "strike":       round(float(row["strike"]), 2),
                        "option_type":  opt_type,
                        "oi":           int(row.get("openInterest") or 0),
                        "iv":           float(row.get("impliedVolatility") or 0) or None,
                        "gamma":        float(row.get("gamma") or 0) or None,
                        "volume":       int(row.get("volume") or 0),
                    })
        except Exception:
            continue
    return rows


def _fetch_oi_yfinance(ticker: str, today: str, now: str,
                       time_of_day: str) -> list[dict]:
    tkr_obj  = yf.Ticker(ticker)
    expiries = list(tkr_obj.options or [])[:2]
    rows = []
    for expiry in expiries:
        try:
            chain = tkr_obj.option_chain(expiry)
            for opt_type, df in (("call", chain.calls), ("put", chain.puts)):
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    rows.append({
                        "ticker":       ticker,
                        "date":         today,
                        "time_of_day":  time_of_day,
                        "collected_at": now,
                        "expiry":       expiry,
                        "strike":       round(float(row["strike"]), 2),
                        "option_type":  opt_type,
                        "oi":           int(row.get("openInterest") or 0),
                        "iv":           float(row.get("impliedVolatility") or 0) or None,
                        "gamma":        None,      # not available from yfinance
                        "volume":       int(row.get("volume") or 0),
                    })
        except Exception:
            continue
    return rows


# ── T0-D: Earnings IV run-up ───────────────────────────────────────────────────

def archive_earnings_iv(tickers: list[str] | None = None) -> dict:
    """
    For tickers with earnings within 30 days, snapshot ATM IV, front-month IV,
    and back-month IV. Captures the IV run-up curve leading into the event.
    post_crush_pct is filled in by label_earnings_outcomes() after the event.
    """
    from scripts.db import ensure_archive_tables, _insert_earnings_iv
    from scripts.data_fetch import pick_expiry
    from scripts.analyze import days_to_earnings
    ensure_archive_tables()

    targets   = tickers or WATCHLIST
    today     = date.today()
    today_str = today.isoformat()
    collected, skipped, errors = [], [], []

    for ticker in targets:
        try:
            tkr_obj   = yf.Ticker(ticker)
            earn_days = days_to_earnings(tkr_obj)

            if earn_days is None or earn_days > 30 or earn_days < 0:
                skipped.append(ticker)
                continue

            earnings_date = (today + timedelta(days=int(earn_days))).isoformat()

            spot = None
            try:
                hist = tkr_obj.history(period="2d")
                if not hist.empty:
                    spot = float(hist["Close"].iloc[-1])
            except Exception:
                pass

            # Front expiry = expires near or before earnings; back = expires after
            front_exp, _ = pick_expiry(tkr_obj, min_dte=0, max_dte=int(earn_days) + 7)
            back_exp,  _ = pick_expiry(tkr_obj, min_dte=int(earn_days) + 7, max_dte=60)

            atm_iv = front_iv = back_iv = None

            if front_exp and spot:
                try:
                    chain = tkr_obj.option_chain(front_exp)
                    if not chain.calls.empty:
                        df = chain.calls.copy()
                        df["_dist"] = (df["strike"] - spot).abs()
                        atm_row  = df.sort_values("_dist").iloc[0]
                        atm_iv   = float(atm_row.get("impliedVolatility") or 0) or None
                        front_iv = atm_iv
                except Exception:
                    pass

            if back_exp and spot:
                try:
                    chain = tkr_obj.option_chain(back_exp)
                    if not chain.calls.empty:
                        df = chain.calls.copy()
                        df["_dist"] = (df["strike"] - spot).abs()
                        back_iv = float(df.sort_values("_dist").iloc[0].get("impliedVolatility") or 0) or None
                except Exception:
                    pass

            _insert_earnings_iv({
                "ticker":           ticker,
                "earnings_date":    earnings_date,
                "snapshot_date":    today_str,
                "days_to_earnings": int(earn_days),
                "atm_iv":           round(atm_iv,   4) if atm_iv   else None,
                "front_iv":         round(front_iv,  4) if front_iv  else None,
                "back_iv":          round(back_iv,   4) if back_iv   else None,
                "iv_rank":          None,           # to be added from training_snapshots
                "post_crush_pct":   None,           # filled by label_earnings_outcomes()
            })
            collected.append(ticker)
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})
            log.warning(f"T0-D earnings IV failed for {ticker}: {e}")

    log.info(f"T0-D earnings IV: {len(collected)} captured, {len(skipped)} skipped")
    return {"collected": len(collected), "skipped": len(skipped), "errors": errors}


def label_earnings_outcomes() -> dict:
    """
    After an earnings date passes, fetch current (post-event) ATM IV and compute:
      post_crush_pct = (post_iv - pre_iv) / pre_iv * 100

    Negative = IV crushed (expected). Positive = IV expanded after event (rare).
    Called from evening_check once daily.
    """
    from scripts.db import ensure_archive_tables, read_df, execute
    from scripts.data_fetch import pick_expiry
    ensure_archive_tables()

    today = date.today()
    updated = 0

    try:
        df = read_df(
            "SELECT DISTINCT ticker, earnings_date FROM earnings_iv_tracker "
            "WHERE post_crush_pct IS NULL AND earnings_date <= ?",
            [today.isoformat()]
        )
    except Exception:
        return {"updated": 0}

    for _, row in df.iterrows():
        ticker        = row["ticker"]
        earnings_date = row["earnings_date"]
        try:
            tkr_obj = yf.Ticker(ticker)
            hist = tkr_obj.history(period="5d")
            if hist.empty:
                continue
            spot = float(hist["Close"].iloc[-1])

            exp, _ = pick_expiry(tkr_obj, min_dte=0, max_dte=30)
            if not exp:
                continue
            chain = tkr_obj.option_chain(exp)
            if chain.calls.empty:
                continue
            chain.calls["_dist"] = (chain.calls["strike"] - spot).abs()
            post_iv = float(chain.calls.sort_values("_dist").iloc[0].get("impliedVolatility") or 0)
            if not post_iv:
                continue

            pre_df = read_df(
                "SELECT atm_iv FROM earnings_iv_tracker "
                "WHERE ticker=? AND earnings_date=? AND snapshot_date < ? AND atm_iv IS NOT NULL "
                "ORDER BY snapshot_date DESC LIMIT 1",
                [ticker, earnings_date, earnings_date]
            )
            if pre_df.empty:
                continue
            pre_iv = float(pre_df.iloc[0]["atm_iv"])
            if pre_iv <= 0:
                continue

            crush_pct = round((post_iv - pre_iv) / pre_iv * 100, 2)
            execute(
                "UPDATE earnings_iv_tracker SET post_crush_pct=? "
                "WHERE ticker=? AND earnings_date=?",
                [crush_pct, ticker, earnings_date]
            )
            updated += 1
            log.info(f"Earnings IV crush {ticker} ({earnings_date}): {crush_pct:+.1f}%")
        except Exception as e:
            log.warning(f"label_earnings_outcomes failed for {ticker}: {e}")

    return {"updated": updated}


# ── Orchestrator ───────────────────────────────────────────────────────────────

def run_daily_archive() -> dict:
    """
    Runs T0-A + T0-B + T0-D + earnings labeling.
    Called once daily at 4:30 PM ET by the scheduler.
    Failures in any one job do not stop the others.
    """
    log.info("Daily archive starting (T0-A, T0-B, T0-D)...")
    results = {}

    for name, fn in [
        ("intraday_bars",    archive_intraday_bars),
        ("vix_term_struct",  archive_vix_term_structure),
        ("earnings_iv",      archive_earnings_iv),
        ("earnings_labels",  label_earnings_outcomes),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            log.warning(f"Daily archive job '{name}' failed: {e}")
            results[name] = {"ok": False, "error": str(e)}

    log.info(f"Daily archive complete: {results}")
    return results
