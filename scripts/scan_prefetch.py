"""
scan_prefetch.py — Serial I/O prefetch for the paper-trade morning/afternoon scan.

Phase 1 (main thread, before ThreadPoolExecutor):
  1. Batch-download daily price history for all tickers in one yf.download() call.
  2. Batch-download weekly bars (for get_weekly_trend) in one yf.download() call.
  3. Serially fetch option expirations per ticker (E*TRADE ∩ yfinance, rate-limited).
  4. Serially fetch option chains for the DTE windows we actually need.
  Results are pushed into data_fetch._SCAN_CACHE via set_scan_cache().

Phase 2 (ThreadPoolExecutor workers):
  analyze_ticker() calls get_price_history / _get_expirations / get_option_chain /
  get_weekly_trend — all hit the cache and make zero live API calls.
  Workers become pure CPU: no crumb expiry, no rate limits, parallelism is free.
"""
import logging
import time
from datetime import datetime, date

log = logging.getLogger(__name__)

# Index / macro tickers we always prefetch for beta, VIX rank, macro context
_SHARED = ["^VIX", "SPY", "QQQ", "IWM"]


def prefetch_scan_data(
    tickers: list[str],
    params: dict,
    short_params: dict,
    log_obj=None,
) -> dict:
    """Prefetch all scan data serially and populate the data_fetch cache.

    tickers      — PAPER_WATCHLIST tickers
    params       — normal DTE params (min_dte, max_dte, candidate_dte ...)
    short_params — short-DTE params (min_dte=7, max_dte=10)
    log_obj      — logger to use (falls back to module logger)

    Returns stats dict: {hist_ok, hist_fail, wk_ok, wk_fail, exp_ok, exp_fail, chain_ok, chain_fail}
    """
    import yfinance as yf
    from scripts.data_fetch import (
        set_scan_cache, _use_etrade, _et_module, _YF_CONCURRENCY, _fill_bid_ask,
        _load_candidate_dte,
    )

    _log = log_obj or log
    t_start = time.time()
    cache: dict = {}
    stats = {
        "hist_ok": 0, "hist_fail": 0,
        "wk_ok": 0, "wk_fail": 0,
        "exp_ok": 0, "exp_fail": 0,
        "chain_ok": 0, "chain_fail": 0,
    }

    # Combined download list — watchlist + shared indices, deduplicated, order preserved
    all_dl = list(dict.fromkeys(tickers + _SHARED))

    # ── Step 1: Batch daily price history ─────────────────────────────────────
    _log.info(f"[prefetch] step 1 — batch daily history for {len(all_dl)} tickers")
    try:
        import pandas as pd
        raw = yf.download(
            all_dl, period="1y", interval="1d",
            auto_adjust=True, progress=False, threads=False, group_by="ticker",
        )
        if isinstance(raw.columns, pd.MultiIndex):
            for t in all_dl:
                try:
                    df = raw[t].dropna(how="all")
                    if not df.empty:
                        cache[f"ph:{t}"] = df
                        stats["hist_ok"] += 1
                    else:
                        stats["hist_fail"] += 1
                except Exception:
                    stats["hist_fail"] += 1
        else:
            # Single-ticker fallback (shouldn't happen with all_dl > 1)
            if not raw.empty and len(all_dl) == 1:
                cache[f"ph:{all_dl[0]}"] = raw.dropna(how="all")
                stats["hist_ok"] += 1
        _log.info(
            f"[prefetch] daily history done: {stats['hist_ok']} ok, "
            f"{stats['hist_fail']} fail  ({time.time()-t_start:.1f}s)"
        )
    except Exception as e:
        _log.warning(f"[prefetch] batch daily history failed: {e}")

    # ── Step 2: Batch weekly bars for get_weekly_trend ────────────────────────
    _log.info(f"[prefetch] step 2 — batch weekly history for {len(tickers)} tickers")
    try:
        import pandas as pd
        wk_raw = yf.download(
            tickers, period="3y", interval="1wk",
            auto_adjust=True, progress=False, threads=False, group_by="ticker",
        )
        if isinstance(wk_raw.columns, pd.MultiIndex):
            for t in tickers:
                try:
                    df = wk_raw[t].dropna(how="all")
                    if not df.empty:
                        cache[f"wk:{t}"] = df
                        stats["wk_ok"] += 1
                    else:
                        stats["wk_fail"] += 1
                except Exception:
                    stats["wk_fail"] += 1
        else:
            if not wk_raw.empty and len(tickers) == 1:
                cache[f"wk:{tickers[0]}"] = wk_raw.dropna(how="all")
                stats["wk_ok"] += 1
        _log.info(
            f"[prefetch] weekly history done: {stats['wk_ok']} ok, "
            f"{stats['wk_fail']} fail  ({time.time()-t_start:.1f}s)"
        )
    except Exception as e:
        _log.warning(f"[prefetch] batch weekly history failed: {e}")

    # Push history into the live cache now — E*TRADE session warmup happens next
    # and workers may start seeing calls while we're still fetching chains.
    set_scan_cache(cache)
    cache = {}

    # ── Step 3 + 4: Serial expirations + chains ───────────────────────────────
    min_dte_n = int(params.get("min_dte", 2))
    max_dte_n = int(params.get("max_dte", 30))
    min_dte_s = int(short_params.get("min_dte", 7))
    max_dte_s = int(short_params.get("max_dte", 10))
    today     = date.today()
    targets   = _load_candidate_dte()

    # Calendar and Diagonal back-expiry gap windows (mirror analyze.py / settings.toml)
    try:
        from config import rules as _rules
        _cal_min_gap  = int(getattr(_rules, "CALENDAR_MIN_GAP_DAYS", 14))
        _cal_max_gap  = int(getattr(_rules, "CALENDAR_MAX_GAP_DAYS", 60))
        _diag_min_gap = int(getattr(_rules, "DIAGONAL_MIN_GAP_DAYS", 14))
        _diag_max_gap = int(getattr(_rules, "DIAGONAL_MAX_GAP_DAYS", 45))
    except Exception:
        _cal_min_gap, _cal_max_gap   = 14, 60
        _diag_min_gap, _diag_max_gap = 14, 45

    def _pick(exps, min_d, max_d):
        """Pick the expiry within [min_d, max_d] closest to a candidate_dte target."""
        candidates = []
        for exp in exps:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            if min_d <= dte <= max_d:
                candidates.append((exp, dte))
        if not candidates:
            return None
        return min(candidates, key=lambda c: min(abs(c[1] - t) for t in targets))[0]

    def _pick_back(exps, front_exp, min_gap, max_gap):
        """Pick a back-month expiry min_gap..max_gap days after front_exp (for Calendar/Diagonal)."""
        if not front_exp:
            return None
        front_date = datetime.strptime(front_exp, "%Y-%m-%d").date()
        mid = (min_gap + max_gap) / 2
        candidates = []
        for exp in exps:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            gap = (exp_date - front_date).days
            if min_gap <= gap <= max_gap:
                candidates.append((exp, gap))
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c[1] - mid))[0]

    _log.info(f"[prefetch] step 3+4 — expirations + chains for {len(tickers)} tickers (serial)")

    for i, ticker in enumerate(tickers):
        try:
            ticker_obj = yf.Ticker(ticker)

            # -- Expirations: yfinance first, intersect E*TRADE if available --
            yf_exps: list[str] = []
            try:
                with _YF_CONCURRENCY:
                    yf_exps = list(ticker_obj.options)
            except Exception as e:
                _log.debug(f"[prefetch] {ticker} yf exps failed: {e}")

            final_exps = yf_exps
            try:
                if _use_etrade("expirations"):
                    et = _et_module()
                    et_exps = et.get_option_expirations(ticker)
                    if et_exps and yf_exps:
                        yf_set = set(yf_exps)
                        valid = [e for e in et_exps if e in yf_set]
                        final_exps = valid if valid else yf_exps
                    elif et_exps:
                        final_exps = et_exps
            except Exception:
                pass

            if final_exps:
                cache[f"exps:{ticker}"] = final_exps
                stats["exp_ok"] += 1
            else:
                stats["exp_fail"] += 1
                continue

            # -- Chains: prefetch front expiries + Calendar/Diagonal back expiries --
            normal_exp = _pick(final_exps, min_dte_n, max_dte_n)
            short_exp  = _pick(final_exps, min_dte_s, max_dte_s)
            to_fetch = {
                e for e in [
                    normal_exp,
                    short_exp,
                    # Calendar back-month (gap 14-60 days after each front expiry)
                    _pick_back(final_exps, normal_exp, _cal_min_gap, _cal_max_gap),
                    _pick_back(final_exps, short_exp,  _cal_min_gap, _cal_max_gap),
                    # Diagonal back-month (gap 14-45 days after each front expiry)
                    _pick_back(final_exps, normal_exp, _diag_min_gap, _diag_max_gap),
                    _pick_back(final_exps, short_exp,  _diag_min_gap, _diag_max_gap),
                ] if e
            }

            for exp in to_fetch:
                fetched = False
                try:
                    if _use_etrade("option_chain"):
                        try:
                            et = _et_module()
                            calls, puts = et.get_option_chain(ticker, exp)
                            if calls is not None and not calls.empty:
                                # Store raw (no bid/ask fill) so get_option_chain applies
                                # _fill_bid_ask with the caller's spot/dte at analysis time.
                                cache[f"chain:{ticker}:{exp}"] = (calls, puts)
                                stats["chain_ok"] += 1
                                fetched = True
                        except Exception:
                            pass
                    if not fetched:
                        with _YF_CONCURRENCY:
                            chain = ticker_obj.option_chain(exp)
                        cache[f"chain:{ticker}:{exp}"] = (chain.calls, chain.puts)
                        stats["chain_ok"] += 1
                except Exception as e:
                    _log.debug(f"[prefetch] {ticker} chain {exp} failed: {e}")
                    stats["chain_fail"] += 1

        except Exception as e:
            _log.warning(f"[prefetch] {ticker} error: {e}")

        # Rate-limit: 0.25 s between tickers avoids E*TRADE 429s
        if i < len(tickers) - 1:
            time.sleep(0.25)

    set_scan_cache(cache)

    elapsed = time.time() - t_start
    _log.info(
        f"[prefetch] done in {elapsed:.0f}s — "
        f"hist {stats['hist_ok']}/{len(all_dl)}, "
        f"weekly {stats['wk_ok']}/{len(tickers)}, "
        f"exps {stats['exp_ok']}/{len(tickers)}, "
        f"chains {stats['chain_ok']} (fail {stats['chain_fail']})"
    )
    return stats
