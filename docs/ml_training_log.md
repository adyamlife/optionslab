# ML Training Log — Options Strategy Lab

Last updated: 2026-08-11

---

## Goal

Build a pipeline that identifies which option candidates to paper-trade, learns from outcomes, and improves selection over time. The system is still in early data-collection phase (Phase 2 of 3).

---

## Model Inventory

| Model | File | Purpose | Current Metrics | Last Trained |
|---|---|---|---|---|
| Regime Classifier | `regime_classifier.joblib` | Bull / Bear / Chop label for market context | Accuracy 33% (3-class, ~random) | 2026-08-11 |
| Return Regressor | `return_regressor.joblib` | Predict forward return of a candidate | R²=−0.09 (below mean baseline) | 2026-08-11 |
| Volatility Regressor | `volatility_regressor.joblib` | Predict forward realized vol | R²=0.51, RMSE=0.18 | 2026-08-11 |
| POP Classifier | `pop_classifier.joblib` | Probability of profit per candidate | AUC=0.741, Brier=0.184 (calibrated) | 2026-08-11 |
| Direction Classifier | `direction_classifier.joblib` | Up / Down / Flat market direction | AUC=0.539, weak Flat recall (4.5%) | 2026-08-11 |
| IV Direction Classifier | `iv_direction_classifier.joblib` | Expanding vs Contracting IV | AUC=0.726, P@10=100% — strongest model | 2026-08-11 |
| Meta Ensemble (win) | `meta_ensemble.joblib` | Rank candidates by P(win) | AUC=0.569, P@10=0% — not usable yet | 2026-08-11 |
| Meta Ensemble (direction) | `meta_ensemble_direction.joblib` | Market direction from base model outputs | AUC=0.533, barely above naive | 2026-08-11 |
| Anomaly Detector | `anomaly_detector.joblib` | Flag unusual market conditions | Not evaluated this session | 2026-07-08 |
| Trade Win Model | `train_trade_win_model.py` | Win prediction for Calendar/Long Strangle only | Temporal split broken (all data 3 days) | 2026-08-11 |

Calibrated versions (isotonic regression) saved as `*_calibrated.joblib` for all except trade win model.

---

## Training Data Pipeline

### Sources
- **`training_snapshots` table** (DuckDB `ml_training.duckdb`) — snapshot of market features per ticker per collection run, ~40k rows, 9,971 labeled
- **`regime_training` CSV** — regime labels with forward HV, used by regime/return/volatility models
- **Chain Greeks index** — per-strike Greeks from OI snapshots, used to enrich POP features

### Key Scripts
| Script | Purpose |
|---|---|
| `scripts/training_data_collector.py` | Collects snapshots every 30 min during market hours (cron) |
| `scripts/train_all.py` | Master pipeline: DB migration → regime backfill → train regime/return/vol/POP → calibrate → grid calibrate |
| `scripts/train_regime_classifier.py` | Trains regime model |
| `scripts/train_return_model.py` | Trains return regressor |
| `scripts/train_volatility_model.py` | Trains volatility regressor |
| `scripts/train_pop_model.py` | Trains POP classifier — uses `NUMERIC_COLS` list for both training AND inference |
| `scripts/train_direction_model.py` | Trains direction classifier |
| `scripts/train_iv_direction_model.py` | Trains IV direction classifier |
| `scripts/train_meta_ensemble.py` | Trains meta ensemble on base-model outputs |
| `scripts/train_trade_win_model.py` | Trains trade-win model (Calendar + Long Strangle only, post 2026-07-13) |
| `scripts/calibrate_models.py` | Isotonic calibration for all classifiers |
| `scripts/calibrate_optimizer.py` | Grid calibration — recommends credit_delta_grid / width_grid from labeled IC outcomes |

### Standard Retrain Order
```
python -m scripts.train_all --skip-backfill       # regime, return, vol, POP, calibrate, grid
python -m scripts.train_direction_model
python -m scripts.train_iv_direction_model
python -m scripts.train_meta_ensemble
python -m scripts.calibrate_models                 # recalibrate all after any retrain
```

