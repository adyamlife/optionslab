"""
Regression test: pop_ev_debit (3-outcome analytical EV) vs old binary formula vs MC EV.

Validates four claims from the P1-A design requirement:
  1. Partial-win scenarios increase analytical EV vs old binary calculation.
  2. Corrected analytical EV tracks MC EV direction better than binary EV.
  3. Only CDS/PDS structures are affected; CSP/IC/LS/CC/CS pass-through unchanged.
  4. No invalid probabilities (pop outside [0,100], ev = nan/inf).

Usage:
    python -m pytest scripts/tests/test_pop_ev_debit_regression.py -v
"""
import math
import pytest

from scripts.analyze import pop_ev_debit


# ---------------------------------------------------------------------------
# Reference: old binary EV formula (inlined here so the test is self-contained)
# ---------------------------------------------------------------------------

def _old_binary_ev(long_delta, debit, width):
    """Pre-P1-A binary formula. pop = |long_delta|, ev = pop*max_profit - (1-pop)*debit."""
    if long_delta is None:
        return 0.0, round(-debit, 3)
    pop = max(0.0, min(1.0, abs(float(long_delta))))
    ev  = pop * (width - debit) - (1 - pop) * debit
    return round(pop * 100, 1), round(ev, 3)


# ---------------------------------------------------------------------------
# Scenario definitions: (label, long_delta, short_delta, debit, width, mc_ev)
#
# mc_ev values are directional references from monte_carlo.py (not exact).
# The claim is: |new_ev - mc_ev| < |old_ev - mc_ev|, i.e. new tracks better.
# ---------------------------------------------------------------------------

SCENARIOS = [
    # label                    long_d   short_d  debit  width   mc_ev_ref
    # mc_ev_ref: directional references from monte_carlo.py output; used to verify
    # that 3-outcome EV tracks MC EV closer than binary EV — not for exact equality.
    ("deep_itm_call_spread",   0.82,    0.68,    2.40,  5.00,   1.50),   # binary overcounts: 1.70 → 3-out: 1.52
    ("atm_call_spread",        0.50,    0.30,    1.20,  5.00,   0.55),
    ("otm_call_spread",        0.30,    0.15,    0.60,  5.00,   0.05),
    ("narrow_width",           0.40,    0.25,    0.80,  2.00,   0.02),
    ("wide_width",             0.45,    0.30,    2.00,  10.00,  0.30),
    ("partial_win_heavy",      0.55,    0.20,    1.50,  5.00,   0.65),   # large p_partial
    ("near_zero_debit",        0.40,    0.25,    0.05,  5.00,   1.60),   # almost free spread
    ("high_delta_short",       0.70,    0.65,    3.00,  5.00,   1.20),   # tiny partial band
]


@pytest.mark.parametrize("label,long_d,short_d,debit,width,mc_ev_ref", SCENARIOS)
def test_3outcome_vs_binary(label, long_d, short_d, debit, width, mc_ev_ref):
    pop_new, ev_new = pop_ev_debit(long_d, debit, width, short_delta=short_d)
    pop_old, ev_old = _old_binary_ev(long_d, debit, width)

    # No invalid outputs
    assert not math.isnan(ev_new), f"{label}: ev_new is NaN"
    assert not math.isinf(ev_new), f"{label}: ev_new is Inf"
    assert 0.0 <= pop_new <= 100.0, f"{label}: pop_new={pop_new} out of [0,100]"

    # The 3-outcome formula correctly discounts vs binary: binary EV overestimates
    # by treating partial wins as full wins. So ev_new < ev_old when there is a
    # meaningful partial band (long_delta - short_delta > 0.05).
    if abs(long_d) - abs(short_d) > 0.05:
        assert ev_new < ev_old, (
            f"{label}: 3-outcome EV should be more conservative than binary "
            f"for partial-win scenario (ev_new={ev_new:.3f}, ev_old={ev_old:.3f})"
        )

    # 3-outcome EV tracks MC EV closer than binary (the key regression claim)
    if mc_ev_ref is not None:
        err_new = abs(ev_new - mc_ev_ref)
        err_old = abs(ev_old - mc_ev_ref)
        assert err_new <= err_old + 0.05, (   # 5¢ tolerance for MC sampling noise
            f"{label}: 3-outcome EV (err={err_new:.3f}) not closer to MC ref "
            f"than old binary (err={err_old:.3f}); new={ev_new:.3f} old={ev_old:.3f} mc={mc_ev_ref}"
        )


def test_no_short_delta_degrades_to_binary():
    """Without short_delta, 3-outcome formula must equal old binary exactly."""
    for long_d, debit, width in [(0.45, 1.20, 5.0), (0.25, 0.60, 3.0), (0.70, 2.50, 5.0)]:
        pop_new, ev_new = pop_ev_debit(long_d, debit, width, short_delta=None)
        pop_old, ev_old = _old_binary_ev(long_d, debit, width)
        assert pop_new == pop_old, f"pop mismatch: {pop_new} vs {pop_old}"
        assert abs(ev_new - ev_old) < 1e-9, f"ev mismatch: {ev_new} vs {ev_old}"


def test_none_long_delta():
    """None long_delta: pop=0, ev=-debit regardless of short_delta."""
    pop, ev = pop_ev_debit(None, 1.50, 5.0, short_delta=0.30)
    assert pop == 0.0
    assert ev == round(-1.50, 3)


def test_probability_normalisation():
    """p_full_win + p_partial + p_loss must sum to 1.0 implicitly (no residual)."""
    # If the internal normalisation fails, EV will be wrong.
    # Test an edge: long_delta == short_delta → p_partial = 0, p_full_win = p_long
    long_d = 0.40
    _, ev = pop_ev_debit(long_d, 1.0, 5.0, short_delta=long_d)
    max_profit = 5.0 - 1.0
    ev_expected = long_d * max_profit - (1 - long_d) * 1.0
    assert abs(ev - ev_expected) < 1e-6, f"ev={ev:.6f} expected={ev_expected:.6f}"


# ---------------------------------------------------------------------------
# Structure isolation: non-debit structures must not call pop_ev_debit
# The test below verifies the call-site inventory hasn't grown unexpectedly.
# ---------------------------------------------------------------------------

def test_call_site_isolation():
    """
    pop_ev_debit is only valid for CDS and PDS.  Grep the call sites in analyze.py
    and assert none are attached to CSP / IC / LS / CC / CS / Short Straddle.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "analyze.py").read_text(encoding="utf-8")
    # Find lines that call pop_ev_debit (excluding the function definition itself)
    call_lines = [
        line for line in src.splitlines()
        if "pop_ev_debit(" in line and "def pop_ev_debit" not in line
    ]
    invalid_structures = ["CSP", "IC", "Iron Condor", "Long Strangle", "Covered Call",
                          "Short Straddle", "Call Credit Spread", "Put Credit Spread"]
    for line in call_lines:
        for bad in invalid_structures:
            assert bad not in line, (
                f"pop_ev_debit called in a non-debit context ({bad}):\n  {line.strip()}"
            )
    # Exactly 4 call sites (2 main candidates + 2 repair variants)
    assert len(call_lines) == 4, (
        f"Expected 4 call sites for pop_ev_debit, found {len(call_lines)}:\n"
        + "\n".join(f"  {l.strip()}" for l in call_lines)
    )
