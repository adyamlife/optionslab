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
ANOMALY_EXTREME_THRESHOLD = 20    # score <= this → extreme outlier
ANOMALY_PENALTY           = -0.5

# ── Meta-ensemble ─────────────────────────────────────────────────────────────
META_BULLISH_THRESHOLD = 70   # meta_score >= this → strong bullish consensus
META_BEARISH_THRESHOLD = 30   # meta_score <= this → strong bearish consensus
META_CONSENSUS_BONUS   =  0.5
META_CONFLICT_PENALTY  = -0.5
