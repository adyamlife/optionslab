"""Shared blended classifiers — must live in an importable module so joblib
can unpickle artifacts that store instances of these classes."""
import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin


class BlendedBinaryClassifier(ClassifierMixin, BaseEstimator):
    """0.5 XGBoost + 0.5 CatBoost probability blend for binary classification.

    Inherits BaseEstimator/ClassifierMixin (not just duck-typed) because
    sklearn.calibration.CalibratedClassifierCV needs classes_ (cv="prefit",
    older sklearn) or __sklearn_tags__ (sklearn >=1.6, via FrozenEstimator) —
    without either, calibration silently fails (previously caught by a bare
    `except: pass` upstream) and falls back to the raw, uncalibrated model
    with no indication anything went wrong. Diagnosed 2026-08-22 after
    train_return_classifier.py reported identical raw/calibrated Brier scores
    on every run, across two prior fix attempts (classes_, then FrozenEstimator)
    before this one — each surfaced the next missing piece only once the
    previous silent-failure hiding it was removed.

    Attribute names (xgb_m/cb_m) match the __init__ signature exactly, as
    sklearn's BaseEstimator.get_params() requires for introspection.
    """
    def __init__(self, xgb_m=None, cb_m=None):
        self.xgb_m = xgb_m
        self.cb_m  = cb_m
        # Sub-models are always already-fitted when this wrapper is constructed
        # in practice — set classes_/n_features_in_ now so check_is_fitted()
        # (used internally by CalibratedClassifierCV/FrozenEstimator) passes
        # without requiring a separate .fit() call.
        if xgb_m is not None:
            self.classes_ = getattr(xgb_m, "classes_", np.array([0, 1]))
            self.n_features_in_ = getattr(xgb_m, "n_features_in_", None)

    def fit(self, X, y):
        # Already-fitted sub-models are passed in; nothing to (re)fit, but a
        # fit() method is part of the sklearn estimator contract.
        self.classes_ = getattr(self.xgb_m, "classes_", np.array([0, 1]))
        self.n_features_in_ = getattr(self.xgb_m, "n_features_in_", None)
        return self

    def predict_proba(self, X):
        return 0.5 * self.xgb_m.predict_proba(X) + 0.5 * self.cb_m.predict_proba(X)

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)

    @property
    def feature_importances_(self):
        return (self.xgb_m.feature_importances_
                if hasattr(self.xgb_m, "feature_importances_")
                else self.cb_m.feature_importances_)


class BlendedMultiClassifier(ClassifierMixin, BaseEstimator):
    """0.5 XGBoost + 0.5 CatBoost blend for multi-class. Drop-in predict_proba.

    Same BaseEstimator/ClassifierMixin fix as BlendedBinaryClassifier — not
    currently run through CalibratedClassifierCV anywhere, but fixed in
    parallel so it doesn't hit the identical silent-failure trap if
    calibration is ever added here (see BlendedBinaryClassifier docstring).
    """
    def __init__(self, xgb_m=None, cb_m=None):
        self.xgb_m = xgb_m
        self.cb_m  = cb_m
        if xgb_m is not None:
            self.classes_ = getattr(xgb_m, "classes_", None)
            self.n_features_in_ = getattr(xgb_m, "n_features_in_", None)

    def fit(self, X, y):
        self.classes_ = getattr(self.xgb_m, "classes_", None)
        self.n_features_in_ = getattr(self.xgb_m, "n_features_in_", None)
        return self

    def predict_proba(self, X):
        return 0.5 * self.xgb_m.predict_proba(X) + 0.5 * self.cb_m.predict_proba(X)

    def predict(self, X):
        return np.argmax(self.predict_proba(X), axis=1)

    @property
    def feature_importances_(self):
        return (self.xgb_m.feature_importances_
                if hasattr(self.xgb_m, "feature_importances_")
                else self.cb_m.feature_importances_)
