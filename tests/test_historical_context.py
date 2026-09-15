from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from kdtb.backtest.cost_model import CostModel
from kdtb.context import (
    EVENT_TYPE_CATEGORIES,
    HistoricalContextError,
    HistoricalContextPolicy,
    HistoricalContextService,
)
from kdtb.data.benchmarks import BENCHMARK_SOURCE, BenchmarkDataError
from kdtb.research.inference import issuer_clustered_mean_ci
from kdtb.schemas.economic_event import (
    EconomicEvent,
    EventIssuer,
    EventSourceProvenance,
)
from scripts.analyze_research_inference import _category_scenario_frame

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _event(
    receipt_no: str = "20240120000001",
    *,
    event_type: str = "major_supply_contract",
    market: str = "KOSPI",
    receipt_time: datetime | None = None,
) -> EconomicEvent:
    receipt_time = receipt_time or datetime.strptime(receipt_no[:8], "%Y%m%d")
    return EconomicEvent(
        economic_event_id=f"dart:{receipt_no}",
        event_type=event_type,
        issuer=EventIssuer(
            corp_code="00126380",
            corp_name="current issuer",
            stock_code="005930",
        ),
        market=market,
        primary_receipt_no=receipt_no,
        original_timestamp=receipt_time,
        latest_update_timestamp=receipt_time,
        status="active",
        normalized_fields={},
        source_provenance=(
            EventSourceProvenance(
                receipt_no=receipt_no,
                report_name="단일판매ㆍ공급계약체결",
                receipt_timestamp=receipt_time,
                source="DART",
                action="original",
                relationship="primary",
                raw_payload_sha256="a" * 64,
            ),
        ),
        lineage_status="self_contained",
    )


def _row(
    receipt_no: str,
    *,
    corp_code: str,
    stock_code: str,
    market: str = "KOSPI",
    event_date: str = "2024-01-01",
    entry_date: str = "2024-01-02",
    exit_date: str = "2024-01-08",
    entry_close: float | None = 100.0,
    exit_close: float | None = 110.0,
    benchmark_entry: float | None = 1_000.0,
    benchmark_exit: float | None = 1_020.0,
) -> dict[str, object]:
    return {
        "receipt_no": receipt_no,
        "corp_code": corp_code,
        "corp_name": f"issuer-{corp_code}",
        "stock_code": stock_code,
        "report_name": "단일판매ㆍ공급계약체결",
        "event_date": event_date,
        "market": market,
        "t+1_date": entry_date,
        "t+5_date": exit_date,
        "t+1_close": entry_close,
        "t+5_close": exit_close,
        "benchmark_source": BENCHMARK_SOURCE,
        "benchmark_symbol": market,
        "benchmark_t1_close": benchmark_entry,
        "benchmark_t5_close": benchmark_exit,
        "benchmark_alignment": "complete",
    }


def _write_supply_contracts(tmp_path, rows):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "event_study_supply_contract.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return data_dir, path


def _fixture_rows() -> list[dict[str, object]]:
    return [
        _row(
            "20240101000001",
            corp_code="00000001",
            stock_code="000001",
        ),
        _row(
            "20240102000001",
            corp_code="00000002",
            stock_code="000002",
            event_date="2024-01-02",
            entry_date="2024-01-03",
            exit_date="2024-01-09",
            exit_close=95.0,
            benchmark_exit=1_010.0,
        ),
        _row(
            "20240103000001",
            corp_code="00000001",
            stock_code="000001",
            event_date="2024-01-03",
            entry_date="2024-01-04",
            exit_date="2024-01-10",
            exit_close=105.0,
            benchmark_exit=1_000.0,
        ),
        _row(
            "20240104000001",
            corp_code="00000004",
            stock_code="000004",
            market="KOSDAQ",
            event_date="2024-01-04",
            entry_date="2024-01-05",
            exit_date="2024-01-11",
        ),
        _row(
            "20240120000001",
            corp_code="00000005",
            stock_code="000005",
            event_date="2024-01-20",
            entry_date="2024-01-22",
            exit_date="2024-01-26",
        ),
        _row(
            "20240105000001",
            corp_code="00000006",
            stock_code="000006",
            event_date="2024-01-05",
            entry_date="2024-01-08",
            exit_date="2024-01-20",
        ),
        _row(
            "20240106000001",
            corp_code="00000007",
            stock_code="000007",
            event_date="2024-01-06",
            entry_date="2024-01-08",
            exit_date="2024-01-12",
            exit_close=None,
        ),
    ]


