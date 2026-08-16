"""
P1: 60-80% POP calibration failure investigation.

Key question: is the 29-point error (predicted 66%, actual 37% over 2 weeks,
n=38) a degraded-stack artifact — i.e. trades entered when direction/meta/IV
models were missing or erroring, so POP was the only selection signal?

POP classification uses GEOMETRIC implied POP (max_profit / max_loss geometry),
matching weekly_calibration_check.py's _implied_pop() function. NOT the ML
pop_score from pop_classifier.joblib.

Hypotheses ranked by prior:
  H1 (stack degradation) — trades entered Jul 16 had no direction, IV, meta.
     POP-only selection != well-calibrated POP. Expect trades concentrated on
     a single date, missing meta_score / direction / iv signals at entry.
  H2 (structure mismatch) — Long Strangle POP scores are systematically too
     high vs historical win rate. Expect Long Strangle over-represented.
  H3 (regime mismatch) — POP was trained pre-optimizer; scores don't
     transfer to post-Jul-13 data. Expect no strong date pattern.
  H4 (base-rate drift) — win rate genuinely dropped (market regime shift).
     Expect failure to persist in future weeks too.

Run:
  python -m scripts.investigate_pop_failure
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAPER_TRADES_PATH = ROOT / "data" / "paper_trades.json"
CLOSED_STATUSES   = {"expired_loss", "expired_profit", "closed_target"}

# Known stack-degradation date from ml_predictions log warnings (Jul 16 scan)
_DEGRADED_DATE = "2026-07-16"

# Must match weekly_calibration_check.py exactly
CREDIT_STRUCTURES = {
    "Call Credit Spread", "Put Credit Spread", "Iron Condor",
    "Iron Butterfly", "Cash Secured Put", "Naked Put",
    "Short Strangle", "Short Straddle", "Covered Call",
}


def _implied_pop(t: dict) -> float | None:
    """Geometric implied POP — matches weekly_calibration_check.py."""
    mp = t.get("max_profit")
    ml = t.get("max_loss")
    if mp is None or ml is None or (mp + ml) <= 0:
        return None
    if t.get("structure") in CREDIT_STRUCTURES:
        return ml / (ml + mp)
    else:
        return mp / (ml + mp)


def _pop_bucket(pop: float) -> str:
    if pop < 0.40:  return "0-40"
    if pop < 0.60:  return "40-60"
    if pop < 0.80:  return "60-80"
    return "80-100"


def _ml_signals(t: dict) -> dict:
    """Return the ML signals dict captured at entry."""
    return t.get("ml_scores_at_entry") or {}


def _win(t: dict) -> bool:
    return bool((t.get("exit") or {}).get("win"))


def _load_trades() -> list[dict]:
    with open(PAPER_TRADES_PATH) as f:
        raw = json.load(f)
    trades = raw if isinstance(raw, list) else raw.get("trades", [])
    return [t for t in trades if t.get("status") in CLOSED_STATUSES]


def run() -> None:
    trades = _load_trades()
    print(f"Total closed trades loaded: {len(trades)}")

    # ── Diagnose implied POP field availability ───────────────────────────────
    has_mp = sum(1 for t in trades if t.get("max_profit") is not None)
    has_ml = sum(1 for t in trades if t.get("max_loss") is not None)
    has_pop_at_entry = sum(1 for t in trades if t.get("pop_at_entry") is not None)
    print(f"  max_profit present: {has_mp}/{len(trades)}")
    print(f"  max_loss present  : {has_ml}/{len(trades)}")
    print(f"  pop_at_entry      : {has_pop_at_entry}/{len(trades)}")

    # If max_profit/max_loss unavailable, fall back to pop_at_entry
    def _pop_for_trade(t: dict) -> float | None:
        p = _implied_pop(t)
        if p is not None:
            return p
        return t.get("pop_at_entry")

    # ── Filter to 60-80 POP bucket ────────────────────────────────────────────
    bucket_trades = []
    pop_missing = 0
    for t in trades:
        pop = _pop_for_trade(t)
        if pop is None:
            pop_missing += 1
            continue
        if _pop_bucket(float(pop)) == "60-80":
            bucket_trades.append(t)

    print(f"\n60-80 POP bucket: {len(bucket_trades)} trades  "
          f"(POP unavailable on {pop_missing} trades)")

    if not bucket_trades:
        print("No trades in 60-80 bucket.")
        print("\nSample of first 5 closed trades (field inspection):")
        for t in trades[:5]:
            print(f"  id={t.get('id')}  structure={t.get('structure')}  "
                  f"max_profit={t.get('max_profit')}  max_loss={t.get('max_loss')}  "
                  f"pop_at_entry={t.get('pop_at_entry')}  "
                  f"implied_pop={_implied_pop(t)}")
        return

    n = len(bucket_trades)
    wins = sum(1 for t in bucket_trades if _win(t))
    actual_wr = wins / n

    implied_pops = [_pop_for_trade(t) for t in bucket_trades]
    pred_mean = sum(implied_pops) / len(implied_pops)

    print(f"  Implied POP (mean): {pred_mean:.1%}")
    print(f"  Actual win rate   : {actual_wr:.1%}  ({wins}/{n})")
    print(f"  Error             : {actual_wr - pred_mean:+.1%}")

    # ── H1: Entry date concentration ─────────────────────────────────────────
    print(f"\n── H1: Entry date distribution ──")
    by_date: dict[str, list] = defaultdict(list)
    for t in bucket_trades:
        d = (t.get("entered_at") or "")[:10]
        by_date[d].append(t)

    for d in sorted(by_date):
        ts = by_date[d]
        w = sum(1 for t in ts if _win(t))
        flag = "  <- DEGRADED STACK" if d == _DEGRADED_DATE else ""
        print(f"  {d}  n={len(ts):>3}  wins={w}  wr={w/len(ts):.0%}{flag}")

    degraded_n = len(by_date.get(_DEGRADED_DATE, []))
    if degraded_n > 0:
        print(f"\n  {_DEGRADED_DATE} accounts for {degraded_n}/{n} = {degraded_n/n:.0%} of 60-80 bucket")

    # ── H1b: Signal availability at entry ────────────────────────────────────
    print(f"\n── H1b: Signal availability in 60-80 bucket ──")
    sig_keys = ["meta_score", "p_up", "p_flat", "p_down",
                "iv_expanding_prob", "iv_direction", "expected_return"]
    missing_counts: dict[str, int] = defaultdict(int)
    for t in bucket_trades:
        ml = _ml_signals(t)
        for k in sig_keys:
            if ml.get(k) is None:
                missing_counts[k] += 1

    for k in sig_keys:
        m = missing_counts[k]
        print(f"  {k:<22} missing: {m:>3}/{n}  ({m/n:.0%})")

    # ── H2: Structure breakdown ───────────────────────────────────────────────
    print(f"\n── H2: Structure breakdown in 60-80 bucket ──")
    by_struct: dict[str, list] = defaultdict(list)
    for t in bucket_trades:
        s = t.get("structure") or "unknown"
        by_struct[s].append(t)

    for s, ts in sorted(by_struct.items()):
        w = sum(1 for t in ts if _win(t))
        pops = [_pop_for_trade(t) or 0.0 for t in ts]
        pred = sum(pops) / len(pops)
        print(f"  {s:<25} n={len(ts):>3}  pred={pred:.1%}  actual={w/len(ts):.1%}  "
              f"error={w/len(ts)-pred:+.1%}")

    # ── H3: Compare 60-80 bucket across all weeks ─────────────────────────────
    print(f"\n── H3: 60-80 bucket win rate by week ──")
    week_data: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pred": []})
    for t in bucket_trades:
        d = (t.get("entered_at") or "")[:10]
        if not d:
            continue
        dt = date.fromisoformat(d)
        week = str(dt - timedelta(days=dt.weekday()))
        pop = _pop_for_trade(t) or 0.0
        win = _win(t)
        week_data[week]["n"] += 1
        week_data[week]["wins"] += int(win)
        week_data[week]["pred"].append(pop)

    for week in sorted(week_data):
        wd = week_data[week]
        pred = sum(wd["pred"]) / len(wd["pred"]) if wd["pred"] else 0.0
        act  = wd["wins"] / wd["n"] if wd["n"] > 0 else 0.0
        print(f"  week {week}  n={wd['n']:>3}  pred={pred:.1%}  actual={act:.1%}  "
              f"error={act-pred:+.1%}")

    # ── H4: Overall win rate trend by week (all buckets) ─────────────────────
    print(f"\n── H4: Overall win rate by entry week (all buckets) ──")
    all_week: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    for t in trades:
        d = (t.get("entered_at") or "")[:10]
        if not d:
            continue
        dt = date.fromisoformat(d)
        week = str(dt - timedelta(days=dt.weekday()))
        all_week[week]["n"] += 1
        all_week[week]["wins"] += int(_win(t))

    for week in sorted(all_week):
        wd = all_week[week]
        act = wd["wins"] / wd["n"] if wd["n"] > 0 else 0.0
        print(f"  week {week}  n={wd['n']:>3}  actual={act:.1%}")

    # ── Calibration_history comparison ────────────────────────────────────────
    print(f"\n── Calibration_history: 60-80 bucket rows ──")
    try:
        from scripts.db import connect
        with connect(read_only=True) as con:
            rows = con.execute("""
                SELECT week_ending, n_trades, predicted_value, actual_value, abs_error, notes
                FROM calibration_history
                WHERE metric = 'hra' AND pop_bucket = '60-80'
                ORDER BY week_ending
            """).fetchall()
        if rows:
            print(f"  {'Week':>12}  {'N':>4}  {'Pred':>6}  {'Actual':>6}  {'Error':>7}  Notes")
            for r in rows:
                print(f"  {r[0]}  {r[1]:>4}  {r[2]:>6.1%}  {r[3]:>6.1%}  {r[3]-r[2]:>+7.1%}  {r[5] or ''}")
        else:
            print("  No calibration_history rows found.")
    except Exception as e:
        print(f"  DB query failed: {e}")

    print()


if __name__ == "__main__":
    run()
