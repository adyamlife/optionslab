from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

COVERED_CALL = OptionStructure(
    name          = "Covered Call",
    is_credit     = True,
    option_type   = "call",
    iv_env        = "High",
    trend         = "Any",
    strike_schema = StrikeSchema.SINGLE_LEG,
    expiry_pnl_fn = "covered_call",
    short_delta_lo = 0.20,
    short_delta_hi = 0.35,
    min_credit_pct = 0.0,
    capital_type   = "shares",
    hedge = HedgeDef(
        structure    = "Roll Call Up or Close Position",
        details      = "If stock rallies sharply, roll the call up to a higher strike for additional credit, or close the position to cap loss.",
        rationale    = "Covered call caps upside at the short strike. If stock spikes above your call, you face opportunity cost. Rolling preserves upside exposure.",
        protection_note = "Hedge does NOT recover the upside loss — rolling just reduces the pain by collecting more premium. The shares may be called away at assignment.",
        cost_pct     = 0.15,
        cost_base    = "max_profit",
        delta_change = +0.05,
        opt_type     = "call",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_ABOVE_HI,
    ),
)
