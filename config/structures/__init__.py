"""
Structure registry — single source of truth for all option strategies.

Usage:
    from config.structures import STRUCTURES, get, ALL_STRUCTURES, CREDIT_STRUCTURES, DEBIT_STRUCTURES, STRUCTURE_MATRIX

    st = get("Put Credit Spread")
    st.hedge.cost_pct          # → 0.22
    st.hedge.strike_mode       # → HedgeStrikeMode.ONE_WIDTH_BELOW_LO
    st.expiry_pnl_fn           # → "put_credit"
"""

from config.structures.put_credit_spread  import PUT_CREDIT_SPREAD
from config.structures.call_credit_spread import CALL_CREDIT_SPREAD
from config.structures.iron_condor        import IRON_CONDOR
from config.structures.call_debit_spread  import CALL_DEBIT_SPREAD
from config.structures.put_debit_spread   import PUT_DEBIT_SPREAD
from config.structures.jade_lizard        import JADE_LIZARD
from config.structures.calendar_spread    import CALENDAR_SPREAD
from config.structures.diagonal_spread    import DIAGONAL_SPREAD
from config.structures.cash_secured_put   import CASH_SECURED_PUT
from config.structures.covered_call       import COVERED_CALL
from config.structures.naked_put          import NAKED_PUT
from config.structures.leaps_long_call    import LEAPS_LONG_CALL
from config.structures.risk_reversal        import RISK_REVERSAL
from config.structures.bear_combo           import BEAR_COMBO
from config.structures.financed_long_call   import FINANCED_LONG_CALL
from config.structures.financed_long_put    import FINANCED_LONG_PUT
from config.structures.ratio_call_backspread import RATIO_CALL_BACKSPREAD
from config.structures.ratio_put_backspread  import RATIO_PUT_BACKSPREAD
from config.structures.long_strangle        import LONG_STRANGLE
from config.structures._base                import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

_ALL = [
    PUT_CREDIT_SPREAD,
    CALL_CREDIT_SPREAD,
    IRON_CONDOR,
    CALL_DEBIT_SPREAD,
    PUT_DEBIT_SPREAD,
    JADE_LIZARD,
    CALENDAR_SPREAD,
    DIAGONAL_SPREAD,
    CASH_SECURED_PUT,
    COVERED_CALL,
    NAKED_PUT,
    LEAPS_LONG_CALL,
    RISK_REVERSAL,
    BEAR_COMBO,
    FINANCED_LONG_CALL,
    FINANCED_LONG_PUT,
    RATIO_CALL_BACKSPREAD,
    RATIO_PUT_BACKSPREAD,
    LONG_STRANGLE,
]

# Primary registry — keyed by structure name string
STRUCTURES: dict[str, OptionStructure] = {s.name: s for s in _ALL}


def get(name: str) -> OptionStructure:
    """Return the OptionStructure for a given name. Raises KeyError if unknown."""
    return STRUCTURES[name]


def get_or_none(name: str) -> OptionStructure | None:
    return STRUCTURES.get(name)


# ── Derived sets (replace hardcoded lists in analyze.py) ─────────────────────

ALL_STRUCTURES: list[str] = [s.name for s in _ALL]

CREDIT_STRUCTURES: set[str] = {s.name for s in _ALL if s.is_credit}
DEBIT_STRUCTURES:  set[str] = {s.name for s in _ALL if not s.is_credit}

# Candidate map: (iv_env, trend) → list of structure names eligible to compete.
# select_structure() in analyze.py uses this for shortlisting; the list is ordered
# by _ALL registration order, which is the tiebreak when scores are equal.
STRUCTURE_CANDIDATES: dict[tuple[str, str], list[str]] = {}
for _s in _ALL:
    for _iv in _s.allowed_iv:
        for _tr in _s.allowed_trends:
            STRUCTURE_CANDIDATES.setdefault((_iv, _tr), []).append(_s.name)

# Legacy single-winner matrix kept for backward compatibility.
# First registered structure per slot wins; use STRUCTURE_CANDIDATES for ranking.
STRUCTURE_MATRIX: dict[tuple[str, str], str] = {
    slot: names[0]
    for slot, names in STRUCTURE_CANDIDATES.items()
    if names
}
