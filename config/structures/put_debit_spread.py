from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

PUT_DEBIT_SPREAD = OptionStructure(
    name          = "Put Debit Spread",
    abbr          = "PDS",
    is_credit     = False,
    option_type   = "put",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Downtrend",),
    strike_schema = StrikeSchema.TWO_LEG,
    expiry_pnl_fn = "put_debit",
    long_delta_lo  = 0.40,
    long_delta_hi  = 0.55,
    short_delta_lo = 0.15,
    short_delta_hi = 0.20,
    hedge = HedgeDef(
        structure    = "Buy OTM Call (squeeze protection)",
        details      = "Buy an OTM call above current price. Pays off if a short squeeze or unexpected rally reverses the bearish spread.",
        rationale    = "Put debit spread is bearish (negative delta). OTM call offsets some debit loss on a sudden upside reversal.",
        protection_note = "Hedge partially offsets the debit you paid if the stock rises. Shifts breakeven; does not cap loss at the call strike.",
        cost_pct     = 0.20,
        cost_base    = "max_loss",
        delta_change = +0.15,
        opt_type     = "call",
        strike_mode  = HedgeStrikeMode.ATM_CALL,
    ),
    signal_profile = SignalProfile(
        bias="bearish",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=False, uses_skew=True, uses_sentiment=True,
    ),
)
