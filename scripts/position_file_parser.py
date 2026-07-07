"""Parse and analyse E*TRADE option position export files (CSV or TSV)."""
import re
import csv
import io
from datetime import date, timedelta
from pathlib import Path

MONTH_ABBREV = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
}

UPLOAD_PATH = Path(__file__).parent.parent / "data" / "uploaded_positions.csv"
LIVE_POS_DIR = Path(__file__).parent.parent / "data" / "live_position"

# ── Summary / junk rows to skip ───────────────────────────────────────────────

_SKIP_PATTERNS = re.compile(
    r'portfolio analysis|margin debit|total|cash|money market',
    re.I
)

def _is_junk(s):
    return bool(_SKIP_PATTERNS.search(s)) or s.strip().startswith('"')


# ── Date helpers ──────────────────────────────────────────────────────────────

def _third_friday(year, month):
    """3rd Friday of the given month — standard monthly option expiry."""
    d = date(year, month, 1)
    days_to_fri = (4 - d.weekday()) % 7
    return d + timedelta(days=days_to_fri + 14)


def _parse_expiry(s):
    """
    Handles two formats:
      'Jul-02-26'  → '2026-07-02'   (full date)
      'Jun26'      → 3rd Friday of June 2026  (month + 2-digit year)
      'Jan27'      → 3rd Friday of Jan  2027
    """
    s = s.strip()
    full = re.match(r'(\w{3})-(\d{1,2})-(\d{2,4})$', s)
    if full:
        mon = MONTH_ABBREV.get(full.group(1), 0)
        day = int(full.group(2))
        yr  = int(full.group(3))
        if yr < 100:
            yr += 2000
        if mon:
            return f"{yr:04d}-{mon:02d}-{day:02d}"

    monthly = re.match(r'(\w{3})(\d{2})$', s)
    if monthly:
        mon = MONTH_ABBREV.get(monthly.group(1), 0)
        yr  = 2000 + int(monthly.group(2))
        if mon:
            return _third_friday(yr, mon).isoformat()

    return s          # return as-is if nothing matched


def _parse_expiry_token(rest):
    """
    Try to consume an expiry token at the start of `rest`.
    Returns (expiry_iso, remaining) or (None, rest).
    Handles 'Jul-02-26 ' and 'Jun26 ' and 'Jan28 '.
    """
    # Full: Jul-02-26
    m = re.match(r'(\w{3}-\d{1,2}-\d{2,4})\s+', rest)
    if m:
        return _parse_expiry(m.group(1)), rest[m.end():]
    # Month+year: Jun26 / Jan27 / Jan28
    m = re.match(r'(\w{3}\d{2})\s+', rest)
    if m and MONTH_ABBREV.get(m.group(1)[:3]):
        return _parse_expiry(m.group(1)), rest[m.end():]
    return None, rest


# ── Position description parser ───────────────────────────────────────────────

def _parse_desc(pos):
    """
    Parses a position description string into a structured dict.
    Returns a dict with 'kind' key:
      'group'        – header row, no quantity
      'spread'       – two-legged vertical spread
      'iron_condor'  – four-legged iron condor
      'covered_call' – covered call spread row
      'single_leg'   – individual option leg
      'unknown'      – has qty but couldn't classify further
    """
    indent = len(pos) - len(pos.lstrip())
    s = pos.strip()

    if not s or _is_junk(s):
        return {'kind': 'skip', 'indent': indent}

    # No quantity prefix → group / category header
    qty_m = re.match(r'^([+-]?\d+)\s+', s)
    if not qty_m:
        return {'kind': 'group', 'name': s, 'indent': indent}

    qty  = int(qty_m.group(1))
    rest = s[qty_m.end():]

    expiry, rest = _parse_expiry_token(rest)
    if expiry is None:
        return {'kind': 'unknown', 'raw': s, 'indent': indent}

    base = {'qty': qty, 'expiry': expiry, 'indent': indent, 'raw': s}

    # ── Iron Condor: "460/462.5/497.5/500 Iron Condor"
    ic = re.match(
        r'\$?(\d+\.?\d*)/\$?(\d+\.?\d*)/\$?(\d+\.?\d*)/\$?(\d+\.?\d*)\s+Iron\s+Condor',
        rest, re.I
    )
    if ic:
        strikes = sorted(float(ic.group(i)) for i in range(1, 5))
        return {**base, 'kind': 'iron_condor',
                'put_long': strikes[0], 'put_short': strikes[1],
                'call_short': strikes[2], 'call_long': strikes[3],
                'option_type': 'Iron Condor'}

    # ── Vertical Spread: "10.5/13.5 Call Vertical"
    vs = re.match(
        r'\$?(\d+\.?\d*)/\$?(\d+\.?\d*)\s+(Call|Put)\s+Vertical',
        rest, re.I
    )
    if vs:
        return {**base, 'kind': 'spread',
                'strike_lo': float(vs.group(1)), 'strike_hi': float(vs.group(2)),
                'option_type': vs.group(3).capitalize()}

    # ── Covered Call: "65 Covered Call" or "$65 Covered Call"
    cc = re.match(r'\$?(\d+\.?\d*)\s+Covered\s+Call', rest, re.I)
    if cc:
        return {**base, 'kind': 'covered_call',
                'strike': float(cc.group(1)), 'option_type': 'Call'}

    # ── Single Leg: "$10.5 Call" or "10.5 Call" or "$10.5 Put"
    leg = re.match(r'\$?(\d+\.?\d*)\s+(Call|Put)', rest, re.I)
    if leg:
        return {**base, 'kind': 'single_leg',
                'strike': float(leg.group(1)),
                'option_type': leg.group(2).capitalize()}

    return {**base, 'kind': 'unknown'}


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _num(v):
    if v is None or str(v).strip() in ('', '--', '-'):
        return None
    try:
        return float(str(v).replace(',', '').strip())
    except ValueError:
        return None


