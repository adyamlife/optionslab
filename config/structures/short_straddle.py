from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

SHORT_STRADDLE = OptionStructure(
    name          = "Short Straddle",
    abbr          = "SSD",
    is_credit     = True,
    option_type   = "both",
    allowed_iv      = ("High",),
    allowed_trends  = ("Range-bound",),
    strike_schema = StrikeSchema.TWO_LEG,   # both legs at ATM; same strike
    expiry_pnl_fn = "short_straddle",
    # Both legs at ATM — delta target is ~0.50 for each
    short_delta_lo = 0.45,
    short_delta_hi = 0.55,
    min_credit_pct = 0.35,   # ATM straddle collects the highest possible premium
    capital_type   = "margin",
    requires_margin = True,
    dte_min = 21,
    dte_max = 45,
    profit_target_pct = 0.25,   # narrow profit zone — exit quickly when profitable
    hedge = HedgeDef(
        structure    = "Buy OTM Strangle (Convert to Iron Butterfly)",
        details      = "Buy OTM call and OTM put to cap upside and downside risk, converting the naked straddle to an Iron Butterfly.",
        rationale    = "Short Straddle has unlimited risk in both directions. OTM wings define maximum loss and reduce margin requirement.",
        protection_note = "Adding wings reduces net premium received. The resulting Iron Butterfly has a tighter profit zone but defined risk.",
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
