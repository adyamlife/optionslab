from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

BEAR_COMBO = OptionStructure(
    name          = "Bear Combo",
    abbr          = "BRC",
    is_credit     = False,        # usually net debit (put debit > call credit); can flip
    option_type   = "both",
    allowed_iv      = ("High",),
    allowed_trends  = ("Downtrend",),
    strike_schema = StrikeSchema.NONE,   # 4 legs, custom field names
    expiry_pnl_fn = "bear_combo",
    # put debit side: long put delta (closer to ATM for directional exposure)
    long_delta_lo  = 0.35,
    long_delta_hi  = 0.50,
    # put short / call short: same OTM delta band as credit spreads
    short_delta_lo = 0.15,
    short_delta_hi = 0.25,
    capital_type    = "debit",    # max loss = call spread width + net cost (fully defined)
    requires_margin = False,      # all four legs defined; no naked exposure
    hedge = HedgeDef(
        structure    = "Tighten Put Spread Width",
        details      = "Reduce the put spread width to lower max loss on the call side.",
        rationale    = "Bear Combo max loss is call spread width + net cost. If IV spikes against you on the upside, a narrower call spread limits damage.",
        protection_note = "All legs are already defined-risk. No naked exposure. Max loss is fixed at entry.",
        cost_pct     = 0.0,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "call",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_ABOVE_HI,
    ),
    signal_profile = SignalProfile(
        bias="bearish",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=False, uses_skew=True, uses_sentiment=True,
    ),
)
