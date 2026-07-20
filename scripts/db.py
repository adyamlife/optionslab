"""
Central DuckDB connection for the ML training database.

All reads and writes go through read_df() / append_df() / execute() here
so there is exactly one place to change the DB path or connection settings.

Database: data/ml_training.duckdb
Main table: regime_training  (one row per ticker per trading day)
"""
from pathlib import Path
import duckdb
import json as _json_mod
import pandas as pd

_ROOT    = Path(__file__).resolve().parent.parent
_DB_PATH = _ROOT / "data" / "ml_training.duckdb"

# Table that holds all backfill + daily rows
TABLE = "regime_training"


def connect() -> duckdb.DuckDBPyConnection:
    """Return a new DuckDB connection to the shared database file."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(_DB_PATH))


def read_df(query: str | None = None, params: list | None = None) -> pd.DataFrame:
    """
    Read from the database and return a DataFrame.
    If query is None, returns the full regime_training table.
    """
    sql = query or f"SELECT * FROM {TABLE}"
    with connect() as con:
        return con.execute(sql, params or []).df()


def execute(sql: str, params: list | None = None) -> None:
    """Run a write statement (INSERT / UPDATE / DELETE / CREATE)."""
    with connect() as con:
        con.execute(sql, params or [])
        con.commit()


def table_exists() -> bool:
    with connect() as con:
        result = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            f"WHERE table_name = '{TABLE}'"
        ).fetchone()
        return result[0] > 0


def append_df(df: pd.DataFrame) -> None:
    """Append rows to regime_training, creating the table if needed."""
    if df.empty:
        return
    with connect() as con:
        if not table_exists():
            con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM df")
        else:
            con.execute(f"INSERT INTO {TABLE} SELECT * FROM df")
        con.commit()


def upsert_df(df: pd.DataFrame, key_cols: list[str] = None) -> int:
    """
    Replace rows matching (ticker, date) with updated values.
    Used by label_pending_regime_rows() to fill in labels in-place.
    Returns number of rows updated.
    """
    key_cols = key_cols or ["ticker", "date"]
    if df.empty:
        return 0

    with connect() as con:
        # Write incoming rows to a temp table, then UPDATE matching rows
        con.execute("CREATE TEMP TABLE _upsert AS SELECT * FROM df")
        key_clause = " AND ".join(f"t.{c} = u.{c}" for c in key_cols)
        all_cols = [c for c in df.columns if c not in key_cols]
        set_clause = ", ".join(f"t.{c} = u.{c}" for c in all_cols)
        updated = con.execute(
            f"UPDATE {TABLE} t SET {set_clause} FROM _upsert u WHERE {key_clause}"
        ).rowcount
        con.execute("DROP TABLE IF EXISTS _upsert")
        con.commit()
    return updated or 0


def row_count() -> int:
    if not table_exists():
        return 0
    with connect() as con:
        return con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]


# ── Training snapshots (POP model) ────────────────────────────────────────────

SNAPSHOTS_TABLE = "training_snapshots"
CHAIN_TABLE     = "option_chain_snapshots"

_snapshot_tables_ready = False  # guard: ensure DDL runs once per process

_SNAPSHOTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {SNAPSHOTS_TABLE} (
    snapshot_id      VARCHAR PRIMARY KEY,
    collected_at     VARCHAR,
    ticker           VARCHAR,
    spot             DOUBLE,
    iv_env           VARCHAR,
    trend            VARCHAR,
    weekly_trend     VARCHAR,
    regime           VARCHAR,
    rsi              DOUBLE,
    macd_trend       VARCHAR,
    adx              DOUBLE,
    atm_iv           DOUBLE,
    iv_rank_proxy    DOUBLE,
    hv20             DOUBLE,
    pcr              DOUBLE,
    vix              DOUBLE,
    earnings_days_away DOUBLE,
    status           VARCHAR,
    recommended_structure VARCHAR,
    signal_score     DOUBLE,
    expiry           VARCHAR,
    dte              DOUBLE,
    vol_oi_ratio     DOUBLE,
    iv_skew          DOUBLE,
    iv_term_slope    DOUBLE,
    otm_pcr          DOUBLE,
    beta_60d         DOUBLE,
    atr_pct          DOUBLE,
    iv_rank_52w      DOUBLE,
    sector_etf       VARCHAR,
    sector_trend     VARCHAR,
    sector_rsi       DOUBLE,
    sector_iv_ratio  DOUBLE,
    spy_trend        VARCHAR,
    spy_rsi          DOUBLE,
    qqq_trend        VARCHAR,
    qqq_rsi          DOUBLE,
    iwm_trend        VARCHAR,
    iwm_rsi          DOUBLE,
    vvix             DOUBLE,
    vix_3m           DOUBLE,
    vix_term_slope   DOUBLE,
    earnings_inside_expiry BOOLEAN,
    news_sentiment_score   DOUBLE,
    analyst_rec_change     DOUBLE,
    short_interest_pct     DOUBLE,
    iv_skew_20d      DOUBLE,
    gex_proxy        DOUBLE,
    max_pain_strike  DOUBLE,
    oi_concentration DOUBLE,
    wings_iv_ratio   DOUBLE,
    yield_10y        DOUBLE,
    yield_3m         DOUBLE,
    yield_curve      DOUBLE,
    dollar_index     DOUBLE,
    fed_within_dte   DOUBLE,
    cpi_within_dte   DOUBLE,
    ppi_days_away    DOUBLE,
    ppi_within_dte   DOUBLE,
    jobs_days_away   DOUBLE,
    jobs_within_dte  DOUBLE,
    days_to_opex     DOUBLE,
    opex_within_dte  DOUBLE,
    is_opex_week     DOUBLE,
    is_monthly_opex  DOUBLE,
    is_quarterly_opex DOUBLE,
    iv_pct_rank      DOUBLE,
    gamma_pct_rank   DOUBLE,
    volume_pct_rank  DOUBLE,
    momentum_pct_rank DOUBLE,
    oi_pct_rank      DOUBLE,
    forward_1d       DOUBLE,
    forward_3d       DOUBLE,
    forward_5d       DOUBLE,
    future_hv5d      DOUBLE,
    candidate        JSON,
    news_headlines   JSON,
    labeled          BOOLEAN,
    outcome          JSON,
    labeled_at       VARCHAR,
    source           VARCHAR,
    paper_trade_id   VARCHAR,
    ml_meta_score      DOUBLE,
    ml_p_win           DOUBLE,
    ml_confidence      DOUBLE,
    ml_ranker_score    DOUBLE,
    ml_return_score    DOUBLE,
    ml_anomaly_score   DOUBLE,
    ml_composite_score DOUBLE,
    ml_confidence_tier VARCHAR,
    ml_expected_vol    DOUBLE,
    ml_iv_expanding    DOUBLE,
    ml_p_return_gt10   DOUBLE,
    ml_p_up            DOUBLE,
    ml_regime          VARCHAR,
    garch_vol_at_entry DOUBLE,
    call_vol           BIGINT,
    put_vol            BIGINT,
    sector_return_1d   DOUBLE,
    move_index         DOUBLE
)
"""

