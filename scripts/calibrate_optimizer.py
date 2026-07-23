"""
calibrate_optimizer.py — Tune delta/width grids from labeled trade outcomes.

The optimizer (optimize_credit_spread / optimize_debit_spread in analyze.py)
searches over CREDIT_DELTA_GRID × WIDTH_GRID defined in config/settings.toml.
Those grids are currently hand-picked. This script analyzes labeled training
snapshots to find which delta/width combinations actually produce the best
risk-adjusted results, then prints recommended grid values and optionally
writes them back to settings.toml.

How it works:
  1. Load labeled training_snapshots (same source as run_strategy_backtest).
  2. Parse each snapshot's candidate JSON for short_delta and width (in points).
  3. Bucket short_delta into 0.05-wide bins, width into predefined tiers.
  4. For each (delta_bucket, width_bucket) × structure:
       - win_rate     — fraction of labeled trades that were profitable
       - avg_pnl      — mean pnl_per_share
       - sharpe       — mean/std pnl_per_share scaled to annual
       - n_trades     — sample count (buckets with < MIN_SAMPLE are flagged)
  5. Rank buckets by avg_pnl × win_rate (risk-adjusted score).
  6. Emit top-N per option type (credit/debit) as recommended grid values.
  7. Optionally write CREDIT_DELTA_GRID and WIDTH_GRID to settings.toml [--write].

Calibration versioning:
  Every run (report or --write) is saved to data/calibration_history.jsonl.
  Each record stores: run_id, n_trades, date_range, grids_hash, recommended
  grids, and calibration_metrics (weighted stats of the recommended buckets).
  When --write is used, the run_id is embedded in settings.toml so you can
  always answer "which calibration produced these settings?"

  Use --history to view past calibration runs.

Run:
  python -m scripts.calibrate_optimizer              # report only
  python -m scripts.calibrate_optimizer --write      # update settings.toml
  python -m scripts.calibrate_optimizer --history    # show past runs
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_HISTORY_PATH = _ROOT / "data" / "calibration_history.jsonl"

log = logging.getLogger(__name__)

MIN_SAMPLE = 10      # buckets with fewer trades are reported but flagged as low-confidence
DELTA_STEP = 0.05    # bin width for short_delta
WIDTH_TIERS = [1, 2, 3, 5, 7, 10, 15, 20, 30]  # upper edge of each width bucket


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bucket_delta(delta: float) -> float:
    """Round delta to nearest DELTA_STEP bin centre."""
    return round(round(delta / DELTA_STEP) * DELTA_STEP, 2)


def _bucket_width(width: float) -> int:
    """Return the smallest WIDTH_TIER >= width."""
    for t in WIDTH_TIERS:
        if width <= t:
            return t
    return WIDTH_TIERS[-1]


def _try_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_candidate(raw) -> dict | None:
    """
    Extract short_delta and width from candidate JSON or dict.

    Candidate JSON stores leg strikes rather than short_delta/width directly:
      put_short_strike, put_long_strike, call_short_strike, call_long_strike,
      spot_at_entry, net_delta, max_profit, is_credit.

    Width  — put_short − put_long (or call_long − call_short).
    Short delta — abs(net_delta) for simple spreads.
                  For Iron Condors (net_delta ≈ 0), uses average OTM distance
                  of both short strikes from spot as a moneyness proxy.
                  Delta-bucket results for condors are approximate; width-bucket
                  results are reliable for all structures.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None

    structure  = raw.get("structure") or ""
    is_credit  = raw.get("is_credit")
    put_short  = _try_float(raw.get("put_short_strike"))
    put_long   = _try_float(raw.get("put_long_strike"))
    call_short = _try_float(raw.get("call_short_strike"))
    call_long  = _try_float(raw.get("call_long_strike"))
    spot       = _try_float(raw.get("spot_at_entry"))
    net_delta  = _try_float(raw.get("net_delta"))

    # Width: prefer put-side, fall back to call-side, then stored "width" key
    if put_short is not None and put_long is not None:
        width = abs(put_short - put_long)
    elif call_long is not None and call_short is not None:
        width = abs(call_long - call_short)
    else:
        width = _try_float(raw.get("width"))

    if width is None or width == 0:
        return None

    # Short delta proxy:
    # Iron Condor net_delta ≈ 0 (put/call sides cancel) so use average OTM
    # distance of the two short strikes from spot as a moneyness-based proxy.
    is_condor = "Condor" in structure or "condor" in structure
    if is_condor and spot and spot > 0:
        otm_distances = []
        if put_short is not None:
            otm_distances.append(abs(spot - put_short) / spot)
        if call_short is not None:
            otm_distances.append(abs(call_short - spot) / spot)
        short_delta = sum(otm_distances) / len(otm_distances) if otm_distances else None
    else:
        short_delta = abs(net_delta) if net_delta is not None else None

    if short_delta is None:
        return None

    return {
        "structure":   structure,
        "is_credit":   bool(is_credit),
        "short_delta": short_delta,
        "width":       width,
        "credit":      _try_float(raw.get("max_profit")),
        "debit":       None,
    }


