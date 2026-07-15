import sys
import os
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, jsonify, request, abort, session, redirect, url_for, Response
from werkzeug.security import check_password_hash

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from config.watchlist import WATCHLIST, WATCHLIST_ARCHIVE, WATCHLIST_ALL
from config.feature_flags import ff
from scripts.backtest import run_backtest, WIDTH, DTE
from config import rules
from scripts.analyze import analyze_ticker, PARAM_INFO, DEFAULT_PARAMS
from scripts.ai_assessment import get_ai_assessment
import scripts.position_tracker as pt
import scripts.etrade_client as etrade
import scripts.position_file_parser as pfp
import scripts.paper_trade_engine   as pte
import scripts.market_context       as mc
import scripts.decision_provider    as decision_provider
from config import scoring as sc

from flask_compress import Compress

# ── Load users from secrets.toml ──────────────────────────────────────────────

def _load_auth_config():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    _path = Path(__file__).parent.parent / "config" / "secrets.toml"
    cfg = tomllib.loads(_path.read_text(encoding="utf-8"))
    return cfg.get("users", {}), cfg.get("roles", {}), cfg.get("app", {}).get("secret_key")

_USERS, _ROLES, _SECRET_KEY = _load_auth_config()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or _SECRET_KEY or os.urandom(24)

# ── Logging setup ─────────────────────────────────────────────────────────────
# Configure once here so all loggers (app.logger + every module's
# logging.getLogger(__name__)) write to the same files, regardless of
# whether they're called from a gunicorn worker or a background thread.
import logging as _logging
_LOG_DIR = Path(__file__).parent.parent / "data"
_LOG_DIR.mkdir(exist_ok=True)

_fmt = _logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                           datefmt="%Y-%m-%d %H:%M:%S")

# Main application log — INFO+ from all loggers
_app_handler = _logging.FileHandler(_LOG_DIR / "optionlab.log")
_app_handler.setLevel(_logging.INFO)
_app_handler.setFormatter(_fmt)

# Scheduler/paper-trade log — INFO+ from scheduler-related modules only
_sched_handler = _logging.FileHandler(_LOG_DIR / "scheduler.log")
_sched_handler.setLevel(_logging.INFO)
_sched_handler.setFormatter(_fmt)

# Root logger → optionlab.log (catches everything, including third-party at WARNING+)
_root = _logging.getLogger()
_root.setLevel(_logging.INFO)
_root.addHandler(_app_handler)

# Noisy third-party loggers: keep at WARNING so they don't flood optionlab.log
for _noisy in ("urllib3", "urllib3.connectionpool", "yfinance", "peewee",
               "apscheduler.executors", "apscheduler.scheduler"):
    _logging.getLogger(_noisy).setLevel(_logging.WARNING)

# Scheduler-related modules also write to dedicated scheduler.log
for _sched_mod in ("scripts.paper_trade_engine", "scripts.training_data_collector",
                   "scripts.candidate_ranker", "scripts.data_fetch",
                   "scripts.regime_backfill", "scripts.ml_cache", "web.app"):
    _lg = _logging.getLogger(_sched_mod)
    _lg.addHandler(_sched_handler)

# Flask's own logger → same root handler; make sure it propagates
app.logger.setLevel(_logging.INFO)
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30   # 30 days
app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"
app.config["COMPRESS_ALGORITHM"] = "gzip"
# flask-compress uses a SEPARATE algorithm list for streamed responses (which
# Flask's static file serving falls under) and otherwise ignores
# COMPRESS_ALGORITHM above, defaulting to ["zstd", "br", "deflate"]. Force
# gzip here too so static CSS/JS always use a universally-supported encoding
# instead of zstd, which was being served with a browser decoding mismatch
# that silently corrupted style.css for some browsers.
app.config["COMPRESS_ALGORITHM_STREAMING"] = ["gzip"]
app.config["COMPRESS_LEVEL"] = 6
app.config["COMPRESS_MIN_SIZE"] = 500
Compress(app)

# Cache-busting query param for static assets (CSS/JS) — changes every time
# the server restarts, so browsers always fetch fresh files after a deploy
# instead of serving a stale cached copy. See {{ asset_v }} usage in templates.
_ASSET_VERSION = str(int(time.time()))

@app.context_processor
def _inject_globals():
    return {
        "current_role": _current_role(),
        "now":          datetime.now(timezone.utc),
        "features":     ff.as_dict(),   # {{ features.live_positions.enabled }}
        "ff":           ff,             # {{ ff.role_gte(current_role, "trader") }}
        "asset_v":      _ASSET_VERSION,
    }

def _current_role() -> str | None:
    return _ROLES.get(session.get("user"))

def _is_allowed(path: str) -> bool:
    role    = _current_role()
    prefixes = ff.route_prefixes(role)
    if prefixes is None:          # None = unrestricted (top-tier role)
        return True
    allowed = prefixes
    if allowed is None:
        return True
    return any(path == p or (p != "/" and path.startswith(p.rstrip("/") + "/")) for p in allowed)

# ── Brute-force protection: max 10 attempts per IP per 10 minutes ─────────────

_login_attempts: dict = {}   # {ip: [timestamp, ...]}
_MAX_ATTEMPTS   = 10
_WINDOW_SECS    = 600

def _is_rate_limited(ip: str) -> bool:
    now  = time.time()
    hits = [t for t in _login_attempts.get(ip, []) if now - t < _WINDOW_SECS]
    _login_attempts[ip] = hits
    return len(hits) >= _MAX_ATTEMPTS

def _record_attempt(ip: str):
    _login_attempts.setdefault(ip, []).append(time.time())

# ── Auth guard ────────────────────────────────────────────────────────────────

@app.before_request
def require_login():
    if request.path == "/login" or request.path.startswith("/static"):
        return
    if not session.get("user"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return redirect(url_for("login", next=request.path))
    if not _is_allowed(request.path):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Forbidden"}), 403
        return render_template("403.html", role=_current_role()), 403


# ── Login / logout routes ─────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip       = request.remote_addr
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        if _is_rate_limited(ip):
            error = "Too many failed attempts. Try again in 10 minutes."
        else:
            pwd_hash = _USERS.get(username)
            if pwd_hash and check_password_hash(pwd_hash, password):
                session.permanent = True
                session["user"]   = username
                next_url = request.args.get("next") or "/"
                # Safety: only allow relative redirects
                if not next_url.startswith("/"):
                    next_url = "/"
                return redirect(next_url)
            else:
                _record_attempt(ip)
                # Constant-time delay to blunt timing attacks
                time.sleep(0.5)
                error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/me")
def api_me():
    user = session.get("user")
    role = _ROLES.get(user)
    return jsonify({"user": user, "role": role})

DEFAULTS = {
    "ticker": WATCHLIST[0],
    "period": "3y",
    "width": WIDTH,
    "credit_min_pct": rules.CREDIT_MIN_CREDIT_PCT_OF_WIDTH * 100,
    "dte": DTE,
}


def clean(v):
    import numpy as np
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (np.floating,)):
        return None if math.isnan(float(v)) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, list):
        return [clean(i) for i in v]
    if isinstance(v, dict):
        return {k: clean(val) for k, val in v.items()}
    return v


@app.route("/")
def index():
    live_params = [
        {"key": k, "default": v, "description": PARAM_INFO[k][1]}
        for k, v in DEFAULT_PARAMS.items()
    ]
    return render_template("index.html", watchlist=WATCHLIST, defaults=DEFAULTS, live_params=live_params)


@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html", watchlist=WATCHLIST, defaults=DEFAULTS)


@app.route("/ml-admin")
def ml_admin_page():
    return render_template("ml_admin.html")


@app.route("/api/backtest")
def api_backtest():
    ticker = request.args.get("ticker", DEFAULTS["ticker"])
    period = request.args.get("period", DEFAULTS["period"])
    width = float(request.args.get("width", DEFAULTS["width"]))
    credit_min_pct = float(request.args.get("credit_min_pct", DEFAULTS["credit_min_pct"])) / 100
    dte = int(request.args.get("dte", DEFAULTS["dte"]))

    try:
        df, skip_nt, skip_f = run_backtest(ticker, period=period, credit_min_pct=credit_min_pct,
                                            width=width, dte=dte)
    except Exception as e:
        return jsonify({"error": str(e)})

    if df.empty:
        return jsonify({
            "summary": {
                "trades_taken": 0, "skipped_no_trade": skip_nt, "skipped_filter": skip_f,
                "win_rate": "-", "avg_win": "-", "avg_loss": "-", "expectancy": "-",
            },
            "by_structure": [],
        })

    win_rate = df["win"].mean() * 100
    avg_win = df.loc[df["win"], "pnl"].mean() if df["win"].any() else 0
    avg_loss = df.loc[~df["win"], "pnl"].mean() if (~df["win"]).any() else 0
    expectancy = df["pnl"].mean()

    by_structure = []
    grouped = df.groupby("structure").agg(
        n=("pnl", "size"), win_rate=("win", "mean"), avg_pnl=("pnl", "mean")
    )
    for structure, row in grouped.iterrows():
        by_structure.append({
            "structure": structure,
            "n": int(row["n"]),
            "win_rate": f"{row['win_rate']*100:.1f}%",
            "avg_pnl": f"${row['avg_pnl']:.3f}",
        })

    structure_win_rates = (df.groupby("structure")["win"].mean() * 100).to_dict()

    top_trades = []
    for _, row in df.sort_values("pnl", ascending=False).head(3).iterrows():
        top_trades.append({
            "date": str(row["date"]),
            "structure": row["structure"],
            "details": row["details"],
            "pnl": f"${row['pnl']:.3f}",
            "win": bool(row["win"]),
            "flags": row["flags"],
            "structure_win_rate": f"{structure_win_rates[row['structure']]:.1f}%",
        })

    flag_counts = (df.loc[df["flags"] != "-", "flags"]
                   .str.split(", ").explode().value_counts())
    flags_summary = [{"flag": k, "count": int(v)} for k, v in flag_counts.items()]

    return jsonify({
        "summary": {
            "trades_taken": len(df),
            "skipped_no_trade": skip_nt,
            "skipped_filter": skip_f,
            "win_rate": f"{win_rate:.1f}%",
            "avg_win": f"${avg_win:.3f}",
            "avg_loss": f"${avg_loss:.3f}",
            "expectancy": f"${expectancy:.3f}",
        },
        "by_structure": by_structure,
        "top_trades": top_trades,
        "flags_summary": flags_summary,
    })


