"""
Deterministic Confidence Engine — replaces the XGB meta-ensemble stacker.

Problem: XGB stacker (847 test rows, 17 features) learns noise and dilutes
strong signals. IV direction (AUC=0.762, P@10=100%) drops to P@10=50% through
the stacker.

Solution: Precision-weighted deterministic aggregation with AUC-based weights:
  1. iv_expanding_prob (AUC=0.762, P@10=100%) — strongest ranker
  2. p_return_gt10     (AUC=0.662, 2.49x lift)
  3. p_up              (AUC=0.563, directional)
  4. p_uptrend         (regime context, weakest)

IV signal is down-weighted when the prediction is near 0.5 (uncertain).
Anomaly gate applies a 20% score penalty when market is anomalous.
"""
from __future__ import annotations

# AUC-proportional weights: (AUC - 0.5) / sum(AUC - 0.5), floored/capped
_DEFAULT_WEIGHTS: dict[str, float] = {
    "iv_expanding_prob": 0.40,  # P@10=100% — strongest ranker
    "p_return_gt10":     0.30,  # AUC=0.662, 2.49x lift
    "p_up":              0.20,  # AUC=0.563, directional
    "p_uptrend":         0.10,  # regime context
}

_ANOMALY_PENALTY  = 0.80   # 20% score reduction when is_anomaly=True
_IV_CONF_FLOOR    = 0.30   # min IV weight scale (even at 0 confidence)


def compute_confidence(
    iv_expanding_prob: float | None,
    p_return_gt10:     float | None,
    p_up:              float | None,
    p_uptrend:         float | None,
    is_anomaly:        bool = False,
    weights:           dict | None = None,
) -> dict:
    """
    Compute deterministic composite score (0–100) and confidence tier.

    Args:
        iv_expanding_prob: P(IV is expanding) from iv_direction_classifier
        p_return_gt10:     P(forward return > 10%) from return_classifier
        p_up:              P(price goes up) from direction_classifier
        p_uptrend:         P(uptrend regime) from regime_classifier
        is_anomaly:        True if anomaly_detector flagged this row
        weights:           Override default AUC-based weights (sums to 1.0)

    Returns dict with:
        composite_score:  float 0-100  (primary trade-quality signal)
        confidence_tier:  "High" | "Medium" | "Low"
        iv_confidence:    float 0-1    (certainty of the IV signal)
        anomaly_penalty:  bool         (whether the 20% penalty was applied)
        weights_used:     dict         (effective weights, post IV-scaling)
    """
    w = weights or _DEFAULT_WEIGHTS

    # iv_confidence: 0 when iv_expanding_prob≈0.5 (uncertain), 1 when near 0 or 1
    iv_conf = abs(float(iv_expanding_prob) - 0.5) * 2.0 if iv_expanding_prob is not None else 0.0

    # Down-weight IV signal linearly when prediction is near-uncertain
    iv_scale = _IV_CONF_FLOOR + (1.0 - _IV_CONF_FLOOR) * iv_conf
    eff_w = {
        "iv_expanding_prob": w["iv_expanding_prob"] * iv_scale,
        "p_return_gt10":     w["p_return_gt10"],
        "p_up":              w["p_up"],
        "p_uptrend":         w["p_uptrend"],
    }

    signal_vals: dict[str, float | None] = {
        "iv_expanding_prob": iv_expanding_prob,
        "p_return_gt10":     p_return_gt10,
        "p_up":              p_up,
        "p_uptrend":         p_uptrend,
    }

    parts, total_w = [], 0.0
    for key, val in signal_vals.items():
        if val is not None:
            parts.append(float(val) * eff_w[key])
            total_w += eff_w[key]

    if total_w == 0:
        return {
            "composite_score": None,
            "confidence_tier": "Low",
            "iv_confidence":   None,
            "anomaly_penalty": False,
            "weights_used":    {},
        }

    raw_score     = sum(parts) / total_w
    score         = round(raw_score * 100.0, 1)
    anomaly_hit   = bool(is_anomaly and score is not None)
    if anomaly_hit:
        score = round(score * _ANOMALY_PENALTY, 1)

    # Confidence tier:
    #  High   — IV is certain (conf≥0.60) AND signals largely agree AND ≥3 signals
    #  Medium — IV has some signal (conf≥0.30) AND ≥2 signals available
    #  Low    — everything else (uncertain IV, few signals, wide disagreement)
    n_signals = sum(1 for v in signal_vals.values() if v is not None)
    available = [float(v) for v in signal_vals.values() if v is not None]
    spread    = max(available) - min(available) if len(available) > 1 else 1.0

    if iv_conf >= 0.60 and n_signals >= 3 and spread < 0.30:
        tier = "High"
    elif iv_conf >= 0.30 and n_signals >= 2 and spread < 0.50:
        tier = "Medium"
    else:
        tier = "Low"

    return {
        "composite_score": score,
        "confidence_tier": tier,
        "iv_confidence":   round(iv_conf, 3),
        "anomaly_penalty": anomaly_hit,
        "weights_used":    {k: round(v, 3) for k, v in eff_w.items()},
    }
