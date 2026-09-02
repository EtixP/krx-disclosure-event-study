from __future__ import annotations

import csv
from datetime import date

import pandas as pd
import pytest

from kdtb.data.benchmarks import (
    BENCHMARK_SOURCE,
    BenchmarkDataError,
    NaverBenchmarkClient,
    add_benchmark_context,
    normalize_benchmark_history,
    require_benchmark_columns,
)
from scripts.backfill_event_study_benchmarks import enrich_event_csv


def _history(*rows: tuple[str, str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": observed,
                "market": market,
                "benchmark_symbol": market,
                "close": close,
                "source": BENCHMARK_SOURCE,
            }
            for market, observed, close in rows
        ]
    )


def _event(**overrides) -> dict:
    row = {
        "market": "KOSPI",
        "t0_date": "2026-01-02",
        # The stock did not record a T+1 close until Jan 6.  Jan 5 exists in
        # the index but must not be selected by positional offset.
        "t+1_date": "2026-01-06",
        "t+2_date": "2026-01-07",
        "t+5_date": "2026-01-12",
        "ret_1d": 0.05,
        "ret_2d": 0.08,
        "ret_5d": 0.12,
    }
    row.update(overrides)
    return row


def test_alignment_uses_stock_actual_dates_and_market_benchmark():
    history = _history(
        ("KOSPI", "2026-01-02", 100.0),
        ("KOSPI", "2026-01-05", 150.0),
        ("KOSPI", "2026-01-06", 102.0),
        ("KOSPI", "2026-01-07", 104.0),
        ("KOSPI", "2026-01-12", 110.0),
        ("KOSDAQ", "2026-01-02", 200.0),
        ("KOSDAQ", "2026-01-06", 240.0),
        ("KOSDAQ", "2026-01-07", 250.0),
        ("KOSDAQ", "2026-01-12", 260.0),
    )

    result = add_benchmark_context(pd.DataFrame([_event()]), history, strict=True)
    row = result.iloc[0]

    assert row["benchmark_symbol"] == "KOSPI"
    assert row["benchmark_t1_close"] == 102.0
    assert row["benchmark_ret_1d"] == pytest.approx(0.02)
    assert row["abnormal_ret_1d"] == pytest.approx(0.03)
    assert row["abnormal_ret_5d"] == pytest.approx(0.02)


def test_missing_exact_date_is_missing_and_strict_mode_fails():
    history = _history(
        ("KOSPI", "2026-01-02", 100.0),
        ("KOSPI", "2026-01-06", 102.0),
        ("KOSPI", "2026-01-07", 104.0),
    )

    result = add_benchmark_context(pd.DataFrame([_event()]), history)
    assert pd.isna(result.loc[0, "benchmark_t5_close"])
    assert pd.isna(result.loc[0, "benchmark_ret_5d"])
    assert pd.isna(result.loc[0, "abnormal_ret_5d"])
    assert result.loc[0, "benchmark_alignment"] == "missing"

    with pytest.raises(BenchmarkDataError, match="missing exact-date"):
        add_benchmark_context(pd.DataFrame([_event()]), history, strict=True)
    with pytest.raises(BenchmarkDataError, match="missing exact-date"):
        require_benchmark_columns(result)


def test_blank_stock_observation_stays_missing_without_float_conversion():
    history = _history(
        ("KOSPI", "2026-01-02", 100.0),
        ("KOSPI", "2026-01-06", 102.0),
        ("KOSPI", "2026-01-07", 104.0),
    )
    event = _event(**{"t+5_date": "", "ret_5d": ""})

    result = add_benchmark_context(pd.DataFrame([event]), history, strict=True)

    assert pd.isna(result.loc[0, "benchmark_t5_close"])
    assert pd.isna(result.loc[0, "benchmark_ret_5d"])
    assert pd.isna(result.loc[0, "abnormal_ret_5d"])
    assert result.loc[0, "benchmark_alignment"] == "complete"


