from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from kdtb.learning.chronology import ObservedSessionHorizon, require_chronology, sha256
from kdtb.learning.dataset import load_mock_trades
from kdtb.learning.features import FEATURE_NAMES
from kdtb.learning.policy import LearnedPolicy
from kdtb.learning.synthetic import synthetic_trades
from kdtb.learning.walk_forward_trainer import make_folds, run_walk_forward


def _row(**changes):
    return {
        "id": "0000001",
        "receipt_no": "20240102000001",
        "corp_code": "00123456",
        "stock_code": "001234",
        "event_date": "2024-01-02",
        "market": "KOSPI",
        "t0_date": "2024-01-02",
        "t+1_date": "2024-01-03",
        "t+5_date": "2024-01-09",
        "t0_close": 95.0,
        "t+1_close": 100.0,
        "t+5_close": 110.0,
        **changes,
    }


def _load(tmp_path, rows, **kwargs):
    path = tmp_path / "input.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return load_mock_trades("buyback", csv_path=str(path), return_basis="raw", **kwargs)


def _fit(frame, cutoff="2023-01-01"):
    return LearnedPolicy(random_state=0).fit(
        frame[FEATURE_NAMES].to_numpy(float),
        frame["realized_net_return"].to_numpy(float),
        decision_dates=frame["decision_date"],
        label_end_dates=frame["label_end_date"],
        row_ids=frame["row_id"],
        cutoff=cutoff,
    )


def test_strict_dataset_preserves_identity_and_daily_dates(tmp_path):
    frame = _load(tmp_path, [_row()])
    row = frame.iloc[0]
    assert row["id"] == "0000001"
    assert row["stock_code"] == "001234"
    assert row["corp_code"] == "00123456"
    assert row["event_id"] == row["id"]
    assert row["issuer_id"] == row["corp_code"]
    assert (
        row["feature_observation_date"] < row["decision_date"] < row["label_end_date"]
    )
    assert row["decision_date"] == row["entry_date"] == row["t+1_date"]
    assert frame.attrs["admission"]["input_sha256"] == sha256(tmp_path / "input.csv")
    assert not frame.attrs["admission"]["db_used"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"t0_date": None}, "missing_date"),
        ({"t+5_date": "invalid"}, "malformed_date"),
        ({"t0_date": "2024-01-02T10:00:00"}, "malformed_date"),
        ({"t+5_date": "2024-01-01"}, "reversed_window"),
        ({"t0_date": "2023-12-28"}, "reversed_window"),
        ({"t0_date": "2024-01-03", "t+1_date": "2024-01-04"}, "t0_session_mismatch"),
        ({"t+1_date": "2024-01-04"}, "entry_session_mismatch"),
        ({"t+5_date": "2024-01-10"}, "label_session_mismatch"),
        ({"event_date": "2020-01-01"}, "event_before_calendar_horizon"),
        (
            {
                "event_date": "2028-01-03",
                "t0_date": "2028-01-03",
                "t+1_date": "2028-01-04",
                "t+5_date": "2028-01-10",
            },
            "insufficient_calendar_horizon",
        ),
        ({"market": "UNKNOWN"}, "unsupported_market"),
        ({"stock_code": None}, "missing_identity"),
        ({"t+1_close": 0}, "invalid_stock_price"),
        ({"t0_close": "nonnumeric"}, "invalid_price_or_benchmark"),
    ],
)
def test_bad_rows_quarantine_with_stable_reasons(tmp_path, changes, reason):
    frame = _load(tmp_path, [_row(**changes)])
    assert frame.empty
    assert frame.attrs["admission"]["reason_counts"] == {reason: 1}


def test_missing_columns_and_unversioned_supply_fail_before_db_access(
    tmp_path, monkeypatch
):
    row = _row()
    del row["receipt_no"]
    with pytest.raises(ValueError, match="requires columns.*receipt_no"):
        _load(tmp_path, [row])
    import sqlite3

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: pytest.fail("strict path opened DB")
    )
    with pytest.raises(ValueError, match="supply_contract_unversioned_enrichment"):
        load_mock_trades("supply_contract")
    _load(tmp_path, [_row()])


def test_duplicate_identity_quarantines_every_copy(tmp_path):
    frame = _load(tmp_path, [_row(), _row(t0_close=88.0)])
    assert frame.empty
    assert frame.attrs["admission"]["reason_counts"] == {"duplicate_identity": 2}


def test_observed_sessions_handle_weekend_and_recorded_holiday(tmp_path):
    frame = _load(tmp_path, [_row(event_date="2023-12-30")])
    assert len(frame) == 1  # weekend + New Year's Day -> first observed Jan 2
    horizon = ObservedSessionHorizon()
    assert horizon.window("KOSPI", pd.Timestamp("2023-12-30")) == tuple(
        pd.Timestamp(day) for day in ("2024-01-02", "2024-01-03", "2024-01-09")
    )
    assert "not authoritative" in horizon.metadata()["semantics"]