def _col(row, *names):
    """Case/space-insensitive column lookup, first match wins."""
    for name in names:
        nl = name.lower()
        for k, v in row.items():
            if k.strip().lower() == nl:
                return v
    return None


def _extract_ticker(name):
    """'SPOT Iron Condor' → 'SPOT', 'KEEL multiple' → 'KEEL'."""
    parts = name.strip().split()
    return parts[0].upper() if parts else None


# ── File parser ───────────────────────────────────────────────────────────────

def parse_upload(file_path):
    """
    Read an E*TRADE position export and return a list of position groups.
    Each group: {name, ticker, spreads: [{...parsed rows + legs list}]}
    """
    path = Path(file_path)
    raw  = path.read_text(encoding='utf-8-sig', errors='replace')
    delim = '\t' if raw.count('\t') > raw.count(',') else ','

    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    rows   = [{k.strip(): (v.strip() if isinstance(v, str) else v)
               for k, v in r.items()} for r in reader]

    if not rows:
        return []

    pos_col = next((k for k in rows[0] if 'position' in k.lower()), None)
    if not pos_col:
        return []

    groups     = []
    cur_group  = None
    cur_spread = None

    for row in rows:
        pos_str = (row.get(pos_col) or '').strip()
        if not pos_str or _is_junk(pos_str):
            continue

        p = _parse_desc(pos_str)
        if p['kind'] == 'skip':
            continue

        ul_price = _num(_col(row, 'U/L Price', 'UL Price'))
        ul_chg   = _num(_col(row, 'U/L Change', 'U/L Chang', 'UL Change'))
        mark     = _num(_col(row, 'Mark'))
        mark_chg = _num(_col(row, 'Mark Chg', 'Mark Change'))
        cost_val = _num(_col(row, 'Cost Value'))
        mkt_val  = _num(_col(row, 'Market Value'))

        if p['kind'] == 'group':
            cur_group = {
                'name':    p['name'],
                'ticker':  _extract_ticker(p['name']),
                'spreads': [],
                # store group-row financials so we can fall back if needed
                '_group_ul': ul_price,
                '_group_cost': cost_val,
                '_group_mkt':  mkt_val,
            }
            groups.append(cur_group)
            cur_spread = None

        elif p['kind'] in ('spread', 'iron_condor', 'covered_call', 'single_leg', 'unknown'):
            if cur_group is None:
                cur_group = {'name': 'Unknown', 'ticker': None, 'spreads': [],
                             '_group_ul': None, '_group_cost': None, '_group_mkt': None}
                groups.append(cur_group)
            cur_spread = {
                **p,
                'ul_price':     ul_price,
                'ul_change':    ul_chg,
                'cost_value':   cost_val,
                'market_value': mkt_val,
                'mark':         mark,
                'mark_chg':     mark_chg,
                'legs':         [],
            }
            cur_group['spreads'].append(cur_spread)

        elif p['kind'] == 'single_leg' or p['kind'] == 'unknown':
            # Handled above — shouldn't reach here
            pass

        # Leg rows — may be children of an identified spread OR orphaned
        if p['kind'] == 'single_leg' and p.get('indent', 0) > 0:
            # This is an indented leg — add to current spread
            if cur_spread is not None and p.get('indent', 0) > cur_spread.get('indent', -1):
                cur_spread['legs'].append({
                    **p,
                    'ul_price':     ul_price,
                    'mark':         mark,
                    'mark_chg':     mark_chg,
                    'cost_value':   cost_val,
                    'market_value': mkt_val,
                })
                continue   # already handled above as a leg, don't fall through

    # Second pass: for any group that has NO spreads but was followed by legs,
    # they would have been caught as 'single_leg' spreads above.
    # Also: for groups where the group row itself has financial data but
    # no child spread rows (e.g. KEEL), the legs become standalone positions.

    return groups


# ── Re-parse with proper orphan-leg handling ──────────────────────────────────

def parse_upload(file_path):       # noqa: F811  (redefine with fixed logic)
    """
    Read an E*TRADE position export and return a list of position groups.
    """
    path = Path(file_path)
    raw  = path.read_text(encoding='utf-8-sig', errors='replace')
    delim = '\t' if raw.count('\t') > raw.count(',') else ','

    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    rows   = []
    for r in reader:
        d = {}
        for k, v in r.items():
            ks = k.strip() if k else ''
            # Preserve whitespace in Position column so we can detect indentation
            if ks.lower() == 'position':
                d[ks] = v          # keep raw (with leading spaces)
            else:
                d[ks] = v.strip() if isinstance(v, str) else v
        rows.append(d)

    if not rows:
        return []

    pos_col = next((k for k in rows[0] if k.lower() == 'position'), None)
    if not pos_col:
        return []

    groups     = []
    cur_group  = None
    cur_spread = None          # most recent spread/condor/coveredcall row

    for row in rows:
        pos_str = row.get(pos_col) or ''
        if not pos_str.strip() or _is_junk(pos_str):
            continue

        p = _parse_desc(pos_str)
        if p['kind'] == 'skip':
            continue

        ul_price = _num(_col(row, 'U/L Price', 'UL Price'))
        ul_chg   = _num(_col(row, 'U/L Change', 'U/L Chang', 'UL Change'))
        mark     = _num(_col(row, 'Mark'))
        mark_chg = _num(_col(row, 'Mark Chg', 'Mark Change'))
        cost_val = _num(_col(row, 'Cost Value'))
        mkt_val  = _num(_col(row, 'Market Value'))

        def _financial():
            return {'ul_price': ul_price, 'ul_change': ul_chg,
                    'mark': mark, 'mark_chg': mark_chg,
                    'cost_value': cost_val, 'market_value': mkt_val}

        # ── GROUP header ──────────────────────────────────────────────────────
        if p['kind'] == 'group':
            cur_group = {
                'name':    p['name'],
                'ticker':  _extract_ticker(p['name']),
                'spreads': [],
            }
            groups.append(cur_group)
            cur_spread = None
            continue

        # ── Need a group to attach to ─────────────────────────────────────────
        if cur_group is None:
            cur_group = {'name': 'Unknown', 'ticker': None, 'spreads': []}
            groups.append(cur_group)

        # ── Named multi-leg structures ─────────────────────────────────────────
        if p['kind'] in ('spread', 'iron_condor', 'covered_call'):
            cur_spread = {**p, **_financial(), 'legs': []}
            cur_group['spreads'].append(cur_spread)
            continue

        # ── Single leg or unknown ─────────────────────────────────────────────
        if p['kind'] in ('single_leg', 'unknown'):
            indent = p.get('indent', 0)

            # If indented AND a parent spread exists → it's a child leg
            if indent > 0 and cur_spread is not None:
                cur_spread['legs'].append({**p, **_financial()})
            else:
                # Orphan: treat as its own single-option position
                cur_spread = {**p, **_financial(), 'legs': []}
                cur_group['spreads'].append(cur_spread)

    return groups


