from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

DOUBLE_CALENDAR = OptionStructure(
    name          = "Double Calendar",
    abbr          = "DCL",
    is_credit     = False,
    option_type   = "both",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Range-bound",),
    # Two calendar spreads: put calendar below current price, call calendar above.
    # NONE — multi-expiry, no fixed-strike schema applies.
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "double_calendar",
    # Strikes are OTM on each side (~0.30 delta)
    short_delta_lo = 0.25,
    short_delta_hi = 0.35,
    long_delta_lo  = 0.25,
    long_delta_hi  = 0.35,
    capital_type    = "debit",
    requires_margin = False,
    dte_min = 21,
    dte_max = 45,
    profit_target_pct = 0.25,
    hedge = HedgeDef(
        structure    = "Buy OTM Strangle (Large-Move Protection)",
        details      = "Buy a wider OTM call and OTM put beyond the calendar strikes to cap loss on a large gap move.",
        rationale    = "Double Calendar profits from time decay near the two strikes. A sharp move beyond both calendars collapses the spreads; an OTM strangle offsets that loss.",
        protection_note = "Strangle gain partially offsets the calendar collapse on a large move. Net position becomes complex and path-dependent.",
        cost_pct     = 0.25,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "both",
        strike_mode  = HedgeStrikeMode.OTM_STRANGLE,
    ),
    signal_profile = SignalProfile(
        bias="neutral",
        needs_trend=False, needs_momentum=True,
        uses_term_structure=True, uses_skew=False, uses_sentiment=True,
    ),
)
