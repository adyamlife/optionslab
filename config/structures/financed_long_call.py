from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

FINANCED_LONG_CALL = OptionStructure(
    name          = "Financed Long Call",
    is_credit     = False,
    option_type   = "both",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Uptrend",),
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "financed_long_call",
    long_delta_lo  = 0.25,
    long_delta_hi  = 0.40,
    short_delta_lo = 0.20,
    short_delta_hi = 0.30,
    capital_type    = "debit",
    requires_margin = False,
    hedge = HedgeDef(
        structure    = "No additional hedge needed",
        details      = "Put credit spread already defines downside. Max loss is fixed at entry.",
        rationale    = "The put credit spread acts as a built-in hedge — max loss = spread width − net proceeds.",
        protection_note = "All risk is defined. The long put caps the credit spread's downside.",
        cost_pct     = 0.0,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_BELOW_LO,
    ),
)
