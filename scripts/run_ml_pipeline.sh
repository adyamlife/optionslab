#!/usr/bin/env bash
# run_ml_pipeline.sh — Run all ML training steps + calibration on the Ubuntu server.
#
# Usage:
#   cd /path/to/project-y
#   bash scripts/run_ml_pipeline.sh [--calibrate-write]
#
# Pass --calibrate-write to also write recommended grids to config/settings.toml.
# Without it the calibration runs in dry-run mode (report only).
#
# Steps:
#   1. DB schema migration   — adds gate_summary column if missing
#   2. Regime backfill       — rebuild regime_training.csv incl. forward_hv
#   3. Regime classifier     — train_regime_classifier.py → models/regime_classifier.joblib
#   4. Return regressor      — train_return_model.py      → models/return_regressor.joblib
#   5. Volatility regressor  — train_volatility_model.py  → models/volatility_regressor.joblib
#   6. POP model             — train_pop_model.py         → skips if < min labeled rows
#   7. Calibration           — calibrate_optimizer.py     → data/calibration_history.jsonl
#
# Each step is logged to logs/ml_pipeline_<timestamp>.log as well as stdout.

set -euo pipefail

CALIBRATE_WRITE=""
for arg in "$@"; do
  [[ "$arg" == "--calibrate-write" ]] && CALIBRATE_WRITE="--write"
done

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LOG_DIR="logs"
mkdir -p "$LOG_DIR" "data/models"
LOGFILE="$LOG_DIR/ml_pipeline_${TIMESTAMP//:/}.log"

# Tee all output to log file
exec > >(tee -a "$LOGFILE") 2>&1

echo "======================================================"
echo "ML Pipeline  started $TIMESTAMP"
echo "Log: $LOGFILE"
echo "======================================================"

run_step() {
  local step="$1"; shift
  echo ""
  echo "── Step $step ────────────────────────────────────────"
  date -u
  "$@"
  echo "Step $step complete."
}

# 1. DB schema migration (auto-runs on any db import, but trigger explicitly)
run_step 1 python -c "
from scripts.db import connect
con = connect()
con.close()
print('DB schema up to date.')
"

# 2. Regime backfill — regenerates regime_training.csv with forward_hv
run_step 2 python -m scripts.regime_backfill

# 3. Regime classifier
run_step 3 python -m scripts.train_regime_classifier

# 4. Return regressor
run_step 4 python -m scripts.train_return_model

# 5. Volatility regressor
run_step 5 python -m scripts.train_volatility_model

# 6. POP model (may exit early if insufficient labeled data — that's expected)
run_step 6 python -m scripts.train_pop_model || true

# 7. Probability calibration (isotonic regression on trained classifiers)
run_step 7 python -m scripts.calibrate_models

# 8. Grid calibration (delta/width optimizer grids from labeled outcomes)
run_step 8 python -m scripts.calibrate_optimizer $CALIBRATE_WRITE

echo ""
echo "======================================================"
echo "ML Pipeline  finished $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "Log saved to $LOGFILE"
echo "======================================================"
