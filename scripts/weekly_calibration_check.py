"""
Weekly calibration observer — runs Sunday 9 PM via Windows Task Scheduler
(after weekly_profile_build.py at 8 PM).

Reads expired paper trades from paper_trades.json, joins to training_snapshots
and ticker_profile_snapshots, then appends four calibration metrics to
calibration_history. Zero trading-logic changes — observation only.

Four metrics tracked:
  1. hra          — predicted POP bucket vs actual win rate (all 81 trades)
  2. bayes_vs_actual — profile bayes_survival vs actual win rate by regime
                       (subset with regime_training match, ~23 trades currently)
  3. vp_accuracy  — profile vp_ratio vs realized hv20/future_hv5d
  4. containment  — profile containment vs actual price containment over DTE

Status: DO NOT act on these numbers until 20+ weekly data points (Phase 3).
        They are stored for future validation only.

Schedule: Task Scheduler → Action → python scripts/weekly_calibration_check.py
"""
import sys
import json
import logging
import datetime
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [weekly_calibration] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

PAPER_TRADES_PATH = ROOT / "data" / "paper_trades.json"
CLOSED_STATUSES   = {"expired_loss", "expired_profit", "closed_target"}

# Ubuntu regime label → profile key
REGIME_KEY = {
    "Mean-reverting":    "mr",
    "Trending":          "tr",
    "Low-vol-squeeze":   "lv",
    "High-vol-breakout": "hv",
}


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_expired_trades() -> list[dict]:
    with open(PAPER_TRADES_PATH) as f:
        raw = json.load(f)
    trades = raw if isinstance(raw, list) else raw.get("trades", [])
    return [t for t in trades if t.get("status") in CLOSED_STATUSES]


def _enrich_from_db(trades: list[dict], con) -> list[dict]:
    """
    Join each trade to:
      - training_snapshots  → hv20, future_hv5d, atm_iv (via snapshot_id prefix)
      - regime_training     → regime_label, forward_return, forward_hv (by ticker+date)
      - ticker_profile_snapshots → predicted bayes_survival, vp_ratio, containment
    """
    enriched = []
    for t in trades:
        d      = (t.get("entered_at") or "")[:10]
        ticker = t.get("ticker", "")
        if not d or not ticker:
            continue

        rec = {
            "ticker":        ticker,
            "structure":     t.get("structure"),
            "status":        t.get("status"),
            "entered_at":    d,
            "expiry":        t.get("expiry"),
            "spot_at_entry": t.get("spot_at_entry"),
            "entry_mid":     t.get("entry_mid"),
            "max_profit":    t.get("max_profit"),
            "max_loss":      t.get("max_loss"),
            "dte_at_entry":  t.get("dte_at_entry"),
            "win":           (t.get("exit") or {}).get("win"),
            "pnl_per_share": (t.get("exit") or {}).get("pnl_per_share"),
            "exit_ul":       (t.get("exit") or {}).get("ul_price"),
            # DB-joined fields (filled below)
            "hv20":          None,
            "future_hv5d":   None,
            "atm_iv":        None,
            "regime_label":  None,
            "forward_return": None,
            "forward_hv":    None,
        }

        # 1. training_snapshots via snapshot_id prefix
        d_compact = d.replace("-", "")
        snap_prefix = f"paper-{d_compact}_{ticker}"
        try:
            row = con.execute(
                "SELECT hv20, future_hv5d, atm_iv "
                "FROM training_snapshots WHERE snapshot_id LIKE ? "
                "ORDER BY collected_at DESC LIMIT 1",
                [f"{snap_prefix}%"],
            ).fetchone()
            if row:
                rec["hv20"]        = row[0]
                rec["future_hv5d"] = row[1]
                rec["atm_iv"]      = row[2]
        except Exception:
            pass

        # 2. regime_training by ticker + date (main watchlist tickers only)
        try:
            row2 = con.execute(
                "SELECT regime_label, forward_return, forward_hv "
                "FROM regime_training WHERE ticker=? AND date=? LIMIT 1",
                [ticker, d],
            ).fetchone()
            if row2:
                rec["regime_label"]  = row2[0]
                rec["forward_return"] = row2[1]
                rec["forward_hv"]    = row2[2]
        except Exception:
            pass

        # 3. ticker_profile_snapshots — most recent profile available.
        # We use the latest profile regardless of entry date: for Phase 2 validation
        # the question is "does the profile predict correctly?", not "was it available
        # at entry time?". Profiles are built from multi-year backfill so this is
        # a retrospective accuracy check, not a live trading decision.
        try:
            prof = con.execute(
                "SELECT * FROM ticker_profile_snapshots "
                "WHERE ticker=? "
                "ORDER BY profile_date DESC LIMIT 1",
                [ticker],
            ).fetchdf()
            if not prof.empty:
                rec["profile"] = prof.iloc[0].to_dict()
            else:
                rec["profile"] = None
        except Exception:
            rec["profile"] = None

        enriched.append(rec)

    log.info(
        "Enriched %d trades: %d with regime_label, %d with hv20, %d with profile",
        len(enriched),
        sum(1 for r in enriched if r["regime_label"]),
        sum(1 for r in enriched if r["hv20"] is not None),
        sum(1 for r in enriched if r["profile"]),
    )
    return enriched


