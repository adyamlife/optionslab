from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

CALL_CREDIT_SPREAD = OptionStructure(
    name          = "Call Credit Spread",
    is_credit     = True,
    option_type   = "call",
    iv_env        = "High",
    trend         = "Downtrend",
    strike_schema = StrikeSchema.TWO_LEG,
    expiry_pnl_fn = "call_credit",
    short_delta_lo = 0.15,
    short_delta_hi = 0.25,
    min_credit_pct = 0.25,
    hedge = HedgeDef(
        structure    = "Buy OTM Protective Call",
        details      = "Buy a call 1–2 strikes above your long call leg. Limits loss on a gap-up through both spread legs.",
        rationale    = "Spread is net negative delta (bearish). Cheap OTM call caps loss if stock spikes through both strikes.",
        protection_note = "Hedge does NOT reduce loss within the normal spread loss zone — it only caps loss above the call you buy.",
        cost_pct     = 0.22,
        cost_base    = "max_profit",
        delta_change = +0.10,
        opt_type     = "call",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_ABOVE_HI,
    ),
)