def _compute_grids_hash(grids: dict) -> str:
    """SHA-256 of the canonical JSON representation of the recommended grids."""
    canonical = json.dumps(grids, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _extract_date_range(df: pd.DataFrame) -> dict:
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    if dates.empty:
        return {"from": None, "to": None}
    return {
        "from": dates.min().strftime("%Y-%m-%d"),
        "to":   dates.max().strftime("%Y-%m-%d"),
    }


def _compute_calibration_metrics(bucket_df: pd.DataFrame, grids: dict) -> dict:
    """
    Weighted summary stats of the buckets whose delta/width values appear in
    the recommended grids. Used as a quality proxy for the calibration run —
    higher win_rate and avg_pnl means the recommended buckets have stronger
    historical backing.
    """
    rec_deltas = set(grids.get("credit_delta_grid", []) + grids.get("debit_long_delta_grid", []))
    rec_widths  = set(grids.get("width_grid", []))
    in_rec = bucket_df[
        bucket_df["delta_bucket"].isin(rec_deltas) |
        bucket_df["width_bucket"].isin(rec_widths)
    ]
    if in_rec.empty:
        return {}
    weights = in_rec["n_trades"].values.astype(float)
    total   = weights.sum()
    return {
        "weighted_win_rate": round(float(np.dot(weights, in_rec["win_rate"].values)) / total, 3),
        "weighted_avg_pnl":  round(float(np.dot(weights, in_rec["avg_pnl"].values))  / total, 4),
        "weighted_sharpe":   round(float(np.dot(weights, in_rec["sharpe"].values))   / total, 3),
        "n_buckets_in_rec":  int(len(in_rec)),
        "total_trades_in_rec": int(int(total)),
    }


# ── Data loading ───────────────────────────────────────────────────────────────

def load_calibration_data() -> pd.DataFrame:
    """
    Load labeled training_snapshots with parsed candidate fields.
    Returns rows with columns: ticker, date, structure, is_credit,
    short_delta, width, pnl_per_share, win.
    """
    from scripts.db import read_df, SNAPSHOTS_TABLE

    df = read_df(f"SELECT * FROM {SNAPSHOTS_TABLE} WHERE labeled = true")
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["collected_at"].str[:10], errors="coerce")

    def _parse_outcome(raw) -> tuple[float | None, bool | None]:
        if raw is None:
            return None, None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return None, None
        if not isinstance(raw, dict) or raw.get("unlabelable"):
            return None, None
        return raw.get("pnl_per_share"), raw.get("win")

    outcomes = df["outcome"].apply(_parse_outcome)
    df["pnl_per_share"] = outcomes.apply(lambda t: t[0])
    df["win"]           = outcomes.apply(lambda t: t[1])

    cands = df["candidate"].apply(_parse_candidate)
    for field in ("structure", "is_credit", "short_delta", "width", "credit", "debit"):
        df[field] = cands.apply(lambda c, f=field: c.get(f) if c else None)

    df = df.dropna(subset=["pnl_per_share", "win", "short_delta", "width"])
    df["delta_bucket"] = df["short_delta"].apply(_bucket_delta)
    df["width_bucket"] = df["width"].apply(_bucket_width)

    return df[["ticker", "date", "structure", "is_credit",
               "short_delta", "width", "delta_bucket", "width_bucket",
               "pnl_per_share", "win"]].copy()


# ── Analysis ───────────────────────────────────────────────────────────────────

