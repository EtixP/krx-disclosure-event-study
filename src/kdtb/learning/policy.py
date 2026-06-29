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

    def fit(self, X: np.ndarray, returns: np.ndarray) -> "LearnedPolicy":
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
