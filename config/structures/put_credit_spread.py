from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

PUT_CREDIT_SPREAD = OptionStructure(
    name          = "Put Credit Spread",
    abbr          = "PCS",
    is_credit     = True,
    option_type   = "put",
    allowed_iv      = ("High",),
    allowed_trends  = ("Uptrend",),
    strike_schema = StrikeSchema.TWO_LEG,
    expiry_pnl_fn = "put_credit",
    short_delta_lo = 0.15,
    short_delta_hi = 0.25,
    min_credit_pct = 0.25,
    capital_type   = "spread_width",
    hedge = HedgeDef(
        structure    = "Buy OTM Protective Put",
        details      = "Buy a put 1–2 strikes below your long put leg. Kicks in only if stock gaps down past both spread legs.",
        rationale    = "Spread is net positive delta (bullish). Cheap OTM put limits loss on a catastrophic gap-down through both strikes.",
        protection_note = "Hedge does NOT reduce loss within the normal spread loss zone — it only caps loss below the put you buy.",
        cost_pct     = 0.22,
        cost_base    = "max_profit",
        delta_change = -0.10,
        opt_type     = "put",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_BELOW_LO,
    ),
    signal_profile = SignalProfile(
        bias="bullish",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=False, uses_skew=True, uses_sentiment=True,
    ),
)
