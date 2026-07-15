"""
Portfolio Backtest — rolling simulation using ML signals.

Uses the regime_training DuckDB table (with labeled forward_return) to simulate
a rolling portfolio: at each date, rank candidates by score, take top-N,
and track realized PnL over the 10-day forward horizon.

By default uses a proxy score from price-derived features. In production,
replace proxy_score with actual composite_score from regime_predictor.

Metrics:
  total_return, ann_return, Sharpe, max_drawdown, profit_factor,
  win_rate, Prec@K (win fraction of top-N picks vs universe),
  lift vs equal-weight universe, average turnover, quarterly breakdown.

Run standalone: python -m scripts.backtest
"""
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
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
