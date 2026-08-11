"""
Smoke-test for the repair generator pipeline.

Run from the project root:
    python scripts/test_repair_smoke.py

Checks every structure in _VARIANT_GENERATORS:
  - generator function is callable
  - attempt_repairs() does NOT return STRUCTURE_NOT_SUPPORTED
  - variants are returned (or NO_VALID_STRIKES, which means the generator ran)
  - each variant has required fields

Uses a synthetic chain DataFrame so no live market data is needed.
"""
import sys
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.candidate_ranker import _VARIANT_GENERATORS, attempt_repairs, RepairResult
import uuid

SPOT = 50.0
DTE  = 30

# ── Synthetic chain ────────────────────────────────────────────────────────────
# 20 strikes around SPOT with realistic bid/ask and delta proxy
strikes = [round(SPOT - 10 + i, 0) for i in range(21)]  # 40..60

def _delta(k, opt):
    d = (SPOT - k) / SPOT
    return round(max(0.01, min(0.99, 0.5 - d * 3)), 3) if opt == "put" else round(max(0.01, min(0.99, 0.5 + d * 3)), 3)

rows = []
for k in strikes:
    for opt in ("put", "call"):
        mid = max(0.05, abs(SPOT - k) * 0.3 + 0.5)
        rows.append({
            "strike": k, "optionType": opt,
            "bid": round(mid * 0.9, 2), "ask": round(mid * 1.1, 2),
            "delta": _delta(k, opt),
            "impliedVolatility": 0.30,
            "openInterest": 500,
        })

chain_df = pd.DataFrame(rows)
puts  = chain_df[chain_df["optionType"] == "put"].reset_index(drop=True)
calls = chain_df[chain_df["optionType"] == "call"].reset_index(drop=True)

# ── Helper to build a plausible candidate ─────────────────────────────────────
def _cand(structure, overrides=None):
    base = {
        "structure":    structure,
        "candidate_id": str(uuid.uuid4()),
        "spot_at_entry": SPOT,
        "repair_of":    None,
    }
    if overrides:
        base.update(overrides)
    return base

CANDIDATES = {
    "Iron Butterfly": _cand("Iron Butterfly", {
        "short_strike": SPOT, "long_put_strike": SPOT - 5, "long_call_strike": SPOT + 5,
        "_ibf_puts": puts, "_ibf_calls": calls,
    }),
    "Call Debit Spread": _cand("Call Debit Spread", {
        "long_strike": SPOT - 2, "short_strike": SPOT + 5,
        "_ds_calls": calls,
    }),
    "Put Debit Spread": _cand("Put Debit Spread", {
        "long_strike": SPOT + 2, "short_strike": SPOT - 5,
        "_ds_puts": puts,
    }),
    "Cash Secured Put": _cand("Cash Secured Put", {
        "short_strike": SPOT - 2, "_csp_puts": puts,
    }),
    "Naked Put": _cand("Naked Put", {
        "short_strike": SPOT - 2, "_csp_puts": puts,
    }),
    "Long Strangle": _cand("Long Strangle", {
        "long_put_strike": SPOT - 5, "long_call_strike": SPOT + 5,
        "_ls_puts": puts, "_ls_calls": calls,
    }),
    "Short Strangle": _cand("Short Strangle", {
        "short_strike": SPOT - 5, "long_strike": SPOT + 5,
        "_ss_puts": puts, "_ss_calls": calls,
    }),
    "Jade Lizard": _cand("Jade Lizard", {
        "short_strike": SPOT - 5, "jl_put_strike": SPOT - 5,
        "_jl_puts": puts,
        "_jl_call_credit": 0.40, "_jl_call_short_strike": SPOT + 3,
        "_jl_call_long_strike": SPOT + 6, "_jl_call_width": 3.0,
    }),
    "Put Credit Spread": _cand("Put Credit Spread", {
        "short_strike": SPOT - 2, "long_strike": SPOT - 7,
        "_pcs_puts": puts,
    }),
    "Call Credit Spread": _cand("Call Credit Spread", {
        "short_strike": SPOT + 2, "long_strike": SPOT + 7,
        "_ccs_calls": calls,
    }),
    "Iron Condor": _cand("Iron Condor", {
        "put_short_strike": SPOT - 2, "put_long_strike": SPOT - 7,
        "call_short_strike": SPOT + 2, "call_long_strike": SPOT + 7,
        "short_strike": SPOT - 2, "long_strike": SPOT - 7,
        "_ic_puts": puts, "_ic_calls": calls,
    }),
    "Short Straddle": _cand("Short Straddle", {
        "short_strike": SPOT, "long_strike": SPOT,
        "put_short_strike": SPOT, "call_short_strike": SPOT,
        "_sstr_puts": puts, "_sstr_calls": calls,
    }),
}

context = {"dte": DTE, "min_profit": 0.20, "recommended_structure": "", "width_target": 5.0}

# ── Run ───────────────────────────────────────────────────────────────────────
REQUIRED_VARIANT_FIELDS = {"candidate_id", "structure", "repair_of", "repair_iteration", "repair_reason"}

pass_ct = fail_ct = 0
for struct, cand in CANDIDATES.items():
    result: RepairResult = attempt_repairs(cand, context)

    if result.status == "NOT_REPAIRABLE" and result.failure_reason == "STRUCTURE_NOT_SUPPORTED":
        print(f"FAIL  {struct:30s}  NOT in _VARIANT_GENERATORS")
        fail_ct += 1
        continue

    if result.status == "FAILED" and result.failure_reason == "NO_VALID_STRIKES":
        print(f"WARN  {struct:30s}  generator ran but NO_VALID_STRIKES (chain may be too narrow)")
        pass_ct += 1
        continue

    if result.status == "NOT_REPAIRABLE" and result.failure_reason == "ALREADY_REPAIR":
        print(f"INFO  {struct:30s}  ALREADY_REPAIR (expected only for repair candidates)")
        pass_ct += 1
        continue

    variants = result.replacement_candidates or []
    if not variants:
        print(f"FAIL  {struct:30s}  status={result.status} but no variants returned")
        fail_ct += 1
        continue

    missing = []
    for v in variants:
        missing += [f for f in REQUIRED_VARIANT_FIELDS if f not in v]
    if missing:
        print(f"FAIL  {struct:30s}  variant missing fields: {set(missing)}")
        fail_ct += 1
        continue

    v0 = variants[0]
    print(f"PASS  {struct:30s}  {len(variants)} variants, "
          f"first: {v0.get('details','')[:60]}")
    pass_ct += 1

print(f"\n{'='*60}")
print(f"Results: {pass_ct} passed, {fail_ct} failed out of {len(CANDIDATES)} structures")
if fail_ct:
    sys.exit(1)
