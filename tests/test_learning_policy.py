from __future__ import annotations

import numpy as np

from kdtb.learning.policy import (
    AlwaysTrade,
    LearnedPolicy,
    NeverTrade,
    policy_pnl,
)


def test_never_trade_decides_all_false():
    X = np.random.RandomState(0).rand(10, 8)
    assert NeverTrade().decide(X).sum() == 0


def test_always_trade_decides_all_true():
    X = np.random.RandomState(0).rand(10, 8)
    assert AlwaysTrade().decide(X).all()


def test_policy_pnl_sums_only_traded_rows():
    returns = np.array([0.10, -0.05, 0.03])

    class TradeFirstTwo:
        def decide(self, X):
            return np.array([True, True, False])

    pnl, n = policy_pnl(TradeFirstTwo(), np.zeros((3, 8)), returns)
    assert abs(pnl - 0.05) < 1e-9
    assert n == 2


def test_learned_policy_abstains_with_too_little_data():
    X = np.random.RandomState(1).rand(10, 8)
    returns = np.random.RandomState(2).randn(10) * 0.02
    p = LearnedPolicy(min_train=60).fit(X, returns)
    assert p.model is None
    assert p.decide(X).sum() == 0


def test_learned_policy_learns_planted_edge():
    """If feature 0 perfectly predicts profitable trades, the learner should
    trade the profitable ones and earn positive PnL on held-out data."""
    rng = np.random.RandomState(42)
    n = 600
    X = rng.rand(n, 8)
    # planted: when feature 0 > 0.5, the trade is reliably profitable (+2%),
    # otherwise reliably unprofitable (-2%), plus small noise.
    base = np.where(X[:, 0] > 0.5, 0.02, -0.02)
    returns = base + rng.randn(n) * 0.003

    split = 450
    p = LearnedPolicy(random_state=0).fit(X[:split], returns[:split])
    assert p.model is not None

    # On held-out data, the learned policy should beat always-trade and be positive.
    Xte, rte = X[split:], returns[split:]
    model_pnl, model_trades = policy_pnl(p, Xte, rte)
    always_pnl, _ = policy_pnl(AlwaysTrade(), Xte, rte)
    assert model_trades > 0
    assert model_pnl > 0
    assert model_pnl > always_pnl


def test_learned_policy_abstains_on_pure_noise():
    """When returns are pure noise (no learnable structure), the PnL-optimal
    threshold should mostly abstain — at worst it must not lose much."""
    rng = np.random.RandomState(7)
    n = 600
    X = rng.rand(n, 8)
    returns = rng.randn(n) * 0.02  # zero-mean noise, no relationship to X

    split = 450
    p = LearnedPolicy(random_state=0).fit(X[:split], returns[:split])
    Xte, rte = X[split:], returns[split:]
    model_pnl, _ = policy_pnl(p, Xte, rte)
    always_pnl, _ = policy_pnl(AlwaysTrade(), Xte, rte)
    # The learner should not do meaningfully worse than always-trade on noise,
    # and typically does better by abstaining. Allow a small slack for variance.
    assert model_pnl >= always_pnl - 0.05
