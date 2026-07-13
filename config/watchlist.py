"""Watchlist tiers, loaded from config/settings.toml.

WATCHLIST           — primary: liquid options, used for ML training + paper trades + UI.
WATCHLIST_ARCHIVE   — archive-only: thin OI names, OI/bar/earnings data collected
                      but excluded from ML training and paper trades.
WATCHLIST_ALL       — union of both, used by data_archive.py jobs.

Edit config/settings.toml to change either list.
"""
import tomllib
from pathlib import Path

_SETTINGS_PATH = Path(__file__).parent / "settings.toml"
with open(_SETTINGS_PATH, "rb") as f:
    _settings = tomllib.load(f)

WATCHLIST         = _settings["watchlist"]
WATCHLIST_ARCHIVE = _settings.get("watchlist_archive_only", [])
WATCHLIST_ALL     = WATCHLIST + [t for t in WATCHLIST_ARCHIVE if t not in WATCHLIST]

_extra = _settings.get("paper_trade_extra", [])
PAPER_WATCHLIST   = WATCHLIST + [t for t in _extra if t not in WATCHLIST]
