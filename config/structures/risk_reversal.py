from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

RISK_REVERSAL = OptionStructure(
    name          = "Risk Reversal",
    is_credit     = True,        # typically net credit (put skew > call in Low IV)
    option_type   = "both",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Uptrend",),
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "risk_reversal",
    short_delta_lo = 0.20,       # short put delta range
    short_delta_hi = 0.30,
    long_delta_lo  = 0.20,       # long call delta range
    long_delta_hi  = 0.30,
    capital_type    = "margin",
    requires_margin = True,      # short put leg is uncovered
    hedge = HedgeDef(
        structure    = "Buy OTM Put ⚠ CRITICAL — defines downside risk",
        details      = "Buy a put below the naked short put strike. Converts Risk Reversal to a risk-defined structure (debit spread + long call).",
        rationale    = "Risk Reversal carries UNDEFINED downside risk from the short put. A market crash creates catastrophic loss without this hedge.",
        protection_note = "Without hedge: loss below the short put strike is uncapped (stock can go to zero). With hedge: max loss = put spread width − net credit.",
        cost_pct     = 0.40,
        cost_base    = "max_profit",
        delta_change = -0.20,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.OTM_PUT_NEAR_SHORT,
        urgency      = "critical",
    ),
)
