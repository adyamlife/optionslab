from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

RATIO_PUT_BACKSPREAD = OptionStructure(
    name          = "Ratio Put Backspread",
    is_credit     = False,
    option_type   = "put",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Downtrend",),
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "ratio_put_backspread",
    short_delta_lo = 0.40,
    short_delta_hi = 0.55,
    long_delta_lo  = 0.20,
    long_delta_hi  = 0.35,
    capital_type    = "debit",
    requires_margin = False,
    hedge = HedgeDef(
        structure    = "No hedge — loss zone is the dead zone between strikes",
        details      = "Max loss occurs if stock ends just below the short put at expiry (dead zone). Close early if stock drifts into that band.",
        rationale    = "The 2:1 long/short ratio self-hedges on large crashes. Dead zone risk between short and long put strikes is inherent to the structure.",
        protection_note = "If stock likely to land in dead zone, close the position early rather than hedging.",
        cost_pct     = 0.0,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_BELOW_LO,
    ),
)