def analyze_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute per-(structure, delta_bucket, width_bucket) metrics.
    Returns a DataFrame sorted by risk_adj_score descending.
    """
    rows = []
    for (struct, db, wb), grp in df.groupby(["structure", "delta_bucket", "width_bucket"]):
        pnl = grp["pnl_per_share"].values.astype(float)
        win = grp["win"].values.astype(float)
        n   = len(pnl)
        avg_pnl  = float(np.mean(pnl))
        win_rate = float(np.mean(win))
        std      = float(np.std(pnl, ddof=1)) if n > 1 else 0.0
        sharpe   = (avg_pnl / std * np.sqrt(52)) if std > 0 else 0.0
        risk_adj = win_rate * avg_pnl if avg_pnl > 0 else win_rate * avg_pnl * 2
        rows.append({
            "structure":    struct,
            "delta_bucket": db,
            "width_bucket": wb,
            "n_trades":     n,
            "win_rate":     round(win_rate, 3),
            "avg_pnl":      round(avg_pnl, 4),
            "sharpe":       round(sharpe, 3),
            "risk_adj":     round(risk_adj, 5),
            "low_confidence": n < MIN_SAMPLE,
        })
    return pd.DataFrame(rows).sort_values("risk_adj", ascending=False)


def recommend_grids(
    bucket_df: pd.DataFrame,
    top_credit_n: int = 5,
    top_debit_n: int = 5,
) -> dict:
    """
    Extract recommended delta and width grid values from the top-performing buckets.
    """
    credit_df = bucket_df[~bucket_df["low_confidence"] & bucket_df["structure"].str.contains("Credit|Condor|Lizard|Put$|Call$", na=False)]
    debit_df  = bucket_df[~bucket_df["low_confidence"] & bucket_df["structure"].str.contains("Debit|Spread|LEAPS|Calendar|Diagonal", na=False)]

    top_credit = credit_df.head(top_credit_n)
    top_debit  = debit_df.head(top_debit_n)

    credit_deltas = sorted(set(top_credit["delta_bucket"].tolist())) if len(top_credit) else []
    credit_widths = sorted(set(top_credit["width_bucket"].tolist())) if len(top_credit) else []
    debit_deltas  = sorted(set(top_debit["delta_bucket"].tolist()))  if len(top_debit)  else []

    return {
        "credit_delta_grid":      credit_deltas,
        "width_grid":             credit_widths,
        "debit_long_delta_grid":  debit_deltas,
        "debit_short_delta_grid": [],
    }


# ── Calibration history ────────────────────────────────────────────────────────

def save_calibration_run(record: dict) -> None:
    """Append one calibration run record to data/calibration_history.jsonl."""
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    log.info(f"Calibration run {record['run_id']} saved to {_HISTORY_PATH}")


def list_calibration_history(n: int = 10) -> list[dict]:
    """Return the last n calibration runs, newest first."""
    if not _HISTORY_PATH.exists():
        return []
    lines = _HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    records = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return list(reversed(records[-n:]))


# ── Settings writer ────────────────────────────────────────────────────────────

def write_grids_to_settings(grids: dict, run_id: str) -> None:
    """
    Write recommended grid values back to config/settings.toml [optimize_grid] section.
    Embeds the calibration run_id as a comment and updates last_calibration_run_id.
    Makes a .bak copy before modifying.
    """
    import re

    toml_path = _ROOT / "config" / "settings.toml"
    bak_path  = toml_path.with_suffix(".toml.bak")

    text = toml_path.read_text(encoding="utf-8")
    bak_path.write_text(text, encoding="utf-8")

    def _replace_list(src: str, key: str, values: list) -> str:
        if not values:
            return src
        val_str = "[" + ", ".join(str(v) for v in values) + "]"
        pattern = rf"({re.escape(key)}\s*=\s*)\[.*?\]"
        return re.sub(pattern, rf"\g<1>{val_str}", src, count=1)

    for key, values in grids.items():
        if values:
            text = _replace_list(text, key, values)

    # Embed run_id as a comment + key directly after [optimize_grid]
    run_comment = f"# calibrated: {run_id}\n"
    run_id_key  = f'last_calibration_run_id = "{run_id}"\n'

    # Replace existing calibration comment + key if present, otherwise inject after [optimize_grid]
    if "# calibrated:" in text:
        text = re.sub(r"# calibrated:.*\n", run_comment, text, count=1)
    else:
        text = text.replace("[optimize_grid]\n", f"[optimize_grid]\n{run_comment}", 1)

    if "last_calibration_run_id" in text:
        text = re.sub(r'last_calibration_run_id\s*=\s*"[^"]*"\n', run_id_key, text, count=1)
    else:
        # Insert after the comment line
        text = text.replace(run_comment, run_comment + run_id_key, 1)

    toml_path.write_text(text, encoding="utf-8")
    log.info(f"Updated {toml_path} with run_id={run_id} (backup at {bak_path})")


# ── Main pipeline ──────────────────────────────────────────────────────────────

def run_calibration(write: bool = False) -> dict:
    """
    Full calibration pipeline. Returns analysis results dict.
    Every run is saved to data/calibration_history.jsonl regardless of --write.
    If write=True, also updates config/settings.toml.
    """
    df = load_calibration_data()
    if df.empty:
        return {"ok": False, "error": "No labeled snapshots with parseable candidate data"}

    bucket_df = analyze_buckets(df)
    grids     = recommend_grids(bucket_df)

    run_id     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_range = _extract_date_range(df)
    grids_hash = _compute_grids_hash(grids)
    cal_metrics = _compute_calibration_metrics(bucket_df, grids)

    result = {
        "ok":                True,
        "run_id":            run_id,
        "n_trades":          int(len(df)),
        "n_buckets":         int(len(bucket_df)),
        "low_conf_buckets":  int(bucket_df["low_confidence"].sum()),
        "date_range":        date_range,
        "grids_hash":        grids_hash,
        "recommended_grids": grids,
        "calibration_metrics": cal_metrics,
        "top_buckets":       bucket_df.head(20).to_dict("records"),
        "written":           False,
    }

    if write:
        write_grids_to_settings(grids, run_id)
        result["written"] = True

    save_calibration_run({
        "run_id":              run_id,
        "n_trades":            result["n_trades"],
        "date_range":          date_range,
        "grids_hash":          grids_hash,
        "recommended_grids":   grids,
        "calibration_metrics": cal_metrics,
        "written":             result["written"],
    })

    return result


# ── Console reporters ──────────────────────────────────────────────────────────

def _print_calibration_result(result: dict) -> None:
    print(f"\n=== Optimizer Calibration  run_id={result['run_id']} ===")
    dr = result["date_range"]
    print(f"Data range        : {dr['from']} → {dr['to']}  ({result['n_trades']} labeled trades)")
    print(f"Grids hash        : {result['grids_hash']}")
    print(f"Buckets analyzed  : {result['n_buckets']}  ({result['low_conf_buckets']} flagged low-confidence)")

    cm = result.get("calibration_metrics") or {}
    if cm:
        print(f"\nRecommended-bucket quality (weighted):")
        print(f"  win rate  {cm.get('weighted_win_rate', 0):.1%}   "
              f"avg P&L  ${cm.get('weighted_avg_pnl', 0):.3f}   "
              f"sharpe  {cm.get('weighted_sharpe', 0):.3f}   "
              f"({cm.get('total_trades_in_rec', 0)} trades across {cm.get('n_buckets_in_rec', 0)} buckets)")

    print(f"\nTop performing (delta × width) buckets:")
    print(f"  {'Structure':<30}  {'Δ':>5}  {'W':>4}  {'win%':>6}  {'avg$':>7}  {'sharpe':>7}  {'n':>4}  {'conf'}")
    print("  " + "─" * 78)
    for r in result["top_buckets"]:
        conf = "LOW" if r["low_confidence"] else "   "
        print(f"  {r['structure']:<30}  {r['delta_bucket']:>5.2f}  {r['width_bucket']:>4}  "
              f"{r['win_rate']:>5.0%}  ${r['avg_pnl']:>6.3f}  {r['sharpe']:>7.3f}  {r['n_trades']:>4}  {conf}")

    g = result["recommended_grids"]
    print(f"\nRecommended grid values:")
    print(f"  credit_delta_grid      = {g['credit_delta_grid']}")
    print(f"  width_grid             = {g['width_grid']}")
    print(f"  debit_long_delta_grid  = {g['debit_long_delta_grid']}")

    if result["written"]:
        print(f"\nWritten to config/settings.toml  (backup: settings.toml.bak)")
    else:
        print(f"\nDry run — add --write to update config/settings.toml")


def _print_history(records: list[dict]) -> None:
    if not records:
        print("No calibration history found. Run calibrate_optimizer at least once.")
        return
    print(f"\n{'Run ID':<22}  {'Trades':>7}  {'Date range':<24}  {'Hash':>12}  {'Written':>7}  {'w_win%':>6}  {'w_avg$':>7}")
    print("─" * 95)
    for r in records:
        dr  = r.get("date_range") or {}
        cm  = r.get("calibration_metrics") or {}
        date_range = f"{dr.get('from','?')} → {dr.get('to','?')}"
        written    = "yes" if r.get("written") else "no"
        w_win  = f"{cm.get('weighted_win_rate', 0):.1%}" if cm else "—"
        w_pnl  = f"${cm.get('weighted_avg_pnl', 0):.3f}" if cm else "—"
        print(f"{r.get('run_id','?'):<22}  {r.get('n_trades',0):>7}  {date_range:<24}  "
              f"{r.get('grids_hash','?'):>12}  {written:>7}  {w_win:>6}  {w_pnl:>7}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if "--history" in sys.argv:
        _print_history(list_calibration_history(n=20))
        sys.exit(0)

    _write  = "--write" in sys.argv
    result  = run_calibration(write=_write)

    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)

    _print_calibration_result(result)
