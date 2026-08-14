"""
Task #5 — A/B backtest: delta-proxy EV ranking vs MC EV ranking.

Uses the labeled paper trades in data/paper_trades.json as the outcome source.
For each closed trade, reconstructs what rank it would have received under
each EV method, then compares realized P&L by ranking method.

Run:
  python -m scripts.backtest_mc_ab

Requires:
  - data/paper_trades.json with closed trades (pnl_per_share populated)
  - data/eval_log.jsonl with scan records that include ev_mc / ev_delta_proxy
    (populated by offline_eval.record_scan() after Task #3 lands)

Until enough eval_log data accumulates, the script also runs a simulation-based
comparison using the current universe of tickers to show the *ranking difference*
between the two EV methods even without labeled outcomes.
"""
import json
import statistics
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
PT_FILE  = ROOT / "data" / "paper_trades.json"
EVAL_LOG = ROOT / "data" / "eval_log.jsonl"


# ── Section 1: Eval-log based comparison (requires recorded scans) ────────────

def compare_from_eval_log():
    if not EVAL_LOG.exists():
        print("eval_log.jsonl not found — skipping eval-log comparison")
        return

    records = []
    with open(EVAL_LOG, encoding="utf-8") as fh:
        for line in fh:
            try:
                records.append(json.loads(line.strip()))
            except Exception:
                pass

    if not records:
        print("eval_log.jsonl is empty — skipping eval-log comparison")
        return

    # Find scans that have ev_mc populated (i.e. post Task #3 deployment)
    mc_scans = [r for r in records
                if any(c.get("ev_mc") is not None for c in r.get("candidates", []))]

    print(f"\n── Eval-log A/B Summary ────────────────────────────────────")
    print(f"  Total scan records  : {len(records)}")
    print(f"  Scans with MC EV    : {len(mc_scans)}")

    if not mc_scans:
        print("  No scans with MC EV yet — deploy to Ubuntu and let scans run.")
        return

    proxy_top1_wins, mc_top1_wins = [], []
    ev_differences = []

    for scan in mc_scans:
        cands = scan.get("candidates", [])
        # Only candidates with both EV sources and known outcomes
        scored = [c for c in cands
                  if c.get("ev_mc") is not None
                  and c.get("ev_delta_proxy") is not None
                  and c.get("actual_return") is not None]
        if len(scored) < 2:
            continue

        # Rank by each method
        by_mc    = sorted(scored, key=lambda c: c["ev_mc"],          reverse=True)
        by_proxy = sorted(scored, key=lambda c: c["ev_delta_proxy"], reverse=True)

        mc_top1_wins.append(1 if (by_mc[0].get("outcome") == "win") else 0)
        proxy_top1_wins.append(1 if (by_proxy[0].get("outcome") == "win") else 0)

        for c in scored:
            ev_differences.append(c["ev_mc"] - c["ev_delta_proxy"])

    if mc_top1_wins:
        print(f"\n  Top-1 win rate (proxy ranking): {statistics.mean(proxy_top1_wins):.1%}  "
              f"n={len(proxy_top1_wins)}")
        print(f"  Top-1 win rate (MC ranking)   : {statistics.mean(mc_top1_wins):.1%}  "
              f"n={len(mc_top1_wins)}")

    if ev_differences:
        pos = sum(1 for d in ev_differences if d > 0)
        neg = sum(1 for d in ev_differences if d < 0)
        print(f"\n  EV difference (MC − proxy) across all candidates:")
        print(f"    Mean : ${statistics.mean(ev_differences):+.4f}")
        print(f"    Stdev: ${statistics.stdev(ev_differences):.4f}")
        print(f"    MC > proxy: {pos}/{len(ev_differences)} candidates "
              f"({pos/len(ev_differences):.0%})")
        print(f"    MC < proxy: {neg}/{len(ev_differences)} candidates "
              f"({neg/len(ev_differences):.0%})")


# ── Section 2: Paper-trade EV calibration (proxy EV vs realized P&L) ─────────

