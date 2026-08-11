"""
Snapshot feature audit — Phase 1 prerequisite for the trade-win model.

Produces a completeness report organized by feature family:
  - Trade geometry  (objective facts about the trade as structured)
  - Optimizer opinion (derived judgments: EV, POP, gates, penalties)
  - Market context  (base-model outputs already in regime_training)

For each candidate feature reports:
  present    — % of labeled rows where the column/key exists at all
  non_null   — % of labeled rows where the value is non-null / non-NaN
  entry_safe — manually flagged: is this value available at entry time?

Run:
  python -m scripts.audit_snapshot_features
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ── Feature catalogue ─────────────────────────────────────────────────────────
# Each entry: (family, field_path, entry_safe_flag)
# field_path uses dot notation for nested JSON:  "greeks.delta"
# entry_safe=True  → available at scan/entry time (no leakage risk)
# entry_safe=False → populated only after expiry (leakage if used as feature)
# entry_safe="?"   → unknown — audit will flag for manual review

_CATALOGUE: list[tuple[str, str, object]] = [
    # ── Trade geometry ────────────────────────────────────────────────────────
    ("geometry", "structure",           True),
    ("geometry", "ticker",              True),
    ("geometry", "dte",                 True),
    ("geometry", "expiry",              True),
    ("geometry", "entry_price",         True),
    ("geometry", "spread_width",        True),
    ("geometry", "max_profit",          True),
    ("geometry", "max_loss",            True),
    ("geometry", "breakeven_lower",     True),
    ("geometry", "breakeven_upper",     True),
    ("geometry", "capital_required",    True),
    ("geometry", "credit",              True),
    ("geometry", "debit",               True),
    ("geometry", "short_strike",        True),
    ("geometry", "long_strike",         True),
    ("geometry", "short_strike_pct",    True),   # strikes as % of spot
    ("geometry", "long_strike_pct",     True),
    # ── Greeks ────────────────────────────────────────────────────────────────
    ("greeks",   "net_delta",           True),
    ("greeks",   "net_gamma",           True),
    ("greeks",   "net_theta",           True),
    ("greeks",   "net_vega",            True),
    ("greeks",   "greeks.delta",        True),
    ("greeks",   "greeks.gamma",        True),
    ("greeks",   "greeks.theta",        True),
    ("greeks",   "greeks.vega",         True),
    # ── Optimizer opinion ─────────────────────────────────────────────────────
    ("optimizer","pop",                 True),
    ("optimizer","ev",                  True),
    ("optimizer","expected_value",      True),
    ("optimizer","meets_min_profit",    True),
    ("optimizer","quality_score",       True),
    ("optimizer","quality_flag",        True),
    ("optimizer","gamma_penalty",       True),
    ("optimizer","vega_penalty",        True),
    ("optimizer","gate_penalties",      True),
    ("optimizer","score",               True),
    ("optimizer","rank",                True),
    # ── Outcome (should NOT be features — confirming leakage boundary) ────────
    ("outcome",  "outcome.win",         False),
    ("outcome",  "outcome.forward_return", False),
    ("outcome",  "outcome.expiry_price",   False),
    # ── Market context (already in regime_training — confirm join feasibility) -
    ("market",   "iv_rank",             True),
    ("market",   "iv_percentile",       True),
    ("market",   "underlying_price",    True),
    ("market",   "expected_move",       True),
]


def _extract(row: dict, path: str):
    """Extract a value from a flat or dot-notated path in a row dict."""
    parts = path.split(".", 1)
    val = row.get(parts[0])
    if len(parts) == 1:
        return val
    if isinstance(val, dict):
        return _extract(val, parts[1])
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return _extract(parsed, parts[1])
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and np.isnan(v):
        return True
    return False


def run():
    from scripts.db import connect, SNAPSHOTS_TABLE

    print("Loading labeled snapshots...")
    with connect(read_only=True) as con:
        df = con.execute(
            f"SELECT * FROM {SNAPSHOTS_TABLE} WHERE labeled = true"
        ).df()

    n = len(df)
    print(f"Labeled rows: {n:,}\n")

    # ── Print one example row (column names + raw values) ────────────────────
    print("=" * 70)
    print("Column inventory (first labeled row, raw values)")
    print("=" * 70)
    row0 = df.iloc[0].to_dict()
    for col, val in row0.items():
        preview = str(val)[:120].replace("\n", " ")
        print(f"  {col:<30}  {preview}")
    print()

    # ── JSON field discovery — union of all keys in any JSON columns ──────────
    json_cols = [c for c in df.columns if df[c].dtype == object]
    discovered_json_keys: set[str] = set()
    for col in json_cols:
        sample = df[col].dropna().head(200)
        for raw in sample:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    discovered_json_keys.update(parsed.keys())
            except (json.JSONDecodeError, TypeError):
                pass

    if discovered_json_keys:
        print("=" * 70)
        print("JSON keys discovered in first 200 rows:")
        for k in sorted(discovered_json_keys):
            print(f"  {k}")
        print()

    # ── Completeness report ───────────────────────────────────────────────────
    # Flatten each row into a dict for path-based extraction
    rows_as_dicts = []
    for _, r in df.iterrows():
        flat = r.to_dict()
        # merge any JSON column payloads into a shallow union
        for col in json_cols:
            raw = flat.get(col)
            if raw is None:
                continue
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    flat.update(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        rows_as_dicts.append(flat)

    families: dict[str, list] = {}
    for family, path, entry_safe in _CATALOGUE:
        families.setdefault(family, [])
        vals = [_extract(r, path) for r in rows_as_dicts]
        present  = sum(1 for v in vals if v is not None) / n
        non_null = sum(1 for v in vals if not _is_missing(v)) / n
        families[family].append({
            "field": path,
            "present": present,
            "non_null": non_null,
            "entry_safe": entry_safe,
        })

    print("=" * 70)
    print(f"{'Field':<30} {'Present':>8} {'Non-null':>9} {'Entry-safe':>11}")
    print("=" * 70)
    for family, entries in families.items():
        print(f"\n── {family.upper()} ──")
        for e in entries:
            es = "✓" if e["entry_safe"] is True else ("✗ LEAKAGE" if e["entry_safe"] is False else "?")
            present_pct  = f"{e['present']:.0%}"
            non_null_pct = f"{e['non_null']:.0%}"
            flag = "  ← sparse" if e["non_null"] < 0.50 else ""
            print(f"  {e['field']:<28} {present_pct:>8} {non_null_pct:>9} {es:>11}{flag}")

    print("\n" + "=" * 70)
    print("Note: 'present' = field exists in row; 'non_null' = has usable value.")
    print("Sparse (<50% non-null) features flagged — decide: impute, allow NaN, or exclude.")


if __name__ == "__main__":
    run()
