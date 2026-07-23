from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile

BULL_CALL_LADDER = OptionStructure(
    name          = "Bull Call Ladder",
    abbr          = "BCL",
    is_credit     = False,
    option_type   = "call",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Uptrend",),
    # 3-leg structure: Buy 1 ATM call, Sell 1 OTM call, Sell 1 further OTM call.
    # No standard schema fits; NONE used — strike selection handled ad-hoc.
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "bull_call_ladder",
    # Long ATM leg drives the delta range
    long_delta_lo  = 0.45,
    long_delta_hi  = 0.60,
    # First short leg (OTM)
    short_delta_lo = 0.25,
    short_delta_hi = 0.40,
    capital_type    = "debit",
    requires_margin = False,
    dte_min = 21,
    dte_max = 60,
    profit_target_pct = 0.50,
    hedge = HedgeDef(
        structure    = "Buy Back Upper Short Call (Uncap Upside)",
        details      = "If the stock rallies aggressively past both short strikes, buy back the farther OTM short call to remove the uncapped upside risk zone.",
        rationale    = "Bull Call Ladder becomes short gamma above the upper short strike — a sharp rally above that level turns the position against you. Buying back the upper short removes that exposure.",
        protection_note = "The lower short call still caps gains between the two strikes. Full removal of the ladder converts back to a simple Bull Call Debit Spread.",
        cost_pct     = 0.20,
        cost_base    = "max_loss",
        delta_change = +0.10,
        opt_type     = "call",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_ABOVE_HI,
    ),
    signal_profile = SignalProfile(
        bias="bullish",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=False, uses_skew=False, uses_sentiment=True,
    ),
)