`--skip-backfill` skips rebuilding the regime CSV (safe when data hasn't changed much). Omit it for a full rebuild.

**Do NOT use `--write-grids`** — the calibrator recommends width_grid=[1,2,5] but width=5 has negative avg P&L. Settings were manually set to `[1, 2]`. If you run `--write-grids` it will overwrite with the bad value.

---

## Current Settings (as of 2026-08-11)

In `config/settings.toml`:
```toml
credit_delta_grid      = [0.05, 0.10]   # manually trimmed — calibrator recommended [0.05,0.10,0.15] but data only supports 0.05/0.10
width_grid             = [1, 2]          # manually trimmed — width=5 showed 50% win, -$1.15 avg P&L (12 trades)
debit_long_delta_grid  = [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]
debit_short_delta_grid = [0.20, 0.25, 0.30, 0.35, 0.40]   # widened 2026-08-11 from 0.10-0.20

[debit_spread]
short_delta_lo = 0.25   # raised from 0.15 on 2026-08-11 — old range gave max_loss > max_profit on 65% of trades
short_delta_hi = 0.35
```

In `config/ranking.toml`:
```toml
max_net_delta = 25.0    # TEMP — restore to 3.0 after 2026-08-21 when legacy positions expire
```

---

## Known Issues and Findings

### signal_score contamination (fixed 2026-08-11)
**Problem**: `signal_score` stored in `training_snapshots` is a raw unbounded weighted sum from `compute_signal_alignment()`. On 2026-07-23, new signals were added to `structure_scores.toml`, inflating the raw sum from mean ~0.85 (June) to ~15 (July) to ~48 (August). Models trained on this feature see a completely different scale before and after Jul 23.

**Fix**:
- Removed `signal_score` from `NUMERIC_COLS` in `train_pop_model.py`
- Removed `signal_score` from `_RAW_MARKET_FEATURES` in `train_trade_win_model.py`
- Added `signal_pct` (bounded [-1,1], = score/effective_max) to `training_data_collector.py` and `db.py` schema for future use
- POP AUC unchanged after removal (0.7412 → 0.7407) — confirmed signal_score was noise

**Rule**: Never use `signal_score` as a training feature. Use `signal_pct` once sufficient data with the new scoring version accumulates (post Jul 23 only).

### Regime classifier at chance (ongoing)
Accuracy 33% on 3-class problem (chance = 33.3%). The regime label is not learnable from current features at this data volume. Do not rely on regime predictions for trade selection.

### Return regressor negative R² (ongoing)
R²=−0.09 — predicts worse than the mean. Not usable. Needs more labeled data and/or better features. Do not use `return_score` from this model as a reliable signal.

### Meta ensemble win model not usable (ongoing)
- Win rate in meta data: 17.2%
- Majority-naive accuracy: 82.8%, model accuracy: 74% — worse than predicting no-win
- AUC=0.569, P@10=0%, P@25=0%
- Root cause: only 3,793/6,262 held-out rows matched win targets (~2,469 rows dropped). Investigate join loss before tuning.
- Do not use meta ensemble score as a standalone trade selector

### Trade-win model temporal split broken (ongoing)
All 2,092 system-v2 labeled rows (Calendar Spread + Long Strangle) collapsed into 2026-07-13 to 2026-07-15. No real temporal holdout. Model cannot be trusted for generalization until labeled trades accumulate across more dates.

### Feature drift — major shift (2026-08-10 report)
20 features flagged as major_shift in the drift report. Key shifts:
- VIX: 16.2 → 17.3 (PSI 3.82)
- VVIX: 88.6 → 96.6 (PSI 10.3)
- yield_curve: 0.86 → 0.80 (PSI 10.97)
- spy/qqq/iwm RSI all shifted (PSI 6–11)
- Fed/CPI/jobs calendar features flipped (were frozen at one macro cycle point in baseline)

Models retrained on 2026-08-11 incorporate current distributions.

### calibrate_models.py timezone bug (fixed 2026-08-11)
`_val_test_from_artifact()` did `df["date"] = pd.to_datetime(df["date"])` — crashed for POP (uses `collected_at`) and failed tz comparison for others. Fixed to use `format="mixed", utc=True` and tz-aware Timestamp comparisons.

---

## Grid Calibration History

| Run date | Trades | Buckets | Best bucket | Notes |
|---|---|---|---|---|
| 2026-08-11 | 103 IC trades | 12 (7 low-conf) | δ=0.05 W=1: 97% win, +$0.353, n=31 | Only IC data; debit grids uncalibrated |

DTE analysis: 15–30 DTE at δ=0.05 W=1 is the strongest bucket (96% win, $0.365, n=27).

---

## Phase Status

| Phase | Trigger | Status |
|---|---|---|
| Phase 1: Data collection | — | Active — collecting ~83 tickers every 30 min |
| Phase 2: Paper trading | — | Active — IC, Calendar, Long Strangle live |
| Phase 3: ML-guided selection | ≥20 calibration weeks, ≥100 labeled trades | 2/20 weeks done. Not started. |

Layer B (Historical Reality pipeline): 2 calibration weeks, 201 IV history rows, 83 tickers. Phase 3 needs 20 weeks minimum.

---

## Diagnostics To Run (Deferred)

1. **Meta join loss investigation** — why do 2,469/6,262 held-out rows not match win targets? Run query against `paper_trades.json` join to find which tickers/structures/dates drop out.
2. **Meta win score decile plot** — divide 569 test rows into score deciles, check if actual win% is monotonically increasing.
3. **Baseline comparison for meta** — does simple `p_top_decile` or `return_score` ranking beat the meta ensemble on AUC/P@K?
4. **signal_pct feature** — once 4+ weeks of post-Jul-23 snapshots accumulate with `signal_pct` populated, evaluate adding it back to POP features.
5. **Restore `max_net_delta = 3.0`** in `config/ranking.toml` after 2026-08-21.