# ── Analysis ──────────────────────────────────────────────────────────────────

def _dte(expiry):
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry) - date.today()).days
    except Exception:
        return None


def analyze_spread(spread):
    """
    Compute risk, P&L, POP and breakeven for any parsed spread/position row.
    Returns a JSON-serialisable dict.
    """
    kind      = spread.get('kind', 'unknown')
    cost_val  = spread.get('cost_value')
    mkt_val   = spread.get('market_value')
    ul_price  = spread.get('ul_price')
    qty_raw   = spread.get('qty', 1)
    qty       = abs(qty_raw or 1)
    expiry    = spread.get('expiry')
    opt_type  = spread.get('option_type', '')

    dte_days       = _dte(expiry)
    unrealized_pnl = round(mkt_val - cost_val, 2) if (cost_val is not None and mkt_val is not None) else None

    # --- Initialise outputs ---
    structure = max_profit_ps = max_loss_ps = cost_ps = None
    is_credit = False
    width = pop_est = breakeven = move_to_be = move_to_be_pct = None
    upper_be = lower_be = None          # for Iron Condor
    extra_notes = []

    # ── Vertical spread (Put or Call) ─────────────────────────────────────────
    if kind == 'spread':
        strike_lo = spread.get('strike_lo')
        strike_hi = spread.get('strike_hi')
        if strike_lo is not None and strike_hi is not None and cost_val is not None:
            width    = round(strike_hi - strike_lo, 4)
            cost_ps  = round(cost_val / (100 * qty), 4)
            is_credit = cost_ps < 0

            if opt_type == 'Call':
                if not is_credit:
                    structure     = 'Call Debit Spread'
                    max_profit_ps = round(width - cost_ps, 4)
                    max_loss_ps   = round(cost_ps, 4)
                    breakeven     = round(strike_lo + cost_ps, 4)
                else:
                    structure     = 'Call Credit Spread'
                    cr            = -cost_ps
                    max_profit_ps = round(cr, 4)
                    max_loss_ps   = round(width - cr, 4)
                    breakeven     = round(strike_lo + cr, 4)
            else:
                if not is_credit:
                    structure     = 'Put Debit Spread'
                    max_profit_ps = round(width - cost_ps, 4)
                    max_loss_ps   = round(cost_ps, 4)
                    breakeven     = round(strike_hi - cost_ps, 4)
                else:
                    structure     = 'Put Credit Spread'
                    cr            = -cost_ps
                    max_profit_ps = round(cr, 4)
                    max_loss_ps   = round(width - cr, 4)
                    breakeven     = round(strike_hi - cr, 4)

            if ul_price and breakeven:
                move_to_be     = round(breakeven - ul_price, 4)
                move_to_be_pct = round(move_to_be / ul_price * 100, 2)

    # ── Iron Condor ───────────────────────────────────────────────────────────
    elif kind == 'iron_condor':
        put_lo  = spread.get('put_long')
        put_hi  = spread.get('put_short')
        call_lo = spread.get('call_short')
        call_hi = spread.get('call_long')

        if all(x is not None for x in [put_lo, put_hi, call_lo, call_hi]) and cost_val is not None:
            put_width  = round(put_hi  - put_lo,  4)
            call_width = round(call_hi - call_lo, 4)
            max_width  = max(put_width, call_width)
            cost_ps    = round(cost_val / (100 * qty), 4)
            is_credit  = cost_ps < 0
            structure  = 'Iron Condor'

            if is_credit:
                cr            = -cost_ps
                max_profit_ps = round(cr, 4)
                max_loss_ps   = round(max_width - cr, 4)
                upper_be      = round(call_lo + cr, 4)
                lower_be      = round(put_hi  - cr, 4)
                width         = max_width
                extra_notes.append(
                    f"Profit zone: ${lower_be} – ${upper_be} | "
                    f"Put spread ${put_lo}/${put_hi} · Call spread ${call_lo}/${call_hi}"
                )
            else:
                max_profit_ps = round(-cost_ps, 4)     # net debit (long condor)
                max_loss_ps   = round(cost_ps, 4)
                width         = max_width

        if ul_price and upper_be and lower_be:
            if ul_price < lower_be:
                move_to_be     = round(lower_be - ul_price, 4)
                move_to_be_pct = round(move_to_be / ul_price * 100, 2)
            elif ul_price > upper_be:
                move_to_be     = round(ul_price - upper_be, 4)
                move_to_be_pct = round(-move_to_be / ul_price * 100, 2)
            else:
                move_to_be     = 0
                move_to_be_pct = 0

    # ── Covered Call ──────────────────────────────────────────────────────────
    elif kind == 'covered_call':
        strike = spread.get('strike')
        structure = 'Covered Call'
        is_credit = True      # selling a call = credit
        if cost_val is not None:
            cost_ps   = round(cost_val / (100 * qty), 4)
            # Premium received = abs(cost_ps) for short call
            max_profit_ps = round(-cost_ps, 4) if cost_ps < 0 else None
            max_loss_ps   = None    # loss on stock side is separate
        if strike and ul_price:
            breakeven = round(ul_price - (-cost_ps if cost_ps and cost_ps < 0 else 0), 4)
        extra_notes.append(
            "Covered Call: max profit capped at strike. Stock P&L not included here."
        )

    # ── Single Leg ────────────────────────────────────────────────────────────
    elif kind == 'single_leg':
        strike = spread.get('strike')
        structure = f"Long {opt_type}" if (qty_raw or 0) > 0 else f"Short {opt_type}"
        if cost_val is not None:
            cost_ps     = round(cost_val / (100 * qty), 4)
            is_credit   = cost_ps < 0
            max_loss_ps = round(abs(cost_ps), 4) if not is_credit else None
            # max profit = unlimited for long calls / long puts to $0
            if opt_type == 'Call' and not is_credit:
                extra_notes.append("Long Call: max profit unlimited (stock can rise indefinitely).")
            elif opt_type == 'Put' and not is_credit:
                extra_notes.append(f"Long Put: max profit = strike − premium = ${round(strike - abs(cost_ps), 2) if strike else '?'}.")

        if ul_price and strike:
            if opt_type == 'Call':
                breakeven      = round(strike + abs(cost_ps or 0), 4)
                move_to_be     = round(breakeven - ul_price, 4)
            else:
                breakeven      = round(strike - abs(cost_ps or 0), 4)
                move_to_be     = round(ul_price - breakeven, 4)
            if ul_price:
                move_to_be_pct = round(move_to_be / ul_price * 100, 2)

    # ── POP estimate (market-implied) ─────────────────────────────────────────
    cur_val_ps = None
    if mkt_val is not None:
        cur_val_ps = round(mkt_val / (100 * qty), 4)

    if cur_val_ps is not None and width and width > 0:
        if kind == 'iron_condor' and is_credit:
            # How much of the max-loss zone is reflected in current price
            pop_est = round(min(99, max(1, (1 - abs(cur_val_ps) / width) * 100)), 1)
        elif not is_credit:          # debit spreads / long options
            pop_est = round(min(99, max(1, abs(cur_val_ps) / width * 100)), 1)
        else:                        # credit spreads
            pop_est = round(min(99, max(1, (1 - abs(cur_val_ps) / width) * 100)), 1)

    # ── P&L as % of max ──────────────────────────────────────────────────────
    pnl_pct = None
    if unrealized_pnl is not None and max_profit_ps:
        max_total = max_profit_ps * 100 * qty
        if max_total:
            pnl_pct = round(unrealized_pnl / max_total * 100, 1)

    # ── Hedge candidate ───────────────────────────────────────────────────────
    hedge_candidate = {
        'structure': structure if kind in ('spread', 'iron_condor') else None,
        'max_profit': max_profit_ps,
        'max_loss':   max_loss_ps,
        'net_delta':  None,
    }

    return {
        'desc':             spread.get('raw', ''),
        'kind':             kind,
        'structure':        structure,
        'opt_type':         opt_type,
        'qty':              qty_raw,
        'expiry':           expiry,
        'dte':              dte_days,
        'ul_price':         ul_price,
        'ul_change':        spread.get('ul_change'),
        'strike':           spread.get('strike'),
        'strike_lo':        spread.get('strike_lo'),
        'strike_hi':        spread.get('strike_hi'),
        'put_long':         spread.get('put_long'),
        'put_short':        spread.get('put_short'),
        'call_short':       spread.get('call_short'),
        'call_long':        spread.get('call_long'),
        'width':            width,
        'cost_value':       cost_val,
        'market_value':     mkt_val,
        'mark':             spread.get('mark'),
        'mark_chg':         spread.get('mark_chg'),
        'cost_per_share':   cost_ps,
        'cur_val_per_share': cur_val_ps,
        'max_profit_ps':    max_profit_ps,
        'max_loss_ps':      max_loss_ps,
        'unrealized_pnl':   unrealized_pnl,
        'pnl_pct':          pnl_pct,
        'pop_est':          pop_est,
        'breakeven':        breakeven,
        'upper_be':         upper_be,
        'lower_be':         lower_be,
        'move_to_be':       move_to_be,
        'move_to_be_pct':   move_to_be_pct,
        'is_credit':        is_credit,
        'extra_notes':      extra_notes,
        'legs':             spread.get('legs', []),
        'date_acquired':    spread.get('date_acquired'),
        '_hedge_candidate': hedge_candidate,
    }


