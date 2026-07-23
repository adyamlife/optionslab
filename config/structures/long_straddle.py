from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile, StructureConstraints

LONG_STRADDLE = OptionStructure(
    name          = "Long Straddle",
    abbr          = "LSD",
    is_credit     = False,
    option_type   = "both",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Range-bound",),
    strike_schema = StrikeSchema.TWO_LEG,   # ATM call + ATM put, same strike
    expiry_pnl_fn = "long_straddle",
    # Buy ATM call and ATM put (~0.50 delta each)
    long_delta_lo  = 0.45,
    long_delta_hi  = 0.55,
    capital_type    = "debit",
    requires_margin = False,
    dte_min = 21,
    dte_max = 60,
    profit_target_pct = 0.50,
    # Gate: earnings must be within 21 days — Long Straddle's thesis is the event itself.
    # Absent earnings data also triggers rejection (unknown catalyst → cannot evaluate).
    constraints = StructureConstraints(earnings_dte_max=21),
    hedge = HedgeDef(
        structure    = "No hedge — max loss is the total premium paid",
        details      = "Both legs expire worthless if the stock stays near the strike at expiry. Max loss = total debit paid.",
        rationale    = "Long Straddle is a pure long-vol play. Buying ATM options means break-even requires a move equal to the premium paid in either direction.",
        protection_note = "Close early if IV contracts sharply without a move. Vega losses accelerate when vol collapses.",
        cost_pct     = 0.0,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_BELOW_LO,
    ),
    signal_profile = SignalProfile(
        bias="volatility",
        needs_trend=False, needs_momentum=True,
        uses_term_structure=False, uses_skew=True, uses_sentiment=True,
    ),
)
