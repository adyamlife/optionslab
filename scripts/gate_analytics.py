"""
gate_analytics.py — Query the gate_summary column to understand rejection patterns.

Every training snapshot now carries a gate_summary JSON blob (populated at
collection time by _build_gate_summary in training_data_collector.py) that
records each candidate's rejection status. This module mines that column to
answer three questions:

  1. How often does the optimizer produce trades that fail liquidity?
  2. Which risk gate fires most — expected_move or ex-dividend?
  3. Would relaxing one threshold materially increase trade availability?

Run standalone:
  python -m scripts.gate_analytics               # full report
  python -m scripts.gate_analytics --ticker AAPL # single ticker
  python -m scripts.gate_analytics --days 30     # last N days only
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parent.parent


def _parse_gate_summary(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    return raw if isinstance(raw, dict) else None


def load_gate_data(
    ticker: str | None = None,
    days: int | None = None,
) -> list[dict]:
    """
    Load gate_summary records from training_snapshots.

    Returns a flat list of dicts, one per snapshot, with parsed gate_summary
    and metadata fields (ticker, date, recommended_structure).
    Rows without gate_summary are silently skipped (pre-feature snapshots).
    """
    from scripts.db import read_df, SNAPSHOTS_TABLE

    where_clauses = ["gate_summary IS NOT NULL"]
    if ticker:
        where_clauses.append(f"ticker = '{ticker.upper()}'")
    if days:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        where_clauses.append(f"collected_at >= '{cutoff}'")

    where = " AND ".join(where_clauses)
    df = read_df(
        f"SELECT ticker, collected_at, recommended_structure, gate_summary "
        f"FROM {SNAPSHOTS_TABLE} WHERE {where}"
    )

    records = []
    for _, row in df.iterrows():
        gs = _parse_gate_summary(row.get("gate_summary"))
        if not gs:
            continue
        records.append({
            "ticker":                row.get("ticker"),
            "date":                  str(row.get("collected_at", ""))[:10],
            "recommended_structure": row.get("recommended_structure"),
            **gs,
        })
    return records


def summarize_gates(records: list[dict]) -> dict:
    """
    Aggregate rejection counts across all snapshots.

    Returns:
      {
        "n_snapshots":              int,
        "n_snapshots_no_trade":     int,   all candidates rejected/illiquid
        "data_quality_failures":    int,
        "chain_quality_failures":   int,
        "liquidity_rejection_rate": float, fraction of candidate slots that were illiquid
        "risk_rejection_rate":      float, fraction of candidate slots that hit a risk gate
        "gate_fire_counts":         {gate_phrase: int},  which risk_notes appear most
        "by_structure":             {structure: {n, n_risk_rejected, n_liquidity_ok, win_availability_pct}},
        "threshold_sensitivity":    {gate_phrase: n_unique_snapshots_affected}
      }
    """
    if not records:
        return {"ok": False, "error": "No gate_summary records found"}

    n_snapshots      = len(records)
    n_no_trade       = 0
    n_data_fail      = 0
    n_chain_fail     = 0
    total_candidates = 0
    total_liq_rej    = 0
    total_risk_rej   = 0

    gate_fire_counts: Counter = Counter()
    # per snapshot: which gates fired (for threshold_sensitivity)
    gate_to_snapshot_set: defaultdict[str, set] = defaultdict(set)

    by_structure: defaultdict[str, dict] = defaultdict(lambda: {
        "n": 0, "n_risk_rejected": 0, "n_liquidity_ok": 0
    })

    for i, rec in enumerate(records):
        cands = rec.get("candidates") or []
        if not cands:
            n_no_trade += 1
        elif all(not c.get("liquidity_ok") or c.get("risk_rejected") for c in cands):
            n_no_trade += 1

        if rec.get("data_quality_ok") is False:
            n_data_fail += 1
        if rec.get("chain_quality_ok") is False:
            n_chain_fail += 1

        for c in cands:
            total_candidates += 1
            struct = c.get("structure") or "Unknown"
            by_structure[struct]["n"] += 1

            if not c.get("liquidity_ok"):
                total_liq_rej += 1
            else:
                by_structure[struct]["n_liquidity_ok"] += 1

            if c.get("risk_rejected"):
                total_risk_rej += 1
                by_structure[struct]["n_risk_rejected"] += 1
                for note in (c.get("risk_notes") or []):
                    # Normalize to the leading phrase before the — em-dash
                    phrase = note.split("—")[0].strip()
                    gate_fire_counts[phrase] += 1
                    gate_to_snapshot_set[phrase].add(i)

    liq_rate  = round(total_liq_rej  / total_candidates, 4) if total_candidates else 0.0
    risk_rate = round(total_risk_rej / total_candidates, 4) if total_candidates else 0.0

    # Availability: fraction of candidate slots that were neither illiquid nor risk-rejected
    struct_summary = {}
    for s, m in by_structure.items():
        n = m["n"]
        available = n - m["n_risk_rejected"] - (n - m["n_liquidity_ok"])
        struct_summary[s] = {
            "n_evaluated":        n,
            "n_risk_rejected":    m["n_risk_rejected"],
            "n_liquidity_ok":     m["n_liquidity_ok"],
            "availability_pct":   round(max(available, 0) / n * 100, 1) if n else 0.0,
        }

    threshold_sensitivity = {
        phrase: len(snapshot_set)
        for phrase, snapshot_set in gate_to_snapshot_set.items()
    }

    return {
        "ok":                      True,
        "n_snapshots":             n_snapshots,
        "n_snapshots_no_trade":    n_no_trade,
        "no_trade_rate":           round(n_no_trade / n_snapshots, 4) if n_snapshots else 0.0,
        "data_quality_failures":   n_data_fail,
        "chain_quality_failures":  n_chain_fail,
        "total_candidates":        total_candidates,
        "liquidity_rejection_rate": liq_rate,
        "risk_rejection_rate":     risk_rate,
        "gate_fire_counts":        dict(gate_fire_counts.most_common()),
        "by_structure":            struct_summary,
        "threshold_sensitivity":   dict(
            sorted(threshold_sensitivity.items(), key=lambda x: -x[1])
        ),
    }


def availability_by_threshold(records: list[dict]) -> dict:
    """
    For each risk gate phrase, estimate how many currently-rejected snapshots
    would have become tradeable if that gate had not fired.

    Answers: "Would relaxing expected_move_max_ratio materially increase
    trade availability?"

    Returns {gate_phrase: {"snapshots_affected": int, "pct_of_total": float}}
    """
    n = len(records)
    if not n:
        return {}

    gate_to_snapshot_ids: defaultdict[str, set] = defaultdict(set)
    for i, rec in enumerate(records):
        for c in (rec.get("candidates") or []):
            for note in (c.get("risk_notes") or []):
                phrase = note.split("—")[0].strip()
                gate_to_snapshot_ids[phrase].add(i)

    return {
        phrase: {
            "snapshots_affected": len(ids),
            "pct_of_total":       round(len(ids) / n * 100, 1),
        }
        for phrase, ids in sorted(gate_to_snapshot_ids.items(), key=lambda x: -len(x[1]))
    }


def run_gate_report(
    ticker: str | None = None,
    days: int | None = None,
) -> dict:
    """Full gate analytics pipeline. Used by the Flask API and CLI."""
    records = load_gate_data(ticker=ticker, days=days)
    if not records:
        return {"ok": False, "error": "No gate_summary data found (snapshots collected before this feature was added will not appear)"}

    summary     = summarize_gates(records)
    sensitivity = availability_by_threshold(records)
    summary["threshold_sensitivity_detail"] = sensitivity
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    _ticker = None
    _days   = None
    args = sys.argv[1:]
    if "--ticker" in args:
        _ticker = args[args.index("--ticker") + 1]
    if "--days" in args:
        _days = int(args[args.index("--days") + 1])

    result = run_gate_report(ticker=_ticker, days=_days)
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)

    scope = f" ({_ticker})" if _ticker else ""
    period = f" — last {_days}d" if _days else ""
    print(f"\n=== Gate Rejection Analytics{scope}{period} ===")
    print(f"Snapshots        : {result['n_snapshots']}")
    print(f"No-trade rate    : {result['no_trade_rate']:.1%}  ({result['n_snapshots_no_trade']} snapshots where all candidates rejected)")
    print(f"Data failures    : {result['data_quality_failures']}   Chain failures: {result['chain_quality_failures']}")
    print(f"Liq. reject rate : {result['liquidity_rejection_rate']:.1%}  of candidate slots")
    print(f"Risk reject rate : {result['risk_rejection_rate']:.1%}  of candidate slots")

    if result.get("gate_fire_counts"):
        print(f"\nRisk gate fire counts (most common first):")
        for phrase, cnt in list(result["gate_fire_counts"].items())[:10]:
            print(f"  {cnt:>5}  {phrase}")

    if result.get("threshold_sensitivity_detail"):
        print(f"\nThreshold sensitivity (relaxing this gate would unlock N snapshots):")
        for phrase, d in list(result["threshold_sensitivity_detail"].items())[:5]:
            print(f"  {d['snapshots_affected']:>5} snapshots ({d['pct_of_total']:.1f}%)  —  {phrase}")

    if result.get("by_structure"):
        print(f"\nAvailability by structure:")
        rows = sorted(result["by_structure"].items(), key=lambda x: -x[1]["n_evaluated"])
        print(f"  {'Structure':<30}  {'eval':>5}  {'liq_ok':>6}  {'risk_rej':>8}  {'avail%':>7}")
        print("  " + "─" * 65)
        for s, m in rows:
            print(f"  {s:<30}  {m['n_evaluated']:>5}  {m['n_liquidity_ok']:>6}  "
                  f"{m['n_risk_rejected']:>8}  {m['availability_pct']:>6.1f}%")
