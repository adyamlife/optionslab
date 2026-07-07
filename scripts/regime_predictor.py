"""
Regime Predictor — loads trained models and runs live inference for any ticker.

Runs the regime classifier, return regressor, and volatility regressor against
today's live feature row. Models must have been trained first (via the ML Admin
page or `python -m scripts.train_regime_classifier` etc.).

Stand-alone usage:
    python -m scripts.regime_predictor            # predict all WATCHLIST tickers
    python -m scripts.regime_predictor AAPL MSFT  # predict specific tickers

Constraints:
  - Read-only: never modifies candidate_provider.py / decision_provider.py
  - Gracefully returns warnings for models not yet trained
  - E*TRADE auth failures never halt predictions
"""
from __future__ import annotations

import sys
import json
import math
import warnings as _warnings
from pathlib import Path

# Suppress sklearn "X does not have valid feature names" noise — our feature
# matrix is a DataFrame built to match training columns; the warning is benign.
_warnings.filterwarnings(
    "ignore",
    message="X does not have valid feature names",
    category=UserWarning,
)

import joblib
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_MODELS_DIR = _ROOT / "data" / "models"

_CLASSIFIER_PATH     = _MODELS_DIR / "regime_classifier.joblib"
_RETURN_PATH         = _MODELS_DIR / "return_regressor.joblib"
_VOLATILITY_PATH     = _MODELS_DIR / "volatility_regressor.joblib"
_DIRECTION_PATH      = _MODELS_DIR / "direction_classifier.joblib"
_IV_DIRECTION_PATH   = _MODELS_DIR / "iv_direction_classifier.joblib"
_META_PATH           = _MODELS_DIR / "meta_ensemble.joblib"
_ANOMALY_PATH        = _MODELS_DIR / "anomaly_detector.joblib"

_CATEGORICAL_COLS = ("macd_trend", "trend", "spy_trend", "qqq_trend", "iwm_trend", "sector_etf", "sector_trend")

_model_cache: dict = {}


def _load(path: Path) -> dict | None:
    if path in _model_cache:
        return _model_cache[path]
    if not path.exists():
        return None
    try:
        art = joblib.load(path)
        _model_cache[path] = art
        return art
    except Exception:
        return None


def _build_X(row: dict, artifact: dict) -> pd.DataFrame:
    """Convert a feature dict to a 1-row DataFrame the model expects."""
    encoders = artifact.get("feature_encoders") or {}
    # Derive numeric features from stored feature_cols, excluding categoricals
    feature_cols = artifact.get("feature_cols") or []
    numeric_cols = [c for c in feature_cols if c not in _CATEGORICAL_COLS]

    X = pd.DataFrame([{c: row.get(c) for c in numeric_cols}])
    for c in numeric_cols:
        X[c] = pd.to_numeric(X[c], errors="coerce")

    for col in _CATEGORICAL_COLS:
        if col not in feature_cols:
            continue
        val = str(row.get(col) or "unknown")
        enc = encoders.get(col)
        if enc is None:
            X[col] = 0
        else:
            known = set(enc.classes_)
            safe = val if val in known else enc.classes_[0]
            X[col] = enc.transform([safe])[0]

    # Column order must match training: numerics first, then categoricals in the order
    # they appear in feature_cols (which matches the training script's loop order).
    cat_cols_in_artifact = [c for c in feature_cols if c in _CATEGORICAL_COLS]
    return X[numeric_cols + cat_cols_in_artifact]


