from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema, SignalProfile, StructureConstraints

IRON_CONDOR = OptionStructure(
    name          = "Iron Condor",
    abbr          = "ICO",
    is_credit     = True,
    option_type   = "both",
    allowed_iv      = ("High",),
    allowed_trends  = ("Range-bound",),
    strike_schema = StrikeSchema.IRON_CONDOR,
    expiry_pnl_fn = "iron_condor",
    short_delta_lo = 0.15,
    short_delta_hi = 0.25,
    min_credit_pct = 0.25,
    capital_type   = "spread_width",
    # Gate: reject if earnings are within 12 days — IV spike on either wing
    # converts an IC from a range trade into a defined-loss gap trade.
    constraints = StructureConstraints(earnings_dte_min=12),
    hedge = HedgeDef(
        structure    = "Buy Wider Wings (Gamma Protection)",
        details      = "Buy a deeper OTM put + deeper OTM call to widen the buffer against extreme gap moves.",
        rationale    = "Iron Condor already has defined risk and near-zero delta. Wider wings reduce gamma risk on extreme events (earnings surprise, macro shock).",
        protection_note = "Already defined-risk. Extra wings only help on moves well beyond the existing long strikes — rare but large.",
        cost_pct     = 0.15,
        cost_base    = "max_profit",
        delta_change = 0.0,
        opt_type     = "both",
        strike_mode  = HedgeStrikeMode.ONE_WIDTH_BOTH,
    ),
    signal_profile = SignalProfile(
        bias="neutral",
        needs_trend=True, needs_momentum=True,
        uses_term_structure=False, uses_skew=True, uses_sentiment=True,
    ),
)
