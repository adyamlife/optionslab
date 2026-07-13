"""
Base dataclasses for the structure registry.

Every option structure is an OptionStructure instance. All callers
(analyze.py, app.py, paper_trade_engine.py, position_file_parser.py, API
responses consumed by JS) derive what they need from these objects instead
of maintaining parallel if/elif chains.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class HedgeStrikeMode(str, Enum):
    """How to compute the target strike(s) for the hedge leg."""
    ONE_WIDTH_BELOW_LO = "one_width_below_lo"   # 1 spread-width below long_strike  (Put Credit)
    ONE_WIDTH_ABOVE_HI = "one_width_above_hi"   # 1 spread-width above long_strike  (Call Credit)
    ONE_WIDTH_BOTH     = "one_width_both"        # wider wings on both sides          (Iron Condor)
    ATM_PUT            = "atm_put"              # ATM put at current price            (Call Debit)
    ATM_CALL           = "atm_call"             # ATM call at current price           (Put Debit)
    OTM_PUT_NEAR_SHORT = "otm_put_near_short"   # put below naked short put strike    (Jade Lizard)
    OTM_STRANGLE       = "otm_strangle"         # OTM put + OTM call                 (Calendar/Diagonal)


class StrikeSchema(str, Enum):
    """Key names used in trade["strikes"] and candidate strike fields."""
    SINGLE_LEG  = "single_leg"    # {"short": x}  — Cash Secured Put, naked single-leg
    TWO_LEG     = "two_leg"       # {"short": x, "long": y}
    IRON_CONDOR = "iron_condor"   # {"put_short": x, "put_long": y, "call_short": z, "call_long": w}
    NONE        = "none"          # Calendar, Diagonal, Jade Lizard (no fixed expiry legs)


@dataclass(frozen=True)
class HedgeDef:
    """Everything needed to compute, price, and display a hedge for a structure."""
    structure:       str            # display name  e.g. "Buy OTM Protective Put"
    details:         str            # plain-English trade instruction
    rationale:       str            # why this hedge makes sense
    protection_note: str            # what the hedge does NOT cover
    cost_pct:        float          # fraction of cost_base to estimate hedge premium
    cost_base:       str            # "max_profit" | "max_loss"
    delta_change:    float          # approximate net-delta shift from adding hedge
    opt_type:        str            # "put" | "call" | "both"
    strike_mode:     HedgeStrikeMode
    urgency:         str = "normal" # "normal" | "critical"


@dataclass(frozen=True)
class OptionStructure:
    """
    Single source of truth for one option strategy.

    Parameters are read by analyze.py for strike selection, by app.py for
    hedge computation, by paper_trade_engine.py for pricing/P&L, and
    serialised into API responses consumed by JS.
    """
    name:           str
    is_credit:      bool
    option_type:    str             # "put" | "call" | "both" | "calendar"
    iv_env:         str             # "High" | "Low" | "Any"
    trend:          str             # "Uptrend" | "Downtrend" | "Range-bound" | "Any"
    strike_schema:  StrikeSchema
    expiry_pnl_fn:  str             # key for expiry P&L dispatch in paper_trade_engine + JS
    hedge:          HedgeDef

    # Delta / width parameters (only the relevant ones are non-zero)
    short_delta_lo:  float = 0.0
    short_delta_hi:  float = 0.0
    long_delta_lo:   float = 0.0
    long_delta_hi:   float = 0.0
    min_credit_pct:  float = 0.0

    # True when a margin account is required (naked/uncovered positions).
    # False for cash-secured, share-backed, or fully defined-risk structures.
    requires_margin: bool  = False

    # How to compute the capital consumed at entry:
    #   "debit"        — premium paid × 100 × contracts  (long options, debit spreads)
    #   "spread_width" — (width − net_credit) × 100      (credit spreads, iron condor)
    #   "margin"       — broker margin formula (~20% notional; Naked Put, Jade Lizard)
    #   "cash_secured" — strike × 100 × contracts        (Cash Secured Put)
    #   "shares"       — must already own 100 shares      (Covered Call)
    capital_type: str = "debit"

    # Target DTE range at entry (0 = not constrained by this structure).
    # Used for strike/expiry selection and paper trade engine filtering.
    dte_min: int = 0
    dte_max: int = 0

    # Profit-target exit threshold as a fraction of max possible gain (0 = no target).
    # e.g. 0.50 means exit when unrealised P&L reaches 50% of the premium paid.
    profit_target_pct: float = 0.0
