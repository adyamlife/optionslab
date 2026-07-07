"""Trade Journal — CRUD for data/positions.json and portfolio risk checking."""
import json
import uuid
from datetime import date, timedelta
from pathlib import Path

_PATH = Path(__file__).parent.parent / "data" / "positions.json"


def load():
    if _PATH.exists():
        try:
            with open(_PATH) as f:
                return json.load(f).get("positions", [])
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _save(positions):
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "w") as f:
        json.dump({"positions": positions}, f, indent=2)


def add(ticker, structure, expiry, entry_value, max_profit, max_loss,
        capital_required, is_credit, contracts=1, details="", net_delta=None, net_theta=None):
    """Log a new open trade. entry_value = credit received (credit spreads) or debit paid (debit spreads)."""
    positions = load()
    pos = {
        "id":               str(uuid.uuid4())[:8],
        "ticker":           ticker,
        "structure":        structure,
        "expiry":           expiry,
        "entry_date":       date.today().isoformat(),
        "contracts":        int(contracts),
        "entry_value":      round(float(entry_value), 3),
        "is_credit":        bool(is_credit),
        "max_profit":       max_profit,
        "max_loss":         max_loss,
        "capital_required": capital_required,
        "net_delta":        net_delta,
        "net_theta":        net_theta,
        "details":          details,
        "status":           "open",
        "close_date":       None,
        "close_value":      None,
        "close_pnl":        None,
    }
    positions.append(pos)
    _save(positions)
    return pos


def close_position(position_id, close_value):
    """Mark a position closed. close_value = current spread price.
    P&L for credit spread = (entry_value - close_value) × 100 × contracts.
    P&L for debit spread  = (close_value - entry_value) × 100 × contracts."""
    positions = load()
    for p in positions:
        if p["id"] == position_id and p["status"] == "open":
            p["status"]      = "closed"
            p["close_date"]  = date.today().isoformat()
            p["close_value"] = round(float(close_value), 3)
            ev = p.get("entry_value", 0) or 0
            cv = float(close_value)
            n  = p.get("contracts", 1)
            if p.get("is_credit"):
                p["close_pnl"] = round((ev - cv) * 100 * n, 2)
            else:
                p["close_pnl"] = round((cv - ev) * 100 * n, 2)
            break
    _save(positions)
    return positions


def expire_position(position_id):
    """Mark a position as expired (worthless). Full profit for credit, full loss for debit."""
    positions = load()
    for p in positions:
        if p["id"] == position_id and p["status"] == "open":
            p["status"]     = "closed"
            p["close_date"] = date.today().isoformat()
            p["close_value"] = 0.0
            n = p.get("contracts", 1)
            if p.get("is_credit"):
                p["close_pnl"] = round((p.get("entry_value", 0) or 0) * 100 * n, 2)
            else:
                p["close_pnl"] = -round((p.get("entry_value", 0) or 0) * 100 * n, 2)
            break
    _save(positions)
    return positions


def portfolio_summary(positions):
    from config import rules
    open_pos   = [p for p in positions if p["status"] == "open"]
    closed_pos = [p for p in positions if p["status"] == "closed"]

    capital_deployed = sum(
        (p.get("capital_required") or 0) * p.get("contracts", 1) for p in open_pos
    )
    realized_pnl = sum(p.get("close_pnl") or 0 for p in closed_pos)
    wins = [p for p in closed_pos if (p.get("close_pnl") or 0) > 0]

    return {
        "open_count":         len(open_pos),
        "closed_count":       len(closed_pos),
        "capital_total":      rules.CAPITAL,
        "capital_deployed":   round(capital_deployed, 2),
        "capital_available":  round(rules.CAPITAL - capital_deployed, 2),
        "pct_deployed":       round(capital_deployed / rules.CAPITAL * 100, 1) if rules.CAPITAL else 0,
        "realized_pnl":       round(realized_pnl, 2),
        "win_rate":           round(len(wins) / len(closed_pos) * 100, 1) if closed_pos else None,
    }


def check_risk_limits(positions):
    """Return a list of {level, message} warning dicts for any breached limit."""
    from config import rules
    limits = rules.RISK_LIMITS
    warnings = []
    open_pos = [p for p in positions if p["status"] == "open"]

    # 1. Open position count
    n_open = len(open_pos)
    max_open = limits.get("max_open_positions", 5)
    if n_open >= max_open:
        warnings.append({
            "level": "danger",
            "message": f"Position limit reached: {n_open}/{max_open} positions open. No new trades recommended.",
        })
    elif n_open == max_open - 1:
        warnings.append({
            "level": "warn",
            "message": f"Approaching position limit: {n_open}/{max_open} positions open.",
        })

    # 2. Daily loss
    today = date.today().isoformat()
    today_pnl = sum(p.get("close_pnl") or 0 for p in positions if p.get("close_date") == today)
    max_daily = rules.CAPITAL * limits.get("max_daily_loss_pct", 0.03)
    if today_pnl < 0 and abs(today_pnl) >= max_daily:
        warnings.append({
            "level": "danger",
            "message": f"Daily loss limit hit: -${abs(today_pnl):.2f} today (max ${max_daily:.2f}). Stop new trades today.",
        })

    # 3. Weekly loss
    start_of_week = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    week_pnl = sum(
        p.get("close_pnl") or 0 for p in positions
        if p.get("close_date") and p["close_date"] >= start_of_week
    )
    max_weekly = rules.CAPITAL * limits.get("max_weekly_loss_pct", 0.06)
    if week_pnl < 0 and abs(week_pnl) >= max_weekly:
        warnings.append({
            "level": "danger",
            "message": f"Weekly loss limit hit: -${abs(week_pnl):.2f} this week (max ${max_weekly:.2f}).",
        })

    # 4. Single position size
    max_pos_pct = limits.get("max_position_pct", 0.05)
    for p in open_pos:
        cap = (p.get("capital_required") or 0) * p.get("contracts", 1)
        pct = cap / rules.CAPITAL if rules.CAPITAL else 0
        if pct > max_pos_pct:
            warnings.append({
                "level": "warn",
                "message": (f"{p['ticker']} {p['structure']}: ${cap:.0f} capital "
                             f"({pct*100:.0f}% of account) exceeds {max_pos_pct*100:.0f}% single-position limit."),
            })

    # 5. Total deployment
    total = sum((p.get("capital_required") or 0) * p.get("contracts", 1) for p in open_pos)
    if total > rules.CAPITAL * 0.80:
        warnings.append({
            "level": "warn",
            "message": f"High deployment: ${total:.0f} of ${rules.CAPITAL:.0f} ({total/rules.CAPITAL*100:.0f}%) deployed.",
        })

    return warnings
