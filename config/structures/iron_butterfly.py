from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile, StructureConstraints

IRON_BUTTERFLY = OptionStructure(
    name          = "Iron Butterfly",
    abbr          = "IBF",
    is_credit     = True,
    option_type   = "both",
    allowed_iv      = ("High",),
    allowed_trends  = ("Range-bound",),
    strike_schema = StrikeSchema.IRON_CONDOR,   # same 4-leg shape; ATM shorts
    expiry_pnl_fn = "iron_butterfly",
    # Short legs placed ATM (~0.50 delta); long wings farther OTM for protection
    short_delta_lo = 0.45,
    short_delta_hi = 0.55,
    long_delta_lo  = 0.15,
    long_delta_hi  = 0.25,
    min_credit_pct = 0.30,   # higher premium target than Iron Condor (ATM shorts)
    capital_type   = "spread_width",
    requires_margin = False,
    dte_min = 21,
    dte_max = 45,
    profit_target_pct = 0.25,   # exit early — ATM short options accelerate against you fast
    # Gate: tighter than IC (ATM shorts) — reject if earnings within 10 days.
    # Narrow profit zone means even a modest earnings gap breaches the short strikes.
    constraints = StructureConstraints(earnings_dte_min=10),
    hedge = HedgeDef(
        structure    = "Roll Short Strikes Apart (Convert to Iron Condor)",
        details      = "If the stock moves past a short strike, roll both short options farther OTM to widen the profit zone and collect additional credit.",
        rationale    = "Iron Butterfly has a very narrow profit zone. Rolling short strikes converts it to an Iron Condor, reducing gamma risk on directional moves.",
        protection_note = "Rolling is limited by available credit. After a large move the structure may need to be closed outright.",
        cost_pct     = 0.10,
        cost_base    = "max_profit",
        delta_change = 0.0,
        opt_type     = "both",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_BOTH,
    ),
    signal_profile = SignalProfile(
        bias="neutral",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=False, uses_skew=True, uses_sentiment=True,
    ),
)
