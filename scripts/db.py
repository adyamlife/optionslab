"""
Central DuckDB connection for the ML training database.

All reads and writes go through read_df() / append_df() / execute() here
so there is exactly one place to change the DB path or connection settings.

Database: data/ml_training.duckdb
Main table: regime_training  (one row per ticker per trading day)
"""
from pathlib import Path
import duckdb
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
    candidate        JSON,
    news_headlines   JSON,
    labeled          BOOLEAN,
    outcome          JSON,
    labeled_at       VARCHAR
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


def ensure_snapshot_tables() -> None:
    with connect() as con:
        con.execute(_SNAPSHOTS_DDL)
        con.execute(_CHAIN_DDL)
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_snap_ticker ON {SNAPSHOTS_TABLE} (ticker)")
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_chain_ticker ON {CHAIN_TABLE} (ticker, collected_at)")
        con.commit()


def insert_snapshot(record: dict) -> None:
    """Insert one training snapshot row. JSON fields serialized automatically."""
    import json as _json
    ensure_snapshot_tables()
    r = dict(record)
    r["candidate"]      = _json.dumps(r.get("candidate"))
    r["news_headlines"] = _json.dumps(r.get("news_headlines") or [])
    r["outcome"]        = _json.dumps(r.get("outcome"))
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
