"""
config/roles.py
Structure-level authorization per role.

Each role maps to the list of option structures that role is permitted to see
in live suggestions, enter as paper trades, and view in positions.

"*" means all structures (admin always resolves to ALL_STRUCTURES at runtime).

Usage:
    from config.roles import get_structures_for_role

    allowed = get_structures_for_role("trader")
    # → ["Put Credit Spread", "Call Credit Spread", "Iron Condor", ...]
"""

from __future__ import annotations
from config.structures import ALL_STRUCTURES

# ── Per-role structure allow-lists ────────────────────────────────────────────
#
# Levels (must match features.toml [roles.hierarchy]):
#   observer  = 1   read-only viewer
#   trader    = 2   scan + paper trade (core structures)
#   analyst   = 3   full view inc. live positions and reporting
#   admin     = 10  full access, all structures
#
# When adding a new structure:
#   - It auto-appears for admin (ALL_STRUCTURES is registry-derived)
#   - Add it explicitly to lower roles only when you want to expose it

_ROLE_STRUCTURES: dict[str, list[str] | str] = {
    "observer": [
        "Put Credit Spread",
        "Call Credit Spread",
    ],
    "trader": [
        "Put Credit Spread",
        "Call Credit Spread",
        "Iron Condor",
        "Call Debit Spread",
        "Put Debit Spread",
        "Cash Secured Put",
    ],
    "analyst": [
        "Put Credit Spread",
        "Call Credit Spread",
        "Iron Condor",
        "Call Debit Spread",
        "Put Debit Spread",
        "Cash Secured Put",
        "Jade Lizard",
        "Calendar Spread",
        "Diagonal Spread",
    ],
    "admin": "*",   # wildcard — resolves to ALL_STRUCTURES
}


def get_structures_for_role(role: str | None) -> list[str]:
    """
    Return the list of structure names permitted for the given role.
    Returns empty list for unknown or None roles.
    Admin (or any role mapped to '*') gets every registered structure.
    """
    if not role:
        return []
    mapping = _ROLE_STRUCTURES.get(role)
    if mapping is None:
        return []
    if mapping == "*":
        return list(ALL_STRUCTURES)
    return list(mapping)


def is_structure_allowed(role: str | None, structure: str) -> bool:
    """Return True if role can see/trade the given structure."""
    return structure in get_structures_for_role(role)
