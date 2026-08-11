"""
Win rate by structure — diagnostic before adding structure as a feature.

Run:  python -m scripts.structure_win_rates
"""
from __future__ import annotations
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.db import read_df, SNAPSHOTS_TABLE


def run():
    df = read_df(
        f"""
        SELECT
            JSON_EXTRACT_STRING(candidate, '$.structure') AS structure,
            CAST(JSON_EXTRACT(outcome, '$.win') AS BOOLEAN) AS win,
            LEFT(CAST(collected_at AS VARCHAR), 10)        AS date
        FROM {SNAPSHOTS_TABLE}
        WHERE labeled = true
          AND JSON_EXTRACT(outcome, '$.win') IS NOT NULL
        """
    )

    df = df.dropna(subset=["win"])
    df["win"] = df["win"].astype(bool)

    agg = (
        df.groupby("structure")["win"]
        .agg(rows="count", wins="sum")
        .assign(win_pct=lambda x: x["wins"] / x["rows"] * 100)
        .sort_values("rows", ascending=False)
        .reset_index()
    )

    overall_win_pct = df["win"].mean() * 100

    print(f"\n{'='*58}")
    print(f"  {'Structure':<25} {'Rows':>6}  {'Win%':>6}  {'vs avg':>8}")
    print(f"{'='*58}")
    for _, r in agg.iterrows():
        delta = r["win_pct"] - overall_win_pct
        flag = "  ←" if abs(delta) > 8 else ""
        print(f"  {r['structure']:<25} {int(r['rows']):>6}  {r['win_pct']:>5.1f}%  {delta:>+7.1f}%{flag}")
    print(f"{'─'*58}")
    print(f"  {'OVERALL':<25} {len(df):>6}  {overall_win_pct:>5.1f}%")
    print(f"{'='*58}")
    print(f"\nBase-rate spread: {agg['win_pct'].max():.1f}% – {agg['win_pct'].min():.1f}%"
          f" = {agg['win_pct'].max() - agg['win_pct'].min():.1f}pp")
    print("If spread > 10pp: structure is a meaningful categorical feature.")
    print("If spread < 5pp:  structure adds little beyond what market signals capture.")


if __name__ == "__main__":
    run()