def compute_hedge(candidate):
    """Suggest a hedge for a candidate using the structure registry.
    Returns None for unrecognised structures.
    Costs are estimates — always verify with live chain.
    """
    from config.structures import get_or_none
    s          = candidate.get("structure", "")
    st         = get_or_none(s)
    if st is None:
        return None

    max_profit = candidate.get("max_profit") or 0
    max_loss   = candidate.get("max_loss")   or 0
    net_delta  = candidate.get("net_delta")  or 0
    hd         = st.hedge

    base = max_profit if hd.cost_base == "max_profit" else max_loss
    cost = max(round(base * hd.cost_pct, 2), 0)

    combined_max_profit = round(max_profit - cost, 2)
    combined_delta      = round(net_delta + hd.delta_change, 3)

    if s == "Jade Lizard":
        combined_max_loss      = None
        combined_max_loss_note = "Max loss becomes DEFINED = put spread width − net credit. Verify strike width with live chain."
    else:
        combined_max_loss      = round(max_loss + cost, 2)
        combined_max_loss_note = None

    return {
        "hedge_structure":         hd.structure,
        "hedge_details":           hd.details,
        "rationale":               hd.rationale,
        "protection_note":         hd.protection_note,
        "hedge_cost_per_share":    cost,
        "hedge_cost_per_contract": round(cost * 100, 2),
        "combined_max_profit":     combined_max_profit,
        "combined_max_loss":       combined_max_loss,
        "combined_max_loss_note":  combined_max_loss_note,
        "combined_delta":          combined_delta,
        "cost_note":               "Cost is an estimate (typical OTM premium ratio for this structure). Verify with live options chain before trading.",
    }


def build_top_trades(rows, n=3, exclude=None):
    """Score and rank candidates across all tickers; return top-n.

    Ranking is done entirely inside rank_candidates (composite 0-100 score,
    best-per-ticker, then top-n tickers). See scripts/candidate_ranker.py.
    """
    from scripts.candidate_ranker import rank_candidates

    # Filter excluded ticker:structure pairs (session-side exclusions from UI toggle)
    _excluded = set(exclude or [])
    if _excluded:
        rows = [r for r in rows if not any(
            f"{r['ticker']}:{c['structure']}" in _excluded
            for c in r.get("candidates", [])
        )]

    ranked = rank_candidates(rows, n=n)

    # Run Monte Carlo per ranked trade (GARCH engine when available) and merge
    # p10_pnl / p90_pnl / ev_per_share into pred_dist. This is the only place
    # we have both the candidate (strikes, structure) and the ML row together.
    from scripts.monte_carlo import run_mc as _run_mc
    for item in ranked:
        row, c = item["row"], item["candidate"]
        try:
            mc_out = _run_mc(row.get("ticker"), row, c)
            if mc_out:
                ml = row.get("ml") or {}
                pd_obj = ml.get("pred_dist")
                if pd_obj is not None:
                    pd_obj["p10_pnl"]      = mc_out.get("p10_pnl")
                    pd_obj["p90_pnl"]      = mc_out.get("p90_pnl")
                    pd_obj["ev_per_share"] = mc_out.get("expected_pnl")
                    pd_obj["vol_source"]   = mc_out.get("vol_source")
                item["mc"] = mc_out
        except Exception:
            item["mc"] = None

    candidates = []
    for item in ranked:
        row, c = item["row"], item["candidate"]
        candidates.append({
            "ticker":          row["ticker"],
            "structure":       c["structure"],
            "details":         c["details"],
            "expiry":          row.get("expiry"),
            "dte":             row.get("dte"),
            "pop":             c.get("pop"),
            "ev":              item["ev"],
            "ev_is_proxy":     item["ev_is_proxy"],
            "max_profit":      c.get("max_profit"),
            "max_loss":        c.get("max_loss"),
            "profit_target":   c.get("profit_target"),
            "capital_required":c.get("capital_required"),
            "meets_min_profit":c.get("meets_min_profit"),
            "meets_max_loss":  c.get("meets_max_loss"),
            "meets_both":      item["meets_both"],
            "signal_score":    c.get("signal_score") or 0,
            "signal_rating":   row.get("signal_rating", "Neutral"),
            "signal_notes":    row.get("signal_notes", []),
            "news_sentiment":  row.get("news_sentiment", "Neutral"),
            "news_headlines":  row.get("news_headlines", []),
            "news_bullish":    row.get("news_bullish", 0),
            "news_bearish":    row.get("news_bearish", 0),
            "adx":             row.get("adx"),
            "rel_volume":      row.get("rel_volume"),
            "pcr":             row.get("pcr"),
            "pcr_sentiment":   row.get("pcr_sentiment"),
            "unusual_activity":row.get("unusual_activity", False),
            "net_delta":       c.get("net_delta"),
            "net_theta":       c.get("net_theta"),
            "net_gamma":       c.get("net_gamma"),
            "net_vega":        c.get("net_vega"),
            "gamma_penalty":   c.get("gamma_penalty", 0.0),
            "div_warning":     c.get("div_warning", False),
            "div_penalty":     c.get("div_penalty", 0.0),
            "is_credit":       c.get("is_credit", True),
            "ema200":          row.get("ema200"),
            "ema200_position": row.get("ema200_position"),
            "iv_term_shape":   row.get("iv_term_shape"),
            "iv_term_note":    row.get("iv_term_note"),
            "iv_front_iv":     row.get("iv_front_iv"),
            "iv_back_iv":      row.get("iv_back_iv"),
            "iv_edge_pct":     row.get("iv_edge_pct"),
            "ai_assessment":   row.get("ai_assessment"),
            "ai_confidence":   row.get("ai_confidence"),
            "ai_provider":     row.get("ai_provider"),
            "long_strike":       c.get("long_strike"),
            "short_strike":      c.get("short_strike"),
            "put_long_strike":   c.get("put_long_strike"),
            "put_short_strike":  c.get("put_short_strike"),
            "call_short_strike": c.get("call_short_strike"),
            "call_long_strike":  c.get("call_long_strike"),
            "spot":              row.get("spot"),
            "spot_at_entry":     c.get("spot_at_entry") or row.get("spot"),
            "market_bias":       c.get("market_bias"),
            "short_interest":    row.get("short_interest"),
            "vol_skew_pct":      row.get("vol_skew_pct"),
            "div_ex_date":       row.get("div_ex_date"),
            "div_days_to_ex":    row.get("div_days_to_ex"),
            "div_yield":         row.get("div_yield"),
            "price_change":      row.get("price_change"),
            "price_change_pct":  row.get("price_change_pct"),
            "hv20":              row.get("hv20"),
            "iv_premium":        row.get("iv_premium"),
            "iv_hv_ratio":       row.get("iv_hv_ratio"),
            "analyst_label":     row.get("analyst_label"),
            "analyst_buy":       row.get("analyst_buy"),
            "analyst_hold":      row.get("analyst_hold"),
            "analyst_sell":      row.get("analyst_sell"),
            "analyst_net_score": row.get("analyst_net_score"),
            "risk_free_rate":    row.get("risk_free_rate"),
            "ml":                row.get("ml"),
            "meta_score":        (row.get("ml") or {}).get("meta_score"),
            "pred_dist":         (row.get("ml") or {}).get("pred_dist"),
            "mc":                item.get("mc"),
        })
    top = candidates[:n]
    row_by_ticker = {r["ticker"]: r for r in rows}

    # Call AI for the final top-N concurrently (max 3 API calls, run in parallel)
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FT
    with ThreadPoolExecutor(max_workers=3) as _ai_pool:
        _ai_futs = {}
        for t in top:
            row = row_by_ticker.get(t["ticker"], {})
            ai_ctx = {**row, "recommended_structure": t.get("structure")}
            _ai_futs[id(t)] = (t, _ai_pool.submit(get_ai_assessment, t["ticker"], ai_ctx))
        from config.structures import get_or_none as _get_st
        for t, fut in _ai_futs.values():
            try:
                ai = fut.result(timeout=20)
            except _FT:
                ai = {"assessment": None, "confidence": None, "provider": None, "error": "timeout"}
            except Exception as _e:
                ai = {"assessment": None, "confidence": None, "provider": None, "error": str(_e)}
            t["ai_assessment"] = ai["assessment"]
            t["ai_confidence"]  = ai["confidence"]
            t["ai_provider"]    = ai["provider"]
            t["ai_error"]       = ai["error"]
            _st = _get_st(t.get("structure", ""))
            if _st:
                t["pnl_fn"]        = _st.expiry_pnl_fn
                t["hedge_urgency"] = _st.hedge.urgency
            t["hedge"]          = compute_hedge(t)
            # Exact hedge from live chain
            ex = pfp.get_hedge_exact(t, t.get("ticker"))
            if ex and not ex.get("error"):
                mp  = t.get("max_profit")
                ml  = t.get("max_loss")
                cps = ex.get("cost_per_share", 0)
                ex["primary_max_profit_ps"]  = mp
                ex["primary_max_loss_ps"]    = ml
                ex["combined_max_profit_ps"] = round(mp - cps, 4) if mp is not None else None
                combined_ml = round(ml + cps, 4) if ml is not None else None
                ex["combined_max_loss_ps"]   = combined_ml
                # Flag when adding hedge cost tips a passing trade over its max-loss gate
                ex["hedge_exceeds_max_loss"] = (
                    combined_ml is not None and ml is not None and combined_ml > ml
                    and t.get("meets_max_loss") is True
                )
            t["hedge_exact"] = ex

        # Attach capital / balance check to every top trade for UI warning badge
        try:
            from scripts.candidate_provider import check_balance_for_candidate as _chk_bal
            from scripts.etrade_client import get_account_balance as _get_bal
            _bal = _get_bal()
            _buying_power = (_bal or {}).get("buying_power", 0.0)
        except Exception:
            _buying_power = 0.0
        for t in top:
            try:
                t["capital_check"] = _chk_bal(t, _buying_power)
            except Exception:
                t["capital_check"] = None

    # Record every scan for offline evaluation (Precision@k, NDCG, calibration)
    try:
        from scripts.offline_eval import record_scan as _record_scan
        _record_scan(ranked)
    except Exception as _e:
        log.warning(f"[eval] record_scan failed: {_e}")

    return top