def analyze_groups(groups):
    result = []
    for g in groups:
        analyses = [analyze_spread(s) for s in g.get('spreads', [])]
        result.append({
            'name':    g['name'],
            'ticker':  g['ticker'],
            'spreads': analyses,
        })
    return result


# ── E*TRADE API positions → groups converter ──────────────────────────────────

def _make_leg(p: dict) -> dict:
    qty = p["qty"]
    return {
        "raw":          p["symbol"],
        "qty":          qty,
        "mark":         p["last_price"],
        "mark_chg":     p["change"],
        "cost_value":   round(p["cost_per_share"] * 100 * abs(qty), 2),
        "market_value": round(p["market_value"], 2),
    }


def positions_to_groups(positions: list[dict]) -> list[dict]:
    """
    Convert the list returned by etrade_client.get_positions() into the same
    groups → spreads structure that parse_upload() + analyze_groups() produce.

    Pairing logic:
      • Options with the same underlying + expiry + call/put type are matched
        into vertical spreads (one long leg + one short leg).
      • Unmatched single legs are kept as-is.
      • Iron Condors: a put spread + call spread on the same underlying/expiry
        are merged into one Iron Condor entry.
      • Equity positions (security_type != "OPTN") are kept as single-leg groups.

    NOTE: ul_price / ul_change are NOT fetched here — caller must inject them
    by calling inject_underlying_prices(groups) before analyze_groups().
    """
    from collections import defaultdict

    # Separate equities from options
    opts = [p for p in positions if p.get("security_type") == "OPTN"]
    eqs  = [p for p in positions if p.get("security_type") != "OPTN"]

    # key: (underlying, expiry, call_put)
    buckets: dict = defaultdict(list)
    for p in opts:
        key = (p["underlying"], p["expiry"], (p["call_put"] or "").upper())
        buckets[key].append(p)

    groups: list[dict] = []
    used: set = set()

    # ── Build spread candidates ───────────────────────────────────────────────
    spread_map: dict = {}   # (underlying, expiry) → {put: spread_dict, call: spread_dict}

    for (underlying, expiry, cp), legs in buckets.items():
        if len(legs) < 2:
            continue  # single leg — handled below
        longs  = sorted([l for l in legs if l["qty"] > 0], key=lambda x: x["strike"])
        shorts = sorted([l for l in legs if l["qty"] < 0], key=lambda x: x["strike"])
        if not longs or not shorts:
            continue

        key_ue = (underlying, expiry)
        if key_ue not in spread_map:
            spread_map[key_ue] = {}
        if cp not in spread_map[key_ue]:
            spread_map[key_ue][cp] = []

        for long_leg, short_leg in zip(longs, shorts):
            strike_lo = min(long_leg["strike"], short_leg["strike"])
            strike_hi = max(long_leg["strike"], short_leg["strike"])
            qty       = min(abs(long_leg["qty"]), abs(short_leg["qty"]))

            # Net mark and mark_chg for the spread (credit spread: short - long)
            mark_net  = round(short_leg["last_price"] - long_leg["last_price"], 4)
            chg_net   = round(short_leg["change"]     - long_leg["change"],     4)

            mkt_val  = long_leg["market_value"] + short_leg["market_value"]
            cost_val = (long_leg["cost_per_share"] - short_leg["cost_per_share"]) * 100 * qty

            desc = f"{underlying} {expiry} {strike_lo}/{strike_hi} {cp.title()} Spread"

            spread_dict = {
                "raw":          desc,   # analyze_spread() reads 'raw' for desc field
                "desc":         desc,
                "ticker":       underlying,
                "expiry":       expiry,
                "option_type":  cp.title(),
                "kind":         "spread",
                "strike_lo":    strike_lo,
                "strike_hi":    strike_hi,
                "qty":          qty,
                "mark":         mark_net,
                "mark_chg":     chg_net,
                "cost_value":   round(cost_val, 2),
                "market_value": round(mkt_val, 2),
                "ul_price":     None,   # filled by inject_underlying_prices()
                "ul_change":    None,
                "legs":         [_make_leg(long_leg), _make_leg(short_leg)],
                "date_acquired": long_leg.get("date_acquired"),
            }
            used.add(id(long_leg))
            used.add(id(short_leg))
            spread_map[key_ue][cp].append(spread_dict)

    # ── Merge PUT + CALL spreads → Iron Condor ────────────────────────────────
    ic_keys: set = set()
    for (underlying, expiry), sides in spread_map.items():
        if "PUT" in sides and "CALL" in sides:
            put_sp  = sides["PUT"][0]
            call_sp = sides["CALL"][0]
            mkt_val  = put_sp["market_value"] + call_sp["market_value"]
            cost_val = put_sp["cost_value"]   + call_sp["cost_value"]
            desc = (f"{underlying} {expiry} "
                    f"{put_sp['strike_lo']}/{put_sp['strike_hi']}P "
                    f"{call_sp['strike_lo']}/{call_sp['strike_hi']}C IC")
            ic_spread = {
                "raw":          desc,
                "desc":         desc,
                "ticker":       underlying,
                "expiry":       expiry,
                "option_type":  "IC",
                "kind":         "iron_condor",
                "strike_lo":    put_sp["strike_lo"],
                "strike_hi":    call_sp["strike_hi"],
                "put_long":     put_sp["strike_lo"],
                "put_short":    put_sp["strike_hi"],
                "call_short":   call_sp["strike_lo"],
                "call_long":    call_sp["strike_hi"],
                "qty":          put_sp["qty"],
                "mark":         round(put_sp["mark"] + call_sp["mark"], 4),
                "mark_chg":     round((put_sp["mark_chg"] or 0) + (call_sp["mark_chg"] or 0), 4),
                "cost_value":   round(cost_val, 2),
                "market_value": round(mkt_val, 2),
                "ul_price":     None,
                "ul_change":    None,
                "legs":         put_sp["legs"] + call_sp["legs"],
                "date_acquired": put_sp.get("date_acquired"),
            }
            groups.append({"name": underlying, "ticker": underlying, "spreads": [ic_spread]})
            ic_keys.add((underlying, expiry))
            # Any extra spreads beyond the first IC pair are added as standalone spreads
            for extra in sides["PUT"][1:] + sides["CALL"][1:]:
                existing = next((g for g in groups if g["ticker"] == underlying), None)
                if existing:
                    existing["spreads"].append(extra)
                else:
                    groups.append({"name": underlying, "ticker": underlying, "spreads": [extra]})

    # ── Add remaining single-side spreads ─────────────────────────────────────
    for (underlying, expiry), sides in spread_map.items():
        if (underlying, expiry) in ic_keys:
            continue
        for cp, sp_list in sides.items():
            for sp in sp_list:
                existing = next((g for g in groups if g["ticker"] == underlying), None)
                if existing:
                    existing["spreads"].append(sp)
                else:
                    groups.append({"name": underlying, "ticker": underlying, "spreads": [sp]})

    # ── Single legs (unmatched options) ─────────────────────────────────────
    for p in opts:
        if id(p) in used:
            continue
        cp   = (p.get("call_put") or "").title()
        desc = p["symbol"]
        leg  = {
            "raw":          desc,
            "desc":         desc,
            "ticker":       p["underlying"],
            "expiry":       p["expiry"],
            "option_type":  cp,
            "kind":         "single_leg",
            "strike":       p.get("strike"),
            "qty":          p["qty"],
            "mark":         p["last_price"],
            "mark_chg":     p["change"],
            "cost_value":   round(p["cost_per_share"] * 100 * abs(p["qty"]), 2),
            "market_value": round(p["market_value"], 2),
            "ul_price":     None,
            "ul_change":    None,
            "legs":         [_make_leg(p)],
            "date_acquired": p.get("date_acquired"),
        }
        existing = next((g for g in groups if g["ticker"] == p["underlying"]), None)
        if existing:
            existing["spreads"].append(leg)
        else:
            groups.append({"name": p["underlying"], "ticker": p["underlying"], "spreads": [leg]})

    for p in eqs:
        name = p["underlying"] or p["symbol"]
        desc = p["symbol"]
        leg  = {
            "raw":          desc,
            "desc":         desc,
            "ticker":       name,
            "kind":         "single_leg",
            "option_type":  "",
            "qty":          p["qty"],
            "mark":         p["last_price"] or None,
            "mark_chg":     p["change"] or None,
            "cost_value":   round(p["cost_per_share"] * abs(p["qty"]), 2),
            "market_value": round(p["market_value"], 2),
            "ul_price":     p["last_price"] or None,
            "ul_change":    p["change"] or None,
            "legs":         [],
        }
        groups.append({"name": name, "ticker": name, "spreads": [leg]})

    return groups