def test_query_is_auditable_and_reuses_verified_phase_zero_metrics(tmp_path):
    data_dir, path = _write_supply_contracts(tmp_path, _fixture_rows())
    policy = HistoricalContextPolicy(n_resamples=1_000, random_state=17)
    service = HistoricalContextService(data_dir=data_dir, policy=policy)
    assessed_at = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    result = service.query(_event(), assessed_at=assessed_at)

    assert result.status == "available"
    assert result.selection.source_rows == 7
    assert result.selection.same_market_rows == 6
    assert result.selection.prior_receipt_rows == 5
    assert result.selection.current_lineage_rows == 0
    assert result.selection.missing_stock_outcome_rows == 1
    assert result.selection.future_or_same_day_outcome_rows == 1
    assert result.selection.selected_receipt_nos == (
        "20240101000001",
        "20240102000001",
        "20240103000001",
    )
    assert result.dataset.dataset_id == "committed-phase0-event-study"
    assert result.dataset.dataset_version == "m1.4-v1"
    assert result.dataset.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.dataset.source_max_outcome_date.isoformat() == "2024-01-26"
    assert result.dataset.outcomes_strictly_before.isoformat() == "2024-01-20"
    assert [item.stock_code for item in result.comparables] == [
        "000001",
        "000002",
        "000001",
    ]

    cost = CostModel().roundtrip_cost(
        1.0,
        buy_date="2024-01-02",
        sell_date="2024-01-08",
        market="KOSPI",
    )
    expected = [0.10 - 0.02 - cost, -0.05 - 0.01 - cost, 0.05 - 0.0 - cost]
    assert [item.abnormal_net_return for item in result.comparables] == pytest.approx(
        expected
    )
    inference = issuer_clustered_mean_ci(
        expected,
        ["000001", "000002", "000001"],
        n_resamples=1_000,
        random_state=17,
    )
    assert result.metrics is not None
    assert result.metrics.sample_size == 3
    assert result.metrics.issuer_count == 2
    assert result.metrics.mean_abnormal_net_return == pytest.approx(sum(expected) / 3)
    assert result.metrics.median_abnormal_net_return == pytest.approx(
        sorted(expected)[1]
    )
    assert result.metrics.positive_fraction == pytest.approx(2 / 3)
    assert result.metrics.ci_lower == pytest.approx(inference["ci_lower"])
    assert result.metrics.ci_upper == pytest.approx(inference["ci_upper"])
    assert result.metrics.cost_adjusted is True
    assert result.is_trade_recommendation is False
    assert result.trading_recommendation is None

    replayed = EconomicEvent.model_validate_json(_event().model_dump_json())
    assert service.query(replayed, assessed_at=assessed_at) == result


@pytest.mark.parametrize(("event_type", "category"), EVENT_TYPE_CATEGORIES.items())
@pytest.mark.parametrize("market", ("KOSPI", "KOSDAQ"))
def test_committed_context_reconciles_phase_zero_rows_and_metrics(
    event_type, category, market
):
    service = HistoricalContextService(
        data_dir=PROJECT_ROOT / "data",
        policy=HistoricalContextPolicy(n_resamples=2),
    )
    event = _event(
        "20260908000001",
        event_type=event_type,
        market=market,
    )

    result = service.query(
        event,
        assessed_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
    )
    expected = _category_scenario_frame(category)
    expected = expected.loc[expected["market"] == market]

    assert result.status == "available"
    assert result.dataset.category == category
    assert result.selection.selected_rows == len(expected)
    assert result.metrics is not None
    assert result.metrics.sample_size == len(expected)
    assert result.metrics.issuer_count == expected["stock_code"].nunique()
    assert result.metrics.mean_abnormal_net_return == pytest.approx(
        expected["realistic_abnormal_net"].mean()
    )
    assert result.metrics.median_abnormal_net_return == pytest.approx(
        expected["realistic_abnormal_net"].median()
    )
    assert result.metrics.positive_fraction == pytest.approx(
        (expected["realistic_abnormal_net"] > 0).mean()
    )