def test_changed_missing_duplicate_or_malformed_calendar_fails(tmp_path):
    original = Path("data/benchmark_indices.csv")
    frame = pd.read_csv(original)
    path = tmp_path / "calendar.csv"
    frame.iloc[1:].to_csv(path, index=False)
    with pytest.raises(ValueError, match="hash_mismatch"):
        ObservedSessionHorizon(path)
    with pytest.raises(ValueError, match="market_gap"):
        ObservedSessionHorizon(path, expected_sha256=sha256(path))
    pd.concat([frame, frame.iloc[:1]]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="calendar_values"):
        ObservedSessionHorizon(path, expected_sha256=sha256(path))
    frame.loc[0, "date"] = "invalid"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="calendar_values"):
        ObservedSessionHorizon(path, expected_sha256=sha256(path))


def test_real_anomalous_t0_and_promotion_boundary():
    frame = load_mock_trades("buyback")
    excluded = frame.attrs["admission"]["exclusions"]
    anomaly = next(row for row in excluded if row["receipt_no"] == "20231117000074")
    assert anomaly["reason"] == "t0_session_mismatch"
    assert anomaly["t0_date"] == "2024-03-13"
    boundary = frame[frame.receipt_no == "20231228000427"].iloc[0]
    assert boundary.label_end_date == pd.Timestamp("2024-01-08")
    report = run_walk_forward(frame)
    fold = next(row for row in report.folds if row.period == "2024H1")
    assert fold.test_period_start == "2024-01-01"
    assert boundary.row_id not in fold.promotion_row_ids
    assert fold.promotion_purged_n > 0
    cb = load_mock_trades("convertible_bond")
    assert any(
        row["event_date"] == "2021-10-25"
        and row["t0_date"] == "2025-05-12"
        and row["reason"] == "t0_session_mismatch"
        for row in cb.attrs["admission"]["exclusions"]
    )


def test_metadata_required_even_for_too_small_samples():
    frame = synthetic_trades(3, 1)
    with pytest.raises(ValueError, match="chronology metadata required"):
        run_walk_forward(frame.drop(columns="label_end_date"))
    with pytest.raises(TypeError):
        LearnedPolicy().fit(np.zeros((1, 8)), np.zeros(1))
    invalid = frame.copy()
    invalid.loc[0, "label_end_date"] = invalid.loc[0, "decision_date"]
    with pytest.raises(ValueError, match="chronology"):
        _fit(invalid)
    invalid = frame.copy()
    invalid.loc[0, "feature_observation_date"] = invalid.loc[0, "decision_date"]
    with pytest.raises(ValueError, match="chronology"):
        require_chronology(invalid)


def test_unique_date_cohorts_and_both_inner_maturity_boundaries():
    frame = synthetic_trades(300, 1, seed=4)
    initial = _fit(frame)
    start = pd.Timestamp(initial.diagnostics["calibration_start"])
    fit_idx = frame.index[frame.decision_date < start][-3:]
    calibration_idx = frame.index[frame.decision_date >= start][-3:]
    frame.loc[fit_idx, "label_end_date"] = start  # same-day unavailable for fit
    frame.loc[calibration_idx, "label_end_date"] = pd.Timestamp("2023-01-01")
    policy = _fit(frame)
    diag = policy.diagnostics
    lookup = frame.set_index("row_id")
    fit = lookup.loc[diag["fit_row_ids"]]
    calibration = lookup.loc[diag["calibration_row_ids"]]
    assert (fit.label_end_date < start).all()
    assert (calibration.label_end_date < pd.Timestamp("2023-01-01")).all()
    assert set(fit.decision_date).isdisjoint(calibration.decision_date)
    assert not set(frame.loc[fit_idx, "row_id"]) & set(diag["fit_row_ids"])
    assert not set(frame.loc[calibration_idx, "row_id"]) & set(
        diag["calibration_row_ids"]
    )
    changed = frame.copy()
    changed.loc[fit_idx.union(calibration_idx), "realized_net_return"] = 99999.0
    repeat = _fit(changed)
    assert repeat.state_fingerprint() == policy.state_fingerprint()
    assert repeat.threshold == policy.threshold
    assert repeat.diagnostics == diag


