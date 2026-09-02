from __future__ import annotations

import pandas as pd
import pytest

from kdtb.backtest.cost_model import CostModel
from kdtb.data.benchmarks import BENCHMARK_SOURCE, BenchmarkDataError
from kdtb.learning.dataset import load_mock_trades
from kdtb.learning.features import FEATURE_NAMES, extract_features
from scripts.run_intraday_walkforward import apply_timeaware_returns


def _row() -> dict:
    return {
        "id": 1,
        "event_date": "2025-06-02",
        "market": "KOSPI",
        "t0_date": "2025-06-02",
        "t+1_date": "2025-06-03",
        "t+5_date": "2025-06-10",
        "t0_close": 95.0,
        "t+1_close": 100.0,
        "t+5_close": 110.0,
        "benchmark_source": BENCHMARK_SOURCE,
        "benchmark_symbol": "KOSPI",
        "benchmark_t0_close": 990.0,
        "benchmark_t1_close": 1000.0,
        "benchmark_t5_close": 1050.0,
        "benchmark_alignment": "complete",
    }


def test_mock_trade_exposes_raw_and_uses_abnormal_reward_by_default(tmp_path):
    path = tmp_path / "events.csv"
    pd.DataFrame([_row()]).to_csv(path, index=False)

    result = load_mock_trades(
        "buyback", csv_path=str(path), db_path=str(tmp_path / "missing.db")
    )
    cost = CostModel().roundtrip_cost(
        1.0, buy_date="2025-06-03", sell_date="2025-06-10", market="KOSPI"
    )

    assert result.loc[0, "realized_raw_net_return"] == pytest.approx(0.10 - cost)
    assert result.loc[0, "realized_abnormal_net_return"] == pytest.approx(
        0.10 - 0.05 - cost
    )
    assert result.loc[0, "realized_net_return"] == pytest.approx(
        result.loc[0, "realized_abnormal_net_return"]
    )
    assert result.loc[0, "return_basis"] == "abnormal"


def test_explicit_raw_mock_trade_mode_preserves_legacy_reproduction(tmp_path):
    path = tmp_path / "events.csv"
    row = _row()
    for key in list(row):
        if key.startswith("benchmark_"):
            del row[key]
    pd.DataFrame([row]).to_csv(path, index=False)

    result = load_mock_trades(
        "buyback",
        csv_path=str(path),
        db_path=str(tmp_path / "missing.db"),
        return_basis="raw",
    )

    assert result.loc[0, "realized_net_return"] == pytest.approx(
        result.loc[0, "realized_raw_net_return"]
    )
    assert pd.isna(result.loc[0, "realized_abnormal_net_return"])


def test_intraday_rewards_subtract_benchmark_on_matching_entry_and_exit_dates():
    frame = pd.DataFrame([{**_row(), "filing_mins": 11 * 60}])

    result = apply_timeaware_returns(frame)

    intraday_benchmark_return = 1050.0 / 990.0 - 1.0
    uniform_benchmark_return = 1050.0 / 1000.0 - 1.0
    assert result.loc[0, "ret_timeaware_abnormal"] == pytest.approx(
        result.loc[0, "ret_timeaware_raw"] - intraday_benchmark_return
    )
    assert result.loc[0, "ret_uniform_abnormal"] == pytest.approx(
        result.loc[0, "ret_uniform_raw"] - uniform_benchmark_return
    )
    assert result.loc[0, "ret_timeaware"] == pytest.approx(
        result.loc[0, "ret_timeaware_abnormal"]
    )


def test_abnormal_consumers_fail_when_benchmark_is_missing(tmp_path):
    row = _row()
    del row["benchmark_t5_close"]
    path = tmp_path / "events.csv"
    pd.DataFrame([row]).to_csv(path, index=False)

    with pytest.raises(BenchmarkDataError, match="requires columns"):
        load_mock_trades(
            "buyback", csv_path=str(path), db_path=str(tmp_path / "missing.db")
        )
    with pytest.raises(BenchmarkDataError, match="requires columns"):
        apply_timeaware_returns(pd.DataFrame([{**row, "filing_mins": 600}]))


def test_future_benchmark_outcomes_are_not_decision_time_features():
    row = _row()
    changed_future = {**row, "benchmark_t5_close": 999999.0}

    assert all("benchmark" not in feature for feature in FEATURE_NAMES)
    assert extract_features(row) == extract_features(changed_future)
