"""Rule constants, loaded from config/settings.toml.

Edit config/settings.toml to change defaults - this module just exposes
those values as flat names for the rest of the codebase.
"""
import tomllib
from pathlib import Path

_SETTINGS_PATH = Path(__file__).parent / "settings.toml"
with open(_SETTINGS_PATH, "rb") as f:
    _settings = tomllib.load(f)

CAPITAL = _settings["capital"]["amount"]
MAX_RISK_PCT = _settings["capital"]["max_risk_pct"]
MAX_CONCURRENT_POSITIONS = _settings["capital"]["max_concurrent_positions"]

MIN_DTE = _settings["dte"]["min_dte"]
MAX_DTE = _settings["dte"]["max_dte"]
EVENT_BLACKOUT_DAYS = _settings["dte"]["event_blackout_days"]

IV_RANK_HIGH_THRESHOLD = _settings["iv"]["iv_rank_high_threshold"]

SMA_SHORT = _settings["trend"]["sma_short"]
SMA_LONG = _settings["trend"]["sma_long"]
TREND_BAND_PCT = _settings["trend"]["band_pct"]

CREDIT_SHORT_DELTA_RANGE = (_settings["credit_spread"]["short_delta_lo"], _settings["credit_spread"]["short_delta_hi"])
CREDIT_MIN_CREDIT_PCT_OF_WIDTH = _settings["credit_spread"]["min_credit_pct_of_width"]

DEBIT_LONG_DELTA_RANGE = (_settings["debit_spread"]["long_delta_lo"], _settings["debit_spread"]["long_delta_hi"])
DEBIT_SHORT_DELTA_RANGE = (_settings["debit_spread"]["short_delta_lo"], _settings["debit_spread"]["short_delta_hi"])

MAX_LOSS_PER_TRADE = CAPITAL * MAX_RISK_PCT  # ~$120

MIN_OPEN_INTEREST = _settings["liquidity"]["min_open_interest"]
MIN_PROFIT_AMOUNT = _settings["liquidity"]["min_profit_amount"]

CALENDAR_MIN_GAP_DAYS = _settings["calendar"]["min_gap_days"]
CALENDAR_MAX_GAP_DAYS = _settings["calendar"]["max_gap_days"]

JADE_LIZARD_PUT_DELTA_RANGE = (_settings["jade_lizard"]["put_delta_lo"], _settings["jade_lizard"]["put_delta_hi"])

PROFIT_TARGET_PCT  = _settings["management"]["profit_target_pct"]
STOP_LOSS_MULT     = _settings["management"]["stop_loss_mult"]
EARLY_CLOSE_PCT    = _settings["management"]["early_close_pct"]
MARKET_CLOSE_HOUR  = _settings["management"]["market_close_hour"]

DIAGONAL_LONG_DELTA_RANGE  = (_settings["diagonal"]["long_delta_lo"],  _settings["diagonal"]["long_delta_hi"])
DIAGONAL_SHORT_DELTA_RANGE = (_settings["diagonal"]["short_delta_lo"], _settings["diagonal"]["short_delta_hi"])
DIAGONAL_MIN_GAP_DAYS      = _settings["diagonal"]["min_gap_days"]
DIAGONAL_MAX_GAP_DAYS      = _settings["diagonal"]["max_gap_days"]

RISK_LIMITS = _settings["risk_limits"]

# Strike/width search grids for best-EV optimizer
CREDIT_DELTA_GRID      = _settings["optimize_grid"]["credit_delta_grid"]
WIDTH_GRID             = _settings["optimize_grid"]["width_grid"]
DEBIT_LONG_DELTA_GRID  = _settings["optimize_grid"]["debit_long_delta_grid"]
DEBIT_SHORT_DELTA_GRID = _settings["optimize_grid"]["debit_short_delta_grid"]

# IV edge / vol-surface thresholds
IV_EDGE_SKIP_VP    = _settings["iv_edge"]["skip_vp"]
IV_EDGE_FLAG_VP    = _settings["iv_edge"]["flag_vp"]
IV_EDGE_BONUS_SCALE = _settings["iv_edge"]["bonus_scale"]

# Market constants
RISK_FREE_RATE = _settings["market"]["risk_free_rate"]

# Portfolio risk caps used in candidate_provider
MAX_PORTFOLIO_VEGA       = _settings["risk_limits"]["max_portfolio_vega"]
BREAKEVEN_CUSHION_IV_SCALE = _settings["risk_limits"]["breakeven_cushion_iv_scale"]
