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


@dataclass(frozen=True)
class StructureConstraints:
    """
    Hard pass/fail gates for a structure.  Checked before signal scoring —
    a structure that fails any constraint is excluded from the candidate list
    regardless of its signal alignment score.

    All fields default to the most permissive value (0 / None / empty) so that
    structures without a constraints object still behave as before.
    """
    min_dte:              int   = 0      # minimum days to expiry
    max_dte:              int   = 0      # maximum days to expiry (0 = unconstrained)
    earnings_dte_min:     int   = 0      # hard-reject if earnings DTE < this value
                                         #   credit structures: earnings too close → event risk
    earnings_dte_max:     int   = 0      # hard-reject if earnings DTE > this value
                                         #   vol structures: earnings too far → no catalyst
    min_iv_rank:          float = 0.0    # minimum iv_rank_52w required [0.0–1.0]
    max_iv_rank:          float = 1.0    # maximum iv_rank_52w allowed   [0.0–1.0]
    allowed_trends:       tuple[str, ...] = ()  # empty = all trends pass


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
class SignalProfile:
    """
    Declares which signal categories are applicable to this structure.

    compute_signal_alignment() reads this to decide which checks to run and
    how to interpret them.  Adding a new structure and filling in its profile
    is sufficient to get full signal coverage — no changes to the scoring
    function are needed.

    bias
        "bullish"     — profits from upward price movement
        "bearish"     — profits from downward price movement
        "neutral"     — profits from range-bound, low-volatility markets
        "volatility"  — profits from large moves (direction agnostic)
        "directional" — direction inferred from current trend at score time
                        (used by Diagonal Spread which can be either)
    needs_trend
        True when EMA200, ADX, and weekly-trend checks are applicable.
        Generally True for all directional and neutral structures; False only
        for pure-vol plays where direction is irrelevant.
    needs_momentum
        True when RSI and MACD checks are applicable.
        Almost always True — even neutral/vol structures benefit from knowing
        whether momentum is extended before entry.
    uses_term_structure
        True when the IV term-structure shape (backwardation vs contango)
        provides genuine edge information.  Calendar, Diagonal, and
        ratio backspreads all have a near-term short leg vs far-term long leg.
    uses_skew
        True when the put/call skew differential is a relevant pricing signal.
    uses_sentiment
        True when news, PCR, analyst revisions, and short-interest checks
        should run.  False only if the structure is purely mechanistic
        (e.g. LEAPS, where sentiment noise outweighs the signal at long DTE).
    """
    bias:                str   # see docstring
    needs_trend:         bool
    needs_momentum:      bool
    uses_term_structure: bool
    uses_skew:           bool
    uses_sentiment:      bool
    # True for structures that buy MORE options than they sell (ratio backspreads).
    # Contango (back IV cheap) is advantageous — they want cheap back-month options.
    # Flips the IV term scoring direction from the default (backwardation = good).
    prefers_contango:    bool = False


@dataclass(frozen=True)
class OptionStructure:
    """
    Single source of truth for one option strategy.

    Parameters are read by analyze.py for strike selection, by app.py for
    hedge computation, by paper_trade_engine.py for pricing/P&L, and
    serialised into API responses consumed by JS.
    """
    name:            str
    abbr:            str              # unique 3-char trade-ID tag (e.g. "CDS", "CAL", "ICO")
    is_credit:       bool
    option_type:     str                  # "put" | "call" | "both" | "calendar"
    allowed_iv:      tuple[str, ...]      # subset of {"High", "Low"}
    allowed_trends:  tuple[str, ...]      # subset of {"Uptrend", "Downtrend", "Range-bound"}
    strike_schema:   StrikeSchema
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

    # Hard pass/fail gates checked before signal scoring.
    # None = unconstrained (all market states pass).
    constraints: StructureConstraints | None = None

    # Signal-scoring profile — controls which checks run in compute_signal_alignment().
    # Default covers all checks with a bullish/neutral bias so existing structures that
    # haven't been updated yet still receive broad coverage.
    signal_profile: SignalProfile = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.signal_profile is None:
            object.__setattr__(self, "signal_profile", SignalProfile(
                bias="neutral",
                needs_trend=True, needs_momentum=True,
                uses_term_structure=False, uses_skew=True, uses_sentiment=True,
            ))
