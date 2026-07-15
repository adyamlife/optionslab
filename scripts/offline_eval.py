"""
Offline evaluation framework for the candidate scoring pipeline.

Records every scan's ranked candidates (with composite scores) alongside which
ones were chosen (entered as paper trades) and their eventual actual return.
Once enough data accumulates, computes:

  - Precision@k  — fraction of top-k candidates that yielded a profitable trade
  - NDCG@k       — ranking quality; rewards putting the best outcomes at the top
  - Score calibration — how well the composite score correlates with actual P&L%

Data flow:
  1. Each morning scan writes a "scan record" via record_scan().
  2. When a paper trade closes, update_trade_outcome() fills in actual_return.
  3. compute_metrics() reads the log and returns whatever metrics are computable.

Storage: data/eval_log.jsonl  — one JSON object per scan, appended, never mutated.
                                 Actual returns are written as a side-car patch file
                                 data/eval_outcomes.json  keyed by trade_id.
"""
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DATA_DIR    = Path(__file__).resolve().parent.parent / "data"
_EVAL_LOG    = _DATA_DIR / "eval_log.jsonl"
_OUTCOMES    = _DATA_DIR / "eval_outcomes.json"


# ─── Write side ───────────────────────────────────────────────────────────────

def record_scan(ranked_items: list, scan_ts: Optional[str] = None) -> None:
    """
    Append one scan record to eval_log.jsonl.

    ranked_items: output of rank_candidates() — list of dicts, position 0 = top pick.
    Each item must have at least:
        item["row"]["ticker"], item["candidate"]["structure"],
        item["composite"], item["ev"], item["ev_is_proxy"]
    """
    ts = scan_ts or datetime.now(timezone.utc).isoformat()
    record = {
        "scan_ts": ts,
        "candidates": [],
    }
    for rank, item in enumerate(ranked_items):
        row = item.get("row", {})
        c   = item.get("candidate", {})
        ml  = row.get("ml") or {}
        record["candidates"].append({
            "rank":           rank,
            "ticker":         row.get("ticker"),
            "structure":      c.get("structure"),
            "composite":      round(item.get("composite", 0), 2),
            "ev":             round(item.get("ev", 0), 4),
            "ev_is_proxy":    item.get("ev_is_proxy", False),
            "pop":            c.get("pop"),
            "signal_score":   c.get("signal_score"),
            "ranker_score":   item.get("ranker_score"),
            "spot":           row.get("spot"),
            "atm_iv":         row.get("atm_iv"),
            "iv_env":         row.get("iv_env"),
            "dte":            row.get("dte"),
            # These will be filled later by update_trade_outcome()
            "trade_id":       None,
            "chosen":         False,
            "actual_return":  None,   # % return on capital at expiry/exit
            "outcome":        None,   # "win" | "loss" | "scratch" | "open"
        })
    try:
        with open(_EVAL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        log.debug(f"[eval] recorded scan with {len(ranked_items)} candidates at {ts}")
    except Exception as e:
        log.warning(f"[eval] could not write eval_log: {e}")


def mark_chosen(scan_ts: str, ticker: str, structure: str, trade_id: str) -> None:
    """
    Mark a candidate as chosen (entered as a paper trade).

    Because the log is append-only, this writes a patch entry — a lightweight
    record that update_trade_outcome() merges at read time.
    """
    _write_outcome(trade_id, {
        "scan_ts":   scan_ts,
        "ticker":    ticker,
        "structure": structure,
        "chosen":    True,
        "trade_id":  trade_id,
    })


def update_trade_outcome(
    trade_id: str,
    actual_return_pct: float,
    outcome: str,          # "win" | "loss" | "scratch"
) -> None:
    """
    Record the realized outcome of a closed paper trade.

    actual_return_pct: (exit_value - entry_value) / capital_required × 100
    outcome: "win" if profitable, "loss" if P&L < 0, "scratch" if ~0.
    """
    existing = _load_outcomes()
    if trade_id not in existing:
        existing[trade_id] = {}
    existing[trade_id].update({
        "actual_return_pct": actual_return_pct,
        "outcome":           outcome,
        "closed_at":         datetime.now(timezone.utc).isoformat(),
    })
    _save_outcomes(existing)
    log.info(f"[eval] trade {trade_id} outcome recorded: {outcome} {actual_return_pct:+.1f}%")


# ─── Read / compute side ──────────────────────────────────────────────────────

def compute_metrics(k: int = 3) -> dict:
    """
    Compute offline evaluation metrics from accumulated log data.

    Returns a dict with:
        n_scans, n_candidates, n_chosen, n_closed,
        precision_at_k,        # fraction of top-k chosen that were wins (None if <5 samples)
        ndcg_at_k,             # NDCG@k over closed trades (None if <5 samples)
        calibration,           # list of {score_bucket, avg_return, n} — binned by composite
        score_return_corr,     # Pearson r between composite and actual_return (None if <5)
    """
    scans, outcomes = _load_log_with_outcomes()

    n_scans      = len(scans)
    n_candidates = sum(len(s["candidates"]) for s in scans)
    chosen       = [c for s in scans for c in s["candidates"] if c.get("chosen")]
    closed       = [c for c in chosen if c.get("actual_return") is not None]

    metrics: dict = {
        "n_scans":        n_scans,
        "n_candidates":   n_candidates,
        "n_chosen":       len(chosen),
        "n_closed":       len(closed),
        "precision_at_k": None,
        "ndcg_at_k":      None,
        "score_return_corr": None,
        "calibration":    [],
        "k":              k,
        "computed_at":    datetime.now(timezone.utc).isoformat(),
        "note":           "metrics require ≥5 closed trades" if len(closed) < 5 else "",
    }

    if len(closed) < 5:
        return metrics

    # Precision@k — within each scan, were top-k chosen candidates wins?
    topk_wins = 0
    topk_total = 0
    for scan in scans:
        top_chosen = [c for c in scan["candidates"] if c.get("chosen") and c["rank"] < k]
        for c in top_chosen:
            if c.get("actual_return") is not None:
                topk_total += 1
                if c.get("outcome") == "win":
                    topk_wins += 1
    metrics["precision_at_k"] = round(topk_wins / topk_total, 4) if topk_total else None

    # NDCG@k — rank quality; actual_return as the relevance signal
    # Normalize returns to [0, 1] as relevance scores
    returns = [c["actual_return"] for c in closed]
    r_min, r_max = min(returns), max(returns)
    r_range = (r_max - r_min) or 1.0

    ndcg_scores = []
    for scan in scans:
        ranked_closed = sorted(
            [c for c in scan["candidates"] if c.get("actual_return") is not None],
            key=lambda c: c["rank"],
        )
        if not ranked_closed:
            continue
        # Ideal: sort by actual_return descending
        ideal = sorted(ranked_closed, key=lambda c: -c["actual_return"])
        dcg   = sum(_rel(c, r_min, r_range) / math.log2(i + 2)
                    for i, c in enumerate(ranked_closed[:k]))
        idcg  = sum(_rel(c, r_min, r_range) / math.log2(i + 2)
                    for i, c in enumerate(ideal[:k]))
        if idcg > 0:
            ndcg_scores.append(dcg / idcg)

    metrics["ndcg_at_k"] = round(sum(ndcg_scores) / len(ndcg_scores), 4) if ndcg_scores else None

    # Score–return correlation (Pearson r)
    scores  = [c["composite"] for c in closed if c.get("composite") is not None]
    rets    = [c["actual_return"] for c in closed if c.get("composite") is not None]
    if len(scores) >= 5:
        metrics["score_return_corr"] = round(_pearson(scores, rets), 4)

    # Calibration: bucket composite scores into deciles and show avg actual_return
    buckets: dict[int, list] = {}
    for c in closed:
        comp = c.get("composite")
        ret  = c.get("actual_return")
        if comp is None or ret is None:
            continue
        bucket = int(comp // 10) * 10   # 0,10,20,...,90
        buckets.setdefault(bucket, []).append(ret)
    metrics["calibration"] = [
        {"score_bucket": b, "avg_return": round(sum(v) / len(v), 2), "n": len(v)}
        for b, v in sorted(buckets.items())
    ]

    return metrics


def load_metrics() -> dict:
    """Load the last saved metrics report, or compute fresh if stale/missing."""
    report_path = _DATA_DIR / "eval_metrics.json"
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return compute_metrics()


def save_metrics() -> dict:
    """Compute and persist metrics to data/eval_metrics.json."""
    m = compute_metrics()
    try:
        (_DATA_DIR / "eval_metrics.json").write_text(
            json.dumps(m, indent=2), encoding="utf-8"
        )
        log.info(f"[eval] metrics saved: {m['n_closed']} closed trades, "
                 f"precision@{m['k']}={m['precision_at_k']}, ndcg@{m['k']}={m['ndcg_at_k']}")
    except Exception as e:
        log.warning(f"[eval] could not save metrics: {e}")
    return m


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _rel(c: dict, r_min: float, r_range: float) -> float:
    return (c["actual_return"] - r_min) / r_range


def _pearson(xs: list, ys: list) -> float:
    n  = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num  = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denom = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / denom if denom else 0.0


def _load_outcomes() -> dict:
    try:
        return json.loads(_OUTCOMES.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_outcomes(data: dict) -> None:
    _OUTCOMES.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_outcome(trade_id: str, patch: dict) -> None:
    existing = _load_outcomes()
    if trade_id not in existing:
        existing[trade_id] = {}
    existing[trade_id].update(patch)
    _save_outcomes(existing)


def _load_log_with_outcomes() -> tuple[list, dict]:
    """Load all scan records and merge in outcome patches."""
    outcomes = _load_outcomes()
    scans = []
    try:
        lines = _EVAL_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], outcomes

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            scan = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Merge outcome patches into matching candidates
        for c in scan.get("candidates", []):
            tid = c.get("trade_id")
            if tid and tid in outcomes:
                c.update(outcomes[tid])
            else:
                # Also try to match by scan_ts + ticker + structure
                for patch in outcomes.values():
                    if (patch.get("scan_ts") == scan["scan_ts"]
                            and patch.get("ticker") == c.get("ticker")
                            and patch.get("structure") == c.get("structure")):
                        c.update(patch)
                        break
        scans.append(scan)
    return scans, outcomes