def predict_ticker(ticker: str, today_row: dict | None = None) -> dict:
    """
    Run all available trained models for one ticker.
    today_row: pre-built feature dict (avoids redundant yfinance fetch when
    called from predict_all with shared market context). If None, fetches live.
    Returns a structured dict with regime, expected_return, expected_vol, warnings.
    """
    warnings = []

    if today_row is None:
        try:
            from scripts.regime_backfill import _build_today_row, _fetch_market_context
            from scripts.data_fetch import get_vix_context, get_macro_context
            vix_close, spy_close, qqq_close, iwm_close = _fetch_market_context("6mo")
            vix_ctx   = get_vix_context()
            macro_ctx = get_macro_context(dte=10)
            today_row = _build_today_row(ticker, vix_close=vix_close, spy_close=spy_close,
                                         qqq_close=qqq_close, iwm_close=iwm_close,
                                         vix_ctx=vix_ctx, macro_ctx=macro_ctx)
        except Exception as e:
            warnings.append(f"Feature fetch failed: {e}")
            today_row = None

    if today_row is None:
        return {"ticker": ticker, "ok": False, "error": "Could not build feature row", "warnings": warnings}

    result: dict = {"ticker": ticker, "ok": True, "warnings": warnings,
                    "date": str(today_row.get("date", ""))}

    # ── Regime classifier ────────────────────────────────────────────────────
    art = _load(_CLASSIFIER_PATH)
    if art is None:
        result["regime"] = None
        result["regime_proba"] = None
        warnings.append("Regime classifier not trained yet — run train_regime_classifier")
    else:
        try:
            X = _build_X(today_row, art)
            label_enc = art["label_encoder"]
            proba = art["model"].predict_proba(X)[0]
            pred_idx = int(np.argmax(proba))
            result["regime"] = label_enc.classes_[pred_idx]
            result["regime_proba"] = {
                cls: round(float(p), 3)
                for cls, p in zip(label_enc.classes_, proba)
            }
        except Exception as e:
            result["regime"] = None
            result["regime_proba"] = None
            warnings.append(f"Regime classifier error: {e}")

    # ── Return regressor ─────────────────────────────────────────────────────
    art = _load(_RETURN_PATH)
    if art is None:
        result["expected_return"] = None
        warnings.append("Return model not trained yet — run train_return_model")
    else:
        try:
            X = _build_X(today_row, art)
            result["expected_return"] = round(float(art["model"].predict(X)[0]), 4)
        except Exception as e:
            result["expected_return"] = None
            warnings.append(f"Return model error: {e}")

    # ── Volatility regressor ─────────────────────────────────────────────────
    art = _load(_VOLATILITY_PATH)
    if art is None:
        result["expected_vol"] = None
        warnings.append("Volatility model not trained yet — run train_volatility_model")
    else:
        try:
            X = _build_X(today_row, art)
            result["expected_vol"] = round(float(art["model"].predict(X)[0]), 4)
        except Exception as e:
            result["expected_vol"] = None
            warnings.append(f"Volatility model error: {e}")

    # ── Expected move (derived from vol, not a separate model) ──────────────
    # Formula: annualized_vol × √(10/252) → 10-trading-day 1-σ move as a fraction.
    # This is the standard options market expected move approximation.
    ev = result.get("expected_vol")
    result["expected_move_pct"] = round(ev * math.sqrt(10 / 252), 4) if ev is not None else None

    # ── Direction classifier ──────────────────────────────────────────────────
    art = _load(_DIRECTION_PATH)
    if art is None:
        result["p_up"] = None
        result["direction"] = None
        warnings.append("Direction model not trained yet — run train_direction_model")
    else:
        try:
            X = _build_X(today_row, art)
            p_up = float(art["model"].predict_proba(X)[0][1])
            result["p_up"]     = round(p_up, 4)
            result["direction"] = "Up" if p_up >= 0.5 else "Down"
        except Exception as e:
            result["p_up"] = None
            result["direction"] = None
            warnings.append(f"Direction model error: {e}")

    # ── IV Direction classifier ───────────────────────────────────────────────
    art = _load(_IV_DIRECTION_PATH)
    if art is None:
        result["iv_expanding_prob"] = None
        result["iv_direction"] = None
        warnings.append("IV Direction model not trained yet — run train_iv_direction_model")
    else:
        try:
            X = _build_X(today_row, art)
            p_exp = float(art["model"].predict_proba(X)[0][1])
            result["iv_expanding_prob"] = round(p_exp, 4)
            result["iv_direction"] = "Expanding" if p_exp >= 0.5 else "Contracting"
        except Exception as e:
            result["iv_expanding_prob"] = None
            result["iv_direction"] = None
            warnings.append(f"IV Direction model error: {e}")

    # ── Meta-ensemble scorer ─────────────────────────────────────────────────
    art = _load(_META_PATH)
    if art is None:
        result["meta_score"] = None
        warnings.append("Meta-ensemble not trained yet — run train_meta_ensemble")
    else:
        try:
            # Build the 7-feature meta-input from the other models' outputs.
            # Missing individual model outputs default to 0.5 (maximum uncertainty).
            regime_proba = result.get("regime_proba") or {}
            meta_row = {
                "p_uptrend":       regime_proba.get("Uptrend", 0.333),
                "p_downtrend":     regime_proba.get("Downtrend", 0.333),
                "p_rangebound":    regime_proba.get("Range-bound", 0.333),
                "expected_return": result.get("expected_return") or 0.0,
                "expected_vol":    result.get("expected_vol") or 0.25,
                "p_up":            result.get("p_up") or 0.5,
                "iv_expanding_prob": result.get("iv_expanding_prob") or 0.5,
            }
            X_meta = pd.DataFrame([meta_row])[art["meta_features"]]
            p_meta = float(art["model"].predict_proba(X_meta)[0][1])
            result["meta_score"] = round(p_meta * 100, 1)   # 0–100
        except Exception as e:
            result["meta_score"] = None
            warnings.append(f"Meta-ensemble error: {e}")

    # ── Anomaly detector ─────────────────────────────────────────────────────
    art = _load(_ANOMALY_PATH)
    if art is None:
        result["anomaly_score"] = None
        result["is_anomaly"]    = None
        result["anomaly_flags"] = []
        warnings.append("Anomaly detector not trained yet — run train_anomaly_detector")
    else:
        try:
            from scripts.train_anomaly_detector import score_row as _score_anomaly
            anom = _score_anomaly(today_row, art)
            result.update(anom)
        except Exception as e:
            result["anomaly_score"] = None
            result["is_anomaly"]    = None
            result["anomaly_flags"] = []
            warnings.append(f"Anomaly detector error: {e}")

    # Carry through useful live context for display
    result["live"] = {
        "close":          today_row.get("close"),
        "rsi":            today_row.get("rsi"),
        "hv20":           today_row.get("hv20"),
        "trend":          today_row.get("trend"),
        "vix_close":      today_row.get("vix_close"),
        "rel_strength":   today_row.get("rel_strength_spy"),
        "beta_60d":       today_row.get("beta_60d"),
        "atr_pct":        today_row.get("atr_pct"),
        "iv_rank_52w":    today_row.get("iv_rank_52w"),
        "vol_oi_ratio":   today_row.get("vol_oi_ratio"),
        "iv_skew":        today_row.get("iv_skew"),
        "iv_term_slope":  today_row.get("iv_term_slope"),
        "otm_pcr":        today_row.get("otm_pcr"),
        "spy_trend":      today_row.get("spy_trend"),
        "qqq_trend":      today_row.get("qqq_trend"),
        "iwm_trend":      today_row.get("iwm_trend"),
        "sector_etf":     today_row.get("sector_etf"),
        "sector_trend":   today_row.get("sector_trend"),
        "sector_rsi":     today_row.get("sector_rsi"),
        "sector_trend":   today_row.get("sector_trend"),
        "sector_iv_ratio": today_row.get("sector_iv_ratio"),
        "vvix":                   today_row.get("vvix"),
        "vix_3m":                 today_row.get("vix_3m"),
        "vix_term_slope":         today_row.get("vix_term_slope"),
        "earnings_inside_expiry": today_row.get("earnings_inside_expiry"),
        "news_sentiment_score":   today_row.get("news_sentiment_score"),
        "analyst_rec_change":     today_row.get("analyst_rec_change"),
        "short_interest_pct":     today_row.get("short_interest_pct"),
        # Tier 4 — chain-snapshot-derived
        "iv_skew_20d":      today_row.get("iv_skew_20d"),
        "gex_proxy":        today_row.get("gex_proxy"),
        "max_pain_strike":  today_row.get("max_pain_strike"),
        "oi_concentration": today_row.get("oi_concentration"),
        "wings_iv_ratio":   today_row.get("wings_iv_ratio"),
        # Tier 5 — macro context
        "yield_10y":      today_row.get("yield_10y"),
        "yield_3m":       today_row.get("yield_3m"),
        "yield_curve":    today_row.get("yield_curve"),
        "dollar_index":   today_row.get("dollar_index"),
        "fed_within_dte": today_row.get("fed_within_dte"),
        "cpi_within_dte": today_row.get("cpi_within_dte"),
    }
    return result


