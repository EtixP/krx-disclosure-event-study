from __future__ import annotations

import numpy as np
import pandas as pd

from kdtb.learning.features import FEATURE_NAMES
from kdtb.learning.walk_forward_trainer import (
    make_folds,
    run_walk_forward,
)


def _synthetic_df(n_per_half: int, n_halves: int, edge: bool, seed: int = 0) -> pd.DataFrame:
    """Build a synthetic mock-trade dataset spread across half-year folds.

    If edge=True, feature 0 (market_is_kospi) predicts returns: KOSPI trades
    are profitable, KOSDAQ trades lose. If edge=False, returns are pure noise.
    """
    rng = np.random.RandomState(seed)
    rows = []
    periods = []
    start_year = 2022
    for h in range(n_halves):
        year = start_year + h // 2
        month = 3 if h % 2 == 0 else 9  # H1 vs H2
        for _ in range(n_per_half):
            feats = rng.rand(len(FEATURE_NAMES))
            feats[0] = 1.0 if rng.rand() > 0.5 else 0.0  # market_is_kospi
            if edge:
                base = 0.02 if feats[0] > 0.5 else -0.02
            else:
                base = 0.0
            ret = base + rng.randn() * 0.004
            rows.append(list(feats) + [f"{year}-{month:02d}-15", ret, int(ret > 0)])
    cols = FEATURE_NAMES + ["event_date", "realized_net_return", "label"]
    df = pd.DataFrame(rows, columns=cols)
    df["event_date"] = pd.to_datetime(df["event_date"])
    return df


def test_make_folds_chronological_halves():
    df = _synthetic_df(n_per_half=10, n_halves=4, edge=False)
    folds = make_folds(df)
    labels = [f[0] for f in folds]
    assert labels == ["2022H1", "2022H2", "2023H1", "2023H2"]


def test_trainer_learns_and_profits_on_planted_edge():
    df = _synthetic_df(n_per_half=120, n_halves=8, edge=True, seed=1)
    report = run_walk_forward(df, random_state=0)
    # With a learnable edge, the champion should become a learned policy,
    # get promoted at least once, and end with positive cumulative PnL that
    # beats always-trade (which trades the losers too).
    assert report.n_promotions >= 1
    assert report.cumulative_model_pnl > 0
    assert report.cumulative_model_pnl > report.cumulative_always_pnl
    assert any(f.champion_is_learned for f in report.folds)


def test_trainer_abstains_on_pure_noise():
    df = _synthetic_df(n_per_half=120, n_halves=8, edge=False, seed=2)
    report = run_walk_forward(df, random_state=0)
    # On pure noise the honest outcome is cumulative PnL near zero and clearly
    # better than always-trade only by not losing. It must not run away positive
    # (that would indicate overfitting / look-ahead).
    assert report.cumulative_model_pnl >= report.cumulative_always_pnl - 0.10
    # Sanity: the machine should not have "discovered" a large fake edge.
    assert report.cumulative_model_pnl < 0.50


def test_trainer_no_lookahead_train_strictly_precedes_test():
    """Structural check: for every reported fold, the training rows all have
    event_date strictly before the test fold's earliest event_date."""
    df = _synthetic_df(n_per_half=60, n_halves=6, edge=True, seed=3)
    folds = make_folds(df)
    report = run_walk_forward(df, random_state=0)
    for fr in report.folds:
        test_fold = next(f[1] for f in folds if f[0] == fr.period)
        test_min = test_fold["event_date"].min()
        # reconstruct the train window: folds[0 .. k-2]
        train = pd.concat([folds[j][1] for j in range(fr.fold_index - 1)], ignore_index=True)
        assert train["event_date"].max() < test_min
