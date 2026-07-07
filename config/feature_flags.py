"""
config/feature_flags.py
Feature-flag and role-definition loader for OptionLab.

Usage
-----
Python:
    from config.feature_flags import ff

    # Feature checks
    if ff.enabled("market_context"):                     ...
    if ff.allowed("live_scan_ai_assessment", role):      ...
    tickers = ff.get("market_context_index_futures", "tickers", default=[])

    # Role checks (no hardcoded role names in calling code)
    if ff.role_gte(role, "trader"):    ...   # role is trader-level or above
    if ff.is_top_role(role):           ...   # highest privilege level
    allowed_prefixes = ff.route_prefixes(role)   # None = unrestricted

Jinja2 (injected by app.py context processor):
    {% if features.market_context.enabled %}
    {% if ff.role_gte(current_role, "trader") %}

JavaScript (window.__FEATURES__ injected in base.html):
    if (window.__FEATURES__?.market_context?.enabled) { ... }

Adding a new role
-----------------
1. Add it to [roles.hierarchy] in features.toml with a level number.
2. Add route prefixes to [roles.routes] if needed (omit for full access).
3. Assign the role name to a user in secrets.toml.
4. Done — no Python changes required.

Adding a new feature flag
--------------------------
1. Add a [features.<name>] block to features.toml.
2. Done — no Python changes required.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

try:
    import tomllib                    # Python 3.11+
except ImportError:
    import tomli as tomllib           # pip install tomli


_TOML_PATH  = Path(__file__).parent / "features.toml"
_CACHE_TTL  = 60   # seconds before re-reading the file (0 = always re-read)

# "any" is the implicit baseline role — every authenticated user satisfies it.
_ANY_ROLE   = "any"
_ANY_LEVEL  = 0


class FeatureFlags:
    """
    Unified loader for feature flags and role definitions.

    Both live in features.toml under [features.*] and [roles.*].
    No role names or levels are hardcoded here — everything is data-driven.
    """

    def __init__(self, path: Path = _TOML_PATH) -> None:
        self._path      = path
        self._features: dict[str, Any] = {}
        self._hierarchy: dict[str, int] = {}   # role_name → level
        self._routes:    dict[str, list[str] | None] = {}   # role_name → prefixes | None
        self._loaded_at: float = 0.0
        self._load()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        raw = tomllib.loads(self._path.read_text(encoding="utf-8"))

        self._features  = raw.get("features", {})

        roles = raw.get("roles", {})
        self._hierarchy = {_ANY_ROLE: _ANY_LEVEL, **roles.get("hierarchy", {})}

        # Build route map: roles without an entry get None (= full access)
        raw_routes = roles.get("routes", {})
        self._routes = {}
        for role in self._hierarchy:
            if role == _ANY_ROLE:
                continue
            prefixes = raw_routes.get(role)
            # None means "unrestricted"; a list means "only these prefixes"
            self._routes[role] = prefixes if prefixes is not None else None

        self._loaded_at = time.monotonic()

    def _maybe_reload(self) -> None:
        if _CACHE_TTL > 0 and time.monotonic() - self._loaded_at > _CACHE_TTL:
            try:
                self._load()
            except Exception:
                pass   # keep stale data rather than crashing on a bad TOML edit

    def _section(self, name: str) -> dict[str, Any]:
        self._maybe_reload()
        return self._features.get(name, {})

    def _level(self, role: str | None) -> int:
        self._maybe_reload()
        if not role:
            return -1
        return self._hierarchy.get(role, -1)

    # ── Feature API ───────────────────────────────────────────────────────────

    def enabled(self, name: str) -> bool:
        """Return True if the feature exists and is enabled."""
        return bool(self._section(name).get("enabled", False))

    def allowed(self, name: str, role: str | None) -> bool:
        """
        Return True if the feature is enabled AND role satisfies role_required.
        Unauthenticated (role=None) never passes.
        """
        if not role:
            return False
        section = self._section(name)
        if not section.get("enabled", False):
            return False
        required = section.get("role_required", _ANY_ROLE)
        return self._level(role) >= self._level(required)

    def get(self, name: str, key: str, default: Any = None) -> Any:
        """Read an arbitrary metadata key from a feature section."""
        return self._section(name).get(key, default)

    def as_dict(self) -> dict[str, Any]:
        """
        Full features dict for Jinja2 context and JavaScript injection.
        Keys are feature names; values are the raw section dicts.
        """
        self._maybe_reload()
        return dict(self._features)

    # ── Role API ──────────────────────────────────────────────────────────────

    def role_gte(self, role: str | None, required: str) -> bool:
        """True if role's privilege level >= required's level."""
        return self._level(role) >= self._level(required)

    def is_top_role(self, role: str | None) -> bool:
        """True if role has the highest privilege level defined."""
        self._maybe_reload()
        if not role:
            return False
        top = max(self._hierarchy.values())
        return self._level(role) >= top

    def route_prefixes(self, role: str | None) -> list[str] | None:
        """
        Return the list of allowed route prefixes for role, or None for
        unrestricted access. Returns an empty list for unknown roles.
        """
        self._maybe_reload()
        if not role:
            return []
        return self._routes.get(role, [] if role not in self._hierarchy else None)

    def all_roles(self) -> dict[str, int]:
        """Return the full role→level mapping (excludes 'any')."""
        self._maybe_reload()
        return {k: v for k, v in self._hierarchy.items() if k != _ANY_ROLE}

    # ── Misc ──────────────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Force a reload from disk (call after editing features.toml at runtime)."""
        self._load()


# Singleton — import this everywhere
ff = FeatureFlags()
