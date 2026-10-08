"""Trading policies: two trivial baselines and one learned policy.

A policy maps a feature matrix to a boolean "trade / skip" decision per row.
The reward of a policy over a set of mock trades is the sum of realized net
returns on the rows it chose to trade (skipping costs nothing and earns
nothing — abstaining scores 0).

The LearnedPolicy wraps a gradient-boosted classifier that predicts
P(net return > 0), then picks the probability threshold that maximizes
realized PnL on an internal, time-ordered validation slice. If no threshold
beats abstaining (PnL <= 0 everywhere), it abstains — which is the correct
behavior when the data has no edge.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


class Policy:
    name = "policy"

    def fit(self, X: np.ndarray, returns: np.ndarray) -> "Policy":
        return self

    def decide(self, X: np.ndarray) -> np.ndarray:  # -> bool array
        raise NotImplementedError


class NeverTrade(Policy):
    name = "never_trade"

    def decide(self, X: np.ndarray) -> np.ndarray:
        return np.zeros(len(X), dtype=bool)


class AlwaysTrade(Policy):
    name = "always_trade"

    def decide(self, X: np.ndarray) -> np.ndarray:
        return np.ones(len(X), dtype=bool)


class LearnedPolicy(Policy):
    name = "learned"

    def __init__(
        self,
        random_state: int = 0,
        val_frac: float = 0.3,
        min_train: int = 60,
        n_estimators: int = 150,
        max_depth: int = 3,
        learning_rate: float = 0.05,
    ) -> None:
        self.random_state = random_state
        self.val_frac = val_frac
        self.min_train = min_train
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.model = None
        # threshold > 1.0 means "never fires" — the safe default before training.
        self.threshold = 2.0

    def _fit_historical(self, X: np.ndarray, returns: np.ndarray) -> "LearnedPolicy":
        """Fit on a time-ordered array (caller guarantees rows are sorted by date).

        Internally splits off the most recent `val_frac` as a validation slice
        used only to choose the PnL-optimal probability threshold.
        """
        from sklearn.ensemble import GradientBoostingClassifier

        X = np.asarray(X, dtype=float)
        returns = np.asarray(returns, dtype=float)
        n = len(X)
        if n < self.min_train:
            self.model, self.threshold = None, 2.0
            return self

        cut = max(1, int(n * (1 - self.val_frac)))
        Xtr, Xval = X[:cut], X[cut:]
        rtr, rval = returns[:cut], returns[cut:]
        ytr = (rtr > 0).astype(int)

        if len(Xval) == 0 or ytr.min() == ytr.max():
            # degenerate: no validation rows, or only one class to learn
            self.model, self.threshold = None, 2.0
            return self

        model = GradientBoostingClassifier(
            random_state=self.random_state,
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
        )
        model.fit(Xtr, ytr)
        self.model = model

        # choose the threshold that maximizes validation PnL; abstain (0) is the
        # benchmark, so a threshold only wins if it produces positive PnL.
        proba = model.predict_proba(Xval)[:, 1]
        best_thr, best_pnl = 2.0, 0.0
        for thr in np.linspace(0.30, 0.90, 31):
            sel = proba >= thr
            pnl = float(rval[sel].sum()) if sel.any() else 0.0
            if pnl > best_pnl:
                best_pnl, best_thr = pnl, float(thr)
        self.threshold = best_thr
        return self

    def fit(
        self,
        X: np.ndarray,
        returns: np.ndarray,
        *,
        decision_dates,
        label_end_dates,
        row_ids,
        cutoff,
    ) -> "LearnedPolicy":
        """Fit/choose threshold using daily outcome availability at fixed cutoffs.

        The inner boundary uses unique candidate decision dates, never outcome
        dates or row count. Fit labels must end before calibration begins;
        threshold labels must end before the outer validation period begins.
        """
        import pandas as pd
        from sklearn.ensemble import GradientBoostingClassifier
        from kdtb.learning.chronology import daily_date

        X = np.asarray(X, dtype=float)
        returns = np.asarray(returns, dtype=float)
        decision = pd.DatetimeIndex([daily_date(day) for day in decision_dates])
        ends = pd.DatetimeIndex([daily_date(day) for day in label_end_dates])
        frozen = daily_date(cutoff)
        ids = np.asarray(row_ids, dtype=object)
        if not (len(X) == len(returns) == len(decision) == len(ends) == len(ids)):
            raise ValueError("chronology arrays must have equal lengths")
        if not all(isinstance(key, str) and key for key in ids) or len(set(ids)) != len(
            ids
        ):
            raise ValueError("chronology requires unique string row IDs")
        if (decision >= ends).any() or (decision >= frozen).any():
            raise ValueError("invalid chronology at policy cutoff")
        if not 0 < self.val_frac < 1:
            raise ValueError("val_frac must be between zero and one")
        if not np.isfinite(X).all() or not np.isfinite(returns).all():
            raise ValueError("policy inputs must be finite")
        order = np.lexsort((ids, decision.asi8))
        X, returns, ids = X[order], returns[order], ids[order]
        decision, ends = decision[order], ends[order]
        unique = decision.unique()
        self.model, self.threshold = None, 2.0
        self.diagnostics = {
            "status": "insufficient_unique_dates",
            "outer_validation_cutoff": str(frozen.date()),
            "candidate_n": len(X),
            "candidate_unique_dates": len(unique),
            "calibration_start": None,
            "fit_candidates_n": 0,
            "fit_n": 0,
            "fit_purged_n": 0,
            "calibration_candidates_n": 0,
            "calibration_n": 0,
            "calibration_purged_n": 0,
            "fit_row_ids": [],
            "calibration_row_ids": [],
        }
        if len(unique) < 2:
            return self
        cut = min(len(unique) - 1, max(1, int(len(unique) * (1 - self.val_frac))))
        calibration_start = unique[cut]
        fit_candidates = decision < calibration_start
        calibration_candidates = ~fit_candidates
        fit_mask = fit_candidates & (ends < calibration_start)
        calibration_mask = calibration_candidates & (ends < frozen)
        self.diagnostics.update(
            {
                "calibration_start": str(calibration_start.date()),
                "fit_candidates_n": int(fit_candidates.sum()),
                "fit_n": int(fit_mask.sum()),
                "fit_purged_n": int((fit_candidates & ~fit_mask).sum()),
                "calibration_candidates_n": int(calibration_candidates.sum()),
                "calibration_n": int(calibration_mask.sum()),
                "calibration_purged_n": int(
                    (calibration_candidates & ~calibration_mask).sum()
                ),
                "fit_row_ids": ids[fit_mask].tolist(),
                "calibration_row_ids": ids[calibration_mask].tolist(),
            }
        )
        minimum_fit = max(1, int(self.min_train * (1 - self.val_frac)))
        minimum_calibration = max(1, self.min_train - minimum_fit)
        if fit_mask.sum() < minimum_fit or calibration_mask.sum() < minimum_calibration:
            self.diagnostics["status"] = "insufficient_purged_samples"
            return self
        ytr = (returns[fit_mask] > 0).astype(int)
        if ytr.min() == ytr.max():
            self.diagnostics["status"] = "single_class_fit"
            return self
        model = GradientBoostingClassifier(
            random_state=self.random_state,
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
        )
        model.fit(X[fit_mask], ytr)
        self.model = model
        proba = model.predict_proba(X[calibration_mask])[:, 1]
        rval = returns[calibration_mask]
        best_thr, best_pnl = 2.0, 0.0
        for thr in np.linspace(0.30, 0.90, 31):
            sel = proba >= thr
            pnl = float(rval[sel].sum()) if sel.any() else 0.0
            if pnl > best_pnl:
                best_pnl, best_thr = pnl, float(thr)
        self.threshold = best_thr
        self.diagnostics["status"] = (
            "fitted" if best_thr <= 1 else "no_positive_calibration_threshold"
        )
        return self

    def state_fingerprint(self) -> str:
        """Deterministic fingerprint of actual fitted state, without future labels."""
        import hashlib
        import json

        state = {"threshold": self.threshold, "model": None}
        if self.model is not None:
            state["model"] = {
                "parameters": self.model.get_params(),
                "classes": self.model.classes_.tolist(),
                "prior": self.model.init_.class_prior_.tolist(),
                "trees": [
                    {
                        key: getattr(estimator.tree_, key).tolist()
                        for key in (
                            "children_left",
                            "children_right",
                            "feature",
                            "threshold",
                            "value",
                        )
                    }
                    for estimator in self.model.estimators_.ravel()
                ],
            }
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.model is None:
            return np.zeros(len(X), dtype=float)
        return self.model.predict_proba(np.asarray(X, dtype=float))[:, 1]

    def decide(self, X: np.ndarray) -> np.ndarray:
        if self.model is None:
            return np.zeros(len(X), dtype=bool)
        return self.predict_proba(X) >= self.threshold


def policy_pnl(policy: Policy, X: np.ndarray, returns: np.ndarray) -> tuple[float, int]:
    """Return (total realized net PnL, number of trades) for a policy."""
    sel = policy.decide(np.asarray(X, dtype=float))
    returns = np.asarray(returns, dtype=float)
    if not sel.any():
        return 0.0, 0
    return float(returns[sel].sum()), int(sel.sum())
