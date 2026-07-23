"""
Portfolio Backtest — two modes:

Mode A: regime_training table (run_backtest)
  Rolling portfolio simulation using the labeled forward_return from the
  regime_training CSV/DuckDB table. Ranks candidates by a score column,
  takes top-N per period, computes equal-weight return. By default uses a
  proxy score; pass score_col="signal_score" or "ml_composite_score" for
  the real signals.

  Metrics: total_return, ann_return, Sharpe, max_drawdown, profit_factor,
    win_rate, Prec@K, lift vs equal-weight universe, avg turnover, quarterly.

Mode B: training_snapshots table (run_strategy_backtest)
  Operates on actual labeled trade candidates — outcome.pnl_per_share and
  outcome.win come from real options P&L simulation in label_pending_snapshots().
  Ranks candidates by score (signal_score, ml_composite_score, or proxy), takes
  top-N per date, tracks actual trade P&L. This validates whether the scoring
  system predicts real winning trades, not just stock-level direction.

  Same metrics as Mode A plus per-structure breakdown and score-column comparison.

compare_scores(): runs Mode B with multiple score columns and returns a side-by-side
  table showing which column produces the highest lift over the unranked universe.

Run standalone:
  python -m scripts.backtest          # Mode A (proxy score, regime_training)
  python -m scripts.backtest --snap   # Mode B (signal_score, training_snapshots)
  python -m scripts.backtest --compare  # compare all score columns
"""
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)

# Default option strategy parameters (used by web/app.py default backtest config)
DTE   = 10   # days to expiration — matches FORWARD_DAYS in regime_backfill
WIDTH = 5    # spread width in points (credit spread default)


def _sharpe(returns: np.ndarray, periods_per_year: float = 26.0) -> float:
    """Annualized Sharpe. periods_per_year=26 for 10-day (bi-weekly) periods."""
    if len(returns) < 2:
        return 0.0
    mu    = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    return round(mu / sigma * np.sqrt(periods_per_year), 3) if sigma > 0 else 0.0


def _max_drawdown(period_returns: np.ndarray) -> float:
    """Maximum peak-to-trough drawdown on the cumulative equity curve."""
    if len(period_returns) == 0:
        return 0.0
    equity = np.cumprod(1.0 + period_returns)
    peak   = np.maximum.accumulate(equity)
    dd     = (equity - peak) / (np.abs(peak) + 1e-9)
    return round(float(dd.min()), 4)


def _profit_factor(returns: np.ndarray) -> float:
    gains  = float(returns[returns > 0].sum())
    losses = float(abs(returns[returns < 0].sum()))
    if losses == 0:
        return float("inf")
    return round(gains / losses, 3)


def load_scored_data() -> pd.DataFrame:
    """
    Load labeled rows from DuckDB and attach a proxy composite score.
    In production: join with regime_predictor output for actual composite_score.
    """
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=["forward_return"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)

    # Simple proxy: RSI momentum + low IV rank (lower IV = better selling environment)
    rsi_norm = (pd.to_numeric(df["rsi"], errors="coerce").fillna(50) - 50) / 50
    iv_norm  = 1.0 - pd.to_numeric(df["iv_rank_52w"], errors="coerce").fillna(50) / 100
    df["proxy_score"] = (0.5 * rsi_norm + 0.5 * iv_norm).round(4)
    return df


