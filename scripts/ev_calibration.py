"""
Task #7 — EV calibration analysis: predicted ev_mc vs realized pnl_per_share.

Reads closed paper trades that have entry_ev_mc snapshotted (post Task #3).
Produces:
  1. Bucket table: predicted EV quartile vs avg realized P&L
  2. win-rate calibration: prob_profit_sim vs actual win rate by regime
  3. Pearson correlation: MC EV vs realized P&L, proxy EV vs realized P&L
  4. Regime breakdown: normal / high-vol / earnings-near / earnings-day

Run:
  python -m scripts.ev_calibration

Requires: data/paper_trades.json with entry_ev_mc and pnl_per_share populated.
The script reports how many qualifying trades exist and what analysis is possible.
"""
import json
import statistics
from pathlib import Path

import numpy as np

ROOT    = Path(__file__).resolve().parent.parent
PT_FILE = ROOT / "data" / "paper_trades.json"

_MIN_BUCKET = 5   # minimum trades per bucket for any metric to be reported


def load_closed_trades():
    if not PT_FILE.exists():
        return [], []
    trades = json.loads(PT_FILE.read_text(encoding="utf-8"))
    mc_trades  = [t for t in trades
                  if t.get("pnl_per_share") is not None
                  and t.get("entry_ev_mc") is not None]
    all_closed = [t for t in trades if t.get("pnl_per_share") is not None]
    return mc_trades, all_closed


def ev_bucket_table(trades):
    """Bucket by predicted EV quartile, report avg realized P&L per bucket."""
    if len(trades) < _MIN_BUCKET * 2:
        print(f"  Need ≥{_MIN_BUCKET * 2} trades for bucket analysis "
              f"(have {len(trades)}) — accumulating")
        return

    ev_vals  = np.array([t["entry_ev_mc"]   for t in trades])
    pnl_vals = np.array([t["pnl_per_share"] for t in trades])

    quartiles = np.percentile(ev_vals, [25, 50, 75])
    labels = ["Q1 (lowest EV)", "Q2", "Q3", "Q4 (highest EV)"]

    def bucket(ev):
        if ev <= quartiles[0]:  return 0
        if ev <= quartiles[1]:  return 1
        if ev <= quartiles[2]:  return 2
        return 3

    buckets = [[] for _ in range(4)]
    for ev, pnl in zip(ev_vals, pnl_vals):
        buckets[bucket(ev)].append(pnl)

    print(f"\n  EV Quartile Bucket Table (n={len(trades)}):")
    print(f"  {'Bucket':<20} {'EV range':>14} {'Avg P&L':>9} {'Win%':>7} {'n':>5}")
    print(f"  {'-'*20} {'-'*14} {'-'*9} {'-'*7} {'-'*5}")

    monotone = True
    prev_avg = None
    for i, (label, bucket_pnls) in enumerate(zip(labels, buckets)):
        if not bucket_pnls:
            continue
        avg = statistics.mean(bucket_pnls)
        wr  = sum(1 for p in bucket_pnls if p > 0) / len(bucket_pnls)
        lo  = quartiles[i-1] if i > 0 else ev_vals.min()
        hi  = quartiles[i]   if i < 3 else ev_vals.max()
        print(f"  {label:<20} [{lo:+6.3f},{hi:+6.3f}] {avg:>+9.4f} {wr:>7.1%} {len(bucket_pnls):>5}")
        if prev_avg is not None and avg < prev_avg:
            monotone = False
        prev_avg = avg

    verdict = "monotonically increasing ✓" if monotone else "NOT monotone — EV mis-calibrated"
    print(f"\n  Avg P&L trend across EV buckets: {verdict}")


def win_rate_calibration(trades):
    """Compare prob_profit_sim vs actual win rate overall and by regime."""
    eligible = [t for t in trades if t.get("entry_prob_profit_sim") is not None]
    if not eligible:
        print("  No trades with entry_prob_profit_sim — accumulating")
        return

    print(f"\n  Win-rate Calibration (n={len(eligible)}):")

    # Overall
    pps_vals  = [t["entry_prob_profit_sim"] for t in eligible]
    win_vals  = [1 if t["pnl_per_share"] > 0 else 0 for t in eligible]
    avg_pred  = statistics.mean(pps_vals)
    avg_act   = statistics.mean(win_vals)
    print(f"  Overall: predicted={avg_pred:.1f}%  actual={avg_act:.1%}  "
          f"gap={avg_pred - avg_act*100:+.1f}pp")

    # By regime (using entry regime if stored, else skip)
    regimes = {}
    for t in eligible:
        reg = (t.get("pred_dist") or {}).get("regime") or t.get("regime") or "unknown"
        regimes.setdefault(reg, []).append(t)

    if len(regimes) > 1:
        print(f"\n  By regime:")
        for reg, grp in sorted(regimes.items()):
            if len(grp) < _MIN_BUCKET:
                continue
            p_pred = statistics.mean(t["entry_prob_profit_sim"] for t in grp)
            p_act  = sum(1 for t in grp if t["pnl_per_share"] > 0) / len(grp)
            print(f"    {reg:<22}: predicted={p_pred:.1f}%  actual={p_act:.1%}  "
                  f"n={len(grp)}  gap={p_pred - p_act*100:+.1f}pp")

    # Earnings-near vs normal
    e_near   = [t for t in eligible if (t.get("earnings_days_away") or 99) <= 5]
    e_normal = [t for t in eligible if (t.get("earnings_days_away") or 99) > 5]
    print(f"\n  Earnings proximity:")
    for label, grp in [("earnings-near (≤5d)", e_near), ("normal (>5d)", e_normal)]:
        if len(grp) < _MIN_BUCKET:
            print(f"    {label}: n={len(grp)} — need ≥{_MIN_BUCKET}")
            continue
        p_pred = statistics.mean(t["entry_prob_profit_sim"] for t in grp)
        p_act  = sum(1 for t in grp if t["pnl_per_share"] > 0) / len(grp)
        print(f"    {label:<26}: predicted={p_pred:.1f}%  actual={p_act:.1%}  "
              f"n={len(grp)}  gap={p_pred - p_act*100:+.1f}pp")


