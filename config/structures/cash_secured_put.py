from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

CASH_SECURED_PUT = OptionStructure(
    name          = "Cash Secured Put",
    is_credit     = True,
    option_type   = "put",
    iv_env        = "High",
    trend         = "Uptrend",
    strike_schema = StrikeSchema.SINGLE_LEG,
    expiry_pnl_fn = "cash_secured_put",
    short_delta_lo = 0.15,
    short_delta_hi = 0.30,
    min_credit_pct = 0.0,   # no width to measure against; use raw credit threshold
    hedge = HedgeDef(
        structure    = "Buy OTM Protective Put (convert to PCS)",
        details      = "Buy a put 1–2 strikes below your short put. Converts the naked CSP into a defined-risk Put Credit Spread.",
        rationale    = "CSP carries full downside risk to zero. Adding a long put below caps the max loss and reduces margin requirement.",
        protection_note = "Hedge does NOT eliminate loss in the normal loss zone — it caps the worst-case scenario below the long put.",
        cost_pct     = 0.30,
        cost_base    = "max_profit",
        delta_change = -0.08,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.OTM_PUT_NEAR_SHORT,
        urgency      = "critical",   # naked position — hedge urgency is high
    ),
)
