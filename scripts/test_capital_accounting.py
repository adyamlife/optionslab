"""
One-off diagnostic for the 2026-08-24 capital-accounting fix in
check_circuit_breakers(). Not part of the pytest suite — run standalone:

    python -m scripts.test_capital_accounting

Verifies, in order:
  1. $1,500 capital, no positions -> $1,500 buying power.
  2. Add a position requiring $200 (capital_required, already per-contract)
     -> $1,300 buying power. Confirms NO extra x100 is applied.
  3. Add another requiring $500 -> $800 buying power.
  4. A capital_required=200 position contributes exactly $200, not $20,000
     (the pre-fix double-x100 bug).
  5. Disabling circuit_breakers_enabled does NOT restore buying_power to
     the full $1,500 — it must still reflect open positions.
  6. An over-committed book (open_capital > capital_amount) produces a
     NEGATIVE buying_power, and that negative value is preserved (not
     clamped to 0), so downstream capital gates correctly reject every
     new candidate rather than silently reopening capacity.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.paper_trade_engine import check_circuit_breakers, _open_capital

FAILURES = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        FAILURES.append(label)


def _settings(capital_amount=1500.0, enabled=False):
    return {
        "capital_amount": capital_amount,
        "circuit_breakers_enabled": enabled,
        "max_daily_loss_pct": 0.03,
        "max_weekly_loss_pct": 0.06,
    }


def main():
    print("1) $1,500 capital, no open positions")
    with patch("scripts.paper_trade_engine._load_settings", return_value=_settings()):
        r = check_circuit_breakers([])
    check("buying_power == 1500.00", r["buying_power"] == 1500.00)

    print("\n2) One open position, capital_required=200 (already per-contract)")
    trades = [{"status": "open", "capital_required": 200.0}]
    with patch("scripts.paper_trade_engine._load_settings", return_value=_settings()):
        r = check_circuit_breakers(trades)
    check("open_capital contributed exactly $200 (not $20,000)", _open_capital(trades) == 200.0)
    check("buying_power == 1300.00", r["buying_power"] == 1300.00)

    print("\n3) Second open position, capital_required=500")
    trades.append({"status": "open", "capital_required": 500.0})
    with patch("scripts.paper_trade_engine._load_settings", return_value=_settings()):
        r = check_circuit_breakers(trades)
    check("buying_power == 800.00", r["buying_power"] == 800.00)

    print("\n4) max_loss fallback (per-share) still gets the x100 conversion")
    trades_ml = [{"status": "open", "max_loss": 2.00}]  # no capital_required
    check("open_capital from max_loss=2.00 -> $200.00", _open_capital(trades_ml) == 200.0)

    print("\n5) Disabling circuit breakers must NOT restore buying_power to $1,500")
    with patch("scripts.paper_trade_engine._load_settings", return_value=_settings(enabled=False)):
        r_disabled = check_circuit_breakers(trades)
    check("enabled == False", r_disabled["enabled"] is False)
    check("buying_power still reflects open positions (800.00, not 1500.00)",
          r_disabled["buying_power"] == 800.00)

    print("\n6) Over-committed book -> negative buying_power, not clamped to 0")
    overcommitted = [{"status": "open", "capital_required": 2000.0}]
    with patch("scripts.paper_trade_engine._load_settings", return_value=_settings()):
        r_over = check_circuit_breakers(overcommitted)
    check("buying_power == -500.00 (2000 committed - 1500 capital)", r_over["buying_power"] == -500.00)
    check("buying_power is negative, not clamped to 0", r_over["buying_power"] < 0)
    # Downstream gate simulation: `required > buying_power` must reject any positive requirement.
    _cap_needed = 50.0
    check("downstream gate correctly rejects ($50 needed > -$500 available)",
          _cap_needed > r_over["buying_power"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
