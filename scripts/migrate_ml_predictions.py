"""
migrate_ml_predictions.py — Add new columns to the ml_predictions DuckDB table.

Run from the project root:
    python -m scripts.migrate_ml_predictions

Safe to re-run: each ALTER TABLE is wrapped in try/except so already-existing
columns are silently skipped.
"""

from scripts.db import connect, ensure_ml_predictions_table, ML_PREDICTIONS_TABLE

# (column_name, duckdb_type)
# Ordered: core fields first, then derived model outputs, then JSON blobs.
NEW_COLUMNS: list[tuple[str, str]] = [
    # ── From predict_ticker top-level ─────────────────────────────────────────
    ("date",                "VARCHAR"),   # today_row date string (YYYY-MM-DD)
    ("regime_model",        "VARCHAR"),   # "xgb" | "xgb+catboost"
    ("regime_proba",        "JSON"),      # {Bull:0.4, Bear:0.3, Neutral:0.3}

    # Expected return (from return regressor / return classifier)
    ("expected_return",     "DOUBLE"),    # raw regressor output (fraction)
    ("p_return_positive",   "DOUBLE"),    # P(return > 0)
    ("p_return_gt5",        "DOUBLE"),    # P(return > 5%)
    ("p_return_gt10",       "DOUBLE"),    # P(return > 10%)  ← best AUC 0.662
    ("p_top_decile",        "DOUBLE"),    # P(top-decile return)
    ("return_score",        "DOUBLE"),    # weighted composite 0–100
    ("ranker_score",        "DOUBLE"),    # XGBRanker portfolio rank score

    # Volatility
    ("expected_vol",        "DOUBLE"),    # forward realized vol forecast
    ("expected_move_pct",   "DOUBLE"),    # 10-day 1-sigma move approximation
    ("garch_vol_forecast",  "DOUBLE"),    # GARCH conditional vol

    # Direction classifier
    ("p_up",                "DOUBLE"),
    ("p_flat",              "DOUBLE"),
    ("p_down",              "DOUBLE"),
    ("direction",           "VARCHAR"),   # "Up" | "Down" | "Flat"

    # IV direction classifier
    ("iv_expanding_prob",   "DOUBLE"),    # P(IV expands)
    ("iv_direction",        "VARCHAR"),   # "Expanding" | "Contracting"

    # Meta-ensemble / composite confidence engine
    ("meta_score",          "DOUBLE"),    # XGBoost meta-stacker output 0–100
    ("composite_score",     "DOUBLE"),    # deterministic confidence engine 0–100
    ("confidence_tier",     "VARCHAR"),   # "A" | "B" | "C" | "D"
    ("iv_confidence",       "DOUBLE"),    # IV-direction component of composite
    ("anomaly_penalized",   "BOOLEAN"),   # composite was penalised for anomaly

    # POP model
    ("pop_score",           "DOUBLE"),    # P(trade wins) from POP classifier

    # Historical analogues (k-NN on regime_training)
    ("analogues_win_rate",  "DOUBLE"),
    ("analogues_k",         "INTEGER"),

    # Anomaly detector
    ("anomaly_score",       "DOUBLE"),
    ("is_anomaly",          "BOOLEAN"),
    ("anomaly_flags",       "JSON"),      # list of flagged features

    # Regime streak tracker
    ("ml_regime_streak",    "INTEGER"),   # consecutive days in same regime

    # Composite prediction distribution (full envelope object)
    ("pred_dist",           "JSON"),      # {p_win, ev_std, confidence, signals, …}

    # SHAP attribution (top drivers/drags per model)
    ("shap",                "JSON"),      # {iv_direction: {drivers, drags}, …}

    # Live market context snapshot used as features
    ("live",                "JSON"),
]


def migrate() -> None:
    ensure_ml_predictions_table()
    added, skipped = [], []
    with connect() as con:
        for col, typ in NEW_COLUMNS:
            try:
                con.execute(f"ALTER TABLE {ML_PREDICTIONS_TABLE} ADD COLUMN {col} {typ}")
                added.append(col)
            except Exception:
                skipped.append(col)
        con.commit()

    print(f"ml_predictions migration complete.")
    print(f"  Added  ({len(added)}):   {', '.join(added) if added else '—'}")
    print(f"  Skipped ({len(skipped)}): {', '.join(skipped) if skipped else '—'}")

    # Print final schema
    with connect() as con:
        cols = con.execute(
            f"SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{ML_PREDICTIONS_TABLE}' ORDER BY ordinal_position"
        ).fetchall()
    print(f"\nFinal schema ({len(cols)} columns):")
    for name, dtype in cols:
        print(f"  {name:<24} {dtype}")


if __name__ == "__main__":
    migrate()