def ev_correlation(mc_trades, all_closed):
    """Pearson correlation of each EV type against realized P&L."""
    print(f"\n  EV vs Realized P&L Correlation:")

    if len(mc_trades) >= 3:
        mc_evs = np.array([t["entry_ev_mc"] for t in mc_trades])
        pnls   = np.array([t["pnl_per_share"] for t in mc_trades])
        mc_r   = float(np.corrcoef(mc_evs, pnls)[0, 1])
        print(f"    MC EV vs P&L     : r={mc_r:+.4f}  n={len(mc_trades)}")

        proxy_eligible = [t for t in mc_trades if t.get("entry_ev_delta_proxy") is not None]
        if len(proxy_eligible) >= 3:
            pr_evs  = np.array([t["entry_ev_delta_proxy"] for t in proxy_eligible])
            pr_pnls = np.array([t["pnl_per_share"]        for t in proxy_eligible])
            pr_r    = float(np.corrcoef(pr_evs, pr_pnls)[0, 1])
            print(f"    Proxy EV vs P&L  : r={pr_r:+.4f}  n={len(proxy_eligible)}")
            winner = "MC" if mc_r > pr_r else "Proxy"
            print(f"    Better calibrated: {winner} EV")
    else:
        print(f"    Need ≥3 MC trades (have {len(mc_trades)}) — accumulating")

    # Historical proxy-only trades
    proxy_only = [t for t in all_closed
                  if t.get("entry_ev_delta_proxy") is not None
                  and t.get("entry_ev_mc") is None]
    if len(proxy_only) >= 3:
        pr_evs  = np.array([t["entry_ev_delta_proxy"] for t in proxy_only])
        pr_pnls = np.array([t["pnl_per_share"]        for t in proxy_only])
        pr_r    = float(np.corrcoef(pr_evs, pr_pnls)[0, 1])
        avg_pnl = float(pr_pnls.mean())
        win_rate = float((pr_pnls > 0).mean())
        print(f"\n    Historical proxy-EV trades (pre-MC): n={len(proxy_only)}")
        print(f"      Proxy EV corr : r={pr_r:+.4f}")
        print(f"      Win rate      : {win_rate:.1%}")
        print(f"      Avg P&L/share : ${avg_pnl:+.4f}")


def sample_trade_table(trades, n=10):
    if not trades:
        return
    print(f"\n  Sample trade records (most recent {min(n, len(trades))}):")
    print(f"  {'Ticker':<7} {'Structure':<22} {'MC EV':>7} {'Proxy':>7} {'Realized':>9} {'Match'}")
    print(f"  {'-'*7} {'-'*22} {'-'*7} {'-'*7} {'-'*9} {'-'*5}")
    for t in trades[-n:]:
        mc  = t["entry_ev_mc"]
        prx = t.get("entry_ev_delta_proxy", 0) or 0
        pnl = t["pnl_per_share"]
        # "Match" = MC EV and realized P&L have same sign
        match = "✓" if (mc > 0) == (pnl > 0) else "✗"
        print(f"  {t.get('ticker','?'):<7} {t.get('structure','?'):<22} "
              f"{mc:>+7.3f} {prx:>+7.3f} {pnl:>+9.4f}  {match}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  EV Calibration Analysis")
    print("=" * 60)

    mc_trades, all_closed = load_closed_trades()

    print(f"\n── Data Summary ────────────────────────────────────────────")
    print(f"  Total closed trades     : {len(all_closed)}")
    print(f"  With MC EV snapshotted  : {len(mc_trades)}")

    if not mc_trades:
        print("\n  No trades with MC EV yet.")
        print("  Deploy Tasks #3 and #4 to Ubuntu, let trades close, then re-run.")
        print("=" * 60)
    else:
        sample_trade_table(mc_trades)
        ev_bucket_table(mc_trades)
        win_rate_calibration(mc_trades)
        ev_correlation(mc_trades, all_closed)
        print("\n" + "=" * 60)
        print("  Analysis complete.")
        print("=" * 60)
