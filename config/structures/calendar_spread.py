from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

CALENDAR_SPREAD = OptionStructure(
    name          = "Calendar Spread",
    abbr          = "CAL",
    is_credit     = False,
    option_type   = "calendar",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Range-bound",),
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "calendar",    # path-dependent; expiry P&L not computable at entry
    hedge = HedgeDef(
        structure    = "Buy OTM Strangle (large-move protection)",
        details      = "Buy an OTM call + OTM put (strangle). Pays off if a large directional move collapses the time spread value.",
        rationale    = "Calendar Spread profits from range-bound movement and time decay. A cheap OTM strangle offsets loss if a big move occurs.",
        protection_note = "Strangle gain partially offsets time-spread collapse on a large move. Net position remains complex and path-dependent.",
        cost_pct     = 0.28,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "both",
        strike_mode  = HedgeStrikeMode.OTM_STRANGLE,
    ),
    signal_profile = SignalProfile(
        bias="neutral",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=True, uses_skew=False, uses_sentiment=True,
    ),
)
