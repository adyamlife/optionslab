"""
Phase 2 backfill — recover geometry from candidate.details text.

Pass 1 (--dry-run, default):
  Samples 3 distinct detail strings per structure and prints the parsed
  result alongside the raw text.  Lets you verify the regex before writing.

Pass 2 (--write):
  Parses all labeled rows where spread_width IS NULL, writes geometry
  columns + trade_structure, sets feature_schema_version = 2 for rows
  where geometry was successfully recovered.

Columns written:
  trade_structure    VARCHAR  promoted from candidate.$.structure
  short_strike       DOUBLE   sell-side / lower strike
  long_strike        DOUBLE   buy-side / upper strike (or call strike for strangle)
  spread_width       DOUBLE   |short_strike - long_strike|  (0 for calendars)
  short_strike_pct   DOUBLE   short_strike / spot  (requires spot != NULL)
  long_strike_pct    DOUBLE   long_strike  / spot

Run:
  python -m scripts.backfill_geometry           # dry-run  (safe)
  python -m scripts.backfill_geometry --write   # commit changes
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.db import read_df, connect, ensure_snapshot_tables, SNAPSHOTS_TABLE


# ── Strike parser ─────────────────────────────────────────────────────────────

def _strip_prefix(details: str) -> str:
    """Remove leading tag like '[Best EV] ' or '[High POP] '."""
    return re.sub(r"^\[.*?\]\s*", "", details.strip())


def _parse_leg(leg: str) -> tuple[str, float, str] | None:
    """
    Parse one option leg string.

    Handles:
      "SELL 722.0P"                        — standard
      "Bearish diagonal: BUY 63.0P (…)"   — prefix text (re.search, not re.match)
      "SELL 1× 19C" / "BUY 2x 20C"        — ratio-backspread multiplier prefix
    Returns (action, strike, option_type) or None.
    """
    m = re.search(
        r"\b(BUY|SELL)\s+(?:\d+\s*[×x]\s*)?([\d.]+)\s*([PCpc])\b",
        leg.strip(),
    )
    if not m:
        return None
    return m.group(1), float(m.group(2)), m.group(3).upper()


def parse_details(structure: str, details: str, spot: float | None) -> dict:
    """
    Extract geometry from a candidate.details string.

    Returns a dict with any subset of:
      short_strike, long_strike, spread_width,
      short_strike_pct, long_strike_pct
    Returns {} if the details string cannot be parsed.
    """
    if not details:
        return {}

    raw = _strip_prefix(details)

    # Split into individual legs on '+' or '/'
    # Iron Condor:       "SELL 722.0P/BUY 721.0P + SELL 739.0C/BUY 741.0C"
    # Two-leg spread:    "SELL 450.0P/BUY 448.0P"  or  "BUY 155.0C/SELL 160.0C"
    # Strangle/Straddle: "BUY 450.0P + BUY 460.0C"
    leg_strings = [s.strip() for s in re.split(r"[/+]", raw) if s.strip()]
    legs = [_parse_leg(l) for l in leg_strings]
    legs = [l for l in legs if l is not None]

    if not legs:
        return {}

    result: dict = {}

    if structure in ("Put Credit Spread", "Call Credit Spread",
                     "Put Debit Spread", "Call Debit Spread"):
        # Two legs: one SELL, one BUY
        sells = [l for l in legs if l[0] == "SELL"]
        buys  = [l for l in legs if l[0] == "BUY"]
        if sells and buys:
            short = sells[0][1]
            long_ = buys[0][1]
            result["short_strike"] = short
            result["long_strike"]  = long_
            result["spread_width"] = round(abs(short - long_), 4)

    elif structure in ("Iron Condor", "Iron Butterfly"):
        # SELL Xp / BUY Yp + SELL Ac / BUY Bc
        # Iron Butterfly is the same wire format — both short strikes equal ATM.
        puts  = [(a, s) for a, s, t in legs if t == "P"]
        calls = [(a, s) for a, s, t in legs if t == "C"]
        put_sell  = next((s for a, s in puts  if a == "SELL"), None)
        put_buy   = next((s for a, s in puts  if a == "BUY"),  None)
        call_sell = next((s for a, s in calls if a == "SELL"), None)
        call_buy  = next((s for a, s in calls if a == "BUY"),  None)
        if all(v is not None for v in (put_sell, put_buy, call_sell, call_buy)):
            put_width  = abs(put_sell  - put_buy)
            call_width = abs(call_sell - call_buy)
            result["short_strike"] = put_sell   # put short (lower body)
            result["long_strike"]  = call_sell  # call short (upper body)
            result["spread_width"] = round((put_width + call_width) / 2, 4)

    elif structure in ("Long Strangle", "Long Straddle"):
        # BUY Xp + BUY Yc  (or same strike for straddle)
        put_leg  = next(((a, s) for a, s, t in legs if t == "P"), None)
        call_leg = next(((a, s) for a, s, t in legs if t == "C"), None)
        if put_leg and call_leg:
            put_strike  = put_leg[1]
            call_strike = call_leg[1]
            result["short_strike"] = put_strike   # lower strike
            result["long_strike"]  = call_strike  # upper strike
            result["spread_width"] = round(abs(call_strike - put_strike), 4)

    elif structure == "Calendar Spread":
        # Same strike, different expirations.  Spread width = 0 (not strike-based).
        # Extract the common strike if present.
        strikes = [s for _, s, _ in legs]
        if strikes:
            common = strikes[0]
            result["short_strike"] = common
            result["long_strike"]  = common
            result["spread_width"] = 0.0

    elif structure in ("Short Straddle", "Short Strangle"):
        sells = [l for l in legs if l[0] == "SELL"]
        if len(sells) >= 2:
            put_sell  = next((s for _, s, t in sells if t == "P"), None)
            call_sell = next((s for _, s, t in sells if t == "C"), None)
            if put_sell and call_sell:
                result["short_strike"] = put_sell
                result["long_strike"]  = call_sell
                result["spread_width"] = round(abs(call_sell - put_sell), 4)
        elif len(sells) == 1:
            result["short_strike"] = sells[0][1]
            result["long_strike"]  = sells[0][1]
            result["spread_width"] = 0.0

    elif structure in ("Diagonal Spread",
                       "Ratio Call Backspread", "Ratio Put Backspread"):
        # Diagonal: different expirations, possibly different strikes.
        # Ratio backspreads: SELL 1× X / BUY 2× Y — multiplier already stripped.
        # In both cases: one clear sell-side strike, one buy-side strike.
        sells = [l for l in legs if l[0] == "SELL"]
        buys  = [l for l in legs if l[0] == "BUY"]
        if sells and buys:
            short = sells[0][1]
            long_ = buys[0][1]
            result["short_strike"] = short
            result["long_strike"]  = long_
            result["spread_width"] = round(abs(short - long_), 4)

    elif structure == "Risk Reversal":
        # SELL XP (naked) + BUY YC — naked short put, long call
        sells = [l for l in legs if l[0] == "SELL"]
        buys  = [l for l in legs if l[0] == "BUY"]
        if sells and buys:
            result["short_strike"] = sells[0][1]
            result["long_strike"]  = buys[0][1]
            result["spread_width"] = round(abs(sells[0][1] - buys[0][1]), 4)

    elif structure in ("Covered Call", "Cash Secured Put",
                       "Financed Long Call", "Financed Long Put"):
        # Single short/long leg relevant to geometry
        sells = [l for l in legs if l[0] == "SELL"]
        buys  = [l for l in legs if l[0] == "BUY"]
        if sells:
            result["short_strike"] = sells[0][1]
        if buys:
            result["long_strike"] = buys[0][1]
        if "short_strike" in result and "long_strike" in result:
            result["spread_width"] = round(
                abs(result["short_strike"] - result["long_strike"]), 4)

    # Compute pct columns when spot is available
    if spot and spot > 0:
        if "short_strike" in result:
            result["short_strike_pct"] = round(result["short_strike"] / spot, 6)
        if "long_strike" in result:
            result["long_strike_pct"] = round(result["long_strike"] / spot, 6)

    return result


# ── Diagnostic ────────────────────────────────────────────────────────────────

def diagnostic() -> None:
    """Print 3 sample detail strings per structure with parsed output."""
    ensure_snapshot_tables()
    df = read_df(
        f"""
        SELECT
            JSON_EXTRACT_STRING(candidate, '$.structure') AS structure,
            JSON_EXTRACT_STRING(candidate, '$.details')  AS details,
            spot
        FROM {SNAPSHOTS_TABLE}
        WHERE candidate IS NOT NULL
          AND JSON_EXTRACT_STRING(candidate, '$.details') IS NOT NULL
        """
    )
    df = df.dropna(subset=["structure", "details"])

    print(f"\n{'='*72}")
    print("  Geometry backfill — dry-run diagnostic")
    print(f"{'='*72}")

    for structure, grp in df.groupby("structure"):
        sample = grp.drop_duplicates("details").head(3)
        print(f"\n── {structure} ({len(grp):,} rows) ──")
        for _, row in sample.iterrows():
            parsed = parse_details(structure, row["details"], row.get("spot"))
            print(f"  raw:    {row['details']}")
            if parsed:
                print(f"  parsed: {parsed}")
            else:
                print("  parsed: (no match — will be skipped)")

    print(f"\n{'='*72}")
    print("Run with --write to commit changes.")
    print(f"{'='*72}\n")


# ── Write pass ────────────────────────────────────────────────────────────────

def write() -> None:
    """Parse all eligible rows and write geometry columns to the database."""
    ensure_snapshot_tables()

    # Fetch all rows where geometry is missing (spread_width NULL = not yet backfilled)
    df = read_df(
        f"""
        SELECT
            rowid,
            JSON_EXTRACT_STRING(candidate, '$.structure') AS structure,
            JSON_EXTRACT_STRING(candidate, '$.details')  AS details,
            spot
        FROM {SNAPSHOTS_TABLE}
        WHERE candidate IS NOT NULL
          AND spread_width IS NULL
          AND JSON_EXTRACT_STRING(candidate, '$.details') IS NOT NULL
        """
    )
    df = df.dropna(subset=["structure", "details"])
    total = len(df)
    print(f"Rows to process: {total:,}")

    updated   = 0
    skipped   = 0
    structure_counts: dict[str, int] = {}

    with connect() as con:
        for _, row in df.iterrows():
            structure = row["structure"]
            parsed = parse_details(structure, row["details"], row.get("spot"))

            if not parsed:
                skipped += 1
                continue

            # Build SET clause dynamically from parsed keys + trade_structure
            fields = {"trade_structure": structure, **parsed}
            set_parts = [f"{k} = ?" for k in fields]

            # feature_schema_version: 2 if geometry recovered, else keep as-is
            if "spread_width" in parsed:
                set_parts.append("feature_schema_version = 2")

            sql = (
                f"UPDATE {SNAPSHOTS_TABLE} "
                f"SET {', '.join(set_parts)} "
                f"WHERE rowid = ?"
            )
            values = list(fields.values()) + [int(row["rowid"])]
            con.execute(sql, values)

            updated += 1
            structure_counts[structure] = structure_counts.get(structure, 0) + 1

        # Also backfill trade_structure for rows that already have geometry
        # (e.g. rows written after Phase 1 deployment — they have spread_width
        #  but trade_structure was added later in Phase 2)
        con.execute(
            f"""
            UPDATE {SNAPSHOTS_TABLE}
            SET trade_structure = JSON_EXTRACT_STRING(candidate, '$.structure')
            WHERE trade_structure IS NULL
              AND candidate IS NOT NULL
              AND JSON_EXTRACT_STRING(candidate, '$.structure') IS NOT NULL
            """
        )
        con.commit()

    print(f"\nResults:")
    print(f"  Updated:  {updated:,}  (geometry recovered)")
    print(f"  Skipped:  {skipped:,}  (details not parseable)")
    print(f"\nBy structure:")
    for s, n in sorted(structure_counts.items(), key=lambda x: -x[1]):
        print(f"  {s:<30} {n:>6}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    else:
        diagnostic()
