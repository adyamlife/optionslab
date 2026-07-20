"""ML signal gate thresholds used by decision_provider._apply_ml_signals().

Centralised here so calibration changes after retraining have one place to
update rather than being buried as magic numbers in logic.
"""

# ── Return regressor ──────────────────────────────────────────────────────────
RETURN_CONFLICT_THRESHOLD = 0.03   # |return| > this AND opposite bias → penalty
RETURN_SUPPORT_THRESHOLD  = 0.02   # |return| > this AND same bias    → note only

# ── Volatility regressor ──────────────────────────────────────────────────────
VOL_HIGH_THRESHOLD = 0.60   # annualised vol above this → widen strikes / shorten DTE
VOL_LOW_THRESHOLD  = 0.20   # annualised vol below this → tighter spreads viable

# ── IV direction ──────────────────────────────────────────────────────────────
IV_EXPANDING_SHORT_VOL_PENALTY   = -1.0
IV_EXPANDING_LONG_VOL_BONUS      =  0.5
IV_CONTRACTING_SHORT_VOL_BONUS   =  0.5
IV_CONTRACTING_LONG_VOL_PENALTY  = -0.5

# ── Direction classifier P(up) ────────────────────────────────────────────────
P_UP_STRONG_CONFLICT   = 0.65   # P(up) >= this AND bearish → conflict
P_DOWN_STRONG_CONFLICT = 0.35   # P(up) <= this AND bullish → conflict
P_UP_CONFIRM           = 0.60   # P(up) >= this AND bullish → confirmation note
P_DOWN_CONFIRM         = 0.40   # P(up) <= this AND bearish → confirmation note
DIRECTION_CONFLICT_PENALTY = -0.5

# ── Anomaly detector ──────────────────────────────────────────────────────────
ANOMALY_EXTREME_THRESHOLD = 20    # score <= this → extreme outlier, verdict capped
ANOMALY_PENALTY           = -0.5
ANOMALY_VERDICT_CAP       = "Neutral"  # extreme anomaly caps verdict here

# ── Meta-ensemble ─────────────────────────────────────────────────────────────
META_BULLISH_THRESHOLD = 70   # meta_score >= this → strong bullish consensus
META_BEARISH_THRESHOLD = 30   # meta_score <= this → strong bearish consensus
META_CONSENSUS_BONUS   =  0.5
META_CONFLICT_PENALTY  = -0.5

# ── Structure × Regime penalties / bonuses ───────────────────────────────────
# Applied conditionally: penalty only when regime OR vol context is unfavourable;
# bonus only when regime AND vol context are both favourable.
# Return model weight is intentionally light (low R²); vol model carries more weight.
#
# Unfavourable regimes for premium buyers (long-vol structures):
LONG_VOL_BAD_REGIMES = {"Mean-reverting", "Low-vol-squeeze"}

# Thresholds on predicted forward vol (annualised fraction from volatility_regressor).
# Bottom tercile = low-vol environment → bad for long-option premium buyers.
# Top tercile    = high-vol environment → good for long-option premium buyers.
STRUCT_VOL_LOW_PERCENTILE  = 0.33   # predicted_forward_hv <= this → bad for long-vol
STRUCT_VOL_HIGH_PERCENTILE = 0.67   # predicted_forward_hv >= this → good for long-vol

# Minimum |predicted_return| before it can contribute to a bonus (low-confidence model).
STRUCT_RETURN_MIN_THRESHOLD = 0.02

# Long Call / Long Put — naked long premium; highest theta-decay risk.
LONG_OPTION_BAD_REGIME_PENALTY  = -2.0   # bad regime OR low vol → penalise
LONG_OPTION_GOOD_REGIME_BONUS   = +1.5   # good regime AND high vol AND return aligned → bonus

# Debit spreads — short leg partially offsets theta; smaller adjustments.
DEBIT_SPREAD_BAD_REGIME_PENALTY = -1.0
DEBIT_SPREAD_GOOD_REGIME_BONUS  = +0.75

# ── POP classifier ────────────────────────────────────────────────────────────
# Raw probability bands → score adjustment.  Symmetric around 0.50; capped at
# ±2.0 so POP nudges the score without overpowering regime/IV/return signals.
POP_BANDS = [
    (0.85, +2.0),
    (0.75, +1.5),
    (0.65, +1.0),
    (0.55, +0.5),
    (0.45,  0.0),
    (0.35, -0.5),
    (0.25, -1.0),
    (0.00, -2.0),
]