# ── Metric 1: HRA — POP bucket calibration ───────────────────────────────────

def _implied_pop(rec: dict) -> float | None:
    """
    Market-implied POP from trade geometry.
    Credit structures: POP ≈ max_loss / (max_loss + max_profit)  [credit / width]
    Debit structures:  POP ≈ max_profit / (max_loss + max_profit)
    """
    CREDIT_STRUCTURES = {
        "Call Credit Spread", "Put Credit Spread", "Iron Condor",
        "Iron Butterfly", "Cash Secured Put", "Naked Put",
        "Short Strangle", "Short Straddle", "Covered Call",
    }
    mp  = rec.get("max_profit")
    ml  = rec.get("max_loss")
    if mp is None or ml is None or (mp + ml) <= 0:
        return None
    if rec.get("structure") in CREDIT_STRUCTURES:
        return ml / (ml + mp)   # probability short side expires OTM
    else:
        return mp / (ml + mp)   # probability long side reaches max profit


def _pop_bucket(pop: float) -> str:
    if pop < 0.40:
        return "0-40"
    if pop < 0.60:
        return "40-60"
    if pop < 0.80:
        return "60-80"
    return "80-100"


def compute_hra(enriched: list[dict]) -> list[dict]:
    """
    Group trades by POP bucket; compare mean predicted POP vs actual win rate.
    All 81 expired trades usable (no regime label needed).
    """
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for rec in enriched:
        pop = _implied_pop(rec)
        if pop is None or rec.get("win") is None:
            continue
        buckets[_pop_bucket(pop)].append((pop, int(rec["win"])))

    rows = []
    for bucket, pairs in sorted(buckets.items()):
        n            = len(pairs)
        pred_pop     = sum(p for p, _ in pairs) / n
        actual_wr    = sum(w for _, w in pairs) / n
        rows.append({
            "metric":          "hra",
            "regime":          "",
            "pop_bucket":      bucket,
            "n_trades":        n,
            "predicted_value": round(pred_pop, 4),
            "actual_value":    round(actual_wr, 4),
            "abs_error":       round(abs(actual_wr - pred_pop), 4),
            "notes":           f"implied POP vs actual win rate",
        })
        log.info(
            "[HRA] bucket=%s  n=%d  pred_pop=%.1f%%  actual_wr=%.1f%%  error=%+.1f%%",
            bucket, n, pred_pop * 100, actual_wr * 100, (actual_wr - pred_pop) * 100,
        )

    return rows


# ── Metric 2: Bayes survival vs actual win rate by regime ─────────────────────

def compute_bayes_vs_actual(enriched: list[dict]) -> list[dict]:
    """
    For trades with regime_label AND ticker profile, compare:
      predicted = bayes_survival_{key}  (profile's Bayesian estimate)
      actual    = win rate in that regime
    Only ~23 trades currently have regime_label (main watchlist only).
    """
    from collections import defaultdict
    regime_groups: dict[str, list] = defaultdict(list)
    for rec in enriched:
        lbl  = rec.get("regime_label")
        prof = rec.get("profile")
        win  = rec.get("win")
        if lbl is None or prof is None or win is None:
            continue
        key   = REGIME_KEY.get(lbl)
        bayes = prof.get(f"bayes_survival_{key}") if key else None
        if bayes is None:
            continue
        regime_groups[lbl].append((bayes, int(win)))

    rows = []
    for regime, pairs in regime_groups.items():
        n          = len(pairs)
        pred_surv  = sum(p for p, _ in pairs) / n
        actual_wr  = sum(w for _, w in pairs) / n
        rows.append({
            "metric":          "bayes_vs_actual",
            "regime":          regime,
            "pop_bucket":      "",
            "n_trades":        n,
            "predicted_value": round(pred_surv, 4),
            "actual_value":    round(actual_wr, 4),
            "abs_error":       round(abs(actual_wr - pred_surv), 4),
            "notes":           f"profile bayes_survival vs actual win rate (n={n}, regime_training matched)",
        })
        log.info(
            "[bayes] regime=%-22s  n=%d  pred=%.1f%%  actual=%.1f%%  error=%+.1f%%",
            regime, n, pred_surv * 100, actual_wr * 100, (actual_wr - pred_surv) * 100,
        )

    if not rows:
        log.info("[bayes] No regime-labeled trades with profiles this week")

    return rows