def run_backtest(
    df: pd.DataFrame = None,
    top_n: int = 10,
    score_col: str = "proxy_score",
    return_col: str = "forward_return",
    date_col: str = "date",
) -> dict:
    """
    Rolling backtest: at each date, take top-N by score_col and track
    equal-weight portfolio return over the 10-day forward horizon.
    """
    if df is None:
        df = load_scored_data()

    dates = sorted(df[date_col].unique())
    if len(dates) < 4:
        return {"ok": False, "error": "Insufficient dates for backtest"}

    period_returns, trades = [], []
    prev_tickers: set = set()
    turnovers = []

    for d in dates:
        day_df = df[df[date_col] == d].copy()
        if len(day_df) < 2:
            continue
        day_df   = day_df.sort_values(score_col, ascending=False)
        selected = day_df.head(top_n)
        actual   = selected[return_col].values.astype(float)
        period_ret = float(actual.mean())
        period_returns.append(period_ret)

        curr_tickers = set(selected["ticker"])
        if prev_tickers:
            replaced = len(curr_tickers - prev_tickers) / max(len(curr_tickers), 1)
            turnovers.append(replaced)
        prev_tickers = curr_tickers

        for _, row in selected.iterrows():
            trades.append({
                "date":   d,
                "ticker": row.get("ticker", "?"),
                "score":  float(row.get(score_col, 0)),
                "return": float(row[return_col]),
                "win":    int(row[return_col] > 0),
            })

    if not period_returns:
        return {"ok": False, "error": "No valid periods"}

    rets      = np.array(period_returns)
    trades_df = pd.DataFrame(trades)

    # Universe benchmark (equal-weight all stocks per date)
    uni_rets = np.array([
        float(df[df[date_col] == d][return_col].mean())
        for d in dates
        if len(df[df[date_col] == d]) >= 1
    ])

    base_win = round(float((df[return_col] > 0).mean()), 4)
    prec_win = round(float(trades_df["win"].mean()), 4) if len(trades_df) else 0.0
    lift     = round(prec_win / base_win, 3) if base_win > 0 else None

    quarterly: dict = {}
    if len(trades_df) and "date" in trades_df.columns:
        trades_df["quarter"] = pd.to_datetime(trades_df["date"]).dt.to_period("Q").astype(str)
        for q, grp in trades_df.groupby("quarter"):
            quarterly[q] = {
                "avg_return": round(float(grp["return"].mean()), 4),
                "win_rate":   round(float(grp["win"].mean()), 3),
                "n_trades":   int(len(grp)),
            }

    n_wins  = int((rets > 0).sum())
    n_total = len(rets)
    cum_ret = float(np.prod(1.0 + rets) - 1.0)

    return {
        "ok":              True,
        "total_return":    round(cum_ret, 4),
        "ann_return":      round(float(rets.mean() * 26), 4),
        "sharpe":          _sharpe(rets),
        "max_drawdown":    _max_drawdown(rets),
        "profit_factor":   _profit_factor(rets),
        "win_rate":        round(n_wins / n_total, 4) if n_total else 0.0,
        "n_periods":       n_total,
        "n_trades":        len(trades),
        "prec_win":        prec_win,
        "lift_vs_uni":     lift,
        "universe_return": round(float(uni_rets.mean() * 26), 4) if len(uni_rets) else 0.0,
        "avg_turnover":    round(float(np.mean(turnovers)), 3) if turnovers else None,
        "top_n":           top_n,
        "score_col":       score_col,
        "quarterly":       quarterly,
        "period_returns":  rets.round(4).tolist(),
    }


# ── Mode B: strategy backtest on real labeled trade outcomes ──────────────────