def predict_all(tickers: list[str] | None = None) -> dict:
    """
    Run predictions for all tickers, fetching shared market context once.
    Returns {ok, predictions: [...], warnings: [...], model_status: {...}}
    """
    from config.watchlist import WATCHLIST
    from scripts.regime_backfill import _build_today_row, _fetch_market_context
    from scripts.data_fetch import get_vix_context, get_macro_context

    if tickers is None:
        tickers = list(WATCHLIST.keys()) if isinstance(WATCHLIST, dict) else list(WATCHLIST)

    top_warnings = []
    try:
        vix_close, spy_close, qqq_close, iwm_close = _fetch_market_context("6mo")
    except Exception as e:
        top_warnings.append(f"Market context fetch failed: {e}")
        vix_close = spy_close = qqq_close = iwm_close = None

    try:
        vix_ctx = get_vix_context()
    except Exception:
        vix_ctx = {}
    try:
        macro_ctx = get_macro_context(dte=10)
    except Exception:
        macro_ctx = {}

    predictions = []
    for ticker in tickers:
        try:
            today_row = _build_today_row(
                ticker,
                vix_close=vix_close, spy_close=spy_close,
                qqq_close=qqq_close, iwm_close=iwm_close,
                vix_ctx=vix_ctx, macro_ctx=macro_ctx,
            )
        except Exception as e:
            predictions.append({"ticker": ticker, "ok": False, "error": str(e), "warnings": []})
            continue
        pred = predict_ticker(ticker, today_row=today_row)
        predictions.append(pred)

    model_status = {
        "regime_classifier":       _CLASSIFIER_PATH.exists(),
        "return_regressor":        _RETURN_PATH.exists(),
        "volatility_regressor":    _VOLATILITY_PATH.exists(),
        "direction_classifier":    _DIRECTION_PATH.exists(),
        "iv_direction_classifier": _IV_DIRECTION_PATH.exists(),
        "meta_ensemble":           _META_PATH.exists(),
        "anomaly_detector":        _ANOMALY_PATH.exists(),
    }
    return {
        "ok": True,
        "predictions": predictions,
        "model_status": model_status,
        "warnings": top_warnings,
    }


