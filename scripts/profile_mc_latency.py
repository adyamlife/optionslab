"""
Task #1 — MC latency baseline profiling.

Measures:
  1. GARCH vs GBM split rate across the ticker universe
  2. Time per MC call (path generation vs payoff evaluation)
  3. Candidates surviving gates per scan (simulated from analyze_ticker rows)
  4. Rank stability: top-N overlap and EV rank correlation at 2000 vs 5000 sims

Run:
  python -m scripts.profile_mc_latency
"""
import time
import logging
import statistics
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent


# ── 1. GARCH coverage ─────────────────────────────────────────────────────────

def check_garch_coverage():
    garch_dir = ROOT / "data" / "models" / "garch"
    garch_tickers = set()
    if garch_dir.exists():
        garch_tickers = {p.stem for p in garch_dir.glob("*.joblib")}

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    cfg = tomllib.loads((ROOT / "config" / "settings.toml").read_text(encoding="utf-8"))
    universe = cfg.get("tickers", [])

    covered = garch_tickers & set(universe)
    print(f"\n── GARCH Coverage ──────────────────────────────")
    print(f"  Universe tickers : {len(universe)}")
    print(f"  GARCH models     : {len(garch_tickers)}")
    print(f"  Coverage         : {len(covered)}/{len(universe)} "
          f"({len(covered)/max(len(universe),1)*100:.0f}%)")
    print(f"  Engine path      : {'GARCH' if covered else 'GBM fallback (all tickers)'}")
    return universe, garch_tickers


# ── 2. Timing: path generation vs payoff evaluation ───────────────────────────

def time_path_vs_payoff(ticker="SPY", spot=550.0, iv=0.15, dte=21,
                        n_sims_list=(2000, 5000), n_reps=10):
    from scripts.monte_carlo import simulate_paths, _payoff

    print(f"\n── Path Generation vs Payoff Timing ({ticker}, spot={spot}, dte={dte}) ──")

    # Minimal IC candidate for payoff test
    candidate = {
        "structure":        "Iron Condor",
        "put_long_strike":  spot * 0.90,
        "put_short_strike": spot * 0.95,
        "call_short_strike":spot * 1.05,
        "call_long_strike": spot * 1.10,
        "max_profit":       0.50,
    }

    for n_sims in n_sims_list:
        path_times, payoff_times, total_times = [], [], []

        for _ in range(n_reps):
            t0 = time.perf_counter()
            T, mn, mx, src = simulate_paths(ticker, spot, iv, dte, 0.04, n_sims=n_sims)
            t1 = time.perf_counter()
            pnl = _payoff("Iron Condor", candidate, T)
            np.mean(pnl); np.percentile(pnl, 5); np.mean(pnl > 0)
            t2 = time.perf_counter()

            path_times.append((t1 - t0) * 1000)
            payoff_times.append((t2 - t1) * 1000)
            total_times.append((t2 - t0) * 1000)

        print(f"\n  n_sims={n_sims:,}  engine={src}")
        print(f"    Path gen   : {statistics.mean(path_times):.2f} ms  "
              f"(min={min(path_times):.2f}  max={max(path_times):.2f})")
        print(f"    Payoff eval: {statistics.mean(payoff_times):.2f} ms  "
              f"(min={min(payoff_times):.2f}  max={max(payoff_times):.2f})")
        print(f"    Total      : {statistics.mean(total_times):.2f} ms  "
              f"(p95={sorted(total_times)[int(n_reps*0.95)]:.2f})")
        pct = statistics.mean(path_times) / statistics.mean(total_times) * 100
        print(f"    Path gen is {pct:.0f}% of total — "
              f"{'path-generation-bound' if pct > 60 else 'payoff-evaluation-bound'}")


# ── 3. Full run_mc() timing across candidate types ────────────────────────────

