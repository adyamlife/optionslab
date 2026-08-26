"""
MC forecast width vs. structure eligibility cross-tab — the analysis tool for
the investigation scoped in memory as [[project_mc_forecast_structure_eligibility]].

Fixes, relative to the ad-hoc one-off query run on 2026-08-25:

  1. No more fuzzy day-level trend join — reads `trend` directly from
     scan_decisions.decision_snapshot (exact value at rejection/survival time,
     captured by candidate_ranker.py since 2026-08-25).
  2. No more DTE-mismatched / look-ahead-biased forecast join against
     ticker_forecast_log — reads mc_expiry_p10-p90 directly from
     decision_snapshot (candidate-level for survivors, ticker/expiry-level
     for rejections, both computed in the SAME scan, matching DTE — since
     2026-08-25's analyze.py hoist, see [[project_mc_forecast_structure_eligibility]]).
  3. Repair-variant grouping — collapses all variants of the same
     (scan_decision_id, ticker, structure, expiry) thesis into ONE
     observation before computing survival rates, instead of counting every
     repair attempt as independent evidence.
  4. Rejection-reason filtering — reports strikes_incomplete specifically,
     not "any gate failed" lumped together, since strikes availability is
     the actual question this investigation cares about.

Historical scans before 2026-08-25 won't have trend/mc_expiry_* on
decision_snapshot entries — this script only uses rows where those fields are
present, so older history is silently excluded rather than silently wrong.

Run:
    python -m scripts.mc_structure_crosstab
    python -m scripts.mc_structure_crosstab --since 2026-08-25
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import date


IC_BF  = {"Iron Condor", "Iron Butterfly"}
DEBIT  = {"Call Debit Spread", "Put Debit Spread"}


def _load_theses(since: str | None) -> list[dict]:
    """One row per (scan, ticker, structure, expiry) thesis — repair variants
    collapsed into a single observation: survived if ANY variant survived,
    otherwise the rejection_reason of the first-seen (typically original,
    non-repaired) variant is kept as representative."""
    from scripts.db import connect

    with connect(read_only=True) as con:
        q = "SELECT scan_decision_id, scan_date, decision_snapshot FROM scan_decisions"
        if since:
            q += f" WHERE scan_date >= '{since}'"
        scans = con.execute(q).fetchall()

    theses: dict[tuple, dict] = {}
    for scan_id, scan_date, snap in scans:
        snap = json.loads(snap) if isinstance(snap, str) else (snap or [])
        for e in snap:
            trend = e.get("trend")
            p10, p50, p90 = e.get("mc_expiry_p10"), e.get("mc_expiry_p50"), e.get("mc_expiry_p90")
            if trend is None or not p10 or not p50 or p50 == 0:
                continue  # pre-2026-08-25 scan, or simulation failed for this row
            key = (scan_id, e.get("ticker"), e.get("structure"), e.get("expiry"))
            survived = e.get("disposition") == "survived_filter"
            width = (p90 - p10) / p50 if p90 is not None else None
            if key not in theses:
                theses[key] = {
                    "scan_date": str(scan_date), "ticker": e.get("ticker"),
                    "structure": e.get("structure"), "trend": trend, "width": width,
                    "survived": survived,
                    "rejection_reason": e.get("rejection_reason"),
                }
            else:
                if survived:
                    theses[key]["survived"] = True
                    theses[key]["rejection_reason"] = None
    return list(theses.values())


def main():
    ap = argparse.ArgumentParser(description="MC forecast width vs structure eligibility cross-tab")
    ap.add_argument("--since", type=str, default=None, help="YYYY-MM-DD, only scans on/after this date")
    args = ap.parse_args()

    theses = _load_theses(args.since)
    print(f"Unique (scan, ticker, structure, expiry) theses: {len(theses)}")
    if not theses:
        print("No rows with trend + mc_expiry_p10/p50/p90 present — "
              "run scans with the 2026-08-25 fix deployed, then retry.")
        return

    widths = [t["width"] for t in theses if t["width"] is not None]
    if len(widths) < 3:
        print("Not enough width data for tertiles.")
        return
    q1, q2 = statistics.quantiles(widths, n=3)
    print(f"width tertiles: narrow<{q1:.4f}  normal<{q2:.4f}  wide>={q2:.4f}\n")

    def bucket(w):
        if w is None: return "unknown"
        if w < q1: return "Narrow"
        if w < q2: return "Normal"
        return "Wide"

    # Overall survival cross-tab (any rejection reason)
    cross = defaultdict(lambda: {"ic_bf_n": 0, "ic_bf_surv": 0, "debit_n": 0, "debit_surv": 0})
    # Strikes-specific view: of the theses that failed, how many failed on strikes_incomplete?
    strikes_view = defaultdict(lambda: {"ic_bf_n": 0, "ic_bf_strikes_fail": 0})

    for t in theses:
        key = (t["trend"], bucket(t["width"]))
        if t["structure"] in IC_BF:
            cross[key]["ic_bf_n"] += 1
            if t["survived"]:
                cross[key]["ic_bf_surv"] += 1
            strikes_view[key]["ic_bf_n"] += 1
            if not t["survived"] and t["rejection_reason"] == "strikes_incomplete":
                strikes_view[key]["ic_bf_strikes_fail"] += 1
        if t["structure"] in DEBIT:
            cross[key]["debit_n"] += 1
            if t["survived"]:
                cross[key]["debit_surv"] += 1

    print(f"{'Trend':12s} {'Range':8s} {'IC/BF n':>8s} {'IC/BF surv%':>12s} "
          f"{'strikes_incomplete%':>20s} {'Debit n':>8s} {'Debit surv%':>12s}")
    for key in sorted(cross.keys()):
        c = cross[key]; s = strikes_view[key]
        ic_surv = f"{100*c['ic_bf_surv']/c['ic_bf_n']:.0f}%" if c["ic_bf_n"] else "—"
        strikes_pct = f"{100*s['ic_bf_strikes_fail']/s['ic_bf_n']:.0f}%" if s["ic_bf_n"] else "—"
        d_surv = f"{100*c['debit_surv']/c['debit_n']:.0f}%" if c["debit_n"] else "—"
        print(f"{key[0]:12s} {key[1]:8s} {c['ic_bf_n']:>8d} {ic_surv:>12s} "
              f"{strikes_pct:>20s} {c['debit_n']:>8d} {d_surv:>12s}")


if __name__ == "__main__":
    main()
