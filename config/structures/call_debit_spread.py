from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

CALL_DEBIT_SPREAD = OptionStructure(
    name          = "Call Debit Spread",
    is_credit     = False,
    option_type   = "call",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Uptrend",),
    strike_schema = StrikeSchema.TWO_LEG,
    expiry_pnl_fn = "call_debit",
    long_delta_lo  = 0.40,
    long_delta_hi  = 0.55,
    short_delta_lo = 0.15,
    short_delta_hi = 0.20,
    hedge = HedgeDef(
        structure    = "Buy OTM Put (reversal protection)",
        details      = "Buy an OTM put below current price. Gains value if the bullish call spread reverses sharply.",
        rationale    = "Call debit spread is bullish (positive delta). OTM put offsets some debit loss on a sudden trend reversal.",
        protection_note = "Hedge partially offsets the debit you paid if the stock falls. Does not cap loss at the put strike — it shifts breakeven.",
        cost_pct     = 0.20,
        cost_base    = "max_loss",
        delta_change = -0.15,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ATM_PUT,
    ),
)
