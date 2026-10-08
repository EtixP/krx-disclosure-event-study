"""Explicit compatibility adapter for frozen historical research builders only.

These entry points intentionally reproduce pre-M0.6 unsafe row/date splitting.
They must not be used for current learner decisions. Current generators report
current source hashes; old artifact source bytes are retained separately.
"""

from kdtb.learning.dataset import _load_historical_mock_trades
from kdtb.learning.walk_forward_trainer import (
    _make_historical_folds as make_folds,
    _run_historical_walk_forward as run_walk_forward,
)


def load_mock_trades(category, *args, **kwargs):
    if category == "supply_contract":
        raise ValueError(
            "historical supply enrichment requires unavailable original-version provenance"
        )
    return _load_historical_mock_trades(category, *args, **kwargs)


def synthetic_edge_df(n_per_half=150, n_halves=8, seed=0):
    """Preserve the exact old synthetic baseline, including its row ordering."""
    import numpy as np
    import pandas as pd
    from kdtb.learning.features import FEATURE_NAMES

    rng = np.random.RandomState(seed)
    rows = []
    for h in range(n_halves):
        year = 2022 + h // 2
        month = 3 if h % 2 == 0 else 9
        for _ in range(n_per_half):
            feats = rng.rand(len(FEATURE_NAMES))
            feats[0] = 1.0 if rng.rand() > 0.5 else 0.0
            base = 0.02 if feats[0] > 0.5 else -0.02
            ret = base + rng.randn() * 0.004
            rows.append(list(feats) + [f"{year}-{month:02d}-15", ret, int(ret > 0)])
    df = pd.DataFrame(
        rows, columns=FEATURE_NAMES + ["event_date", "realized_net_return", "label"]
    )
    df["event_date"] = pd.to_datetime(df["event_date"])
    return df