_CHAIN_DDL = f"""
CREATE TABLE IF NOT EXISTS {CHAIN_TABLE} (
    snapshot_id      VARCHAR,
    collected_at     VARCHAR,
    ticker           VARCHAR,
    spot             DOUBLE,
    expiry           VARCHAR,
    dte              INTEGER,
    source           VARCHAR,
    is_position_legs BOOLEAN,
    strikes          JSON
)
"""

# ── Tier 0 Data Flywheel tables ───────────────────────────────────────────────

_INTRADAY_BARS_DDL = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    ticker  VARCHAR NOT NULL,
    date    VARCHAR NOT NULL,
    time    VARCHAR NOT NULL,
    open    DOUBLE,
    high    DOUBLE,
    low     DOUBLE,
    close   DOUBLE,
    volume  BIGINT,
    vwap    DOUBLE,
    PRIMARY KEY (ticker, date, time)
)
"""

_VIX_TERM_DDL = """
CREATE TABLE IF NOT EXISTS vix_term_structure (
    date           VARCHAR PRIMARY KEY,
    vix            DOUBLE,
    vix_3m         DOUBLE,
    vix_6m         DOUBLE,
    contango_ratio DOUBLE,
    term_slope     DOUBLE
)
"""

_OI_CHANGES_DDL = """
CREATE TABLE IF NOT EXISTS oi_changes (
    ticker       VARCHAR NOT NULL,
    date         VARCHAR NOT NULL,
    time_of_day  VARCHAR NOT NULL,
    collected_at VARCHAR,
    expiry       VARCHAR,
    strike       DOUBLE,
    option_type  VARCHAR,
    oi           BIGINT,
    iv           DOUBLE,
    gamma        DOUBLE,
    volume       BIGINT
)
"""

_EARNINGS_IV_DDL = """
CREATE TABLE IF NOT EXISTS earnings_iv_tracker (
    ticker           VARCHAR NOT NULL,
    earnings_date    VARCHAR NOT NULL,
    snapshot_date    VARCHAR NOT NULL,
    days_to_earnings INTEGER,
    atm_iv           DOUBLE,
    front_iv         DOUBLE,
    back_iv          DOUBLE,
    iv_rank          DOUBLE,
    post_crush_pct   DOUBLE,
    PRIMARY KEY (ticker, earnings_date, snapshot_date)
)
"""


def ensure_archive_tables() -> None:
    """Create the four Tier 0 flywheel tables if they don't exist yet."""
    with connect() as con:
        for ddl in (_INTRADAY_BARS_DDL, _VIX_TERM_DDL, _OI_CHANGES_DDL, _EARNINGS_IV_DDL):
            con.execute(ddl)
        con.execute("CREATE INDEX IF NOT EXISTS idx_intraday_ticker ON intraday_bars (ticker, date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_oi_ticker ON oi_changes (ticker, date, expiry)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_earnings_ticker ON earnings_iv_tracker (ticker)")
        con.commit()


