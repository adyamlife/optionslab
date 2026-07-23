from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

SHORT_STRANGLE = OptionStructure(
    name          = "Short Strangle",
    abbr          = "SSG",
    is_credit     = True,
    option_type   = "both",
    allowed_iv      = ("High",),
    allowed_trends  = ("Range-bound",),
    strike_schema = StrikeSchema.TWO_LEG,   # {"short_call": x, "short_put": y}
    expiry_pnl_fn = "short_strangle",
    short_delta_lo = 0.15,
    short_delta_hi = 0.25,
    min_credit_pct = 0.20,
    capital_type   = "margin",
    requires_margin = True,
    dte_min = 21,
    dte_max = 45,
    profit_target_pct = 0.50,
    hedge = HedgeDef(
        structure    = "Buy OTM Strangle (Convert to Iron Condor)",
        details      = "Buy an OTM call above and OTM put below the short strikes to define maximum risk.",
        rationale    = "Short Strangle has theoretically unlimited risk on either side. Buying OTM wings converts it to an Iron Condor with capped loss.",
        protection_note = "Wings add cost and reduce net credit received. Without wings this position requires significant margin and continuous monitoring.",
        cost_pct     = 0.30,
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
