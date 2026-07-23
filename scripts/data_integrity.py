"""
scripts/data_integrity.py
Pre-scoring data quality check.

Runs before compute_signal_alignment() and stamps None on any market dict
field whose underlying data is absent or provably stale.  Evaluators already
return score=None for any market field that is None, which excludes that
signal from both the numerator and effective_max — so this layer requires no
changes to the evaluators themselves.

Usage (in compute_signal_alignment or select_structure):
    from scripts.data_integrity import check_data_quality, apply_quality_mask
    report  = check_data_quality(atm_iv, hv_data, hv30_data, vol_skew_data, iv_ts, calls, puts)
    market  = apply_quality_mask(market, report)
    # market now has None for every stale field — evaluators handle the rest.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── DataQualityReport ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DataQualityReport:
    """
    Outcome of the pre-scoring data integrity check.

    integrity_ok      True when no hard data failures were detected.
                      False does NOT block scoring — it stamps fields None so
                      evaluators skip them, preserving partial signal coverage.
    stale_fields      Market-dict keys that were nulled.  Each entry here means
                      the evaluator for that field will return score=None, so
                      the signal is excluded from effective_max entirely.
    warnings          Soft issues worth surfacing in the UI but not nulling any
                      field (e.g. thin chain, borderline IV).
    iv_stale          True when ATM IV is absent or near-zero; downstream
                      IV-derived signals (iv_premium, iv_rank_52w, vol_skew_pct,
                      iv_term_shape) are all nulled.
    hv_stale          True when historical-vol data could not be computed.
    chain_thin        True when either puts or calls have fewer than 5 strikes
                      with non-zero open interest.
    """
    integrity_ok:  bool
    stale_fields:  tuple[str, ...]
    warnings:      tuple[str, ...]
    iv_stale:      bool
    hv_stale:      bool
    chain_thin:    bool


# ── Public API ────────────────────────────────────────────────────────────────

def check_data_quality(
    atm_iv:        float | None,
    hv_data:       dict | None,
    hv30_data:     dict | None,
    vol_skew_data: dict | None,
    iv_ts:         dict | None,
    calls,                           # pandas DataFrame
    puts,                            # pandas DataFrame
) -> DataQualityReport:
    """
    Inspect raw data objects and return a report describing what is stale or missing.

    Parameters correspond exactly to the local variables already present in
    analyze_ticker() at the point just after all option-chain data is fetched.
    """
    stale:    list[str] = []
    warnings: list[str] = []

    # ── IV staleness ─────────────────────────────────────────────────────────
    # ATM IV near-zero means the options chain returned no implied volatility —
    # typically because E*TRADE Greeks were unavailable (after-hours or data lag).
    # When IV is stale, every signal that depends on it must be excluded.
    iv_stale = (atm_iv is None) or (atm_iv < 1e-3)
    if iv_stale:
        warnings.append(
            f"ATM IV {'absent' if atm_iv is None else f'{atm_iv:.4f}'} — "
            "IV-derived signals (iv_premium, iv_rank_52w, iv_term_shape, vol_skew_pct) excluded"
        )
        stale.extend(["iv_premium", "iv_rank_52w", "iv_term_shape", "vol_skew_pct"])

    # Independent staleness checks even when IV is nominally non-zero
    if vol_skew_data is None and "vol_skew_pct" not in stale:
        stale.append("vol_skew_pct")
        warnings.append("Vol skew data unavailable — skew signal excluded")

    if iv_ts is None and "iv_term_shape" not in stale:
        stale.append("iv_term_shape")
        warnings.append("IV term structure data unavailable — iv_term signal excluded")

    # ── HV staleness ─────────────────────────────────────────────────────────
    hv_stale = hv_data is None
    if hv_stale:
        stale.append("iv_premium")   # iv_premium = atm_iv - hv20; no HV → no premium
        warnings.append("HV20 data unavailable — iv_premium signal excluded")

    hv30_absent = hv30_data is None
    if hv30_absent:
        stale.append("hv30")
        warnings.append("HV30 data unavailable — hv30 signal excluded")

    # ── Chain quality ─────────────────────────────────────────────────────────
    chain_thin = False
    try:
        import pandas as pd
        puts_liquid  = puts[puts["openInterest"].fillna(0) > 0]  if puts  is not None else None
        calls_liquid = calls[calls["openInterest"].fillna(0) > 0] if calls is not None else None
        thin_threshold = 5
        if (puts_liquid  is not None and len(puts_liquid)  < thin_threshold) or \
           (calls_liquid is not None and len(calls_liquid) < thin_threshold):
            chain_thin = True
            warnings.append(
                f"Thin option chain — puts with OI: "
                f"{len(puts_liquid) if puts_liquid is not None else 0}, "
                f"calls with OI: {len(calls_liquid) if calls_liquid is not None else 0} "
                f"(< {thin_threshold} each). Strike selection may be unreliable."
            )
    except Exception:
        pass   # chain quality check is best-effort; never block scoring

    # ── Bid/ask sanity: all bids zero means quotes are stale ─────────────────
    try:
        if puts is not None and not puts.empty:
            put_bids = puts["bid"].fillna(0)
            if (put_bids > 0).sum() == 0:
                chain_thin = True
                warnings.append(
                    "All put bids are zero — option chain quotes may be stale "
                    "(market closed or data provider lag)"
                )
    except Exception:
        pass

    # De-duplicate stale list (e.g. iv_premium may appear from both IV and HV paths)
    seen: set[str] = set()
    deduped: list[str] = []
    for s in stale:
        if s not in seen:
            seen.add(s)
            deduped.append(s)

    return DataQualityReport(
        integrity_ok = len(deduped) == 0,
        stale_fields = tuple(deduped),
        warnings     = tuple(warnings),
        iv_stale     = iv_stale,
        hv_stale     = hv_stale,
        chain_thin   = chain_thin,
    )


def apply_quality_mask(market: dict, report: DataQualityReport) -> dict:
    """
    Return a copy of market with stale fields set to None.

    The original dict is not mutated so the caller can keep the raw values
    for debugging while scoring uses the masked copy.
    """
    if not report.stale_fields:
        return market
    masked = dict(market)
    for key in report.stale_fields:
        if key in masked:
            masked[key] = None
    return masked