def _insert_intraday_bars(rows: list[dict]) -> None:
    if not rows:
        return
    with connect() as con:
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO intraday_bars "
                "(ticker, date, time, open, high, low, close, volume, vwap) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [r["ticker"], r["date"], r["time"],
                 r["open"], r["high"], r["low"], r["close"],
                 r["volume"], r.get("vwap")],
            )
        con.commit()


def _insert_vix_term_structure(record: dict) -> None:
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO vix_term_structure "
            "(date, vix, vix_3m, vix_6m, contango_ratio, term_slope) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [record["date"], record.get("vix"), record.get("vix_3m"),
             record.get("vix_6m"), record.get("contango_ratio"), record.get("term_slope")],
        )
        con.commit()


def _insert_oi_snapshot(rows: list[dict]) -> None:
    if not rows:
        return
    with connect() as con:
        for r in rows:
            con.execute(
                "INSERT INTO oi_changes "
                "(ticker, date, time_of_day, collected_at, expiry, strike, option_type, oi, iv, gamma, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [r["ticker"], r["date"], r["time_of_day"], r["collected_at"],
                 r["expiry"], r["strike"], r["option_type"],
                 r["oi"], r.get("iv"), r.get("gamma"), r.get("volume", 0)],
            )
        con.commit()


def _insert_earnings_iv(record: dict) -> None:
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO earnings_iv_tracker "
            "(ticker, earnings_date, snapshot_date, days_to_earnings, "
            " atm_iv, front_iv, back_iv, iv_rank, post_crush_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [record["ticker"], record["earnings_date"], record["snapshot_date"],
             record.get("days_to_earnings"), record.get("atm_iv"),
             record.get("front_iv"), record.get("back_iv"),
             record.get("iv_rank"), record.get("post_crush_pct")],
        )
        con.commit()


