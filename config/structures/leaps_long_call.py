from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

LEAPS_LONG_CALL = OptionStructure(
    name          = "LEAPS Long Call",
    is_credit     = False,
    option_type   = "call",
    allowed_iv      = ("Low",),           # selected manually; not tied to a single regime slot
    allowed_trends  = ("Uptrend",),
    strike_schema = StrikeSchema.SINGLE_LEG,
    expiry_pnl_fn = "leaps_long_call",

    # Buy slightly ITM (0.60–0.80 delta) — high delta behaves like stock
    # at lower capital outlay; avoids too much time-value decay
    long_delta_lo  = 0.60,
    long_delta_hi  = 0.80,

    # ~2-year LEAPS window: 500–730 DTE at entry
    dte_min = 500,
    dte_max = 730,

    # Exit at 50% gain on the premium paid
    profit_target_pct = 0.50,

    requires_margin = False,   # debit trade — only capital at risk is the premium paid

    hedge = HedgeDef(
        structure    = "Buy OTM Protective Put",
        details      = "Buy an OTM put at or below current price to cap downside if the thesis reverses before expiry.",
        rationale    = "LEAPS call is a long-duration bullish bet. A put hedge limits loss if the stock trends down over the holding period.",
        protection_note = "Hedge does NOT recover full premium paid — it reduces net loss on a sustained downtrend. Cost reduces overall return.",
        cost_pct     = 0.20,
        cost_base    = "max_loss",
        delta_change = -0.20,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ATM_PUT,
        urgency      = "normal",
    ),
)