def time_full_mc(spot=550.0, n_sims_list=(2000, 5000), n_reps=8):
    from scripts.monte_carlo import run_mc

    structures = {
        "Iron Condor": {
            "structure": "Iron Condor",
            "put_long_strike":  spot * 0.90,
            "put_short_strike": spot * 0.95,
            "call_short_strike":spot * 1.05,
            "call_long_strike": spot * 1.10,
            "max_profit": 0.50,
        },
        "Put Debit Spread": {
            "structure": "Put Debit Spread",
            "short_strike": spot * 0.95,
            "long_strike":  spot * 1.00,
            "max_loss": 0.30,
            "dte": 21,
        },
        "Cash-Secured Put": {
            "structure": "Cash-Secured Put",
            "short_strike": spot * 0.95,
            "max_profit": 0.80,
            "dte": 21,
        },
    }

    row = {"spot": spot, "atm_iv": 0.15, "hv20": 0.14,
           "risk_free_rate": 0.04, "dte": 21}

    print(f"\n── Full run_mc() by Structure ──────────────────────────────")
    for n_sims in n_sims_list:
        print(f"\n  n_sims={n_sims:,}")
        for name, cand in structures.items():
            times = []
            for _ in range(n_reps):
                t0 = time.perf_counter()
                run_mc("SPY", row, cand, n_sims=n_sims)
                times.append((time.perf_counter() - t0) * 1000)
            print(f"    {name:<22}: {statistics.mean(times):.2f} ms avg  "
                  f"(min={min(times):.2f}  max={max(times):.2f})")


# ── 4. Candidates surviving gates per scan ────────────────────────────────────

def estimate_candidates_per_scan():
    """
    Load the most recent paper_trades.json or scan output to count candidates.
    Falls back to reading data/paper_trades.json if available.
    """
    print(f"\n── Candidate Counts (from recent scan data) ────────────────")

    pt = ROOT / "data" / "paper_trades.json"
    if not pt.exists():
        print("  paper_trades.json not found — skipping candidate count")
        return

    import json
    trades = json.loads(pt.read_text())
    # paper_trades.json stores completed/open trades — use as proxy for universe size
    tickers = {t.get("ticker") for t in trades if t.get("ticker")}
    print(f"  Tickers with at least one paper trade : {len(tickers)}")
    structs = {}
    for t in trades:
        s = t.get("structure", "unknown")
        structs[s] = structs.get(s, 0) + 1
    print(f"  Structure breakdown:")
    for s, n in sorted(structs.items(), key=lambda x: -x[1]):
        print(f"    {s:<28}: {n}")


# ── 5. Rank stability: 2000 vs 5000 sims ─────────────────────────────────────