def test_same_day_and_future_outcomes_never_enter_replay_context(tmp_path):
    rows = _fixture_rows()
    data_dir, _ = _write_supply_contracts(tmp_path, rows)
    service = HistoricalContextService(
        data_dir=data_dir,
        policy=HistoricalContextPolicy(n_resamples=100, random_state=1),
    )

    result = service.query(
        _event(),
        assessed_at=datetime(2024, 1, 20, 23, 59, tzinfo=timezone.utc),
    )

    assert all(item.exit_date.isoformat() < "2024-01-21" for item in result.comparables)
    assert "20240120000001" not in result.selection.selected_receipt_nos
    assert "20240105000001" in result.selection.selected_receipt_nos


def test_current_canonical_lineage_cannot_be_its_own_comparable(tmp_path):
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    original_receipt = "20240101000001"
    correction_receipt = "20240120000002"
    original_time = datetime(2024, 1, 1)
    correction_time = datetime(2024, 1, 20)
    event = EconomicEvent(
        economic_event_id=f"dart:{original_receipt}",
        event_type="major_supply_contract",
        issuer=EventIssuer(
            corp_code="00000001",
            corp_name="issuer-00000001",
            stock_code="000001",
        ),
        market="KOSPI",
        primary_receipt_no=original_receipt,
        related_receipt_nos=(correction_receipt,),
        original_timestamp=original_time,
        latest_update_timestamp=correction_time,
        status="amended",
        normalized_fields={},
        source_provenance=(
            EventSourceProvenance(
                receipt_no=original_receipt,
                report_name="단일판매ㆍ공급계약체결",
                receipt_timestamp=original_time,
                source="DART",
                action="original",
                relationship="primary",
                raw_payload_sha256="a" * 64,
            ),
            EventSourceProvenance(
                receipt_no=correction_receipt,
                report_name="[기재정정]단일판매ㆍ공급계약체결",
                receipt_timestamp=correction_time,
                source="DART",
                action="correction",
                relationship="dart_family",
                relationship_source_receipt_no=correction_receipt,
                relationship_source_url=(
                    "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
                    f"{correction_receipt}"
                ),
                relationship_evidence_sha256="c" * 64,
                raw_payload_sha256="b" * 64,
            ),
        ),
        lineage_status="complete",
    )

    result = HistoricalContextService(
        data_dir=data_dir,
        policy=HistoricalContextPolicy(n_resamples=100),
    ).query(
        event,
        assessed_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
    )

    assert result.selection.current_lineage_rows == 1
    assert original_receipt not in result.selection.selected_receipt_nos


def test_incomplete_current_lineage_fails_instead_of_risking_self_comparison(
    tmp_path,
):
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    event = _event().model_copy(update={"lineage_status": "unresolved"})

    with pytest.raises(HistoricalContextError, match="current-event lineage"):
        HistoricalContextService(data_dir=data_dir).query(
            event,
            assessed_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
        )


def test_context_rejects_decision_before_same_day_event_source(tmp_path):
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    event = _event(receipt_time=datetime(2024, 1, 20, 15))

    with pytest.raises(ValueError, match="latest source timestamp"):
        HistoricalContextService(data_dir=data_dir).query(
            event,
            assessed_at=datetime(2024, 1, 20, 3, tzinfo=timezone.utc),
        )


