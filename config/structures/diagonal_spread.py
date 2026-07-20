from config.structures._base import OptionStructure, HedgeDef, HedgeStrikeMode, StrikeSchema

DIAGONAL_SPREAD = OptionStructure(
    name          = "Diagonal Spread",
    is_credit     = False,
    option_type   = "calendar",
    allowed_iv      = ("Low",),
    allowed_trends  = ("Uptrend", "Downtrend"),
    strike_schema = StrikeSchema.NONE,
    expiry_pnl_fn = "diagonal",    # path-dependent; expiry P&L not computable at entry
    long_delta_lo  = 0.40,
    long_delta_hi  = 0.60,
    short_delta_lo = 0.20,
    short_delta_hi = 0.35,
    hedge = HedgeDef(
        structure    = "Buy OTM Strangle (large-move protection)",
        details      = "Buy an OTM call + OTM put (strangle). Pays off if a large directional move collapses the diagonal spread value.",
        rationale    = "Diagonal Spread profits from range-bound movement and time decay. A cheap OTM strangle offsets loss if a big move occurs.",
        protection_note = "Strangle gain partially offsets time-spread collapse on a large move. Net position remains complex and path-dependent.",
        cost_pct     = 0.28,
        cost_base    = "max_loss",
        delta_change = 0.0,
        opt_type     = "both",
        strike_mode  = HedgeStrikeMode.OTM_STRANGLE,
    ),
)
