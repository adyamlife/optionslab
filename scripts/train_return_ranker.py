"""
Return Ranker — XGBRanker with rank:ndcg objective.

Groups rows by date; target = continuous forward_return as relevance.
Trains to directly optimize within-date ranking of stocks rather than
predicting a binary threshold — closer to the actual production objective
(pick top-N per day from the universe).

Key differences from return_classifier:
  - XGBRanker vs XGBClassifier
  - rank:ndcg vs binary:logistic
  - Relevance = raw forward_return (higher = more relevant)
  - Data must be sorted by date; groups = stocks per date

Evaluation:
  NDCG@K  — information-theoretically optimal ranking quality
  Prec@K  — fraction of top-K picks with positive forward_return

Run standalone: python -m scripts.train_return_ranker
Output: data/models/return_ranker.joblib
"""
import logging
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import ndcg_score
from xgboost import XGBRanker

_ROOT       = Path(__file__).resolve().parent.parent
_MODEL_PATH = _ROOT / "data" / "models" / "return_ranker.joblib"

log = logging.getLogger(__name__)

from scripts.train_return_classifier import (
    NUMERIC_FEATURES, CAT_COLS, LAG_SOURCES, LAG_COLS,
    build_feature_matrix, compute_lag_features,
)

RETURN_COL = "forward_return"

# XGBRanker (rank:ndcg) requires non-negative integer relevance labels.
# We bucket continuous returns into 4 grades: 0=loss, 1=small gain, 2=strong, 3=top
_GRADE_THRESHOLDS = (0.0, 0.05, 0.10)  # breakpoints for grades 1, 2, 3


def _to_relevance(returns: np.ndarray) -> np.ndarray:
    """Convert forward_return floats to integer relevance grades 0-3."""
    rel = np.zeros(len(returns), dtype=np.int32)
    rel[returns > _GRADE_THRESHOLDS[0]] = 1
    rel[returns > _GRADE_THRESHOLDS[1]] = 2
    rel[returns > _GRADE_THRESHOLDS[2]] = 3
    return rel


def load_data() -> pd.DataFrame:
    from scripts.db import read_df, TABLE
    df = read_df(f"SELECT * FROM {TABLE} WHERE labeled = true")
    df = df.dropna(subset=[RETURN_COL])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["date", "ticker"]).reset_index(drop=True)


def _time_split(df, val_fraction=0.15, test_fraction=0.15):
    dates = np.sort(df["date"].unique())
    n = len(dates)
    val_cut  = dates[int(n * (1 - val_fraction - test_fraction))]
    test_cut = dates[int(n * (1 - test_fraction))]
    train = df[df["date"] <  val_cut]
    val   = df[(df["date"] >= val_cut) & (df["date"] < test_cut)]
    test  = df[df["date"] >= test_cut]
    return train, val, test, val_cut, test_cut


def _group_sizes(df: pd.DataFrame) -> np.ndarray:
    """Number of stocks per date — required by XGBRanker."""
    return df.groupby("date", sort=False).size().values


def _ndcg_at_k(y_score: np.ndarray, y_true: np.ndarray,
               groups: np.ndarray, k: int = 10) -> float:
    """Per-date NDCG@k averaged across dates."""
    scores, start = [], 0
    for g in groups:
        end = start + int(g)
        if g < 2:
            start = end
            continue
        s = y_true[start:end]
        p = y_score[start:end]
        # ndcg_score requires non-negative relevance
        s_shifted = s - s.min()
        try:
            scores.append(float(ndcg_score(s_shifted.reshape(1, -1),
                                           p.reshape(1, -1), k=min(k, int(g)))))
        except Exception:
            pass
        start = end
    return round(float(np.mean(scores)), 4) if scores else 0.0


def _precision_at_k(y_score: np.ndarray, y_true: np.ndarray,
                    groups: np.ndarray, k: int = 10) -> float:
    """Fraction of top-K predicted stocks (per date) with positive forward_return."""
    hits, total, start = 0, 0, 0
    for g in groups:
        end = start + int(g)
        if g < 2:
            start = end
            continue
        order = np.argsort(y_score[start:end])[::-1]
        kk = min(k, int(g))
        top_k  = y_true[start:end][order[:kk]]
        hits  += int((top_k > 0).sum())
        total += kk
        start = end
    return round(hits / total, 4) if total > 0 else 0.0


