"""Explicitly dated planted-edge/null fixtures for the strict research learner."""

from __future__ import annotations

import numpy as np
import pandas as pd

from kdtb.learning.features import FEATURE_NAMES


def synthetic_trades(n_per_half=150, n_halves=8, *, edge=True, seed=0):
    rng = np.random.RandomState(seed)
    rows = []
    for half in range(n_halves):
        year = 2022 + half // 2
        month = 3 if half % 2 == 0 else 9
        days = pd.bdate_range(f"{year}-{month:02d}-01", periods=(n_per_half + 2) // 3)
        for index in range(n_per_half):
            features = rng.rand(len(FEATURE_NAMES))
            features[0] = float(rng.rand() > 0.5)
            base = (0.02 if features[0] else -0.02) if edge else 0.0
            reward = base + rng.randn() * 0.004
            decision = days[index // 3]
            observed = decision - pd.offsets.BDay(1)
            row_id = f"synthetic:{half:03d}:{index:06d}"
            rows.append(
                {
                    **dict(zip(FEATURE_NAMES, features)),
                    "id": row_id,
                    "row_id": row_id,
                    "receipt_no": row_id,
                    "corp_code": f"{index:08d}",
                    "stock_code": f"{index:06d}",
                    "event_date": observed,
                    "t0_date": observed,
                    "feature_observation_date": observed,
                    "entry_date": decision,
                    "decision_date": decision,
                    "label_end_date": decision + pd.offsets.BDay(4),
                    "realized_net_return": reward,
                    "label": int(reward > 0),
                }
            )
    return pd.DataFrame(rows)