def ensure_snapshot_tables() -> None:
    global _snapshot_tables_ready
    if _snapshot_tables_ready:
        return
    with connect() as con:
        con.execute(_SNAPSHOTS_DDL)
        con.execute(_CHAIN_DDL)
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_snap_ticker ON {SNAPSHOTS_TABLE} (ticker)")
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_chain_ticker ON {CHAIN_TABLE} (ticker, collected_at)")
        # Migrate existing tables that pre-date these columns
        for col, typ in [
            ("source",              "VARCHAR"),
            ("paper_trade_id",      "VARCHAR"),
            ("ml_meta_score",       "DOUBLE"),
            ("ml_p_win",            "DOUBLE"),
            ("ml_confidence",       "DOUBLE"),
            ("ml_ranker_score",     "DOUBLE"),
            ("ml_return_score",     "DOUBLE"),
            ("ml_anomaly_score",    "DOUBLE"),
            ("ml_composite_score",  "DOUBLE"),
            ("ml_confidence_tier",  "VARCHAR"),
            ("ml_expected_vol",     "DOUBLE"),
            ("ml_iv_expanding",     "DOUBLE"),
            ("ml_p_return_gt10",    "DOUBLE"),
            ("ml_p_up",             "DOUBLE"),
            ("ml_regime",           "VARCHAR"),
            ("garch_vol_at_entry",  "DOUBLE"),
            ("call_vol",            "BIGINT"),
            ("put_vol",             "BIGINT"),
            # #9 Event calendar
            ("ppi_days_away",       "DOUBLE"),
            ("ppi_within_dte",      "DOUBLE"),
            ("jobs_days_away",      "DOUBLE"),
            ("jobs_within_dte",     "DOUBLE"),
            ("days_to_opex",        "DOUBLE"),
            ("opex_within_dte",     "DOUBLE"),
            ("is_opex_week",        "DOUBLE"),
            ("is_monthly_opex",     "DOUBLE"),
            ("is_quarterly_opex",   "DOUBLE"),
            # #6 Cross-sectional percentile ranks
            ("iv_pct_rank",         "DOUBLE"),
            ("gamma_pct_rank",      "DOUBLE"),
            ("volume_pct_rank",     "DOUBLE"),
            ("momentum_pct_rank",   "DOUBLE"),
            ("oi_pct_rank",         "DOUBLE"),
            # #8 Forward return labels
            ("forward_1d",          "DOUBLE"),
            ("forward_3d",          "DOUBLE"),
            ("forward_5d",          "DOUBLE"),
            ("future_hv5d",         "DOUBLE"),
            # #10/#11 Cross-asset + sector enrichment
            ("sector_return_1d",    "DOUBLE"),
            ("move_index",          "DOUBLE"),
        ]:
            try:
                con.execute(f"ALTER TABLE {SNAPSHOTS_TABLE} ADD COLUMN {col} {typ}")
            except Exception:
                pass  # column already exists
        con.commit()
    _snapshot_tables_ready = True


class _NumpyEncoder(_json_mod.JSONEncoder):
    """Coerce numpy scalars (bool_, int64, float32, …) to native Python types.
    numpy.bool_ is not a subclass of Python bool, so the default encoder rejects it.
    All numpy scalars expose .item() which returns the equivalent native type."""
    def default(self, obj):
        if hasattr(obj, "item"):   # numpy scalar
            return obj.item()
        return super().default(obj)


def _safe_dumps(obj) -> str:
    return _json_mod.dumps(obj, cls=_NumpyEncoder)


