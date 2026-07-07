"""Watchlist, loaded from config/settings.toml.

Edit config/settings.toml to change the watchlist. Earnings dates are
looked up live via yfinance (see scripts/data_fetch.get_next_earnings_date).
"""
import tomllib
from pathlib import Path

_SETTINGS_PATH = Path(__file__).parent / "settings.toml"
with open(_SETTINGS_PATH, "rb") as f:
    _settings = tomllib.load(f)

WATCHLIST = _settings["watchlist"]