def load_snapshot_data() -> pd.DataFrame:
    """
    Load labeled training_snapshots with real options P&L outcomes.

    Each row is a candidate trade that was analyzed, labeled after expiry,
    and has:
      outcome.pnl_per_share  — realized P&L from _payoff_per_share()
      outcome.win            — True/False
      signal_score           — analyze.py scoring pipeline score
      ml_composite_score     — regime_predictor composite (None if model not trained)
      candidate              — JSON with structure, credit/debit, width, strikes

    Filters to rows where outcome.win is not None (i.e. properly labeled, not
    "unlabelable" due to missing price data).
    """
    from scripts.db import read_df, SNAPSHOTS_TABLE

    df = read_df(
        f"SELECT * FROM {SNAPSHOTS_TABLE} WHERE labeled = true"
    )
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

    def _parse_structure(raw) -> str | None:
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return None
        return raw.get("structure") if isinstance(raw, dict) else None

    df["structure"] = df["candidate"].apply(_parse_structure)

    df = df.dropna(subset=["pnl_per_share", "win", "date"])
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)

    # Proxy score — same formula as Mode A, for apples-to-apples comparison
    rsi_norm = (pd.to_numeric(df["rsi"], errors="coerce").fillna(50) - 50) / 50
    iv_norm  = 1.0 - pd.to_numeric(df["iv_rank_52w"], errors="coerce").fillna(50) / 100
    df["proxy_score"] = (0.5 * rsi_norm + 0.5 * iv_norm).round(4)

    # Normalise ml_composite_score and signal_score — fill missing with 0
    df["ml_composite_score"] = pd.to_numeric(df.get("ml_composite_score"), errors="coerce").fillna(0)
    df["signal_score"]       = pd.to_numeric(df.get("signal_score"),       errors="coerce").fillna(0)

    return df


def run_strategy_backtest(
    df: pd.DataFrame = None,
    top_n: int = 10,
    score_col: str = "signal_score",
    date_col: str = "date",
) -> dict:
    """
    Mode B backtest: rank candidates by score_col, take top-N per date,
    compute P&L from actual labeled options outcomes.

    Unlike Mode A (which uses stock price forward_return as a proxy), this
    uses the real per-share P&L that label_pending_snapshots() computed by
    running _payoff_per_share() against the actual strike prices at expiry.

    Returns the same metric shape as run_backtest() plus per-structure breakdown.
    """
    if df is None:
        df = load_snapshot_data()

    if df.empty:
        return {"ok": False, "error": "No labeled snapshots found"}

    if score_col not in df.columns:
        return {"ok": False, "error": f"Score column '{score_col}' not in data"}

    dates = sorted(df[date_col].dt.date.unique())
    if len(dates) < 2:
        return {"ok": False, "error": "Insufficient dates for strategy backtest"}

    period_pnl: list[float] = []
    trades: list[dict] = []
    prev_tickers: set = set()
    turnovers: list[float] = []

    for d in dates:
        day_df = df[df[date_col].dt.date == d].copy()
        if len(day_df) < 1:
            continue
        day_df   = day_df.sort_values(score_col, ascending=False)
        selected = day_df.head(top_n)

        pnls       = selected["pnl_per_share"].values.astype(float)
        period_pnl.append(float(pnls.mean()))

        curr_tickers = set(selected["ticker"])
        if prev_tickers:
            replaced = len(curr_tickers - prev_tickers) / max(len(curr_tickers), 1)
            turnovers.append(replaced)
        prev_tickers = curr_tickers

        for _, row in selected.iterrows():
            trades.append({
                "date":      d,
                "ticker":    row.get("ticker", "?"),
                "structure": row.get("structure"),
                "score":     float(row.get(score_col, 0) or 0),
                "pnl":       float(row["pnl_per_share"]),
                "win":       int(bool(row["win"])),
            })

    if not trades:
        return {"ok": False, "error": "No valid periods after filtering"}

    pnl_arr   = np.array(period_pnl)
    trades_df = pd.DataFrame(trades)

    universe_pnl = float(df["pnl_per_share"].mean())
    universe_win = float((df["win"] == True).mean())

    selected_win = float(trades_df["win"].mean())
    lift = round(selected_win / universe_win, 3) if universe_win > 0 else None

    # Per-structure breakdown
    struct_breakdown: dict = {}
    if "structure" in trades_df.columns:
        for s, grp in trades_df.groupby("structure"):
            struct_breakdown[s] = {
                "win_rate":  round(float(grp["win"].mean()), 3),
                "avg_pnl":   round(float(grp["pnl"].mean()), 4),
                "n_trades":  int(len(grp)),
            }

    # Quarterly
    quarterly: dict = {}
    if len(trades_df) and "date" in trades_df.columns:
        trades_df["quarter"] = pd.to_datetime(trades_df["date"]).dt.to_period("Q").astype(str)
        for q, grp in trades_df.groupby("quarter"):
            quarterly[q] = {
                "avg_pnl":  round(float(grp["pnl"].mean()), 4),
                "win_rate": round(float(grp["win"].mean()), 3),
                "n_trades": int(len(grp)),
            }

    n_wins  = int(pnl_arr[pnl_arr > 0].size)
    n_total = len(pnl_arr)

    return {
        "ok":                 True,
        "mode":               "strategy",
        "score_col":          score_col,
        "top_n":              top_n,
        "n_periods":          n_total,
        "n_trades":           len(trades),
        "avg_pnl_per_share":  round(float(pnl_arr.mean()), 4),
        "total_pnl":          round(float(pnl_arr.sum()), 4),
        "win_rate":           round(n_wins / n_total, 4) if n_total else 0.0,
        "sharpe":             _sharpe(pnl_arr, periods_per_year=52.0),
        "max_drawdown":       _max_drawdown(pnl_arr),
        "profit_factor":      _profit_factor(pnl_arr),
        "prec_win":           round(selected_win, 4),
        "lift_vs_uni":        lift,
        "universe_win_rate":  round(universe_win, 4),
        "universe_avg_pnl":   round(universe_pnl, 4),
        "avg_turnover":       round(float(np.mean(turnovers)), 3) if turnovers else None,
        "struct_breakdown":   struct_breakdown,
        "quarterly":          quarterly,
        "period_pnl":         pnl_arr.round(4).tolist(),
    }


