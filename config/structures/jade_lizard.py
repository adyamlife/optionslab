from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

JADE_LIZARD = OptionStructure(
    name          = "Jade Lizard",
    abbr          = "JAD",
    is_credit     = True,
    option_type   = "both",
    allowed_iv      = ("High",),
    allowed_trends  = ("Uptrend", "Range-bound"),          # recommended when High IV + bullish, via separate logic in analyze.py
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "jade_lizard",
    short_delta_lo = 0.20,
    short_delta_hi = 0.30,
    capital_type    = "margin",
    requires_margin = True,   # naked short put component requires margin account
    hedge = HedgeDef(
        structure    = "Buy OTM Put ⚠ CRITICAL — defines downside risk",
        details      = "Buy a put below the naked short put strike. Converts Jade Lizard → Iron Condor equivalent with defined max loss.",
        rationale    = "Jade Lizard carries UNDEFINED downside risk from the naked short put. A market crash creates catastrophic loss without this hedge.",
        protection_note = "Without hedge: loss on downside is theoretically unlimited. With hedge: max loss = put spread width − net credit (defined and fixed at entry).",
        cost_pct     = 0.35,
        cost_base    = "max_profit",
        delta_change = -0.15,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.OTM_PUT_NEAR_SHORT,
        urgency      = "critical",
    ),
    signal_profile = SignalProfile(
        bias="bullish",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=False, uses_skew=True, uses_sentiment=True,
    ),
)
