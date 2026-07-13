from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

RATIO_CALL_BACKSPREAD = OptionStructure(
    name          = "Ratio Call Backspread",
    is_credit     = False,
    option_type   = "call",
    iv_env        = "Low",
    trend         = "Uptrend",
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "ratio_call_backspread",
    short_delta_lo = 0.40,
    short_delta_hi = 0.55,
    long_delta_lo  = 0.20,
    long_delta_hi  = 0.35,
    capital_type    = "debit",
    requires_margin = False,
    hedge = HedgeDef(
        structure    = "No hedge — loss zone is the dead zone between strikes",
        details      = "Max loss occurs if stock ends just above the short call at expiry (dead zone). Cannot hedge this without unwinding.",
        rationale    = "The 2:1 long/short ratio means the position self-hedges on large moves — unlimited upside, credit kept on collapse. Dead zone risk is inherent.",
        protection_note = "If stock likely to land in dead zone, close the position early rather than hedging.",
        cost_pct     = 0.0,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "call",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_ABOVE_HI,
    ),
)
