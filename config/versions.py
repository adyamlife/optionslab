# Scoring engine version constants.
# Bump the relevant version whenever the corresponding logic changes so that
# stored snapshots and paper trades remain auditable across engine revisions.
#
# Format: "<major>.<minor>" — minor for additive changes, major for breaking changes.

SCORING_VERSION       = "1.1"   # compute_signal_alignment + coverage_ratio/signal_coverage
STRUCTURE_VERSION     = "1.0"   # structure_scores.toml / STRUCTURE_MATRIX
SIGNAL_PARAMS_VERSION = "1.1"   # iv_term_shape → term_slope canonical; vol_pcr/pcr_diverge/iv_hv_ratio added