def compare_scores(
    top_n: int = 10,
    score_cols: list[str] | None = None,
) -> dict:
    """
    Run run_strategy_backtest() with multiple score columns and return a
    side-by-side comparison.

    Answers: "does signal_score outperform proxy_score? does ml_composite_score
    add lift over signal_score?"

    Returns {"ok": bool, "rows": [{"score_col", "win_rate", "avg_pnl", "lift_vs_uni",
    "sharpe", "n_trades"}], "note": str}
    """
    if score_cols is None:
        score_cols = ["proxy_score", "signal_score", "ml_composite_score"]

    df = load_snapshot_data()
    if df.empty:
        return {"ok": False, "error": "No labeled snapshots found"}

    rows = []
    for col in score_cols:
        if col not in df.columns:
            rows.append({"score_col": col, "error": "column not found"})
            continue
        r = run_strategy_backtest(df=df, top_n=top_n, score_col=col)
        if not r.get("ok"):
            rows.append({"score_col": col, "error": r.get("error")})
        else:
            rows.append({
                "score_col":    col,
                "win_rate":     r["prec_win"],
                "avg_pnl":      r["avg_pnl_per_share"],
                "lift_vs_uni":  r["lift_vs_uni"],
                "sharpe":       r["sharpe"],
                "n_trades":     r["n_trades"],
            })

    # Identify best scoring column by lift
    valid = [r for r in rows if "lift_vs_uni" in r and r.get("lift_vs_uni") is not None]
    best = max(valid, key=lambda r: r["lift_vs_uni"]) if valid else None
    note = f"Best scorer: {best['score_col']} (lift={best['lift_vs_uni']:.2f}x)" if best else "No valid results"

    return {"ok": True, "rows": rows, "note": note}


