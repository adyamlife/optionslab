"""
scripts/trade_candidate.py
Canonical output object for a fully-priced option trade.

Pipeline:
  1. Strike selection (analyze.py structure blocks) → loose candidate dict
  2. Validation pass  (ValidationResult) — liquidity, credit threshold, profit/loss gates
  3. Risk sizing pass (RiskResult)       — contract count, capital consumed, risk pct

Everything downstream (paper_trade_engine, decision_provider, API responses,
position_file_parser) should consume TradeCandidate rather than loose dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── ValidationResult ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ValidationResult:
    """
    Structured outcome of trade validation checks.

    passes        True when every hard gate clears.  A single hard failure
                  makes the trade un-enterable; warnings are soft flags.
    failures      List of human-readable strings for each hard gate that failed.
    warnings      Soft flags (not blocking, but worth surfacing in the UI).
    is_liquid     Both bid and ask exist on every leg; chain is priceably.
    credit_ok     For credit structures: net premium > 0.
    credit_pct_ok For credit spreads: credit / width >= the configured minimum.
    meets_min_profit  Net premium (or max profit for debit) >= min_profit_amount.
    meets_max_loss    Max loss <= max_risk_per_trade target.
    """
    passes:            bool
    failures:          tuple[str, ...]
    warnings:          tuple[str, ...]
    is_liquid:         bool
    credit_ok:         bool | None   # None for debit structures
    credit_pct_ok:     bool | None   # None for non-spread structures
    meets_min_profit:  bool | None
    meets_max_loss:    bool | None


# ── RiskResult ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskResult:
    """
    Position sizing for one contract of the trade.

    All dollar amounts are per-contract (multiplier already applied, i.e. × 100).

    contracts          Suggested number of contracts given available capital.
                       Always ≥ 1 when a valid trade exists; None when capital
                       is insufficient for even one contract.
    capital_per_contract  Capital consumed by a single contract (dollars).
    capital_type       String from OptionStructure ("debit", "spread_width",
                       "margin", "cash_secured").
    max_dollar_risk    Worst-case loss on the full position (contracts × max_loss × 100).
                       None for structures with undefined max loss (naked puts/calls).
    max_dollar_profit  Best-case gain on the full position.  None for undefined.
    risk_pct_of_capital  max_dollar_risk / total_account_capital, if computable.
    """
    contracts:            int | None
    capital_per_contract: float
    capital_type:         str
    max_dollar_risk:      float | None
    max_dollar_profit:    float | None
    risk_pct_of_capital:  float | None


# ── TradeCandidate ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeCandidate:
    """
    Complete specification of one candidate trade.

    This is the single object that flows through the pipeline after strike
    selection: it carries everything validation and risk sizing need, and
    everything paper_trade_engine / API responses / position_file_parser
    should read from.

    strikes   Flexible dict whose keys match StrikeSchema conventions:
              SINGLE_LEG  → {"short": x}
              TWO_LEG     → {"short": x, "long": y}
              IRON_CONDOR → {"put_short": x, "put_long": y,
                             "call_short": z, "call_long": w}
              NONE        → {} (Calendar / Diagonal — no fixed-expiry legs)
    """
    # ── Identity ──────────────────────────────────────────────────────────────
    ticker:         str
    structure:      str
    expiry:         str
    dte:            int
    spot_at_entry:  float
    recommended:    bool

    # ── Contract economics ────────────────────────────────────────────────────
    strikes:        dict[str, float]
    is_credit:      bool
    net_premium:    float          # > 0 credit received; < 0 debit paid; per share
    max_profit:     float | None   # per share at expiry; None = undefined (e.g. naked)
    max_loss:       float | None   # per share at expiry; None = undefined
    pop:            float | None   # probability of profit [0–100]
    ev:             float | None   # expected value per share at expiry

    # ── Greeks (net position, per share) ─────────────────────────────────────
    net_delta:      float | None
    net_theta:      float | None
    net_gamma:      float | None
    net_vega:       float | None

    # ── Pipeline outputs ──────────────────────────────────────────────────────
    validation:     ValidationResult
    risk:           RiskResult | None   # None when validation fails hard

    # ── Explainability ────────────────────────────────────────────────────────
    details:        str    # human-readable trade description (from structure block)
    rejection_reason: str  # empty string when passes=True

    @classmethod
    def from_candidate_dict(
        cls,
        d: dict[str, Any],
        *,
        ticker: str,
        expiry: str,
        dte: int,
        total_capital: float,
        credit_min_pct_of_width: float,
        min_profit_amount: float,
        width_target: float,
    ) -> "TradeCandidate":
        """
        Construct a TradeCandidate from an existing loose candidate dict.

        The loose dict is what the structure blocks in analyze.py currently
        produce.  This method extracts, validates, and sizes it into the typed
        canonical form without requiring any change to the structure blocks.

        Parameters
        ----------
        d                       The candidate dict from analyze.py
        ticker                  Ticker symbol
        expiry                  Expiry date string (ISO format)
        dte                     Days to expiry
        total_capital           Total account capital for risk sizing
        credit_min_pct_of_width Threshold for credit / width gate (fraction)
        min_profit_amount       Minimum net premium or max profit per contract ($)
        width_target            Maximum desired loss per contract ($)
        """
        structure  = d.get("structure", "")
        is_credit  = bool(d.get("is_credit", False))
        max_profit = d.get("max_profit")   # per share
        max_loss   = d.get("max_loss")     # per share; None = undefined
        net_prem   = max_profit if is_credit else -(d.get("max_loss") or 0)  # rough: credit = max_profit
        spot       = float(d.get("spot_at_entry") or 0.0)

        # Strikes
        strikes = _extract_strikes(d)

        # ── Validation ────────────────────────────────────────────────────────
        failures: list[str] = []
        warnings: list[str] = []

        # Hard: no usable price data at all (structure block returned None max_profit
        # and a details string describing why — e.g. illiquid, no strikes found)
        is_liquid = max_profit is not None
        if not is_liquid:
            failures.append(f"No valid price: {d.get('details', 'unknown reason')}")

        # Hard: credit structures must return a positive net premium
        credit_ok: bool | None = None
        if is_liquid and is_credit:
            credit_ok = (max_profit is not None and max_profit > 0)
            if not credit_ok:
                failures.append("Net credit ≤ 0 — trade not enterable as a credit")

        # Soft: credit spreads — credit/width threshold (informational, not blocking)
        credit_pct_ok: bool | None = None
        if is_liquid and is_credit and max_loss is not None and max_loss > 0:
            width_est = (max_profit or 0) + max_loss      # credit + max_loss = width
            pct       = (max_profit or 0) / width_est if width_est > 0 else 0.0
            credit_pct_ok = pct >= credit_min_pct_of_width
            if not credit_pct_ok:
                warnings.append(
                    f"Credit/width {pct*100:.0f}% below preferred {credit_min_pct_of_width*100:.0f}%"
                )

        # Soft: minimum profit amount
        profit_check_val = max_profit if is_credit else (max_profit or 0)
        meets_min_profit: bool | None = None
        if is_liquid and profit_check_val is not None:
            meets_min_profit = profit_check_val >= min_profit_amount
            if not meets_min_profit:
                warnings.append(
                    f"Max profit ${profit_check_val:.2f}/shr < min ${min_profit_amount:.2f}"
                )

        # Soft: max loss fits within risk budget
        meets_max_loss: bool | None = None
        if is_liquid and max_loss is not None:
            meets_max_loss = max_loss <= width_target
            if not meets_max_loss:
                warnings.append(
                    f"Max loss ${max_loss:.2f}/shr exceeds risk target ${width_target:.2f}"
                )

        val = ValidationResult(
            passes           = len(failures) == 0,
            failures         = tuple(failures),
            warnings         = tuple(warnings),
            is_liquid        = is_liquid,
            credit_ok        = credit_ok,
            credit_pct_ok    = credit_pct_ok,
            meets_min_profit = meets_min_profit,
            meets_max_loss   = meets_max_loss,
        )

        # ── Risk sizing ───────────────────────────────────────────────────────
        risk: RiskResult | None = None
        if val.passes and is_liquid:
            risk = _size_trade(
                d              = d,
                is_credit      = is_credit,
                max_profit     = max_profit,
                max_loss       = max_loss,
                spot           = spot,
                total_capital  = total_capital,
                structure_name = structure,
            )

        # ── Assemble ──────────────────────────────────────────────────────────
        rejection = "; ".join(failures) if failures else ""
        return cls(
            ticker        = ticker,
            structure     = structure,
            expiry        = expiry,
            dte           = dte,
            spot_at_entry = spot,
            recommended   = bool(d.get("recommended", False)),
            strikes       = strikes,
            is_credit     = is_credit,
            net_premium   = net_prem,
            max_profit    = max_profit,
            max_loss      = max_loss,
            pop           = d.get("pop"),
            ev            = d.get("ev"),
            net_delta     = d.get("net_delta"),
            net_theta     = d.get("net_theta"),
            net_gamma     = d.get("net_gamma"),
            net_vega      = d.get("net_vega"),
            validation    = val,
            risk          = risk,
            details       = d.get("details", ""),
            rejection_reason = rejection,
        )


# ── Private helpers ───────────────────────────────────────────────────────────

def _extract_strikes(d: dict[str, Any]) -> dict[str, float]:
    """Pull strike fields out of a loose candidate dict into a canonical dict."""
    result: dict[str, float] = {}
    # Iron Condor 4-leg schema
    for k in ("put_short_strike", "put_long_strike", "call_short_strike", "call_long_strike"):
        if d.get(k) is not None:
            result[k.replace("_strike", "")] = float(d[k])
    # Two-leg spread schema
    if not result:
        for k in ("short_strike", "long_strike"):
            if d.get(k) is not None:
                result[k.replace("_strike", "")] = float(d[k])
    # Single-leg fallback
    if not result and d.get("short_strike") is not None:
        result["short"] = float(d["short_strike"])
    return result


def _size_trade(
    d: dict[str, Any],
    *,
    is_credit: bool,
    max_profit: float | None,
    max_loss: float | None,
    spot: float,
    total_capital: float,
    structure_name: str,
) -> RiskResult:
    """
    Compute contract count and capital metrics for one candidate.

    capital_type is looked up from the structure registry so sizing logic
    matches the OptionStructure definition exactly.
    """
    try:
        from config.structures import STRUCTURES
        st = STRUCTURES.get(structure_name)
        capital_type = st.capital_type if st else ("spread_width" if is_credit else "debit")
    except Exception:
        capital_type = "spread_width" if is_credit else "debit"

    # Capital consumed per contract (dollars)
    if capital_type == "debit":
        cap_per = abs(max_profit or 0) * 100 if not is_credit else (abs(max_loss or 0) * 100)
        # for debit trades max_loss is the debit paid (stored as positive)
        cap_per = abs(max_loss or max_profit or 0) * 100
    elif capital_type == "spread_width":
        # Buying power reduction = width - credit = max_loss
        cap_per = abs(max_loss or 0) * 100
    elif capital_type == "cash_secured":
        short_k = d.get("short_strike") or spot
        cap_per = float(short_k) * 100
    elif capital_type == "margin":
        # Use pre-computed capital_required when present (Jade Lizard, Risk Reversal,
        # Naked Put, Bear Combo all compute this inline).
        cap_per = float(d.get("capital_required") or 0)
        if cap_per == 0 and spot > 0:
            # Fallback Reg-T estimate: 20% of notional
            cap_per = 0.20 * spot * 100
    else:
        cap_per = abs(max_loss or 0) * 100

    cap_per = max(cap_per, 0.01)   # guard against zero-division

    contracts = int(total_capital // cap_per) if total_capital > 0 else 0
    contracts = max(contracts, 0)

    max_dr = (contracts * abs(max_loss) * 100) if max_loss is not None else None
    max_dp = (contracts * abs(max_profit) * 100) if max_profit is not None else None
    risk_pct = (max_dr / total_capital) if (max_dr is not None and total_capital > 0) else None

    return RiskResult(
        contracts            = contracts if contracts > 0 else None,
        capital_per_contract = round(cap_per, 2),
        capital_type         = capital_type,
        max_dollar_risk      = round(max_dr, 2) if max_dr is not None else None,
        max_dollar_profit    = round(max_dp, 2) if max_dp is not None else None,
        risk_pct_of_capital  = round(risk_pct, 4) if risk_pct is not None else None,
    )
