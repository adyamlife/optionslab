"""
Structure registry — single source of truth for all option strategies.

Routing (which structures compete in each iv_env × trend slot) is defined in
config/structures.toml. Adding a strategy to a slot or reordering candidates
requires only a TOML edit — no Python change.

Complex per-structure metadata (HedgeDef, StrikeSchema, expiry_pnl_fn) lives
in the individual config/structures/*.py files and is imported below.

Usage:
    from config.structures import (
        STRUCTURES, get, get_or_none,
        ALL_STRUCTURES, CREDIT_STRUCTURES, DEBIT_STRUCTURES,
        STRUCTURE_MATRIX, STRUCTURE_CANDIDATES,
        preferred_delta,
    )

    st = get("Put Credit Spread")
    st.hedge.cost_pct          # → 0.22
    st.expiry_pnl_fn           # → "put_credit"

    preferred_delta("Iron Condor")   # → 0.15  (from structures.toml)
"""

from __future__ import annotations

from pathlib import Path

from config.structures.put_credit_spread   import PUT_CREDIT_SPREAD
from config.structures.call_credit_spread  import CALL_CREDIT_SPREAD
from config.structures.iron_condor         import IRON_CONDOR
from config.structures.call_debit_spread   import CALL_DEBIT_SPREAD
from config.structures.put_debit_spread    import PUT_DEBIT_SPREAD
from config.structures.jade_lizard         import JADE_LIZARD
from config.structures.calendar_spread     import CALENDAR_SPREAD
from config.structures.diagonal_spread     import DIAGONAL_SPREAD
from config.structures.cash_secured_put    import CASH_SECURED_PUT
from config.structures.covered_call        import COVERED_CALL
from config.structures.naked_put           import NAKED_PUT
from config.structures.leaps_long_call     import LEAPS_LONG_CALL
from config.structures.risk_reversal       import RISK_REVERSAL
from config.structures.bear_combo          import BEAR_COMBO
from config.structures.financed_long_call  import FINANCED_LONG_CALL
from config.structures.financed_long_put   import FINANCED_LONG_PUT
from config.structures.ratio_call_backspread import RATIO_CALL_BACKSPREAD
from config.structures.ratio_put_backspread  import RATIO_PUT_BACKSPREAD
from config.structures.long_strangle       import LONG_STRANGLE
from config.structures.long_call           import LONG_CALL
from config.structures.long_put            import LONG_PUT
from config.structures.iron_butterfly      import IRON_BUTTERFLY
from config.structures.short_strangle      import SHORT_STRANGLE
from config.structures.short_straddle      import SHORT_STRADDLE
from config.structures.long_straddle       import LONG_STRADDLE
from config.structures.bull_call_ladder    import BULL_CALL_LADDER
from config.structures.double_calendar     import DOUBLE_CALENDAR
from config.structures.poor_mans_covered_call import POOR_MANS_COVERED_CALL
from config.structures._base               import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

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
    LONG_CALL,
    LONG_PUT,
    IRON_BUTTERFLY,
    SHORT_STRANGLE,
    SHORT_STRADDLE,
    LONG_STRADDLE,
    BULL_CALL_LADDER,
    DOUBLE_CALENDAR,
    POOR_MANS_COVERED_CALL,
]

# Primary registry — keyed by structure name string (Python files are authoritative
# for complex metadata: HedgeDef, StrikeSchema, expiry_pnl_fn, delta ranges).
STRUCTURES: dict[str, OptionStructure] = {s.name: s for s in _ALL}


def get(name: str) -> OptionStructure:
    """Return the OptionStructure for a given name. Raises KeyError if unknown."""
    return STRUCTURES[name]


def get_or_none(name: str) -> OptionStructure | None:
    return STRUCTURES.get(name)


# ── Derived sets ──────────────────────────────────────────────────────────────

ALL_STRUCTURES: list[str] = [s.name for s in _ALL]

# is_credit sourced from Python objects (authoritative) not TOML
CREDIT_STRUCTURES: set[str] = {s.name for s in _ALL if s.is_credit}
DEBIT_STRUCTURES:  set[str] = {s.name for s in _ALL if not s.is_credit}


# ── Load structures.toml ──────────────────────────────────────────────────────

def _load_toml() -> dict:
    _path = Path(__file__).parent.parent / "structures.toml"
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(_path.read_text(encoding="utf-8"))


_TOML = _load_toml()

# Per-structure config from TOML — keyed by name.
# Provides: is_credit, iv_envs, trends, min_dte, max_dte, preferred_delta.
_STRUCT_CFG: dict[str, dict] = {s["name"]: s for s in _TOML.get("structure", [])}


def preferred_delta(name: str) -> float | None:
    """Return the preferred short-leg entry delta for a structure, or None if not set."""
    cfg = _STRUCT_CFG.get(name)
    if cfg is None:
        return None
    val = cfg.get("preferred_delta", 0.0)
    return float(val) if val else None


# ── STRUCTURE_CANDIDATES and STRUCTURE_MATRIX from TOML [[slot]] entries ─────
#
# Previously these were derived by iterating each structure's allowed_iv ×
# allowed_trends. Now the TOML [[slot]] entries are authoritative so that:
#   - candidate ordering (= tiebreak when scores are equal) is explicit and
#     visible in one file rather than implicit in the _ALL list order
#   - adding a structure to a slot or reordering requires only a TOML edit

STRUCTURE_CANDIDATES: dict[tuple[str, str], list[str]] = {}

for _slot in _TOML.get("slot", []):
    _key   = (_slot["iv_env"], _slot["trend"])
    _names = _slot.get("candidates", [])
    # Guard: warn if any name is not in the Python registry
    _unknown = [n for n in _names if n not in STRUCTURES]
    if _unknown:
        import warnings
        warnings.warn(
            f"structures.toml slot {_key} references unknown structure(s): {_unknown}. "
            "Add the corresponding Python file and register it in __init__.py.",
            stacklevel=1,
        )
    STRUCTURE_CANDIDATES[_key] = [n for n in _names if n in STRUCTURES]

# Single-winner matrix: first candidate per slot.
# Used as a preliminary structure in analyze_ticker() before signal scoring.
STRUCTURE_MATRIX: dict[tuple[str, str], str] = {
    slot: names[0]
    for slot, names in STRUCTURE_CANDIDATES.items()
    if names
}
