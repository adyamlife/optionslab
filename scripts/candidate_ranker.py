"""
Shared candidate filtering and ranking logic used by both:
  - web/app.py :: build_top_trades()   (Live Suggestions page)
  - scripts/paper_trade_engine.py :: _select_top3()  (morning scan)

Any change to filtering rules or ranking criteria belongs here so both
surfaces stay in sync automatically.
"""
import logging
from config.rules import MIN_PROFIT_AMOUNT
from config.rules import IV_EDGE_SKIP_VP, IV_EDGE_BONUS_SCALE

log = logging.getLogger(__name__)


def _load_ml_gate_cfg() -> dict:
    """Read [ml_gate] from settings.toml. Cached per process."""
    try:
        from pathlib import Path
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        cfg = tomllib.loads((Path(__file__).resolve().parent.parent / "config" / "settings.toml").read_text(encoding="utf-8"))
        g = cfg.get("ml_gate", {})
        return {
            "enabled":       bool(g.get("enabled", True)),
            "min_p_win":     float(g.get("min_p_win",      0.55)),
            "min_confidence":float(g.get("min_confidence", 0.60)),
        }
    except Exception:
        return {"enabled": True, "min_p_win": 0.55, "min_confidence": 0.60}


def _strikes_complete(c) -> bool:
    """Return True only when all required strikes for this structure are non-None."""
    from config.structures import get_or_none as _gst
    from config.structures._base import StrikeSchema as _SS
    st = _gst(c.get("structure", ""))
    if st is None:
        return False
    if st.strike_schema == _SS.IRON_CONDOR:
        return all(c.get(k) is not None for k in (
            "put_long_strike", "put_short_strike", "call_short_strike", "call_long_strike"))
    if st.strike_schema == _SS.SINGLE_LEG:
        return c.get("short_strike") is not None
    return c.get("short_strike") is not None and c.get("long_strike") is not None


def filter_candidates(rows):
    """
    Apply all hard gates to every recommended candidate across `rows` and
    return a flat list of (row, candidate, ev, meets_both, iv_score_adj) tuples
    for the caller to rank and enrich.

    Hard gates (candidate is dropped entirely if any fail):
      1. recommended=True
      2. max_profit not None and >= MIN_PROFIT_AMOUNT ($1.00/sh = $100/contract)
      3. All required strikes non-None (no back-month chain = unenterable)
      4. IV edge not in hard-skip zone (buying rich / selling cheap beyond threshold)
      5. EV computable (ev or pop available)
      6. ML confidence gate — p_win > min_p_win AND confidence > min_confidence
         (only applied when pred_dist is present; bypassed silently when ML not trained)
    """
    _ml_gate = _load_ml_gate_cfg()
    result = []
    for row in rows:
        if (row.get("status") or "").startswith("SKIP"):
            continue
        for c in row.get("candidates", []):
            if not c.get("recommended") or c.get("max_profit") is None:
                continue

            # Gate 1: minimum profit per share
            if c["max_profit"] < MIN_PROFIT_AMOUNT:
                log.info(
                    f"Skip {row['ticker']} {c['structure']} — "
                    f"max_profit ${c['max_profit']:.2f} < ${MIN_PROFIT_AMOUNT:.2f} min "
                    f"(${c['max_profit']*100:.0f}/contract)"
                )
                continue

            # Gate 2: all required strikes present
            if not _strikes_complete(c):
                log.info(
                    f"Skip {row['ticker']} {c['structure']} — "
                    f"required strikes incomplete (no back-month chain?)"
                )
                continue

            # Gate 3: IV edge hard skip
            iv_edge_vp    = c.get("iv_edge_vp")
            iv_edge_label = c.get("iv_edge_label", "fair")
            if (iv_edge_vp is not None
                    and iv_edge_label in ("overpay", "undersell")
                    and abs(iv_edge_vp) > IV_EDGE_SKIP_VP):
                log.info(
                    f"Skip {row['ticker']} {c['structure']} — "
                    f"IV edge {iv_edge_vp:+.1f}vp ({iv_edge_label})"
                )
                continue

            # Gate 4: EV computable
            ev = c.get("ev")
            ev_is_proxy = False
            if ev is None:
                pop = c.get("pop")
                if pop is None:
                    continue
                ev = pop / 100 * c["max_profit"]
                ev_is_proxy = True

            # Gate 5: ML confidence gate (bypassed when pred_dist absent or gate disabled)
            pred_dist = (row.get("ml") or {}).get("pred_dist")
            ml_gate_passed = True
            if _ml_gate["enabled"] and pred_dist is not None:
                p_win      = pred_dist.get("p_win")
                confidence = pred_dist.get("confidence")
                if p_win is not None and confidence is not None:
                    if p_win < _ml_gate["min_p_win"] or confidence < _ml_gate["min_confidence"]:
                        log.info(
                            f"Skip {row['ticker']} {c['structure']} — "
                            f"ML gate: p_win={p_win:.3f} (min {_ml_gate['min_p_win']}) "
                            f"confidence={confidence:.3f} (min {_ml_gate['min_confidence']})"
                        )
                        ml_gate_passed = False
            if not ml_gate_passed:
                continue

            # Soft IV-edge score adjustment (±scaled, does not block)
            iv_score_adj = 0.0
            if iv_edge_vp is not None:
                iv_score_adj = min(max(iv_edge_vp, -5.0), 5.0) * IV_EDGE_BONUS_SCALE

            meets_both = (
                bool(c.get("meets_min_profit"))
                and c.get("meets_max_loss") is not False
            )

            result.append({
                "row":          row,
                "candidate":    c,
                "ev":           round(ev, 4),
                "ev_is_proxy":  ev_is_proxy,
                "meets_both":   meets_both,
                "iv_score_adj": iv_score_adj,
                "iv_edge_vp":   iv_edge_vp,
                "iv_edge_label":iv_edge_label,
                "pred_dist":    pred_dist,
            })
    return result


def rank_candidates(rows, n=3, score_fn=None):
    """
    Filter all rows through the hard gates, then sort and return the top-n.

    `score_fn(item)` is an optional callable returning a numeric bonus to add
    to the base signal score (used by build_top_trades for ML/IV-expansion).
    Default ranking: (not meets_both, -(signal + iv_adj), -ev/max_loss).

    Returns a list of the same (row, candidate, ev, …) dicts from filter_candidates,
    sorted and capped at n.
    """
    items = filter_candidates(rows)

    def _sort_key(item):
        c   = item["candidate"]
        sig = (item["row"].get("signal_score") or 0) + item["iv_score_adj"]
        if score_fn:
            sig += score_fn(item)
        # Normalise EV by max_loss so credit/debit spreads are on equal footing
        ml = c.get("max_loss")
        ev_norm = item["ev"] / ml if (ml and ml > 0) else item["ev"]
        return (not item["meets_both"], -sig, -ev_norm)

    items.sort(key=_sort_key)
    return items[:n]