def inject_underlying_prices(groups: list[dict]) -> None:
    """
    Fetch live underlying prices for all tickers in the groups list and
    inject ul_price / ul_change into every spread in-place.
    Uses E*TRADE batch quotes when authenticated, falls back to yfinance.
    """
    tickers = list({g["ticker"] for g in groups if g.get("ticker")})
    if not tickers:
        return

    quotes: dict = {}

    # Try E*TRADE batch first
    try:
        from scripts import etrade_client as et
        if et.is_authenticated():
            q = et.get_quotes(tickers)
            if q:
                quotes = q
    except Exception:
        pass

    # yfinance fallback for any ticker not covered
    yf_needed = [t for t in tickers if t not in quotes]
    if yf_needed:
        try:
            import yfinance as yf
            raw = yf.download(yf_needed, period="2d", interval="1d",
                              auto_adjust=True, progress=False)
            close = raw["Close"]
            for t in yf_needed:
                try:
                    last = float(close[t].dropna().iloc[-1])
                    prev = float(close[t].dropna().iloc[-2]) if len(close[t].dropna()) > 1 else None
                    chg  = round(last - prev, 4) if prev else None
                    quotes[t] = {"last": last, "change_pct": round(chg / prev * 100, 2) if prev else None,
                                 "change": chg}
                except Exception:
                    pass
        except Exception:
            pass

    # Inject into every spread
    for g in groups:
        ticker = g.get("ticker", "")
        q = quotes.get(ticker, {})
        ul_price  = q.get("last")
        ul_change = q.get("change")
        for sp in g.get("spreads", []):
            if sp.get("ul_price") is None and ul_price:
                sp["ul_price"]  = round(ul_price, 4)
            if sp.get("ul_change") is None and ul_change is not None:
                sp["ul_change"] = round(ul_change, 4)


