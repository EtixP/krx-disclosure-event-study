from __future__ import annotations

from kdtb.backtest.metrics import compute


def test_empty_returns_safe_defaults():
    m = compute([])
    assert m.n_trades == 0
    assert m.win_rate == 0
    assert m.profit_factor is None


def test_all_wins_no_losses():
    m = compute([0.01, 0.02, 0.03])
    assert m.n_trades == 3
    assert m.win_rate == 1.0
    assert m.profit_factor is None  # no losses → undefined
    assert abs(m.mean_return - 0.02) < 1e-9


def test_mixed_returns_basic_stats():
    m = compute([0.10, -0.05, 0.04, -0.02, 0.01])
    assert m.n_trades == 5
    assert m.win_rate == 3 / 5
    assert abs(m.mean_return - 0.016) < 1e-9
    # profit_factor = (0.10+0.04+0.01) / (0.05+0.02) = 0.15 / 0.07
    assert abs(m.profit_factor - (0.15 / 0.07)) < 1e-9


def test_max_drawdown_simple():
    # cumulative: 0.10, 0.05, 0.15, 0.05, 0.00 → peak 0.15, drawdown -0.15
    m = compute([0.10, -0.05, 0.10, -0.10, -0.05])
    assert abs(m.max_drawdown - (-0.15)) < 1e-9


def test_total_return_is_sum():
    rs = [0.01, 0.02, -0.005]
    m = compute(rs)
    assert abs(m.total_return - sum(rs)) < 1e-9


def test_handles_none_and_nan():
    import math
    m = compute([0.01, None, float("nan"), 0.02])
    assert m.n_trades == 2