if __name__ == "__main__":
    tickers = sys.argv[1:] or None
    result = predict_all(tickers)
    for p in result["predictions"]:
        w = "; ".join(p.get("warnings") or [])
        regime = p.get("regime") or "—"
        ret    = p.get("expected_return")
        vol    = p.get("expected_vol")
        p_up   = p.get("p_up")
        iv_dir      = p.get("iv_direction") or "—"
        iv_prob     = p.get("iv_expanding_prob")
        meta_score  = p.get("meta_score")
        anom_score  = p.get("anomaly_score")
        is_anomaly  = p.get("is_anomaly")
        if ret is not None and vol is not None and p_up is not None:
            iv_str   = f"  IV={iv_dir}({iv_prob:.0%})" if iv_prob is not None else ""
            meta_str = f"  meta={meta_score:.0f}/100" if meta_score is not None else ""
            anom_str = (f"  anom={anom_score:.0f}{'⚠' if is_anomaly else ''}"
                        if anom_score is not None else "")
            print(f"{p['ticker']:6s}  regime={regime:12s}  "
                  f"ret={ret:+.2%}  vol={vol:.2%}  P(up)={p_up:.0%}"
                  f"{iv_str}{meta_str}{anom_str}")
        else:
            print(f"{p['ticker']:6s}  {p.get('error') or w}")
    if result.get("warnings"):
        for w in result["warnings"]:
            print(f"[WARN] {w}")
