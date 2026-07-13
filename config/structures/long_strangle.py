from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

LONG_STRANGLE = OptionStructure(
    name          = "Long Strangle",
    is_credit     = False,
    option_type   = "both",
    iv_env        = "Low",
    trend         = "Any",
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "long_strangle",
    long_delta_lo  = 0.20,
    long_delta_hi  = 0.35,
    short_delta_lo = 0.20,
    short_delta_hi = 0.35,
    capital_type    = "debit",
    requires_margin = False,
    hedge = HedgeDef(
        structure    = "No hedge — max loss is the total premium paid",
        details      = "The total debit is your maximum loss. Both legs expire worthless if stock stays between the strikes.",
        rationale    = "Long Strangle is a pure long-vol play. The debit is the built-in risk limit — no additional hedge possible.",
        protection_note = "Max loss = total debit paid. Close early if IV contracts without the expected move.",
        cost_pct     = 0.0,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_BELOW_LO,
    ),
)