def rank_stability_test(spot=550.0, n_candidates=20, n_trials=30):
    """
    Simulate n_candidates IC candidates with slightly varying strikes/credits.
    Compare EV ranking at 2000 vs 5000 sims across n_trials.
    """
    from scripts.monte_carlo import run_mc

    print(f"\n── Rank Stability: 2,000 vs 5,000 sims ────────────────────")
    print(f"  Candidates: {n_candidates}  Trials: {n_trials}")

    row = {"spot": spot, "atm_iv": 0.15, "hv20": 0.14,
           "risk_free_rate": 0.04, "dte": 21}

    rng = np.random.default_rng(42)
    # Generate diverse IC candidates with random credit/width variation
    candidates = []
    for i in range(n_candidates):
        put_short = spot * (0.93 + rng.uniform(0, 0.04))
        call_short = spot * (1.03 + rng.uniform(0, 0.04))
        width = rng.uniform(3, 8)
        credit = rng.uniform(0.20, 0.80)
        candidates.append({
            "structure": "Iron Condor",
            "put_long_strike":   put_short - width,
            "put_short_strike":  put_short,
            "call_short_strike": call_short,
            "call_long_strike":  call_short + width,
            "max_profit":        credit,
            "dte": 21,
        })

    top5_overlaps, rank_corrs, ev_diffs = [], [], []

    for trial in range(n_trials):
        ev_2k = []
        ev_5k = []
        for c in candidates:
            r2 = run_mc("SPY", row, c, n_sims=2000)
            r5 = run_mc("SPY", row, c, n_sims=5000)
            ev_2k.append(r2["expected_pnl"] if r2 else 0.0)
            ev_5k.append(r5["expected_pnl"] if r5 else 0.0)

        # Rank correlation (Spearman)
        rank_2k = np.argsort(np.argsort(ev_2k))
        rank_5k = np.argsort(np.argsort(ev_5k))
        d2 = (rank_2k - rank_5k) ** 2
        n = n_candidates
        rho = 1 - 6 * d2.sum() / (n * (n**2 - 1))
        rank_corrs.append(rho)

        # Top-5 overlap
        top5_2k = set(np.argsort(ev_2k)[-5:])
        top5_5k = set(np.argsort(ev_5k)[-5:])
        top5_overlaps.append(len(top5_2k & top5_5k) / 5)

        # Mean absolute EV difference
        ev_diffs.append(np.mean(np.abs(np.array(ev_2k) - np.array(ev_5k))))

    print(f"  Spearman rank correlation  : {statistics.mean(rank_corrs):.4f}  "
          f"(min={min(rank_corrs):.4f}  max={max(rank_corrs):.4f})")
    print(f"  Top-5 overlap rate         : {statistics.mean(top5_overlaps):.1%}  "
          f"(min={min(top5_overlaps):.1%}  max={max(top5_overlaps):.1%})")
    print(f"  Mean |EV diff| per candidate: ${statistics.mean(ev_diffs):.4f}")

    stable = statistics.mean(rank_corrs) > 0.95 and statistics.mean(top5_overlaps) > 0.80
    print(f"\n  Verdict: {'2,000 sims is SUFFICIENT for batch ranking' if stable else '2,000 sims shows instability — keep 5,000'}")


# ── 6. Projected batch scan cost ─────────────────────────────────────────────

def project_scan_cost(avg_ms_per_call, n_tickers=83, candidates_per_ticker=3):
    total_calls = n_tickers * candidates_per_ticker
    total_ms = total_calls * avg_ms_per_call
    print(f"\n── Projected Batch Scan Cost ───────────────────────────────")
    print(f"  Tickers          : {n_tickers}")
    print(f"  Candidates/ticker: {candidates_per_ticker} (estimate)")
    print(f"  Total MC calls   : {total_calls}")
    print(f"  Time/call        : {avg_ms_per_call:.1f} ms")
    print(f"  Total MC time    : {total_ms/1000:.1f}s")
    if total_ms < 5000:
        verdict = "acceptable — adds <5s to scan"
    elif total_ms < 15000:
        verdict = "marginal — consider path sharing cache"
    else:
        verdict = "too slow — path sharing cache required"
    print(f"  Verdict          : {verdict}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  MC Latency Baseline Profile")
    print("=" * 60)

    universe, garch_tickers = check_garch_coverage()
    time_path_vs_payoff()
    time_full_mc()
    estimate_candidates_per_scan()
    rank_stability_test()

    # Use 2000-sim IC time as the per-call estimate for projection
    # (collected above; re-run a quick single measurement here)
    from scripts.monte_carlo import run_mc
    row = {"spot": 550.0, "atm_iv": 0.15, "hv20": 0.14, "risk_free_rate": 0.04, "dte": 21}
    c = {"structure": "Iron Condor",
         "put_long_strike": 495, "put_short_strike": 522,
         "call_short_strike": 577, "call_long_strike": 605, "max_profit": 0.50}
    _times = []
    for _ in range(20):
        t0 = time.perf_counter()
        run_mc("SPY", row, c, n_sims=2000)
        _times.append((time.perf_counter() - t0) * 1000)
    project_scan_cost(avg_ms_per_call=statistics.mean(_times))

    print("\n" + "=" * 60)
    print("  Profile complete.")
    print("=" * 60)
