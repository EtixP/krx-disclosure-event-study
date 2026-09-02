from __future__ import annotations

import pandas as pd
import pytest

from kdtb.data.benchmarks import BENCHMARK_SOURCE, BenchmarkDataError
from scripts.analyze_event_category import analyze


def _row(*, event_id: int, year: int, market: str = "KOSPI") -> dict:
    return {
        "id": event_id,
        "stock_code": f"{event_id:06d}",
        "event_date": f"{year}-06-01",
        "market": market,
        "t0_date": f"{year}-06-01",
        "t+1_date": f"{year}-06-02",
        "t+2_date": f"{year}-06-03",
        "t+5_date": f"{year}-06-08",
        "t0_close": 100.0,
        "t+1_close": 101.0,
        "t+2_close": 102.0,
        "t+5_close": 105.0,
        "ret_1d": 0.01,
        "ret_2d": 0.02,
        "ret_5d": 0.05,
        "benchmark_source": BENCHMARK_SOURCE,
        "benchmark_symbol": market,
        "benchmark_t0_close": 1000.0,
        "benchmark_t1_close": 1010.0,
        "benchmark_t2_close": 1020.0,
        "benchmark_t5_close": 1030.0,
        "benchmark_ret_1d": 0.01,
        "benchmark_ret_2d": 0.02,
        "benchmark_ret_5d": 0.03,
        "abnormal_ret_1d": 0.0,
        "abnormal_ret_2d": 0.0,
        "abnormal_ret_5d": 0.02,
        "benchmark_alignment": "complete",
    }


def test_category_analysis_applies_per_row_historical_costs(tmp_path):
    path = tmp_path / "events.csv"
    pd.DataFrame([_row(event_id=1, year=2021), _row(event_id=2, year=2025)]).to_csv(
        path, index=False
    )

    result = analyze("fixture", str(path))

    assert result["cost_fraction"]["t5_min"] == pytest.approx(0.00283)
    assert result["cost_fraction"]["t5_max"] == pytest.approx(0.00363)
    expected_mean_pct = ((0.05 - 0.00363) + (0.05 - 0.00283)) / 2 * 100
    assert result["aggregate"]["t5_net"]["mean_pct"] == pytest.approx(
        expected_mean_pct
    )
    expected_abnormal_mean_pct = ((0.02 - 0.00363) + (0.02 - 0.00283)) / 2 * 100
    assert result["aggregate"]["t5_abnormal_net"]["mean_pct"] == pytest.approx(
        expected_abnormal_mean_pct
    )
    assert result["verdict_basis"] == "benchmark_adjusted_net"


def test_category_analysis_rejects_missing_execution_dates(tmp_path):
    path = tmp_path / "events.csv"
    row = _row(event_id=1, year=2024)
    del row["t+5_date"]
    pd.DataFrame([row]).to_csv(path, index=False)

    with pytest.raises(ValueError, match=r"t\+5_date"):
        analyze("fixture", str(path))


def test_category_analysis_rejects_missing_benchmark_context(tmp_path):
    path = tmp_path / "events.csv"
    row = _row(event_id=1, year=2024)
    del row["benchmark_t5_close"]
    pd.DataFrame([row]).to_csv(path, index=False)

    with pytest.raises(BenchmarkDataError, match="requires columns"):
        analyze("fixture", str(path))
