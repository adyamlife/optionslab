"""Shared blended classifiers — must live in an importable module so joblib
can unpickle artifacts that store instances of these classes."""
import numpy as np


class BlendedBinaryClassifier:
    """0.5 XGBoost + 0.5 CatBoost probability blend for binary classification."""
    def __init__(self, xgb_m, cb_m):
        self._xgb = xgb_m
        self._cb  = cb_m

    def predict_proba(self, X):
        return 0.5 * self._xgb.predict_proba(X) + 0.5 * self._cb.predict_proba(X)

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)

    @property
    def feature_importances_(self):
        return (self._xgb.feature_importances_
                if hasattr(self._xgb, "feature_importances_")
                else self._cb.feature_importances_)


class BlendedMultiClassifier:
    """0.5 XGBoost + 0.5 CatBoost blend for multi-class. Drop-in predict_proba."""
    def __init__(self, xgb_m, cb_m):
        self._xgb = xgb_m
        self._cb  = cb_m

    def predict_proba(self, X):
        return 0.5 * self._xgb.predict_proba(X) + 0.5 * self._cb.predict_proba(X)

    def predict(self, X):
        return np.argmax(self.predict_proba(X), axis=1)

    @property
    def feature_importances_(self):
        return (self._xgb.feature_importances_
                if hasattr(self._xgb, "feature_importances_")
                else self._cb.feature_importances_)