def test_insufficient_history_is_explicit_and_keeps_sample_size(tmp_path):
    data_dir, _ = _write_supply_contracts(tmp_path, [_fixture_rows()[0]])

    result = HistoricalContextService(data_dir=data_dir).query(
        _event(),
        assessed_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
    )

    assert result.status == "insufficient_history"
    assert result.metrics is None
    assert result.selection.selected_rows == 1
    assert len(result.comparables) == 1


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    [
        (
            "benchmark_t5_close",
            None,
            BenchmarkDataError,
            "missing exact-date benchmark",
        ),
        (
            "benchmark_t5_close",
            float("inf"),
            BenchmarkDataError,
            "missing exact-date benchmark",
        ),
        (
            "benchmark_t5_close",
            -1.0,
            HistoricalContextError,
            "non-finite or non-positive",
        ),
        (
            "benchmark_source",
            "UNVERIFIED",
            HistoricalContextError,
            "unverified benchmark provider",
        ),
        (
            "benchmark_symbol",
            "KOSDAQ",
            HistoricalContextError,
            "does not match the listing market",
        ),
        (
            "benchmark_alignment",
            "incomplete",
            BenchmarkDataError,
            "missing exact-date benchmark",
        ),
        (
            "t+5_close",
            float("inf"),
            HistoricalContextError,
            "non-finite or non-positive",
        ),
    ],
)
def test_selected_invalid_market_data_fails_instead_of_becoming_zero(
    tmp_path, field, value, error_type, message
):
    rows = _fixture_rows()
    rows[0][field] = value
    data_dir, _ = _write_supply_contracts(tmp_path, rows)

    with pytest.raises(error_type, match=message):
        HistoricalContextService(data_dir=data_dir).query(
            _event(),
            assessed_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "policy_kwargs",
    [
        {"confidence_level": float("nan")},
        {"confidence_level": 1.0},
        {"n_resamples": True},
        {"n_resamples": 1},
        {"random_state": 1.5},
    ],
)
def test_context_policy_rejects_invalid_inference_configuration(policy_kwargs):
    with pytest.raises((TypeError, ValueError)):
        HistoricalContextPolicy(**policy_kwargs)


def test_unsupported_event_type_and_market_fail_explicitly(tmp_path):
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    service = HistoricalContextService(data_dir=data_dir)
    assessed_at = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)

    with pytest.raises(HistoricalContextError, match="no verified historical category"):
        service.query(_event(event_type="other"), assessed_at=assessed_at)
    with pytest.raises(HistoricalContextError, match="benchmark/cost context"):
        service.query(_event(market="OTHER"), assessed_at=assessed_at)


def test_context_result_is_frozen(tmp_path):
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    result = HistoricalContextService(
        data_dir=data_dir,
        policy=HistoricalContextPolicy(n_resamples=100),
    ).query(
        _event(),
        assessed_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
    )

    with pytest.raises(ValidationError, match="frozen"):
        result.status = "insufficient_history"

    payload = result.model_dump(mode="json")
    payload["selection"]["selected_rows_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="selected-row hash"):
        result.model_validate_json(json.dumps(payload))


def test_context_result_rejects_rehashed_contradictory_row(tmp_path):
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    result = HistoricalContextService(
        data_dir=data_dir,
        policy=HistoricalContextPolicy(n_resamples=100),
    ).query(
        _event(),
        assessed_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
    )

    payload = result.model_dump(mode="json")
    payload["comparables"][0]["abnormal_net_return"] += 0.5
    serialized_rows = json.dumps(
        payload["comparables"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["selection"]["selected_rows_sha256"] = hashlib.sha256(
        serialized_rows
    ).hexdigest()

    with pytest.raises(ValidationError, match="abnormal net return"):
        result.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "delta"),
    [
        ("mean_stock_gross_return", 0.0001),
        ("mean_benchmark_gross_return", 0.0001),
        ("mean_modeled_cost_fraction", 0.0001),
        ("mean_abnormal_net_return", 1.0),
        ("median_abnormal_net_return", 0.0001),
        ("std_abnormal_net_return", 0.0001),
        ("positive_fraction", 0.0001),
        ("profit_factor", 0.0001),
        ("ci_lower", 0.0001),
        ("ci_upper", 0.0001),
    ],
)
def test_context_result_rejects_contradictory_derived_metrics(tmp_path, field, delta):
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    result = HistoricalContextService(
        data_dir=data_dir,
        policy=HistoricalContextPolicy(n_resamples=100),
    ).query(
        _event(),
        assessed_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
    )

    payload = result.model_dump(mode="json")
    payload["metrics"][field] += delta

    with pytest.raises(ValidationError, match=field):
        result.model_validate_json(json.dumps(payload))
