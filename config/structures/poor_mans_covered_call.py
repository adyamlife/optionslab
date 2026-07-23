from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

POOR_MANS_COVERED_CALL = OptionStructure(
    name          = "Poor Man's Covered Call",
    abbr          = "PMC",
    is_credit     = False,
    option_type   = "call",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Uptrend",),
    # Back-month deep ITM long call (LEAPS-like) + front-month short OTM call.
    # Different expiries, no fixed-strike schema — same calendar/diagonal family.
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "poor_mans_covered_call",
    # Back-month long leg: deep ITM, high delta, acts like stock ownership
    long_delta_lo  = 0.70,
    long_delta_hi  = 0.85,
    # Front-month short leg: OTM call, generates recurring premium
    short_delta_lo = 0.20,
    short_delta_hi = 0.35,
    capital_type    = "debit",
    requires_margin = False,
    # Back-month leg targets 90–180 DTE; front-month short rolled monthly
    dte_min = 21,
    dte_max = 180,
    profit_target_pct = 0.50,
    hedge = HedgeDef(
        structure    = "Buy OTM Protective Put",
        details      = "Buy an OTM put on the same underlying to cap downside if the bullish thesis fails.",
        rationale    = "PMCC has meaningful downside exposure through the long LEAPS call. A protective put limits max loss if the stock trends sharply lower before the long leg can be rolled.",
        protection_note = "Put hedge adds cost that partially offsets short-call premium collected. Useful only when holding through a binary event or extended drawdown.",
        cost_pct     = 0.20,
        cost_base    = "max_loss",
        delta_change = -0.20,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ATM_PUT,
    ),
    signal_profile = SignalProfile(
        bias="bullish",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=True, uses_skew=False, uses_sentiment=True,
    ),
)