# ── Exact hedge via live options chain ────────────────────────────────────────

def _round_to_strike_inc(price):
    """Round to the nearest typical option strike increment for the given price."""
    inc = _guess_strike_increment(price)
    return round(round(price / inc) * inc, 2)


def _guess_strike_increment(price):
    if price >= 500: return 5.0
    if price >= 200: return 2.5
    if price >= 50:  return 1.0
    if price >= 10:  return 0.5
    return 0.5


def _closest_expiry(available, target_iso, max_days=21):
    """Find the available expiry date closest to target_iso (within max_days)."""
    try:
        td = date.fromisoformat(target_iso)
    except Exception:
        return available[0] if available else None
    best, best_diff = None, None
    for exp in available:
        try:
            ed   = date.fromisoformat(exp)
            diff = abs((ed - td).days)
            if best_diff is None or diff < best_diff:
                best_diff, best = diff, exp
        except Exception:
            pass
    return best if (best_diff is not None and best_diff <= max_days) else None


def _best_option(chain_df, target_strike, opt_type, ticker, expiry):
    """Return a dict with the option closest to target_strike."""
    if chain_df is None or chain_df.empty:
        return None
    df = chain_df.copy()
    df['_dist'] = (df['strike'] - target_strike).abs()
    row  = df.sort_values('_dist').iloc[0]
    bid  = float(row.get('bid')  or 0)
    ask  = float(row.get('ask')  or 0)
    last = float(row.get('lastPrice') or 0)
    mark = round((bid + ask) / 2, 4) if (bid + ask) > 0 else last
    return {
        'option_type': opt_type.capitalize(),
        'strike':  float(row['strike']),
        'mark':    mark,
        'bid':     bid,
        'ask':     ask,
        'iv':      round(float(row.get('impliedVolatility') or 0) * 100, 1),
        'volume':  int(row.get('volume')       or 0),
        'oi':      int(row.get('openInterest') or 0),
        'ticker':  ticker,
        'expiry':  expiry,
    }


