from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

NAKED_PUT = OptionStructure(
    name          = "Naked Put",
    is_credit     = True,
    option_type   = "put",
    allowed_iv      = ("High",),
    allowed_trends  = ("Range-bound",),
    strike_schema = StrikeSchema.SINGLE_LEG,
    expiry_pnl_fn = "cash_secured_put",   # same single-leg put P&L shape
    short_delta_lo = 0.15,
    short_delta_hi = 0.30,
    min_credit_pct  = 0.0,   # premium collected is the entire credit; no width to ratio against
    requires_margin = True,  # naked position — margin account required
    capital_type    = "margin",
    hedge = HedgeDef(
        structure    = "Buy OTM Protective Put (convert to PCS)",
        details      = "Buy a put 1–2 strikes below your short put. Converts the naked put into a defined-risk Put Credit Spread.",
        rationale    = "Naked put carries full downside risk to zero. Adding a long put below caps max loss and satisfies margin requirements.",
        protection_note = "Hedge does NOT eliminate loss in the normal loss zone — it caps the worst-case scenario below the long put strike.",
        cost_pct     = 0.30,
        cost_base    = "max_profit",
        delta_change = -0.08,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.OTM_PUT_NEAR_SHORT,
        urgency      = "critical",
    ),
)