@app.route("/api/market-context")
def api_market_context():
    if not ff.allowed("market_context", _current_role()):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    try:
        cache_secs = ff.get("market_context", "cache_seconds", default=300)
        ctx = mc.get_market_context(
            future_tickers = ff.get("market_context_index_futures", "tickers"),
            future_labels  = ff.get("market_context_index_futures", "labels"),
            sector_tickers = ff.get("market_context_sector_etfs",   "tickers"),
            vix_ticker     = ff.get("market_context_vix",           "ticker", default="^VIX"),
            cache_seconds  = cache_secs,
        )
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analyze")
def api_analyze():
    import json as _json

    overrides = {k: request.args.get(k) for k in DEFAULT_PARAMS if request.args.get(k) not in (None, "")}
    tickers_param = request.args.get("tickers", "").strip()
    run_list = [t.strip().upper() for t in tickers_param.split(",") if t.strip()] if tickers_param else WATCHLIST
    # Session-side exclusions: "AAPL:Put Credit Spread,TSLA:Naked Put"
    _exclude_param = request.args.get("exclude", "").strip()
    _exclude = [x.strip() for x in _exclude_param.split(",") if x.strip()] if _exclude_param else []

    # Capture role NOW (inside request context) — generator runs after context teardown
    from config.roles import get_structures_for_role as _gsfr
    from config.structures import ALL_STRUCTURES as _ALL_ST
    _role          = _current_role()
    _etrade_allowed = ff.allowed("etrade_data_access", _role)
    _allowed_structs = set(_gsfr(_role)) if _role else set(_ALL_ST)

    # Grab cached market context to attach sector tags (best-effort, non-blocking)
    mkt_ctx = None
    if ff.allowed("market_context_per_ticker_tag", _role):
        try:
            mkt_ctx = mc.get_market_context(cache_seconds=ff.get("market_context", "cache_seconds", default=300))
        except Exception:
            pass

    # Detect market regime once — shared across the whole scan session
    _regime = sc.detect_regime(mkt_ctx)

    def generate():
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
        _pool = ThreadPoolExecutor(max_workers=3)  # 3 tickers fetched concurrently
        try:
            yield from _generate_inner(_pool, run_list, overrides, _regime,
                                       _etrade_allowed, mkt_ctx, _allowed_structs,
                                       _exclude)
        except Exception as _gen_err:
            app.logger.error(f"/api/analyze generator crashed: {_gen_err}")
            payload = _json.dumps({'type': 'done', 'top_trades': [], 'total': 0,
                                   'error': str(_gen_err)})
            yield f"data: {payload}\n\n"
        finally:
            _pool.shutdown(wait=False)

    def _generate_inner(_pool, run_list, overrides, _regime,
                        _etrade_allowed, mkt_ctx, _allowed_structs, _exclude=None):
        from concurrent.futures import TimeoutError as _FuturesTimeout

        def _run_ticker(t):
            from scripts.data_fetch import set_force_yfinance
            set_force_yfinance(not _etrade_allowed)
            try:
                return analyze_ticker(t, overrides, regime=_regime)
            finally:
                set_force_yfinance(False)

        # Read ML predictions from the module-level cache (refreshed hourly by the
        # scheduler and once at startup). All tickers get the same cache snapshot —
        # no race condition, no early tickers missing data.
        from scripts.ml_cache import ml_cache as _mlc
        _ml_snapshot = _mlc.get_all()
        if not _ml_snapshot:
            # Cache cold (startup, first deploy, or models not yet trained).
            # Run predictions synchronously now so this scan has ML data.
            app.logger.info("ml_cache cold — running synchronous ML predictions for this scan")
            try:
                from scripts.regime_predictor import predict_all as _pa
                _pr = _pa(run_list)
                _ml_snapshot = {p["ticker"]: p for p in _pr.get("predictions", []) if p.get("ok")}
                app.logger.info("ml_cache fallback filled %d tickers", len(_ml_snapshot))
            except Exception as _mle:
                app.logger.warning("Synchronous ML fallback failed: %s", _mle)
                _ml_snapshot = {}

        # Pre-submit all ticker futures so max_workers=3 can overlap fetches.
        # Results are consumed in original order to preserve streaming order.
        _futures = {ticker: _pool.submit(_run_ticker, ticker) for ticker in run_list}

        rows = []
        for ticker in run_list:
            try:
                future = _futures[ticker]
                # Poll with keepalive pings so the SSE connection stays alive through slow tickers
                row = None
                deadline = 90  # total seconds per ticker
                waited   = 0
                while waited < deadline:
                    try:
                        row = future.result(timeout=5)
                        break
                    except _FuturesTimeout:
                        waited += 5
                        yield ": keepalive\n\n"   # SSE comment — browser ignores, proxy resets idle timer
                if row is None:
                    future.cancel()
                    row = {"ticker": ticker, "status": "TIMEOUT - analysis exceeded 90s", "candidates": []}
            except Exception as e:
                row = {"ticker": ticker, "status": f"ERROR - {e}", "candidates": []}

            # Filter to structures allowed for this role (captured before generator start)
            row["candidates"] = [
                c for c in row.get("candidates", [])
                if c.get("structure") in _allowed_structs
            ]

            row["ml"] = _ml_snapshot.get(ticker)

            if mkt_ctx:
                # Attach sector tag to signal_notes
                tag = mc.get_sector_tag(ticker, mkt_ctx)
                if tag:
                    row["signal_notes"] = list(row.get("signal_notes") or []) + [tag]

                # Assemble complete per-candidate score:
                #   technical (compute_signal_alignment, row-level)
                # + market_ctx bias (compute_market_bias, per-candidate via regime weights)
                # + signal_score_adj (phase-B penalties: gamma, dividend — per-candidate)
                scored = []
                for c in row.get("candidates", []):
                    bias = mc.compute_market_bias(c, ticker, mkt_ctx, regime=_regime)
                    c = dict(c)
                    c["market_bias"]  = bias
                    c["signal_score"] = round(
                        (row.get("signal_score") or 0)
                        + bias["score"]
                        + (c.get("signal_score_adj") or 0),
                        4
                    )
                    c["decision"] = decision_provider.evaluate_candidate(row, c)
                    scored.append(c)
                row["candidates"] = scored

            # Attach hedge to the recommended candidate so the per-ticker Hedge tab has data
            from config.structures import get_or_none as _get_st
            cands = row.get("candidates", [])
            for c in cands:
                if c.get("recommended"):
                    _st = _get_st(c.get("structure", ""))
                    if _st:
                        c["pnl_fn"]       = _st.expiry_pnl_fn
                        c["hedge_urgency"] = _st.hedge.urgency
                    c["hedge"] = compute_hedge(c)
                    ex = pfp.get_hedge_exact(c, ticker)
                    if ex and not ex.get("error"):
                        mp, ml = c.get("max_profit"), c.get("max_loss")
                        cps = ex.get("cost_per_share", 0) or 0
                        ex["primary_max_profit_ps"]  = mp
                        ex["primary_max_loss_ps"]    = ml
                        ex["combined_max_profit_ps"] = round(mp - cps, 4) if mp is not None else None
                        ex["combined_max_loss_ps"]   = round(ml + cps, 4) if ml is not None else None
                    c["hedge_exact"] = ex
                    break

            cleaned = {k: clean(v) for k, v in row.items() if k != "candidates"}
            cleaned["candidates"] = [{k: clean(v) for k, v in c.items()} for c in cands]
            rows.append(cleaned)
            yield f"data: {_json.dumps({'type': 'ticker', 'row': cleaned})}\n\n"

        # Run build_top_trades (AI calls) in background; yield keepalives so nginx
        # doesn't drop the SSE connection during the up-to-60s AI wait.
        _top_fut = _pool.submit(build_top_trades, rows, exclude=_exclude)
        _top_result = []
        while True:
            try:
                _top_result = _top_fut.result(timeout=5)
                break
            except _FuturesTimeout:
                yield ": keepalive\n\n"
            except Exception as _top_err:
                app.logger.error(f"build_top_trades failed: {_top_err}")
                break
        try:
            top_trades = [{k: clean(v) for k, v in t.items()} for t in _top_result]
            payload = _json.dumps({'type': 'done', 'top_trades': top_trades, 'total': len(rows)})
        except Exception as e:
            payload = _json.dumps({'type': 'done', 'top_trades': [], 'total': len(rows), 'error': str(e)})
        yield f"data: {payload}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/positions", methods=["GET"])