def train(out_path=_MODEL_PATH) -> dict:
    df = load_data()
    if df.empty:
        return {"ok": False, "error": "No labeled rows"}

    df = compute_lag_features(df)

    train_df, val_df, test_df, val_cut, test_cut = _time_split(df)
    if train_df.empty or test_df.empty:
        return {"ok": False, "error": "Split produced empty fold"}

    X_train, dummy_cols = build_feature_matrix(train_df, fit=True)
    X_val,   _          = build_feature_matrix(val_df,   dummy_cols=dummy_cols)
    X_test,  _          = build_feature_matrix(test_df,  dummy_cols=dummy_cols)

    # Raw returns for evaluation metrics; integer relevance for XGBRanker training
    y_train_raw = train_df[RETURN_COL].values.astype(float)
    y_val_raw   = val_df[RETURN_COL].values.astype(float)
    y_test_raw  = test_df[RETURN_COL].values.astype(float)

    y_train = _to_relevance(y_train_raw)
    y_val   = _to_relevance(y_val_raw)
    y_test  = _to_relevance(y_test_raw)

    train_groups = _group_sizes(train_df)
    val_groups   = _group_sizes(val_df)
    test_groups  = _group_sizes(test_df)

    model = XGBRanker(
        objective="rank:ndcg",
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(
        X_train, y_train,
        group=train_groups,
        eval_set=[(X_val, y_val)],
        eval_group=[val_groups],
        verbose=False,
    )

    y_pred = model.predict(X_test)
    # NDCG uses integer relevance grades; Prec@K uses raw returns (positive = win)
    ndcg_10 = _ndcg_at_k(y_pred, y_test,     test_groups, k=10)
    ndcg_25 = _ndcg_at_k(y_pred, y_test,     test_groups, k=25)
    prec_10 = _precision_at_k(y_pred, y_test_raw, test_groups, k=10)
    prec_25 = _precision_at_k(y_pred, y_test_raw, test_groups, k=25)

    feature_importances = dict(zip(X_train.columns.tolist(),
                                   model.feature_importances_.tolist()))

    artifact = {
        "model":            model,
        "dummy_cols":       dummy_cols,
        "numeric_features": list(NUMERIC_FEATURES),
        "cat_cols":         list(CAT_COLS),
        "lag_sources":      list(LAG_SOURCES),
        "lag_cols":         list(LAG_COLS),
        "val_cutoff":       str(val_cut),
        "test_cutoff":      str(test_cut),
        "train_rows":       len(train_df),
        "val_rows":         len(val_df),
        "test_rows":        len(test_df),
        "ndcg_10":          ndcg_10,
        "ndcg_25":          ndcg_25,
        "prec_10":          prec_10,
        "prec_25":          prec_25,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out_path)

    return {
        "ok":         True,
        "ndcg_10":    ndcg_10,
        "ndcg_25":    ndcg_25,
        "prec_10":    prec_10,
        "prec_25":    prec_25,
        "feature_importances": feature_importances,
        "train_rows": len(train_df),
        "val_rows":   len(val_df),
        "test_rows":  len(test_df),
        "val_cutoff": str(val_cut),
        "test_cutoff": str(test_cut),
        "model_path": str(out_path),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train()
    if not result.get("ok"):
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(f"NDCG@10={result['ndcg_10']:.4f}  NDCG@25={result['ndcg_25']:.4f}")
    print(f"Prec@10={result['prec_10']:.4f}  Prec@25={result['prec_25']:.4f}")
    print(f"Train rows: {result['train_rows']} | Val: {result['val_rows']} | Test: {result['test_rows']}")
    print(f"Val cutoff: {result['val_cutoff']} | Test cutoff: {result['test_cutoff']}")
    print(f"\nTop 10 features:")
    top10 = sorted(result["feature_importances"].items(), key=lambda x: -x[1])[:10]
    for f, imp in top10:
        print(f"  {f}: {imp:.3f}")
    print(f"\nModel saved to {result['model_path']}")