def _print_strategy_result(result: dict) -> None:
    print(f"\n=== Strategy Backtest (top-{result['top_n']}, score={result['score_col']}) ===")
    print(f"Trades         : {result['n_trades']}  over {result['n_periods']} periods")
    print(f"Win Rate       : {result['win_rate']:.1%}  (universe: {result['universe_win_rate']:.1%})")
    lift_s = f"{result['lift_vs_uni']:.2f}x" if result.get("lift_vs_uni") else "—"
    print(f"Lift vs Uni    : {lift_s}")
    print(f"Avg P&L/share  : ${result['avg_pnl_per_share']:+.3f}  (universe: ${result['universe_avg_pnl']:+.3f})")
    print(f"Total P&L      : ${result['total_pnl']:+.3f}")
    print(f"Sharpe         : {result['sharpe']:.3f}")
    print(f"Max Drawdown   : {result['max_drawdown']:.2%}")
    print(f"Profit Factor  : {result['profit_factor']:.3f}")
    if result.get("struct_breakdown"):
        print("\nBy structure:")
        for s, m in sorted(result["struct_breakdown"].items(), key=lambda x: -x[1]["n_trades"]):
            print(f"  {s:30s}  win={m['win_rate']:.0%}  avg_pnl=${m['avg_pnl']:+.3f}  n={m['n_trades']}")
    if result.get("quarterly"):
        print("\nQuarterly:")
        for q, m in sorted(result["quarterly"].items()):
            print(f"  {q}  avg_pnl=${m['avg_pnl']:+.3f}  win={m['win_rate']:.0%}  n={m['n_trades']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    _mode_snap    = "--snap"    in sys.argv
    _mode_compare = "--compare" in sys.argv

    if _mode_compare:
        cmp = compare_scores()
        if not cmp.get("ok"):
            print("FAILED:", cmp.get("error"))
            sys.exit(1)
        print(f"\n=== Score Column Comparison (training_snapshots) ===")
        print(f"{'Score column':<25}  {'win_rate':>9}  {'avg_pnl':>9}  {'lift':>6}  {'sharpe':>7}  {'n_trades':>8}")
        print("─" * 75)
        for r in cmp["rows"]:
            if "error" in r:
                print(f"  {r['score_col']:<23}  ERROR: {r['error']}")
            else:
                lift_s = f"{r['lift_vs_uni']:.2f}x" if r.get("lift_vs_uni") else "  —  "
                print(f"  {r['score_col']:<23}  {r['win_rate']:>8.1%}  ${r['avg_pnl']:>8.3f}"
                      f"  {lift_s:>6}  {r['sharpe']:>7.3f}  {r['n_trades']:>8}")
        print(f"\n{cmp['note']}")

    elif _mode_snap:
        result = run_strategy_backtest()
        if not result.get("ok"):
            print("FAILED:", result.get("error"))
            sys.exit(1)
        _print_strategy_result(result)

    else:
        result = run_backtest()
        if not result.get("ok"):
            print("FAILED:", result.get("error"))
            sys.exit(1)
        print(f"\n=== Portfolio Backtest (top-{result['top_n']}, score={result['score_col']}) ===")
        print(f"Total Return   : {result['total_return']:+.2%}")
        print(f"Ann. Return    : {result['ann_return']:+.2%}")
        print(f"Sharpe Ratio   : {result['sharpe']:.3f}")
        print(f"Max Drawdown   : {result['max_drawdown']:.2%}")
        print(f"Profit Factor  : {result['profit_factor']:.3f}")
        print(f"Win Rate       : {result['win_rate']:.1%}")
        lift_s = f"{result['lift_vs_uni']:.2f}x" if result.get("lift_vs_uni") else "—"
        print(f"Prec (win)     : {result['prec_win']:.1%}  [lift vs universe: {lift_s}]")
        print(f"Universe Ann.  : {result['universe_return']:+.2%}")
        print(f"Avg Turnover   : {(result.get('avg_turnover') or 0):.1%} per period")
        print(f"N Periods      : {result['n_periods']}  |  N Trades: {result['n_trades']}")
        if result.get("quarterly"):
            print(f"\nQuarterly breakdown:")
            for q, m in sorted(result["quarterly"].items()):
                print(f"  {q}  avg_ret={m['avg_return']:+.2%}  win={m['win_rate']:.0%}  n={m['n_trades']}")