def api_get_positions():
    try:
        positions = pt.load()
        return jsonify({
            "positions": positions,
            "summary":   pt.portfolio_summary(positions),
            "warnings":  pt.check_risk_limits(positions),
        })
    except Exception as e:
        return jsonify({"error": str(e), "positions": [], "summary": {}, "warnings": []}), 500


@app.route("/api/positions", methods=["POST"])
def api_add_position():
    data = request.json or {}
    try:
        pos = pt.add(
            ticker           = data["ticker"],
            structure        = data["structure"],
            expiry           = data["expiry"],
            entry_value      = float(data["entry_value"]),
            max_profit       = data.get("max_profit"),
            max_loss         = data.get("max_loss"),
            capital_required = data.get("capital_required"),
            is_credit        = bool(data.get("is_credit", True)),
            contracts        = int(data.get("contracts", 1)),
            details          = data.get("details", ""),
            net_delta        = data.get("net_delta"),
            net_theta        = data.get("net_theta"),
        )
        return jsonify({"ok": True, "position": pos})
    except KeyError as e:
        return jsonify({"ok": False, "error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/positions/<pos_id>/close", methods=["POST"])
def api_close_position(pos_id):
    data = request.json or {}
    close_value = float(data.get("close_value", 0))
    pt.close_position(pos_id, close_value)
    return jsonify({"ok": True})


@app.route("/api/positions/<pos_id>/expire", methods=["POST"])
def api_expire_position(pos_id):
    pt.expire_position(pos_id)
    return jsonify({"ok": True})


# ── E*TRADE OAuth + account routes ───────────────────────────────────────────

@app.route("/api/etrade/status")
def etrade_status():
    authenticated = etrade.is_authenticated()
    balance = etrade.get_account_balance() if authenticated else None
    return jsonify({
        "authenticated": authenticated,
        "configured":    bool(etrade._CONSUMER_KEY),
        "sandbox":       etrade._SANDBOX,
        "balance":       balance,
    })


@app.route("/api/etrade/login")
def etrade_login():
    """Step 1: get request token and return the authorization URL to the browser."""
    try:
        req = etrade.get_request_token()
        # Stash the request token secret in the Flask session so the callback can use it
        session["et_token"]        = req["oauth_token"]
        session["et_token_secret"] = req["oauth_token_secret"]
        return jsonify({
            "ok":            True,
            "authorize_url": req["authorize_url"],
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/etrade/callback")
def etrade_callback():
    """
    Step 3: E*TRADE redirects here after the user authorizes.
    Exchanges the verifier for an access token, then redirects back to the main page.
    """
    verifier = request.args.get("oauth_verifier") or request.args.get("verifier")
    if not verifier:
        return "Missing verifier — please try again.", 400
    try:
        et_token        = session.pop("et_token", "")
        et_token_secret = session.pop("et_token_secret", "")
        etrade.get_access_token(et_token, et_token_secret, verifier)
        return redirect("/?etrade=connected")
    except Exception as exc:
        return f"E*TRADE auth failed: {exc}", 500


@app.route("/api/etrade/logout", methods=["POST"])
def etrade_logout():
    etrade.logout()
    return jsonify({"ok": True})


@app.route("/api/etrade/positions")
def etrade_positions():
    """Live positions from E*TRADE account (read-only)."""
    if not etrade.is_authenticated():
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    positions = etrade.get_positions()
    return jsonify({"ok": True, "positions": positions or []})


# ── Live Position Files ───────────────────────────────────────────────────────

_LIVE_POS_DIR = Path(os.path.join(os.path.dirname(__file__), "..", "data", "live_position"))


@app.route("/live-positions")
def live_positions_page():
    return render_template("live_positions.html")


@app.route("/api/live-position-files")
def api_live_position_files():
    _LIVE_POS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in _LIVE_POS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in (".csv", ".tsv", ".txt"):
            st = f.stat()
            files.append({
                "name":     f.name,
                "size":     st.st_size,
                "modified": st.st_mtime,
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify({"files": files})


@app.route("/api/analyze-position-file")
def api_analyze_position_file():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify({"ok": False, "error": "No file specified"}), 400
    # Safety: prevent path traversal
    file_path = (_LIVE_POS_DIR / filename).resolve()
    if not str(file_path).startswith(str(_LIVE_POS_DIR.resolve())):
        return jsonify({"ok": False, "error": "Invalid path"}), 400
    if not file_path.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404
    try:
        groups  = pfp.parse_upload(str(file_path))
        results = pfp.analyze_groups(groups)
        for group in results:
            ticker = group.get("ticker")
            for spread in group["spreads"]:
                hc = spread.pop("_hedge_candidate", {})
                spread["hedge"] = compute_hedge(hc) if hc.get("structure") else None
                # Exact hedge from live options chain
                ex = pfp.get_hedge_exact(spread, ticker) if ticker else None
                if ex and not ex.get("error"):
                    mp  = spread.get("max_profit_ps")
                    ml  = spread.get("max_loss_ps")
                    cps = ex.get("cost_per_share", 0)
                    ex["primary_max_profit_ps"]  = mp
                    ex["primary_max_loss_ps"]    = ml
                    ex["combined_max_profit_ps"] = round(mp - cps, 4) if mp is not None else None
                    ex["combined_max_loss_ps"]   = round(ml + cps, 4) if ml is not None else None
                spread["hedge_exact"] = ex
        return jsonify({"ok": True, "groups": results, "filename": filename})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/etrade-positions")
def api_etrade_positions():
    """Fetch live positions from E*TRADE and return the same groups format as CSV analysis."""
    if not etrade.is_authenticated():
        return jsonify({"ok": False, "error": "Not authenticated with E*TRADE. Please log in first.", "auth_required": True}), 401
    try:
        raw_positions = etrade.get_positions()
        if raw_positions is None:
            return jsonify({"ok": False, "error": "E*TRADE returned no data — check connection or token."}), 502

        groups = pfp.positions_to_groups(raw_positions)
        pfp.inject_underlying_prices(groups)
        results = pfp.analyze_groups(groups)

        # Enrich with hedge data (same as CSV route)
        for group in results:
            ticker = group.get("ticker")
            for spread in group["spreads"]:
                hc = spread.pop("_hedge_candidate", {})
                spread["hedge"] = compute_hedge(hc) if hc.get("structure") else None
                ex = pfp.get_hedge_exact(spread, ticker) if ticker else None
                if ex and not ex.get("error"):
                    mp  = spread.get("max_profit_ps")
                    ml  = spread.get("max_loss_ps")
                    cps = ex.get("cost_per_share", 0)
                    ex["primary_max_profit_ps"]  = mp
                    ex["primary_max_loss_ps"]    = ml
                    ex["combined_max_profit_ps"] = round(mp - cps, 4) if mp is not None else None
                    ex["combined_max_loss_ps"]   = round(ml + cps, 4) if ml is not None else None
                spread["hedge_exact"] = ex

        return jsonify({
            "ok":      True,
            "groups":  results,
            "source":  "etrade",
            "count":   len(raw_positions),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/decision", methods=["POST"])
def api_decision():
    """
    Single decision-scoring entrypoint for a HELD position (Live Positions
    or Paper Trades) — see scripts/decision_provider.py for why this re-runs
    the same alignment engine Live Suggestions uses for new candidates,
    rather than a parallel simplified score. The frontend supplies the
    analysis row it already fetched (no redundant re-analysis) plus the
    position-specific facts (P&L%, DTE, breakeven cushion, strike proximity)
    it already computed; this endpoint only does the scoring/verdict.
    """
    body     = request.get_json(silent=True) or {}
    row      = body.get("row") or {}
    position = body.get("position") or {}

    if not position.get("structure"):
        return jsonify({"ok": False, "error": "Missing position.structure"}), 400

    # Enrich row with ML prediction — cache first, live fallback if cache misses.
    ticker = (position.get("ticker") or row.get("ticker") or "").upper()
    if ticker:
        from scripts.ml_cache import ml_cache as _mlc
        ml = _mlc.get(ticker)
        if ml is None:
            # Cache miss (server just started or ticker not in watchlist) — fetch live.
            try:
                from scripts.regime_predictor import predict_ticker
                ml = predict_ticker(ticker)
                if not ml.get("ok"):
                    ml = None
            except Exception as _ml_err:
                app.logger.warning(f"ML predict_ticker({ticker}) failed: {_ml_err}")
                ml = None
        if ml:
            row = dict(row)
            row["ml"] = ml

    try:
        decision = decision_provider.evaluate_position(row, position)
        return jsonify({"ok": True, "decision": decision})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/candidate/enrich", methods=["POST"])
def api_candidate_enrich():
    """
    Monte Carlo outcome + Kelly sizing suggestion for a candidate Live
    Suggestions already has (from the existing /api/analyze SSE stream) —
    no re-analysis here, see scripts/candidate_provider.py for the math.
    """
    from scripts import candidate_provider as cand

    body      = request.get_json(silent=True) or {}
    row       = body.get("row") or {}
    candidate = body.get("candidate") or {}

    if not candidate.get("structure"):
        return jsonify({"ok": False, "error": "Missing candidate.structure"}), 400

    try:
        mc = cand.monte_carlo_outcome(row, candidate)
        kelly = cand.kelly_fraction(candidate.get("pop"), candidate.get("max_profit"), candidate.get("max_loss"))
        return jsonify({"ok": True, "monte_carlo": mc, "kelly_fraction": kelly})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/live-position-greeks-drift", methods=["POST"])
def api_live_position_greeks_drift():
    """
    Given a held position (ticker, structure, strikes, expiry, ul_price),
    return the IV/Greeks it had when first seen plus its current pricing, so
    the UI can show drift since entry. Prices the EXACT held legs (not the
    rulebook's newly-suggested structure) via position_snapshots.py.
    """
    from scripts import position_snapshots as ps

    position = request.get_json(silent=True) or {}
    if not position.get("ticker"):
        return jsonify({"ok": False, "error": "Missing position data"}), 400

    try:
        result = ps.get_entry_snapshot_and_drift(position)
        if result is None:
            return jsonify({"ok": False, "error": "Could not price this position's legs right now"}), 502
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades/greeks-drift/<trade_id>")
def api_paper_trade_greeks_drift(trade_id):
    """
    Same Greeks-drift-since-entry feature as Live Positions, generalized to
    Paper Trades — adapts the trade's stored strikes into the shared
    position_snapshots.py shape (using the trade's stable id as the
    snapshot key, instead of Live Positions' composite key).
    """
    from scripts import paper_trade_engine as pte
    from scripts import position_snapshots as ps

    trades = pte.load_trades()
    trade = next((t for t in trades if t["id"] == trade_id), None)
    if trade is None:
        return jsonify({"ok": False, "error": "Trade not found"}), 404

    ul_price = pte.fetch_underlying_price(trade["ticker"])
    if ul_price is None:
        return jsonify({"ok": False, "error": f"Could not fetch price for {trade['ticker']}"}), 502

    position = ps.paper_trade_to_position_shape(trade, ul_price)
    try:
        result = ps.get_entry_snapshot_and_drift(position)
        if result is None:
            return jsonify({"ok": False, "error": "Could not price this trade's legs right now"}), 502
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Portfolio Upload (kept for API compatibility) ─────────────────────────────

_UPLOAD_PATH = Path(os.path.join(os.path.dirname(__file__), "..", "data", "uploaded_positions.csv"))


@app.route("/portfolio-upload")
def portfolio_upload_page():
    return render_template("portfolio_upload.html")


@app.route("/api/upload-positions", methods=["POST"])
def api_upload_positions():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "No file selected"}), 400

    _UPLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(_UPLOAD_PATH))

    try:
        groups  = pfp.parse_upload(str(_UPLOAD_PATH))
        results = pfp.analyze_groups(groups)

        # Attach hedge suggestions
        for group in results:
            for spread in group["spreads"]:
                hc = spread.pop("_hedge_candidate", {})
                spread["hedge"] = compute_hedge(hc) if hc.get("structure") else None

        return jsonify({"ok": True, "groups": results, "filename": f.filename})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/uploaded-positions")
def api_get_uploaded_positions():
    if not _UPLOAD_PATH.exists():
        return jsonify({"ok": False, "error": "No file uploaded yet"}), 404
    try:
        groups  = pfp.parse_upload(str(_UPLOAD_PATH))
        results = pfp.analyze_groups(groups)
        for group in results:
            for spread in group["spreads"]:
                hc = spread.pop("_hedge_candidate", {})
                spread["hedge"] = compute_hedge(hc) if hc.get("structure") else None
        return jsonify({"ok": True, "groups": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Paper trade routes ────────────────────────────────────────────────────────

@app.route("/paper-trades")
def paper_trades_page():
    return render_template("paper_trades.html")


@app.route("/api/paper-trades/summary")
def api_paper_trades_summary():
    try:
        summary = pte.get_performance_summary()
        return jsonify({"ok": True, **summary})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


_scan_status = {}
_run_history  = []   # in-memory, seeded from disk on startup, newest last
_RUN_HISTORY_MAX  = 500
_RUN_HISTORY_FILE = Path(__file__).parent.parent / "data" / "scheduler_runs.jsonl"
_SCAN_TIMEOUT = 600

# ── Load persisted run history from disk ──────────────────────────────────────
def _load_run_history():
    try:
        if _RUN_HISTORY_FILE.exists():
            lines = _RUN_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
            loaded = []
            for ln in lines[-_RUN_HISTORY_MAX:]:
                try:
                    loaded.append(json.loads(ln))
                except Exception:
                    pass
            _run_history.extend(loaded)
            app.logger.info(f"Loaded {len(loaded)} scheduler run entries from disk")
    except Exception as e:
        app.logger.warning(f"Could not load scheduler run history: {e}")

def _append_run_history(entry):
    _run_history.append(entry)
    if len(_run_history) > _RUN_HISTORY_MAX:
        del _run_history[0]
    try:
        _RUN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _RUN_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # Trim file if it grows beyond 2× the cap
        lines = _RUN_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > _RUN_HISTORY_MAX * 2:
            _RUN_HISTORY_FILE.write_text(
                "\n".join(lines[-_RUN_HISTORY_MAX:]) + "\n", encoding="utf-8")
    except Exception as e:
        app.logger.warning(f"Could not persist run history entry: {e}")

_load_run_history()
# ─────────────────────────────────────────────────────────────────────────────

def _scan_is_running(key):
    import time
    s = _scan_status.get(key, {})
    if s.get("state") != "running":
        return False
    return (time.time() - s.get("started", 0)) < _SCAN_TIMEOUT

def _run_in_bg(key, fn, **kwargs):
    import threading, time, traceback
    from datetime import datetime, timezone
    def _worker():
        t0 = time.time()
        ts = datetime.now(timezone.utc).isoformat()
        try:
            result = fn(**kwargs)
            dur = round(time.time() - t0, 1)
            entry = {"job": key, "ts": ts, "state": "done", "duration_s": dur,
                     "summary": str(result)[:300] if result else ""}
            _scan_status[key] = {"state": "done", "result": result, "started": t0}
            app.logger.info(f"_run_in_bg({key}) done in {dur}s")
        except BaseException as e:
            dur = round(time.time() - t0, 1)
            tb  = traceback.format_exc()
            entry = {"job": key, "ts": ts, "state": "error", "duration_s": dur,
                     "error": str(e), "trace": tb[:1000]}
            _scan_status[key] = {"state": "error", "error": str(e), "trace": tb}
            app.logger.error(f"_run_in_bg({key}) failed in {dur}s: {e}\n{tb}")
        _append_run_history(entry)
    _scan_status[key] = {"state": "running", "started": time.time()}
    threading.Thread(target=_worker, daemon=True).start()


def _is_market_hours_now():
    """Mon-Fri 9:30-16:00 US/Eastern. No holiday calendar — acceptable for a
    training-data cadence (a stray run on a market holiday just yields no
    new candidates, it doesn't corrupt anything)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return (9, 30) <= (now.hour, now.minute) < (16, 0)


def _load_scheduler_config():
    """Read [scheduler] section from secrets.toml with safe defaults."""
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        _path = Path(__file__).parent.parent / "config" / "secrets.toml"
        cfg = tomllib.loads(_path.read_text(encoding="utf-8"))
        s = cfg.get("scheduler", {})
        return {
            "enabled":                  bool(s.get("enabled", True)),
            "collect_enabled":          bool(s.get("collect_enabled", True)),
            "collect_interval_minutes": int(s.get("collect_interval_minutes", 60)),
            "collect_hour_start":       int(s.get("collect_hour_start", 9)),
            "collect_hour_end":         int(s.get("collect_hour_end", 16)),
            "morning_scan_hour":        int(s.get("morning_scan_hour", 10)),
            "morning_scan_minute":      int(s.get("morning_scan_minute", 0)),
            "evening_check_hour":       int(s.get("evening_check_hour", 17)),
            "evening_check_minute":     int(s.get("evening_check_minute", 0)),
            "oi_open_hour":             int(s.get("oi_open_hour", 9)),
            "oi_open_minute":           int(s.get("oi_open_minute", 45)),
            "oi_close_hour":            int(s.get("oi_close_hour", 15)),
            "oi_close_minute":          int(s.get("oi_close_minute", 55)),
            "daily_archive_hour":       int(s.get("daily_archive_hour", 16)),
            "daily_archive_minute":     int(s.get("daily_archive_minute", 30)),
        }
    except Exception as e:
        app.logger.warning(f"Could not read [scheduler] from secrets.toml, using defaults: {e}")
        return {
            "enabled": True, "collect_enabled": True, "collect_interval_minutes": 60,
            "collect_hour_start": 9, "collect_hour_end": 16,
            "morning_scan_hour": 10, "morning_scan_minute": 0,
            "evening_check_hour": 17, "evening_check_minute": 0,
            "oi_open_hour": 9, "oi_open_minute": 45,
            "oi_close_hour": 15, "oi_close_minute": 55,
            "daily_archive_hour": 16, "daily_archive_minute": 30,
        }

_scheduler_cfg = _load_scheduler_config()


def _start_training_data_scheduler():
    """
    In-process interval-based collection during market hours + once-daily labeling.
    Interval and hour window are read from [scheduler] in secrets.toml.
    Guarded against Werkzeug's debug-mode reloader (which forks a watcher
    process) so the scheduler only starts once, not twice.
    """
    if not _scheduler_cfg["enabled"]:
        app.logger.info("Scheduler disabled via secrets.toml [scheduler] enabled=false")
        return None
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None  # reloader's parent watcher process — skip

    from apscheduler.schedulers.background import BackgroundScheduler
    from scripts import training_data_collector as tdc

    hour_start = _scheduler_cfg["collect_hour_start"]
    hour_end   = _scheduler_cfg["collect_hour_end"]
    interval   = _scheduler_cfg["collect_interval_minutes"]

    def _is_collect_window():
        from datetime import datetime
        import pytz
        now = datetime.now(pytz.timezone("America/New_York"))
        return now.weekday() < 5 and hour_start <= now.hour < hour_end

    def _interval_collect():
        if not _is_collect_window():
            return
        if _scan_is_running("training_collect"):
            return
        _run_in_bg("training_collect", tdc.collect_snapshots)
        from scripts.ml_cache import ml_cache as _mlc
        _mlc.refresh_async()

    def _daily_label():
        try:
            tdc.label_pending_snapshots()
        except Exception as e:
            app.logger.error(f"daily label_pending_snapshots failed: {e}")
        try:
            tdc.label_snapshots_with_forward_returns()
        except Exception as e:
            app.logger.error(f"daily label_snapshots_with_forward_returns failed: {e}")
        try:
            from scripts import feature_drift as fd
            fd.compute_drift_report()
        except Exception as e:
            app.logger.error(f"daily feature_drift report failed: {e}")
        try:
            from scripts import regime_backfill as rb
            rb.update_regime_dataset()
            rb.label_pending_regime_rows()
        except Exception as e:
            app.logger.error(f"daily regime dataset update/label failed: {e}")

    def _morning_scan():
        if _scan_is_running("morning_scan"):
            return
        try:
            _run_in_bg("morning_scan", pte.run_morning_scan)
        except Exception as e:
            app.logger.error(f"scheduled morning scan failed: {e}")

    def _afternoon_scan():
        if _scan_is_running("afternoon_scan"):
            return
        try:
            _run_in_bg("afternoon_scan", lambda: pte.run_morning_scan(scan_time="afternoon"))
        except Exception as e:
            app.logger.error(f"scheduled afternoon scan failed: {e}")

    def _run_evening_and_label():
        pte.run_evening_check()
        _daily_label()

    def _evening_check():
        if _scan_is_running("evening_check"):
            return
        _run_in_bg("evening_check", _run_evening_and_label)

    sched = BackgroundScheduler(daemon=True)
    if _scheduler_cfg["collect_enabled"]:
        # Compute start_date so the interval aligns with the last run, not "now".
        # This prevents restarts from shifting the collection schedule:
        # next_run = last_run + interval (keep adding interval until > now).
        import pytz as _pytz
        from datetime import timedelta as _td
        _et = _pytz.timezone("America/New_York")
        _now = datetime.now(_et)
        _interval_td = _td(minutes=interval)

        # Find last successful collect run from persisted history
        _last_collect_ts = None
        for entry in reversed(_run_history):
            if entry.get("job") in ("collect", "training_collect") and entry.get("state") == "done":
                try:
                    _last_collect_ts = datetime.fromisoformat(entry["ts"]).astimezone(_et)
                except Exception:
                    pass
                break

        def _next_window_open(from_dt):
            """Return the datetime of the next collect_hour_start on a weekday."""
            from datetime import timedelta as _td2
            candidate = from_dt.replace(hour=hour_start, minute=0, second=0, microsecond=0)
            if candidate <= from_dt:
                candidate += _td2(days=1)
            # Skip weekend
            while candidate.weekday() >= 5:
                candidate += _td2(days=1)
            return candidate

        if _last_collect_ts:
            _next = _last_collect_ts + _interval_td
            while _next <= _now:
                _next += _interval_td
            # If the computed next run is already outside the collect window,
            # don't schedule mid-evening or overnight — defer to next window open.
            if _next.hour >= hour_end or _next.hour < hour_start or _next.weekday() >= 5:
                _next = _next_window_open(_now)
                app.logger.info(f"Collect job: last run {_last_collect_ts.strftime('%H:%M')} ET — "
                                f"outside window, next run at window open {_next.strftime('%a %H:%M')} ET")
            else:
                app.logger.info(f"Collect job resuming from last run {_last_collect_ts.strftime('%H:%M')} ET "
                                f"— next run {_next.strftime('%H:%M')} ET")
            _start_date = _next
        else:
            # No prior run — if we're currently inside the window fire soon, else wait for next open
            if _is_collect_window():
                _start_date = _now + _interval_td
                app.logger.info(f"Collect job: no prior run found — first run in {interval}m")
            else:
                _start_date = _next_window_open(_now)
                app.logger.info(f"Collect job: no prior run, outside window — first run at {_start_date.strftime('%a %H:%M')} ET")

        sched.add_job(_interval_collect, "interval", minutes=interval,
                      id="collect", name=f"Data Collect ({interval}m)",
                      start_date=_start_date,
                      timezone="America/New_York",
                      max_instances=1,
                      misfire_grace_time=interval * 60 // 2)
        app.logger.info(f"Collect job scheduled every {interval}m, window {hour_start}:00-{hour_end}:00 ET")
    else:
        app.logger.info("Collect job disabled via secrets.toml collect_enabled=false")
    _sc = _scheduler_cfg
    sched.add_job(_morning_scan,  "cron",
                  hour=_sc.get("morning_scan_hour", 10), minute=_sc.get("morning_scan_minute", 0),
                  day_of_week="mon-fri", id="morning_scan", name="Morning Scan",
                  timezone="America/New_York")
    sched.add_job(_afternoon_scan, "cron",
                  hour=_sc.get("afternoon_scan_hour", 14), minute=_sc.get("afternoon_scan_minute", 0),
                  day_of_week="mon-fri", id="afternoon_scan", name="Afternoon Scan",
                  timezone="America/New_York")
    sched.add_job(_evening_check, "cron",
                  hour=_sc.get("evening_check_hour", 17), minute=_sc.get("evening_check_minute", 0),
                  day_of_week="mon-fri", id="evening_check", name="Evening Check",
                  timezone="America/New_York")

    # ── Tier 0 Data Flywheel archive jobs ──────────────────────────────────────
    from scripts import data_archive as _da

    def _oi_open():
        _run_in_bg("oi_open", lambda: _da.archive_oi_snapshot("open"))

    def _oi_close():
        _run_in_bg("oi_close", lambda: _da.archive_oi_snapshot("close"))

    def _daily_archive():
        _run_in_bg("daily_archive", _da.run_daily_archive)

    sched.add_job(_oi_open, "cron",
                  hour=_sc.get("oi_open_hour", 9), minute=_sc.get("oi_open_minute", 45),
                  day_of_week="mon-fri", id="oi_open", name="OI Snapshot (Open)",
                  timezone="America/New_York")
    sched.add_job(_oi_close, "cron",
                  hour=_sc.get("oi_close_hour", 15), minute=_sc.get("oi_close_minute", 55),
                  day_of_week="mon-fri", id="oi_close", name="OI Snapshot (Close)",
                  timezone="America/New_York")
    sched.add_job(_daily_archive, "cron",
                  hour=_sc.get("daily_archive_hour", 16), minute=_sc.get("daily_archive_minute", 30),
                  day_of_week="mon-fri", id="daily_archive", name="Daily Archive (T0-A/B/D)",
                  timezone="America/New_York")

    sched.start()
    app.logger.info(
        "Scheduler started — morning scan 10:00, afternoon scan 14:00, OI open 9:45, "
        "OI close 15:55, daily archive 16:30, evening check 17:00 ET"
    )
    return sched


_training_data_scheduler = _start_training_data_scheduler()

# Warm the ML cache at startup — only on the server (scheduler enabled).
# Skip locally: it fires yfinance + E*TRADE calls on every dev-server restart.
if _scheduler_cfg["enabled"]:
    from scripts.ml_cache import ml_cache as _startup_mlc
    _startup_mlc.refresh_async()


@app.route("/scheduler")
def scheduler_page():
    return render_template("scheduler.html", page="scheduler")


@app.route("/api/scheduler/status")
def api_scheduler_status():
    import time
    from scripts.ml_cache import ml_cache as _mlc
    from scripts.db import row_count, read_df, SNAPSHOTS_TABLE, CHAIN_TABLE, table_exists

    # APScheduler next run times
    jobs = {}
    if _training_data_scheduler:
        for job in _training_data_scheduler.get_jobs():
            nrt = job.next_run_time
            jobs[job.id] = {
                "next_run": nrt.isoformat() if nrt else None,
                "next_run_human": nrt.strftime("%a %b %d %I:%M %p %Z") if nrt else "paused",
            }

    # Last run status for each tracked job
    def _job_status(key):
        s = _scan_status.get(key, {"state": "idle"})
        started = s.get("started")
        age = round(time.time() - started, 0) if started else None
        return {
            "state":   s.get("state", "idle"),
            "started": started,
            "age_min": round(age / 60, 1) if age else None,
            "result":  s.get("result"),
            "error":   s.get("error"),
        }

    # ML cache
    ml_age = _mlc.age_seconds()

    # DuckDB counts
    try:
        regime_rows = row_count()
        snap_rows   = read_df(f"SELECT count(*) AS n FROM {SNAPSHOTS_TABLE}").iloc[0]["n"] if table_exists() else 0
        chain_rows  = read_df(f"SELECT count(*) AS n FROM {CHAIN_TABLE}").iloc[0]["n"] if table_exists() else 0
        labeled     = read_df(f"SELECT count(*) AS n FROM {SNAPSHOTS_TABLE} WHERE labeled = true").iloc[0]["n"] if table_exists() else 0
    except Exception:
        regime_rows = snap_rows = chain_rows = labeled = None

    return jsonify({
        "ok": True,
        "scheduler_enabled": _scheduler_cfg["enabled"],
        "scheduler_cfg": _scheduler_cfg,
        "scheduler_jobs": jobs,
        "job_status": {
            "morning_scan":      _job_status("morning_scan"),
            "evening_check":     _job_status("evening_check"),
            "training_collect":  _job_status("training_collect"),
            "regime_backfill":   _job_status("regime_backfill"),
            "train_models":      _job_status("train_models"),
            "oi_open":           _job_status("oi_open"),
            "oi_close":          _job_status("oi_close"),
            "daily_archive":     _job_status("daily_archive"),
        },
        "ml_cache": {
            "warm":      _mlc.is_warm(),
            "size":      _mlc.size(),
            "age_human": f"{int(ml_age//60)}m {int(ml_age%60)}s ago" if ml_age else "never",
        },
        "db": {
            "regime_rows": regime_rows,
            "snapshots":   int(snap_rows) if snap_rows is not None else None,
            "chain_snaps": int(chain_rows) if chain_rows is not None else None,
            "labeled":     int(labeled) if labeled is not None else None,
        },
    })


@app.route("/api/scheduler/pause/<job_id>", methods=["POST"])
def api_scheduler_pause(job_id):
    if not _training_data_scheduler:
        return jsonify({"ok": False, "error": "Scheduler not running"}), 400
    try:
        _training_data_scheduler.pause_job(job_id)
        return jsonify({"ok": True, "job_id": job_id, "state": "paused"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/scheduler/resume/<job_id>", methods=["POST"])
def api_scheduler_resume(job_id):
    if not _training_data_scheduler:
        return jsonify({"ok": False, "error": "Scheduler not running"}), 400
    try:
        _training_data_scheduler.resume_job(job_id)
        return jsonify({"ok": True, "job_id": job_id, "state": "resumed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/scheduler/logs")
def api_scheduler_logs():
    job_filter = request.args.get("job")
    logs = list(reversed(_run_history))   # newest first
    if job_filter:
        logs = [e for e in logs if e["job"] == job_filter]
    return jsonify({"ok": True, "logs": logs, "total": len(logs)})


@app.route("/api/mispricing/<ticker>")
def api_mispricing(ticker):
    from scripts.vol_surface import compute_mispricing, top_mispriced
    opt_type  = request.args.get("opt_type", "both")
    min_dte   = int(request.args.get("min_dte", 1))
    max_dte   = int(request.args.get("max_dte", 90))
    result    = compute_mispricing(ticker.upper(), opt_type=opt_type,
                                   min_dte=min_dte, max_dte=max_dte)
    if result.get("ok"):
        result["top_mispriced"] = top_mispriced(result)
    return jsonify(result)


@app.route("/api/mispricing/tickers")
def api_mispricing_tickers():
    """Return sorted list of tickers that have at least one chain snapshot."""
    from scripts.db import read_df, CHAIN_TABLE
    try:
        exists = read_df(
            f"SELECT count(*) AS n FROM information_schema.tables "
            f"WHERE table_name = '{CHAIN_TABLE}'"
        ).iloc[0]["n"] > 0
        if not exists:
            return jsonify({"ok": True, "tickers": [], "note": "No chain snapshot table yet"})
        df = read_df(f"SELECT DISTINCT ticker FROM {CHAIN_TABLE} ORDER BY ticker")
        tickers = df["ticker"].tolist()
        return jsonify({"ok": True, "tickers": tickers})
    except Exception as e:
        return jsonify({"ok": False, "tickers": [], "error": str(e)})


@app.route("/mispricing")
def mispricing_page():
    return render_template("mispricing.html", page="mispricing")


@app.route("/api/paper-trades/morning-scan", methods=["POST"])
def api_morning_scan():
    force = request.json.get("force", False) if request.is_json else False
    if _scan_is_running("morning_scan"):
        return jsonify({"ok": False, "running": True, "error": "Scan already in progress"})
    _run_in_bg("morning_scan", pte.run_morning_scan, force=force)
    return jsonify({"ok": True, "running": True, "message": "Morning scan started — refresh dashboard in ~2 minutes"})


@app.route("/api/paper-trades/morning-scan/status")
def api_morning_scan_status():
    s = _scan_status.get("morning_scan", {"state": "idle"})
    return jsonify(s)


@app.route("/api/paper-trades/evening-check", methods=["POST"])
def api_evening_check():
    force = request.json.get("force", False) if request.is_json else False
    try:
        result = pte.run_evening_check(force=force)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/training-data/collect", methods=["POST"])
def api_training_data_collect():
    """Hit by external cron every ~2h during market hours."""
    from scripts import training_data_collector as tdc
    if _scan_is_running("training_collect"):
        return jsonify({"ok": False, "running": True, "error": "Collection already in progress"})
    _run_in_bg("training_collect", tdc.collect_snapshots)
    return jsonify({"ok": True, "running": True, "message": "Snapshot collection started"})


@app.route("/api/training-data/collect/status")
def api_training_data_collect_status():
    s = _scan_status.get("training_collect", {"state": "idle"})
    return jsonify(s)


@app.route("/api/training-data/label", methods=["POST"])
def api_training_data_label():
    """Hit by external cron once a day (e.g. alongside evening check)."""
    from scripts import training_data_collector as tdc
    try:
        result = tdc.label_pending_snapshots()
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/training-data/backfill", methods=["POST"])
def api_training_data_backfill():
    """
    One-shot backfill of event calendar and cross-sectional rank features
    for all existing snapshots. Safe to call multiple times.
    """
    from scripts import training_data_collector as tdc
    try:
        result = tdc.backfill_snapshot_features()
        # Also run forward return labeling so older snapshots get labeled
        fwd_result = tdc.label_snapshots_with_forward_returns()
        return jsonify({"ok": True, "backfill": result, "forward_returns": fwd_result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/training-data/summary")
def api_training_data_summary():
    from scripts import training_data_collector as tdc
    try:
        return jsonify({"ok": True, **tdc.get_dataset_summary()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/eval/metrics")
def api_eval_metrics():
    """
    Offline scoring-pipeline evaluation metrics.
    Returns Precision@k, NDCG@k, score-return correlation, and calibration bins.
    Metrics are None until ≥5 paper trades have closed — the framework records
    data from the first scan even with zero closed trades.
    """
    from scripts.offline_eval import compute_metrics as _cm
    try:
        k = int(request.args.get("k", 3))
        return jsonify({"ok": True, **_cm(k=k)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/eval/save-metrics", methods=["POST"])
def api_eval_save_metrics():
    """Compute and persist eval_metrics.json — call from cron alongside daily label."""
    from scripts.offline_eval import save_metrics as _sm
    try:
        m = _sm()
        return jsonify({"ok": True, **m})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/training-data/backfill-regime", methods=["POST"])
def api_training_data_backfill_regime():
    """One-shot 2yr Yahoo backfill for the regime classifier. Slow (loops
    every backfill ticker's full history) — runs in the background, same
    pattern as morning-scan/training-data collection."""
    from scripts import regime_backfill as rb
    if _scan_is_running("regime_backfill"):
        return jsonify({"ok": False, "running": True, "error": "Backfill already in progress"})
    _run_in_bg("regime_backfill", rb.build_regime_dataset)
    return jsonify({"ok": True, "running": True, "message": "Regime backfill started"})


@app.route("/api/training-data/backfill-regime/status")
def api_training_data_backfill_regime_status():
    s = _scan_status.get("regime_backfill", {"state": "idle"})
    return jsonify(s)


@app.route("/api/training-data/train-models", methods=["POST"])
def api_train_models():
    """Train all ML models in dependency order. Runs in the background —
    poll /api/training-data/train-models/status to check completion."""
    if _scan_is_running("train_models"):
        return jsonify({"ok": False, "running": True, "error": "Training already in progress"})

    def _run_all_training():
        results = {}
        from scripts.train_regime_classifier import train as train_regime
        from scripts.train_return_classifier import train as train_return_clf
        from scripts.train_return_model import train as train_return_reg
        from scripts.train_volatility_model import train as train_vol
        from scripts.train_direction_model import train as train_direction
        from scripts.train_iv_direction_model import train as train_iv_direction
        from scripts.train_return_ranker import train as train_ranker
        from scripts.train_meta_ensemble import train as train_meta
        from scripts.train_anomaly_detector import train as train_anomaly
        # Order matters: base models before meta-ensemble
        for name, fn in [
            ("regime_classifier",      train_regime),
            ("return_classifier",      train_return_clf),   # used by meta-ensemble
            ("return_regressor",       train_return_reg),   # kept for backward compat
            ("volatility_regressor",   train_vol),
            ("direction_classifier",   train_direction),
            ("iv_direction_classifier", train_iv_direction),
            ("return_ranker",          train_ranker),
            ("meta_ensemble",          train_meta),         # depends on all above
            ("anomaly_detector",       train_anomaly),
        ]:
            try:
                results[name] = fn()
            except Exception as e:
                results[name] = {"ok": False, "error": str(e)}
        return results

    _run_in_bg("train_models", _run_all_training)
    return jsonify({"ok": True, "running": True, "message": "Model training started (9 models)"})


@app.route("/api/training-data/train-models/status")
def api_train_models_status():
    s = _scan_status.get("train_models", {"state": "idle"})
    return jsonify(s)


@app.route("/api/ml/audit")
def api_ml_audit():
    """Return calibration curves and Brier scores for all trained classifiers."""
    from scripts.model_audit import run_audit
    try:
        return jsonify(run_audit())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/ml/calibrate", methods=["POST"])
def api_ml_calibrate():
    """Calibrate all trained classifiers without retraining. Runs in background."""
    def _run():
        from scripts.calibrate_models import calibrate_all
        return calibrate_all()
    _run_in_bg("calibrate_models", _run)
    return jsonify({"ok": True, "running": True, "message": "Calibration started"})


@app.route("/api/archive/run", methods=["POST"])
def api_archive_run():
    """
    Manually trigger all Tier 0 archive jobs — useful for testing or catching up
    after a server restart. Runs in background; poll /api/scheduler/status for job state.
    """
    from scripts import data_archive as _da
    job = request.json.get("job", "all") if request.is_json else "all"

    if job == "vix":
        _run_in_bg("daily_archive", _da.archive_vix_term_structure)
    elif job == "intraday":
        _run_in_bg("daily_archive", _da.archive_intraday_bars)
    elif job == "oi":
        time_of_day = request.json.get("time_of_day", "close") if request.is_json else "close"
        bg_key = "oi_open" if time_of_day == "open" else "oi_close"
        _run_in_bg(bg_key, lambda: _da.archive_oi_snapshot(time_of_day))
    elif job == "earnings":
        _run_in_bg("daily_archive", _da.archive_earnings_iv)
    else:
        _run_in_bg("daily_archive", _da.run_daily_archive)

    return jsonify({"ok": True, "job": job, "status": "started"})


@app.route("/api/archive/status")
def api_archive_status():
    """Row counts for all four Tier 0 flywheel tables."""
    from scripts.db import connect, ensure_archive_tables
    ensure_archive_tables()
    counts = {}
    try:
        with connect() as con:
            for table in ("intraday_bars", "vix_term_structure", "oi_changes", "earnings_iv_tracker"):
                try:
                    counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                except Exception:
                    counts[table] = 0
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "counts": counts})


@app.route("/api/ml/cache/status")
def api_ml_cache_status():
    """Return age, size, and warm status of the ML prediction cache."""
    from scripts.ml_cache import ml_cache as _mlc
    age = _mlc.age_seconds()
    return jsonify({
        "ok": True,
        "warm": _mlc.is_warm(),
        "size": _mlc.size(),
        "age_seconds": round(age, 1) if age is not None else None,
        "age_human": (f"{int(age//60)}m {int(age%60)}s ago" if age is not None else "never refreshed"),
    })


@app.route("/api/ml/cache/refresh", methods=["POST"])
def api_ml_cache_refresh():
    """Manually trigger an ML cache refresh (runs synchronously — may take ~30s)."""
    from scripts.ml_cache import ml_cache as _mlc
    result = _mlc.refresh()
    return jsonify(result)


@app.route("/api/ml/predict")
def api_ml_predict():
    """Run live predictions for all WATCHLIST tickers using the trained models.
    Also refreshes the ML cache so Live Suggestions picks up fresh data immediately."""
    try:
        from scripts.regime_predictor import predict_all
        from scripts.ml_cache import ml_cache as _mlc
        result = predict_all()
        # Sync the cache with what we just computed so the next scan is instant.
        new_cache = {}
        for p in result.get("predictions", []):
            if p.get("ok"):
                new_cache[p["ticker"]] = p
        if new_cache:
            from datetime import datetime, timezone
            with _mlc._lock:
                _mlc._by_ticker = new_cache
                _mlc._refreshed_at = datetime.now(timezone.utc)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/ml/predict/<ticker>")
def api_ml_predict_ticker(ticker):
    try:
        from scripts.regime_predictor import predict_ticker
        result = predict_ticker(ticker.upper())
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades/live-marks")
def api_paper_trades_live_marks():
    import json as _json

    def generate():
        try:
            for trade_id, mark_data in pte.get_live_marks_iter():
                yield f"data: {_json.dumps({'id': trade_id, 'data': mark_data})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"
        yield f"data: {_json.dumps({'done': True})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/paper-trades/delete/<trade_id>", methods=["DELETE"])
def api_delete_paper_trade(trade_id):
    trades = pte.load_trades()
    before = len(trades)
    trades = [t for t in trades if t["id"] != trade_id]
    if len(trades) == before:
        return jsonify({"ok": False, "error": "Trade not found"}), 404
    pte.save_trades(trades)
    return jsonify({"ok": True})


_POS_ACTIONS_PATH = Path(os.path.join(os.path.dirname(__file__), "..", "data", "position_actions.json"))


_WATCHLIST_MAX = 50


@app.route("/api/watchlist")
def api_get_watchlist():
    return jsonify({
        "ok": True,
        "watchlist": WATCHLIST,
        "watchlist_archive_only": WATCHLIST_ARCHIVE,
        "max": _WATCHLIST_MAX,
    })


@app.route("/api/ticker-validate")
def api_ticker_validate():
    q = (request.args.get("q") or "").strip().upper()
    if not q:
        return jsonify({"ok": False, "error": "No ticker provided"}), 400
    try:
        import yfinance as yf
        t     = yf.Ticker(q)
        fi    = t.fast_info
        price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
        if not price:
            return jsonify({"ok": False, "error": f"No price data for {q}"}), 404
        try:
            name = t.info.get("shortName") or t.info.get("longName")
        except Exception:
            name = None
        return jsonify({"ok": True, "ticker": q, "price": round(float(price), 2), "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/record-position-action", methods=["POST"])
def api_record_position_action():
    import json as _json
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"ok": False, "error": "No JSON body"}), 400
    _POS_ACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    actions = []
    if _POS_ACTIONS_PATH.exists():
        try:
            actions = _json.loads(_POS_ACTIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            actions = []
    actions.append(payload)
    _POS_ACTIONS_PATH.write_text(_json.dumps(actions, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


if __name__ == "__main__":
    settings_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.toml")
    app.run(debug=True, port=5000, extra_files=[settings_path], threaded=True)