@pytest.mark.parametrize("bad_close", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_benchmark_close_is_rejected_before_alignment(bad_close):
    history = _history(("KOSPI", "2026-01-02", bad_close))

    with pytest.raises(BenchmarkDataError, match="non-finite"):
        normalize_benchmark_history(history)
    with pytest.raises(BenchmarkDataError, match="non-finite"):
        add_benchmark_context(pd.DataFrame([_event()]), history, strict=True)


def test_require_benchmark_columns_rejects_infinite_close():
    history = _history(
        ("KOSPI", "2026-01-02", 100.0),
        ("KOSPI", "2026-01-06", 102.0),
        ("KOSPI", "2026-01-07", 104.0),
        ("KOSPI", "2026-01-12", 110.0),
    )
    result = add_benchmark_context(pd.DataFrame([_event()]), history, strict=True)
    result.loc[0, "benchmark_t1_close"] = float("inf")

    with pytest.raises(BenchmarkDataError, match="missing exact-date"):
        require_benchmark_columns(result)


def test_csv_enrichment_preserves_every_existing_field_lexeme(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text(
        "corp_code,stock_code,market,t0_date,t+1_date,t+2_date,t+5_date,ret_1d,ret_2d,ret_5d\n"
        "00123456,005930,KOSPI,2026-01-02,2026-01-06,2026-01-07,2026-01-12,"
        "0.10000000000000001,-0.020000000000000004,1.2300\n",
        encoding="utf-8",
    )
    with path.open(encoding="utf-8", newline="") as handle:
        original_reader = csv.DictReader(handle)
        original_fields = list(original_reader.fieldnames or [])
        original_rows = list(original_reader)
    history = _history(
        ("KOSPI", "2026-01-02", 100.0),
        ("KOSPI", "2026-01-06", 102.0),
        ("KOSPI", "2026-01-07", 104.0),
        ("KOSPI", "2026-01-12", 110.0),
    )

    assert enrich_event_csv(path, history) == (1, 1)
    assert enrich_event_csv(path, history) == (1, 1)

    with path.open(encoding="utf-8", newline="") as handle:
        enriched_reader = csv.DictReader(handle)
        enriched_fields = list(enriched_reader.fieldnames or [])
        enriched_rows = list(enriched_reader)
    assert enriched_fields[: len(original_fields)] == original_fields
    assert [
        {column: row[column] for column in original_fields} for row in enriched_rows
    ] == original_rows
    assert enriched_rows[0]["corp_code"] == "00123456"
    assert enriched_rows[0]["stock_code"] == "005930"
    assert enriched_rows[0]["ret_2d"] == "-0.020000000000000004"


class _Response:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status != 200:
            raise RuntimeError(self.status)

    def json(self):
        return self._payload


def test_naver_client_normalizes_paginated_history_without_zero_fill():
    pages = {
        1: [
            {"localTradedAt": "2026-01-07", "closePrice": "2,500.25"},
            {"localTradedAt": "2026-01-06", "closePrice": "2,480.10"},
        ],
        2: [
            {"localTradedAt": "2026-01-05", "closePrice": "2,470.00"},
            {"localTradedAt": "2026-01-02", "closePrice": "2,450.00"},
        ],
    }

    def fake_get(_url, *, params, **_kwargs):
        return _Response(pages.get(params["page"], []))

    frame = NaverBenchmarkClient(
        request_get=fake_get, sleep_seconds=0, page_size=2
    ).fetch("KOSPI", date(2026, 1, 2), date(2026, 1, 6))

    assert frame["date"].tolist() == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert frame["close"].tolist() == [2450.0, 2470.0, 2480.1]
    assert set(frame["source"]) == {BENCHMARK_SOURCE}


def test_naver_client_rejects_unsupported_market_and_bad_payload():
    client = NaverBenchmarkClient(
        request_get=lambda *_args, **_kwargs: _Response({"not": "a list"}),
        sleep_seconds=0,
    )
    with pytest.raises(BenchmarkDataError, match="unsupported"):
        client.fetch("KONEX", date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(BenchmarkDataError, match="not a list"):
        client.fetch("KOSPI", date(2026, 1, 1), date(2026, 1, 2))