def compare_from_paper_trades():
    if not PT_FILE.exists():
        print("paper_trades.json not found")
        return

    trades = json.loads(PT_FILE.read_text(encoding="utf-8"))
    closed = [t for t in trades
              if t.get("pnl_per_share") is not None
              and t.get("entry_ev_mc") is not None]
    proxy_only = [t for t in trades
                  if t.get("pnl_per_share") is not None
                  and t.get("entry_ev_delta_proxy") is not None
                  and t.get("entry_ev_mc") is None]

    print(f"\n── Paper Trade EV Calibration ──────────────────────────────")
    total_closed = sum(1 for t in trades if t.get("pnl_per_share") is not None)
    print(f"  Total closed trades       : {total_closed}")
    print(f"  With MC EV snapshotted    : {len(closed)}  (post Task #3 deployment)")
    print(f"  With proxy EV only        : {len(proxy_only)}  (pre Task #3)")

    if closed:
        print(f"\n  MC EV vs Realized P&L (n={len(closed)}):")
        for t in closed[:10]:  # show first 10
            print(f"    {t.get('ticker','?'):6s} {t.get('structure','?'):<22} "
                  f"MC EV={t['entry_ev_mc']:+.3f}  "
                  f"Proxy EV={t.get('entry_ev_delta_proxy',0):+.3f}  "
                  f"Realized={t['pnl_per_share']:+.3f}")

        mc_evs  = [t["entry_ev_mc"]     for t in closed]
        pnls    = [t["pnl_per_share"]   for t in closed]
        pr_evs  = [t.get("entry_ev_delta_proxy", 0) for t in closed]

        # Correlation: higher = better calibration
        try:
            import numpy as np
            mc_corr  = float(np.corrcoef(mc_evs, pnls)[0, 1])
            prx_corr = float(np.corrcoef(pr_evs, pnls)[0, 1])
            print(f"\n  Pearson correlation with realized P&L:")
            print(f"    MC EV    : {mc_corr:+.4f}")
            print(f"    Proxy EV : {prx_corr:+.4f}")
            winner = "MC" if mc_corr > prx_corr else "Proxy"
            print(f"    Better calibrated: {winner} EV")
        except Exception as e:
            print(f"  (correlation skipped: {e})")
    else:
        print("\n  No trades with MC EV yet — deploy Task #3 to Ubuntu and")
        print("  let trades accumulate before running this section.")

    if proxy_only:
        pr_evs = [t["entry_ev_delta_proxy"] for t in proxy_only]
        pnls   = [t["pnl_per_share"]        for t in proxy_only]
        try:
            import numpy as np
            prx_corr = float(np.corrcoef(pr_evs, pnls)[0, 1])
            avg_pnl  = statistics.mean(pnls)
            win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
            print(f"\n  Historical proxy-EV trades (n={len(proxy_only)}):")
            print(f"    Win rate       : {win_rate:.1%}")
            print(f"    Avg P&L/share  : ${avg_pnl:+.4f}")
            print(f"    Proxy EV corr  : {prx_corr:+.4f}")
        except Exception as e:
            print(f"  (proxy-only stats skipped: {e})")


# ── Section 3: Current ranking divergence (live universe snapshot) ────────────

def show_ranking_divergence(rows=None):
    """
    Show how MC EV and proxy EV disagree on candidate ordering.
    Pass pre-fetched rows to avoid triggering a live market data fetch.
    Does not require labeled outcomes.
    """
    print(f"\n── Current Ranking Divergence ──────────────────────────────")
    if rows is None:
        print("  Pass rows= to this function or call after a real scan.")
        print("  (Skipping live fetch to avoid yfinance/DuckDB conflicts.)")
        return

    items, _, _snap = filter_candidates(rows)
    both = [it for it in items
            if it.get("ev_mc") is not None and it.get("ev_delta_proxy") is not None]

    if not both:
        print("  No candidates with both EV sources in current universe")
        return

    print(f"  Candidates with both EV sources: {len(both)}")
    print(f"\n  {'Ticker':<8} {'Structure':<24} {'MC EV':>8} {'Proxy EV':>9} {'Diff':>7}")
    print(f"  {'-'*8} {'-'*24} {'-'*8} {'-'*9} {'-'*7}")
    for it in sorted(both, key=lambda x: abs(x['ev_mc'] - x['ev_delta_proxy']), reverse=True)[:15]:
        t = it["row"].get("ticker", "?")
        s = it["candidate"].get("structure", "?")
        diff = it["ev_mc"] - it["ev_delta_proxy"]
        print(f"  {t:<8} {s:<24} {it['ev_mc']:>+8.3f} {it['ev_delta_proxy']:>+9.3f} {diff:>+7.3f}")

    # Top-3 by each method
    top3_mc    = sorted(both, key=lambda x: x["ev_mc"],          reverse=True)[:3]
    top3_proxy = sorted(both, key=lambda x: x["ev_delta_proxy"], reverse=True)[:3]
    mc_ids    = {(it["row"].get("ticker"), it["candidate"].get("structure")) for it in top3_mc}
    proxy_ids = {(it["row"].get("ticker"), it["candidate"].get("structure")) for it in top3_proxy}
    overlap   = mc_ids & proxy_ids
    print(f"\n  Top-3 overlap (MC vs proxy): {len(overlap)}/3")
    if mc_ids != proxy_ids:
        print(f"  Different selections — MC would pick differently from proxy")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  A/B Backtest: Proxy EV vs MC EV Ranking")
    print("=" * 60)

    compare_from_eval_log()
    compare_from_paper_trades()
    show_ranking_divergence()  # pass rows=<list> after a real scan to see divergence

    print("\n" + "=" * 60)
    print("  Note: Full A/B results accumulate as paper trades close.")
    print("  Re-run this script after deploying to Ubuntu.")
    print("=" * 60)