# ── Metric 3: VP ratio accuracy ───────────────────────────────────────────────

def compute_vp_accuracy(enriched: list[dict]) -> list[dict]:
    """
    Compare profile vp_ratio_{key} (predicted hv20/forward_hv) vs
    realized hv20/future_hv5d from training_snapshots.
    Grouped by regime where available; aggregate where not.
    """
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for rec in enriched:
        hv20        = rec.get("hv20")
        future_hv5d = rec.get("future_hv5d")
        prof        = rec.get("profile")
        if hv20 is None or future_hv5d is None or future_hv5d <= 0 or prof is None:
            continue
        realized_vp = hv20 / future_hv5d

        lbl = rec.get("regime_label")
        key = REGIME_KEY.get(lbl) if lbl else None
        pred_vp = prof.get(f"vp_ratio_{key}") if key else None
        if pred_vp is None:
            # Fall back to average across regimes from profile
            vps = [prof.get(f"vp_ratio_{k}") for k in ("mr", "tr", "lv", "hv")]
            vps = [v for v in vps if v is not None]
            pred_vp = sum(vps) / len(vps) if vps else None
        if pred_vp is None:
            continue

        group = lbl if lbl else "aggregate"
        groups[group].append((pred_vp, realized_vp))

    rows = []
    for group, pairs in groups.items():
        n         = len(pairs)
        pred_mean = sum(p for p, _ in pairs) / n
        real_mean = sum(r for _, r in pairs) / n
        rows.append({
            "metric":          "vp_accuracy",
            "regime":          group,
            "pop_bucket":      "",
            "n_trades":        n,
            "predicted_value": round(pred_mean, 4),
            "actual_value":    round(real_mean, 4),
            "abs_error":       round(abs(real_mean - pred_mean), 4),
            "notes":           "profile vp_ratio vs realized hv20/future_hv5d",
        })
        log.info(
            "[vp]    group=%-22s  n=%d  pred_vp=%.3f  realized_vp=%.3f  error=%+.3f",
            group, n, pred_mean, real_mean, real_mean - pred_mean,
        )

    return rows


# ── Metric 4: Containment accuracy ───────────────────────────────────────────

def compute_containment_accuracy(enriched: list[dict]) -> list[dict]:
    """
    Compare profile containment_{key} vs actual price containment over the trade DTE.
    Containment: |exit_ul - spot_at_entry| / spot_at_entry < hv20 * sqrt(dte/252).
    No regime label needed — works for all trades with spot_at_entry + exit_ul + hv20.
    """
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for rec in enriched:
        spot       = rec.get("spot_at_entry")
        exit_ul    = rec.get("exit_ul")
        hv20       = rec.get("hv20")
        dte        = rec.get("dte_at_entry") or 21   # fallback to 21d
        prof       = rec.get("profile")
        if spot is None or exit_ul is None or hv20 is None or prof is None:
            continue
        if spot <= 0 or hv20 <= 0:
            continue

        # 1-sigma band over DTE
        sigma_dte = hv20 * math.sqrt(float(dte) / 252.0)
        actual_move_pct = abs(exit_ul - spot) / spot
        actually_contained = int(actual_move_pct < sigma_dte)

        lbl = rec.get("regime_label")
        key = REGIME_KEY.get(lbl) if lbl else None
        pred_contain = prof.get(f"containment_{key}") if key else None
        if pred_contain is None:
            # Weighted average across regime cells proportional to their n
            nums = []
            for k in ("mr", "tr", "lv", "hv"):
                c = prof.get(f"containment_{k}")
                n = prof.get(f"n_{k}") or 0
                if c is not None and n > 0:
                    nums.append((c, n))
            if nums:
                total_n    = sum(n for _, n in nums)
                pred_contain = sum(c * n for c, n in nums) / total_n
        if pred_contain is None:
            continue

        group = lbl if lbl else "aggregate"
        groups[group].append((pred_contain, actually_contained))

    rows = []
    for group, pairs in groups.items():
        n         = len(pairs)
        pred_mean = sum(p for p, _ in pairs) / n
        real_mean = sum(r for _, r in pairs) / n
        rows.append({
            "metric":          "containment_accuracy",
            "regime":          group,
            "pop_bucket":      "",
            "n_trades":        n,
            "predicted_value": round(pred_mean, 4),
            "actual_value":    round(real_mean, 4),
            "abs_error":       round(abs(real_mean - pred_mean), 4),
            "notes":           f"profile containment vs actual |move|<hv20*sqrt(dte/252)",
        })
        log.info(
            "[contain] group=%-22s  n=%d  pred=%.1f%%  actual=%.1f%%  error=%+.1f%%",
            group, n, pred_mean * 100, real_mean * 100, (real_mean - pred_mean) * 100,
        )

    return rows