def get_hedge_exact(spread_analysis, ticker):
    """
    Fetch live options chain via yfinance and return exact hedge trade details.
    Returns a dict with trade specifics, or {'error': '...'} on failure.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {'error': 'yfinance not available'}

    structure = spread_analysis.get('structure')
    expiry    = spread_analysis.get('expiry')
    qty_raw   = spread_analysis.get('qty', -1)
    contracts = abs(qty_raw or 1)
    ul_price  = spread_analysis.get('ul_price')

    if not structure or not ticker or not expiry:
        return None

    # Support both naming styles (file-parser vs analyze.py candidate)
    strike_lo = spread_analysis.get('strike_lo') or spread_analysis.get('long_strike')
    strike_hi = spread_analysis.get('strike_hi') or spread_analysis.get('short_strike')
    ul_price  = ul_price or spread_analysis.get('spot_at_entry')

    hedge_type  = None      # 'put', 'call', or 'both'
    target_put  = None
    target_call = None
    rationale   = None
    hedge_side  = 'buy'     # all hedges are buys unless noted

    # ── Registry-driven hedge strike computation ──────────────────────────────
    from config.structures import get_or_none
    from config.structures._base import HedgeStrikeMode

    st = get_or_none(structure)
    if st is not None:
        mode = st.hedge.strike_mode
        hedge_type = st.hedge.opt_type

        if mode == HedgeStrikeMode.ONE_WIDTH_BELOW_LO:
            if strike_lo is None: return None
            width      = (strike_hi - strike_lo) if strike_hi else 1.0
            target_put = strike_lo - width
            rationale  = (f"Buy put ${target_put:.2f} — 1 spread-width below your long put "
                          f"(${strike_lo}). Activates only if stock gaps below both spread legs.")

        elif mode == HedgeStrikeMode.ONE_WIDTH_ABOVE_HI:
            if strike_hi is None: return None
            width       = (strike_hi - strike_lo) if strike_lo else 1.0
            target_call = strike_hi + width
            rationale   = (f"Buy call ${target_call:.2f} — 1 spread-width above your long call "
                           f"(${strike_hi}). Activates only if stock gaps above both spread legs.")

        elif mode == HedgeStrikeMode.ONE_WIDTH_BOTH:
            put_lo  = spread_analysis.get('put_long')   or spread_analysis.get('put_long_strike')
            put_hi  = spread_analysis.get('put_short')  or spread_analysis.get('put_short_strike')
            call_lo = spread_analysis.get('call_short') or spread_analysis.get('call_short_strike')
            call_hi = spread_analysis.get('call_long')  or spread_analysis.get('call_long_strike')
            if any(x is None for x in [put_lo, put_hi, call_lo, call_hi]): return None
            target_put  = put_lo  - (put_hi  - put_lo)
            target_call = call_hi + (call_hi - call_lo)
            rationale   = (f"Buy put ${target_put:.2f} + call ${target_call:.2f} — "
                           f"wider wings beyond existing outer strikes (${put_lo} / ${call_hi}).")

        elif mode == HedgeStrikeMode.ATM_PUT:
            if ul_price is None: return None
            target_put = _round_to_strike_inc(ul_price)
            rationale  = (f"Buy ATM put ~${target_put:.2f} — offsets debit loss if the "
                          f"bullish spread reverses (stock at ${ul_price}).")

        elif mode == HedgeStrikeMode.ATM_CALL:
            if ul_price is None: return None
            target_call = _round_to_strike_inc(ul_price)
            rationale   = (f"Buy ATM call ~${target_call:.2f} — offsets debit loss if the "
                           f"bearish spread reverses (stock at ${ul_price}).")

        elif mode == HedgeStrikeMode.OTM_PUT_NEAR_SHORT:
            # Jade Lizard — buy put just below the naked short put strike
            put_strike = spread_analysis.get('strike') or (ul_price * 0.97 if ul_price else None)
            if put_strike is None: return None
            inc        = _guess_strike_increment(put_strike)
            target_put = put_strike - inc
            rationale  = (f"Buy put ${target_put:.2f} below short put — converts Jade Lizard "
                          f"to defined-risk (Iron Condor-like) position.")

        elif mode == HedgeStrikeMode.OTM_STRANGLE:
            if ul_price is None: return None
            target_put  = _round_to_strike_inc(ul_price * 0.95)
            target_call = _round_to_strike_inc(ul_price * 1.05)
            rationale   = (f"Buy ${target_put:.2f} put + ${target_call:.2f} call strangle — "
                           f"protects against large moves in either direction.")

        else:
            return None  # unknown mode

    # ── Single-leg / live-position structures (not in registry) ──────────────
    elif structure == 'Covered Call':
        # Covered call risk = stock declining. Hedge = protective put.
        if ul_price is None:
            return None
        hedge_type = 'put'
        target_put = _round_to_strike_inc(ul_price * 0.95)   # ~5% OTM put
        rationale  = (f"Buy protective put ~${target_put:.2f} (~5% OTM, stock at ${ul_price:.2f}) "
                      f"— limits downside if the stock falls while the covered call caps your upside.")

    elif structure in ('Long Call',):
        # Sell a higher-strike call to turn into a spread (reduce cost basis to zero)
        strike = spread_analysis.get('strike')
        max_loss = spread_analysis.get('max_loss_ps') or spread_analysis.get('max_loss', 0)
        if strike is None:
            return None
        # Target: sell at breakeven = strike + premium paid → free trade
        breakeven = strike + (max_loss or 0)
        target_call = _round_to_strike_inc(breakeven)
        if target_call <= strike:
            target_call = strike + _guess_strike_increment(strike)
        hedge_type  = 'call'
        hedge_side  = 'sell'
        rationale   = (f"Sell call ~${target_call:.2f} (near your breakeven ${breakeven:.2f}) — "
                       f"converts Long Call into a bull call spread; reduces cost to near zero "
                       f"while capping max profit at ${target_call - strike:.2f}/share.")

    elif structure in ('Long Put',):
        strike = spread_analysis.get('strike')
        max_loss = spread_analysis.get('max_loss_ps') or spread_analysis.get('max_loss', 0)
        if strike is None:
            return None
        breakeven = strike - (max_loss or 0)
        target_put = _round_to_strike_inc(breakeven)
        if target_put >= strike:
            target_put = strike - _guess_strike_increment(strike)
        hedge_type  = 'put'
        hedge_side  = 'sell'
        rationale   = (f"Sell put ~${target_put:.2f} (near your breakeven ${breakeven:.2f}) — "
                       f"converts Long Put into a bear put spread; reduces cost to near zero "
                       f"while capping max profit at ${strike - target_put:.2f}/share.")

    elif structure in ('Short Call',):
        # Naked short call — buy a higher call to define risk
        strike = spread_analysis.get('strike')
        if strike is None:
            return None
        inc = _guess_strike_increment(strike)
        target_call = strike + inc
        hedge_type  = 'call'
        rationale   = (f"Buy call ${target_call:.2f} — defines your max loss on the naked short call "
                       f"(${strike:.2f}). Converts to a call credit spread, capping unlimited upside risk.")

    elif structure in ('Short Put',):
        # Naked short put — buy a lower put to define risk
        strike = spread_analysis.get('strike')
        if strike is None:
            return None
        inc = _guess_strike_increment(strike)
        target_put  = strike - inc
        hedge_type  = 'put'
        rationale   = (f"Buy put ${target_put:.2f} — defines your max loss on the naked short put "
                       f"(${strike:.2f}). Converts to a put credit spread, eliminating unlimited downside.")

    else:
        return None

    try:
        tkr   = yf.Ticker(ticker)
        avail = list(tkr.options or [])
        if not avail:
            return {'error': f'No options data for {ticker}'}

        matched = _closest_expiry(avail, expiry)
        if matched is None:
            return {'error': f'No expiry near {expiry} for {ticker} (available: {avail[:3]})'}

        chain = tkr.option_chain(matched)

        action_verb = 'Sell' if hedge_side == 'sell' else 'Buy'

        if hedge_type == 'both':
            put_leg  = _best_option(chain.puts,  target_put,  'put',  ticker, matched)
            call_leg = _best_option(chain.calls, target_call, 'call', ticker, matched)
            if not put_leg or not call_leg:
                return {'error': 'Could not find hedge strikes in chain'}
            cost_ps = round(put_leg['mark'] + call_leg['mark'], 4)
            total   = round(cost_ps * 100 * contracts, 2)
            return {
                'type':          'two_leg',
                'ticker':        ticker,
                'expiry_used':   matched,
                'legs':          [put_leg, call_leg],
                'contracts':     contracts,
                'hedge_side':    hedge_side,
                'cost_per_share': cost_ps,
                'total_cost':    total,
                'rationale':     rationale,
                'trade_summary': (
                    f"{action_verb} {contracts}x {ticker} {matched} ${put_leg['strike']:.2f} Put "
                    f"@ ${put_leg['mark']:.3f}  +  {action_verb} {contracts}x {ticker} {matched} "
                    f"${call_leg['strike']:.2f} Call @ ${call_leg['mark']:.3f}  "
                    f"= ${total:.2f} total"
                ),
            }
        else:
            target  = target_put if hedge_type == 'put' else target_call
            opt_df  = chain.puts if hedge_type == 'put' else chain.calls
            leg     = _best_option(opt_df, target, hedge_type, ticker, matched)
            if not leg:
                return {'error': f'No {hedge_type} near ${target:.2f} in {ticker} chain'}
            total = round(leg['mark'] * 100 * contracts, 2)
            return {
                'type':          'single_leg',
                'ticker':        ticker,
                'expiry_used':   matched,
                'option_type':   leg['option_type'],
                'strike':        leg['strike'],
                'mark':          leg['mark'],
                'bid':           leg['bid'],
                'ask':           leg['ask'],
                'iv':            leg['iv'],
                'volume':        leg['volume'],
                'oi':            leg['oi'],
                'contracts':     contracts,
                'hedge_side':    hedge_side,
                'cost_per_share': leg['mark'],
                'total_cost':    total,
                'rationale':     rationale,
                'trade_summary': (
                    f"{action_verb} {contracts}x {ticker} {matched} ${leg['strike']:.2f} "
                    f"{leg['option_type']} @ ${leg['mark']:.3f} "
                    f"(bid ${leg['bid']:.3f} / ask ${leg['ask']:.3f}) "
                    f"= ${total:.2f} total"
                ),
            }
    except Exception as e:
        return {'error': str(e)}