def insert_snapshot(record: dict) -> None:
    """Insert one training snapshot row. JSON fields serialized automatically."""
    import json as _json
    ensure_snapshot_tables()
    r = dict(record)
    r["candidate"]      = _safe_dumps(r.get("candidate"))
    r["news_headlines"] = _safe_dumps(r.get("news_headlines") or [])
    r["outcome"]        = _safe_dumps(r.get("outcome"))
    cols = list(r.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    vals = [r[c] for c in cols]
    with connect() as con:
        con.execute(
            f"INSERT OR REPLACE INTO {SNAPSHOTS_TABLE} ({col_names}) VALUES ({placeholders})",
            vals,
        )
        con.commit()


def insert_chain_snapshot(record: dict) -> None:
    """Insert one chain snapshot row."""
    import json as _json
    ensure_snapshot_tables()
    r = dict(record)
    r["strikes"]          = _json.dumps(r.get("strikes") or [])
    r["is_position_legs"] = bool(r.get("is_position_legs", False))
    cols = list(r.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    vals = [r[c] for c in cols]
    with connect() as con:
        con.execute(
            f"INSERT INTO {CHAIN_TABLE} ({col_names}) VALUES ({placeholders})",
            vals,
        )
        con.commit()


def load_all_snapshots() -> list[dict]:
    """Return all training_snapshots as a list of dicts with JSON fields parsed."""
    import json as _json
    ensure_snapshot_tables()
    df = read_df(f"SELECT * FROM {SNAPSHOTS_TABLE}")
    records = df.to_dict("records")
    for r in records:
        for field in ("candidate", "news_headlines", "outcome"):
            if isinstance(r.get(field), str):
                try:
                    r[field] = _json.loads(r[field])
                except Exception:
                    pass
    return records


def update_snapshot_labels(records: list[dict]) -> int:
    """Update labeled/outcome/labeled_at for a list of snapshots by snapshot_id."""
    import json as _json
    if not records:
        return 0
    updated = 0
    with connect() as con:
        for r in records:
            con.execute(
                f"UPDATE {SNAPSHOTS_TABLE} SET labeled=?, outcome=?, labeled_at=? "
                f"WHERE snapshot_id=?",
                [
                    bool(r.get("labeled")),
                    _json.dumps(r.get("outcome")),
                    r.get("labeled_at"),
                    r["snapshot_id"],
                ],
            )
            updated += 1
        con.commit()
    return updated


def update_snapshot_forward_returns(records: list[dict]) -> int:
    """Update forward_1d/3d/5d and future_hv5d for a list of snapshots by snapshot_id."""
    if not records:
        return 0
    updated = 0
    with connect() as con:
        for r in records:
            con.execute(
                f"UPDATE {SNAPSHOTS_TABLE} "
                f"SET forward_1d=?, forward_3d=?, forward_5d=?, future_hv5d=? "
                f"WHERE snapshot_id=?",
                [r.get("forward_1d"), r.get("forward_3d"),
                 r.get("forward_5d"), r.get("future_hv5d"),
                 r["snapshot_id"]],
            )
            updated += 1
        con.commit()
    return updated


def update_snapshot_xsec_ranks(records: list[dict]) -> int:
    """Update cross-sectional percentile rank columns for a batch of snapshots."""
    if not records:
        return 0
    with connect() as con:
        for r in records:
            con.execute(
                f"UPDATE {SNAPSHOTS_TABLE} "
                f"SET iv_pct_rank=?, gamma_pct_rank=?, volume_pct_rank=?, "
                f"    momentum_pct_rank=?, oi_pct_rank=? "
                f"WHERE snapshot_id=?",
                [r.get("iv_pct_rank"), r.get("gamma_pct_rank"),
                 r.get("volume_pct_rank"), r.get("momentum_pct_rank"),
                 r.get("oi_pct_rank"), r["snapshot_id"]],
            )
        con.commit()
    return len(records)


# ── ML Predictions history ────────────────────────────────────────────────────

ML_PREDICTIONS_TABLE = "ml_predictions"

_ML_PREDICTIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS {ML_PREDICTIONS_TABLE} (
    ticker          VARCHAR NOT NULL,
    scanned_at      VARCHAR NOT NULL,
    regime          VARCHAR,
    p_win           DOUBLE,
    confidence      DOUBLE,
    pred_return     DOUBLE,
    pred_vol        DOUBLE,
    signal_rating   VARCHAR,
    ok              BOOLEAN,
    source          VARCHAR,
    raw_json        JSON
)
"""

_ml_predictions_table_ready = False


def ensure_ml_predictions_table() -> None:
    global _ml_predictions_table_ready
    if _ml_predictions_table_ready:
        return
    with connect() as con:
        con.execute(_ML_PREDICTIONS_DDL)
        con.execute(
            f"CREATE INDEX IF NOT EXISTS idx_ml_pred_ticker "
            f"ON {ML_PREDICTIONS_TABLE} (ticker, scanned_at)"
        )
        con.commit()
    _ml_predictions_table_ready = True


def insert_ml_predictions(predictions: dict[str, dict], scanned_at: str, source: str = "scan") -> int:
    """
    Insert one row per ticker from a {ticker: pred} snapshot dict.
    Writes all model output fields as typed columns plus raw_json for forward compat.
    Returns number of rows inserted.
    """
    import json as _json
    if not predictions:
        return 0
    ensure_ml_predictions_table()

    def _f(d, k):
        v = d.get(k)
        return float(v) if v is not None else None

    def _b(d, k):
        v = d.get(k)
        return bool(v) if v is not None else None

    def _j(v):
        return _json.dumps(v) if v is not None else None

    rows = []
    for ticker, p in predictions.items():
        pd_ = p.get("pred_dist") or {}
        rows.append((
            ticker, scanned_at,
            # core
            p.get("regime"),
            _f(pd_, "p_win"),
            _f(pd_, "confidence"),
            _f(p, "expected_return"),
            _f(p, "expected_vol"),
            p.get("confidence_tier"),
            _b(p, "ok"),
            source,
            # date + regime model
            p.get("date"),
            p.get("regime_model"),
            _j(p.get("regime_proba")),
            # return classifier
            _f(p, "p_return_positive"),
            _f(p, "p_return_gt5"),
            _f(p, "p_return_gt10"),
            _f(p, "p_top_decile"),
            _f(p, "return_score"),
            _f(p, "ranker_score"),
            # vol
            _f(p, "expected_move_pct"),
            _f(p, "garch_vol_forecast"),
            # direction
            _f(p, "p_up"),
            _f(p, "p_flat"),
            _f(p, "p_down"),
            p.get("direction"),
            # IV direction
            _f(p, "iv_expanding_prob"),
            p.get("iv_direction"),
            # meta / composite
            _f(p, "meta_score"),
            _f(p, "composite_score"),
            _f(p, "iv_confidence"),
            _b(p, "anomaly_penalized"),
            # pop + analogues
            _f(p, "pop_score"),
            _f(p, "analogues_win_rate"),
            p.get("analogues_k"),
            # anomaly
            _f(p, "anomaly_score"),
            _b(p, "is_anomaly"),
            _j(p.get("anomaly_flags")),
            # streak
            p.get("ml_regime_streak"),
            # JSON blobs
            _j(p.get("pred_dist")),
            _j(p.get("shap")),
            _j(p.get("live")),
            _json.dumps(p),
        ))

    cols = (
        "ticker, scanned_at, regime, p_win, confidence, expected_return, "
        "expected_vol, signal_rating, ok, source, "
        "date, regime_model, regime_proba, "
        "p_return_positive, p_return_gt5, p_return_gt10, p_top_decile, "
        "return_score, ranker_score, expected_move_pct, garch_vol_forecast, "
        "p_up, p_flat, p_down, direction, "
        "iv_expanding_prob, iv_direction, "
        "meta_score, composite_score, iv_confidence, anomaly_penalized, "
        "pop_score, analogues_win_rate, analogues_k, "
        "anomaly_score, is_anomaly, anomaly_flags, ml_regime_streak, "
        "pred_dist, shap, live, raw_json"
    )
    placeholders = ", ".join("?" * len(rows[0]))
    with connect() as con:
        con.executemany(
            f"INSERT INTO {ML_PREDICTIONS_TABLE} ({cols}) VALUES ({placeholders})",
            rows,
        )
        con.commit()
    return len(rows)


def load_ml_predictions_history(ticker: str | None = None, limit: int = 500) -> list[dict]:
    """Return recent ML prediction rows, optionally filtered by ticker."""
    ensure_ml_predictions_table()
    where = f"WHERE ticker = ?" if ticker else ""
    params = [ticker] if ticker else []
    df = read_df(
        f"SELECT ticker, scanned_at, regime, p_win, confidence, pred_return, "
        f"pred_vol, signal_rating, source FROM {ML_PREDICTIONS_TABLE} "
        f"{where} ORDER BY scanned_at DESC LIMIT {limit}",
        params or None,
    )
    return df.to_dict("records")


def load_latest_ml_predictions() -> dict[str, dict]:
    """Return the most-recent prediction per ticker as {ticker: pred_dict}."""
    import json as _json
    ensure_ml_predictions_table()
    try:
        df = read_df(
            f"SELECT ticker, raw_json FROM ("
            f"  SELECT ticker, raw_json, "
            f"         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY scanned_at DESC) AS rn "
            f"  FROM {ML_PREDICTIONS_TABLE}"
            f") t WHERE rn = 1"
        )
        result = {}
        for _, row in df.iterrows():
            try:
                result[row["ticker"]] = _json.loads(row["raw_json"]) if row.get("raw_json") else {}
            except Exception:
                pass
        return result
    except Exception:
        return {}


def load_ml_predictions_count() -> int:
    """Total rows in ml_predictions — useful for health checks."""
    try:
        ensure_ml_predictions_table()
        with connect() as con:
            return con.execute(f"SELECT count(*) FROM {ML_PREDICTIONS_TABLE}").fetchone()[0]
    except Exception:
        return 0


def load_chain_index_from_db() -> dict:
    """
    Rebuild the chain lookup index from DuckDB:
      { ticker: { date_str: { (strike, opt_type): {iv, delta, gamma, theta, vega} } } }
    """
    import json as _json
    ensure_snapshot_tables()
    df = read_df(f"SELECT ticker, collected_at, strikes FROM {CHAIN_TABLE}")
    index: dict = {}
    for _, row in df.iterrows():
        t   = row["ticker"]
        day = str(row["collected_at"] or "")[:10]
        if not t or not day:
            continue
        try:
            strikes = _json.loads(row["strikes"]) if isinstance(row["strikes"], str) else (row["strikes"] or [])
        except Exception:
            continue
        index.setdefault(t, {}).setdefault(day, {})
        for s in strikes:
            key = (round(float(s["strike"]), 2), s["opt_type"])
            if key not in index[t][day]:
                index[t][day][key] = {
                    "iv":    float(s.get("iv") or 0),
                    "delta": s.get("delta"),
                    "gamma": s.get("gamma"),
                    "theta": s.get("theta"),
                    "vega":  s.get("vega"),
                }
    return index
