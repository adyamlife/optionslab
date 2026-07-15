"""
Interaction features — engineered cross-signals for ML training.

Call add_interaction_features(df) to append INTERACTION_FEATURES columns.
Import INTERACTION_FEATURES to add them to NUMERIC_FEATURES in training scripts.
"""
import numpy as np
import pandas as pd

INTERACTION_FEATURES = [
    "iv_x_earnings",  # iv_rank_52w × earnings_inside_expiry  (vol premium at event risk)
    "atr_x_trend",    # atr_pct × |trend_sign|  (volatility magnitude × trend conviction)
    "rsi_iv_cheap",   # (rsi−50)/50 × (1 − iv_rank/100)  (momentum when vol is cheap)
    "vol_spread",     # hv20 − vix_close/100  (realized vol premium vs. market vol)
    "rsi_x_vix",      # (rsi−50)/50 × vix_close/20  (stretched RSI in high-vol regimes)
]

_TREND_MAP = {"Uptrend": 1.0, "Downtrend": -1.0, "Range-bound": 0.0}


def add_interaction_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    iv_rank  = pd.to_numeric(df.get("iv_rank_52w", np.nan), errors="coerce").fillna(50.0)
    earnings = pd.to_numeric(df.get("earnings_inside_expiry", 0), errors="coerce").fillna(0.0)
    atr_pct  = pd.to_numeric(df.get("atr_pct", np.nan), errors="coerce").fillna(1.0)
    rsi      = pd.to_numeric(df.get("rsi", np.nan), errors="coerce").fillna(50.0)
    hv20     = pd.to_numeric(df.get("hv20", np.nan), errors="coerce").fillna(0.0)
    vix      = pd.to_numeric(df.get("vix_close", np.nan), errors="coerce").fillna(20.0)

    trend_col = (
        df["trend"] if "trend" in df.columns
        else pd.Series(["Range-bound"] * len(df), index=df.index)
    )
    trend_sign = trend_col.map(_TREND_MAP).fillna(0.0)

    rsi_norm = (rsi - 50.0) / 50.0

    df["iv_x_earnings"] = (iv_rank / 100.0 * earnings).round(4)
    df["atr_x_trend"]   = (atr_pct * trend_sign.abs()).round(4)
    df["rsi_iv_cheap"]  = (rsi_norm * (1.0 - iv_rank / 100.0)).round(4)
    df["vol_spread"]    = (hv20 - vix / 100.0).round(4)
    df["rsi_x_vix"]     = (rsi_norm * vix / 20.0).round(4)

    return df