# ── DB write ──────────────────────────────────────────────────────────────────

def _write_to_db(rows: list[dict], week_ending: str, con) -> int:
    from datetime import datetime as _dt
    now = _dt.now().isoformat()
    written = 0
    for row in rows:
        try:
            con.execute(
                "INSERT OR REPLACE INTO calibration_history "
                "(week_ending, metric, regime, pop_bucket, n_trades, "
                " predicted_value, actual_value, abs_error, notes, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    week_ending,
                    row["metric"],
                    row.get("regime", ""),
                    row.get("pop_bucket", ""),
                    row["n_trades"],
                    row["predicted_value"],
                    row["actual_value"],
                    row["abs_error"],
                    row.get("notes"),
                    now,
                ],
            )
            written += 1
        except Exception as exc:
            log.warning("DB write failed for %s/%s: %s", row["metric"], row.get("regime"), exc)
    return written


# ── Phase 3 readiness check ───────────────────────────────────────────────────

def _check_phase3_readiness(con) -> None:
    """Log a readiness summary. No action taken — informational only."""
    try:
        df = con.execute(
            "SELECT metric, COUNT(DISTINCT week_ending) as weeks "
            "FROM calibration_history GROUP BY metric"
        ).fetchdf()
        log.info("[phase3] Calibration history summary:")
        for _, row in df.iterrows():
            ready = "READY" if row["weeks"] >= 20 else f"need {20 - int(row['weeks'])} more weeks"
            log.info("  %-25s %2d weeks  %s", row["metric"], row["weeks"], ready)
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> None:
    from scripts.db import connect, ensure_calibration_history_table, ensure_ticker_profile_snapshots_table

    ensure_calibration_history_table()
    ensure_ticker_profile_snapshots_table()

    # Week ending = this Sunday
    today      = datetime.date.today()
    week_ending = today.isoformat()

    log.info("=== Weekly calibration check for week ending %s ===", week_ending)

    trades = _load_expired_trades()
    log.info("Loaded %d expired/closed trades from paper_trades.json", len(trades))

    if not trades:
        log.info("No expired trades — nothing to compute")
        return

    with connect() as con:
        enriched = _enrich_from_db(trades, con)

        all_rows: list[dict] = []
        all_rows.extend(compute_hra(enriched))
        all_rows.extend(compute_bayes_vs_actual(enriched))
        all_rows.extend(compute_vp_accuracy(enriched))
        all_rows.extend(compute_containment_accuracy(enriched))

        written = _write_to_db(all_rows, week_ending, con)
        con.commit()

        log.info("Wrote %d calibration rows to calibration_history", written)
        _check_phase3_readiness(con)

    # Print summary table for log review
    log.info("=== Summary ===")
    for row in all_rows:
        log.info(
            "  %-25s %-22s %-8s  n=%3d  pred=%.3f  actual=%.3f  err=%+.3f",
            row["metric"], row.get("regime") or "-", row.get("pop_bucket") or "-",
            row["n_trades"], row["predicted_value"], row["actual_value"],
            row["actual_value"] - row["predicted_value"],
        )


if __name__ == "__main__":
    from scripts.run_log import record
    record("weekly_calibration", run)