def test_promotion_cutoff_same_day_and_future_label_perturbation():
    frame = synthetic_trades(120, 5, seed=1)
    # First test starts Jan 1 2023, even though first test observation is March.
    candidate = (frame.decision_date >= "2022-07-01") & (
        frame.decision_date < "2023-01-01"
    )
    frame.loc[candidate, "label_end_date"] = pd.Timestamp("2023-01-01")
    report = run_walk_forward(frame)
    first = report.folds[0]
    assert first.test_period_start == "2023-01-01"
    assert first.promotion_n == 0
    assert first.promotion_status == "no_mature_promotion_labels"
    assert first.model_trades == 0
    changed = frame.copy()
    changed.loc[candidate, "realized_net_return"] = 1e6
    after = run_walk_forward(changed).folds[0]
    assert asdict(after) == asdict(first)


def test_available_promotion_plus_unavailable_labels_does_not_change_state_or_decisions():
    frame = synthetic_trades(120, 6, seed=1)
    candidates = frame.index[
        (frame.decision_date >= "2022-07-01") & (frame.decision_date < "2023-01-01")
    ]
    unavailable = candidates[-5:]
    frame.loc[unavailable, "label_end_date"] = pd.Timestamp("2023-01-02")
    first = run_walk_forward(frame).folds[0]
    assert first.promotion_n == 115
    changed = frame.copy()
    changed.loc[unavailable, "realized_net_return"] *= -100000
    next_first = run_walk_forward(changed).folds[0]
    assert asdict(first) == asdict(next_first)
    # Later test outcomes change scoring but must not change decisions/state.
    future = frame.decision_date >= "2023-01-01"
    changed.loc[future, "realized_net_return"] = 1e6
    changed_first = run_walk_forward(changed).folds[0]
    for key in (
        "challenger_threshold",
        "challenger_state_sha256",
        "champion_state_sha256",
        "champion_trained_before",
        "champion_selected_before",
        "promoted",
        "test_decisions",
    ):
        assert getattr(first, key) == getattr(changed_first, key)


def test_permutation_invariance_dataset_policy_and_replay(tmp_path):
    rows = [_row(), _row(id="000002", receipt_no="20240102000002", t0_close=101)]
    first = _load(tmp_path, rows)
    second = _load(tmp_path, rows[::-1])
    pd.testing.assert_frame_equal(first, second)
    frame = synthetic_trades(120, 5, seed=7)
    shuffled = frame.sample(frac=1, random_state=42)
    assert asdict(run_walk_forward(frame)) == asdict(run_walk_forward(shuffled))
    first_half = frame[frame.decision_date < "2022-07-01"]
    assert (
        _fit(first_half).state_fingerprint()
        == _fit(first_half.sample(frac=1)).state_fingerprint()
    )


def test_empty_purged_windows_and_one_date_abstain():
    frame = synthetic_trades(120, 1)
    frame["label_end_date"] = pd.Timestamp("2023-01-01")
    policy = _fit(frame)
    assert policy.model is None and policy.threshold == 2.0
    assert policy.diagnostics["status"] == "insufficient_purged_samples"
    assert policy.diagnostics["fit_n"] == policy.diagnostics["calibration_n"] == 0
    frame["decision_date"] = pd.Timestamp("2022-03-01")
    policy = _fit(frame)
    assert policy.diagnostics["status"] == "insufficient_unique_dates"


def test_folds_use_decision_day_not_event_day():
    frame = synthetic_trades(3, 1)
    frame["event_date"] = frame["t0_date"] = frame["feature_observation_date"] = (
        pd.Timestamp("2023-12-28")
    )
    frame["entry_date"] = frame["decision_date"] = pd.Timestamp("2024-01-02")
    frame["label_end_date"] = pd.Timestamp("2024-01-08")
    assert make_folds(frame)[0][0] == "2024H1"


@pytest.mark.parametrize(
    "source,alias", [("t+1_date", "decision_date"), ("t+5_date", "label_end_date")]
)
def test_retained_source_date_aliases_cannot_disagree(source, alias):
    frame = synthetic_trades(3, 1)
    frame[source] = frame[alias] + pd.Timedelta(days=1)
    with pytest.raises(ValueError, match="contradictory chronology alias"):
        run_walk_forward(frame)


def test_nonfinite_outcomes_are_invalid_input_even_when_unavailable():
    frame = synthetic_trades(120, 3)
    frame.loc[0, "label_end_date"] = pd.Timestamp("2030-01-01")
    frame.loc[0, "realized_net_return"] = np.nan
    with pytest.raises(ValueError, match="finite"):
        run_walk_forward(frame)


def test_duplicate_quarantine_diagnostics_are_permutation_invariant(tmp_path):
    # Duplicate identities can have different source dates but the same reason.
    # Sorting only by identity/reason would retain arbitrary input order.
    rows = [_row(), _row(t0_date="2024-02-01")]
    forward = _load(tmp_path, rows).attrs["admission"]
    reverse = _load(tmp_path, rows[::-1]).attrs["admission"]
    assert forward["exclusions"] == reverse["exclusions"]
    assert forward["reason_counts"] == reverse["reason_counts"]
