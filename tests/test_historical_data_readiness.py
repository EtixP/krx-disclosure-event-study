from __future__ import annotations

import csv
from datetime import date
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import urllib.request

import pytest

from scripts import audit_historical_data_readiness as audit


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts/m0_7/historical_data_readiness_v1.json"


def _payload() -> dict:
    return audit.build_audit()


def _base_row(**updates: str) -> dict[str, str]:
    row = {column: "1" for column in audit.REQUIRED_CATEGORY_COLUMNS}
    row.update(
        {
            "id": "1",
            "receipt_no": "20240102000001",
            "corp_code": "00000001",
            "corp_name": "issuer",
            "stock_code": "000001",
            "report_name": "report",
            "event_date": "2024-01-02",
            "market": "KOSPI",
            "t0_close": "100",
            "t+1_close": "101",
            "t+2_close": "102",
            "t+5_close": "105",
            "error": "",
            "t0_date": "2024-01-02",
            "t+1_date": "2024-01-03",
            "t+2_date": "2024-01-04",
            "t+5_date": "2024-01-09",
            "benchmark_source": "pinned",
            "benchmark_symbol": "KOSPI",
            "benchmark_t0_close": "1000",
            "benchmark_t1_close": "1001",
            "benchmark_t2_close": "1002",
            "benchmark_t5_close": "1005",
        }
    )
    row.update(updates)
    return row


def test_artifact_reproduces_byte_identically_and_all_sources_are_pinned(tmp_path):
    payload = _payload()
    output = tmp_path / "historical_data_readiness.json"
    audit.write_new_json(output, payload)
    assert output.read_bytes() == ARTIFACT.read_bytes()
    recorded = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert {item["path"] for item in recorded["inputs"]} == set(
        audit.EXPECTED_INPUT_SHA256
    )
    for item in [*recorded["inputs"], *recorded["generator_sources"]]:
        assert audit.sha256_file(ROOT / item["path"]) == item["sha256"]
    assert recorded["generation"]["database_used"] is False
    assert recorded["generation"]["network_used"] is False


def test_recorded_exact_command_reproduces_committed_bytes():
    recorded = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    command = recorded["generation"]["command"]
    assert command[0] == ".venv/bin/python"
    assert recorded["generation"]["runtime"]["python_executable"] == command[0]
    output = Path(command[-1])
    output.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    try:
        subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert output.read_bytes() == ARTIFACT.read_bytes()
    finally:
        output.unlink(missing_ok=True)


def test_population_and_primary_exclusions_reconcile_without_event_double_counting():
    payload = _payload()
    assert payload["population"] == {
        "row_copies": {
            "input": 28_979,
            "admitted": 28_670,
            "excluded": 309,
            "interpretation": "category-row copies; not independent events",
        },
        "unique_receipts": {
            "input": 28_737,
            "at_least_one_copy_admitted": 28_432,
            "every_copy_admitted": 28_428,
            "interpretation": "receipt identities collapsed only for population accounting",
        },
    }
    expected = {
        "supply_contract": (8_624, 8_585, 39),
        "buyback": (4_846, 4_800, 46),
        "rights_offering": (5_991, 5_870, 121),
        "bonus_issue": (840, 827, 13),
        "convertible_bond": (4_202, 4_160, 42),
        "halt_resumption": (2_598, 2_570, 28),
        "shareholder_change": (1_878, 1_858, 20),
    }
    primary = {reason: 0 for reason in audit.PRIMARY_REASON_ORDER}
    defects = {reason: 0 for reason in audit.DEFECT_FLAG_ORDER}
    for category, counts in expected.items():
        item = payload["categories"][category]
        assert (
            item["row_copy_count"],
            item["admitted_row_copies"],
            item["excluded_row_copies"],
        ) == counts
        assert sum(item["primary_reason_counts"].values()) == counts[2]
        for reason, count in item["primary_reason_counts"].items():
            primary[reason] += count
        for reason, count in item["nonexclusive_defect_counts"].items():
            defects[reason] += count
    assert primary["invalid_stock_price"] == 165
    assert primary["missing_date"] == 86
    assert primary["t0_session_mismatch"] == 58
    assert sum(primary.values()) == 309
    assert defects["invalid_stock_price"] == 251
    assert defects["missing_date"] == 86
    assert defects["source_row_error"] == 60


def test_every_cross_category_copy_is_retained_and_conflicts_are_not_canonicalized():
    overlap = _payload()["cross_category_overlaps"]
    assert overlap["cross_category_unique_receipts"] == 242
    assert overlap["cross_category_row_copies"] == 484
    assert overlap["field_conflict_receipts"] == 4
    assert overlap["category_dependent_admission_receipts"] == 4
    conflicts = {
        item["receipt_no"]: item
        for item in overlap["receipts"]
        if item["has_field_conflict"]
    }
    assert set(conflicts) == {
        "20220509900146",
        "20230921900106",
        "20230926900688",
        "20250124900682",
    }
    for item in overlap["receipts"]:
        assert item["canonical_copy_selected"] is False
        assert len(item["category_copies"]) == len(item["categories"])
        assert all(
            copy["fields"]["receipt_no"] == item["receipt_no"]
            for copy in item["category_copies"]
        )
    assert {
        entry["field"] for entry in conflicts["20220509900146"]["field_conflicts"]
    } >= {
        "error",
        "t0_close",
        "t0_date",
        "t+5_close",
    }
    assert {
        copy["category"]: copy["admission"]["admitted"]
        for copy in conflicts["20250124900682"]["category_copies"]
    } == {"convertible_bond": False, "rights_offering": True}


def test_delayed_windows_filing_times_and_identifier_shapes_are_explicit():
    payload = _payload()
    delayed = payload["delayed_t0_windows"]
    assert delayed["affected_row_copies"] == 58
    assert delayed["maximum_calendar_days"] == 1_341
    assert delayed["longest_examples"][0]["receipt_no"] == "20220314000892"
    filing = payload["buyback_filing_time_coverage"]
    assert filing["exact_time_supplied"] == 3_604
    assert filing["exact_time_not_supplied"] == 1_242
    assert filing["mutually_exclusive_time_buckets"] == {
        "pre_open_before_09_00": 52,
        "session_09_00_through_15_29": 1_703,
        "exact_close_15_30": 11,
        "after_close_after_15_30": 1_838,
    }
    assert payload["security_identifier_shape"]["row_copy_counts"] == {
        "six_digit_numeric": 28_972,
        "six_character_alphanumeric": 7,
        "missing": 0,
        "other": 0,
    }


def test_gate_reason_codes_and_evidence_pillars_are_fixed_and_fail_closed():
    payload = _payload()
    gate = payload["ml_readiness_gate"]
    assert gate["decision"] == "NO_GO"
    assert gate["blocker_reason_codes"] == list(audit.GATE_BLOCKER_CODES)
    assert gate["reason_code_order"] == list(audit.GATE_BLOCKER_CODES)
    assert gate["historical_intraday_is_daily_gate_input"] is False
    assert payload["informational_status"] == [
        {
            "code": audit.INTRADAY_INFORMATION_CODE,
            "status": "prospective_collection_only",
            "gate_blocker": False,
            "detail": "the prospective KIS path cannot reconstruct historical spreads, latency, order books or fills",
        }
    ]
    for evidence in payload["evidence"].values():
        assert evidence["gate_pillar_satisfied_for_all_admitted_copies"] is False
        assert evidence["admitted_row_copies_without_pillar"] == 28_670
    assert (
        payload["evidence"]["point_in_time_disclosure_evidence"][
            "database_inference_used"
        ]
        is False
    )

    complete = {
        pillar: {"gate_pillar_satisfied_for_all_admitted_copies": True}
        for pillar in audit.PILLAR_BLOCKER_CODES
    }
    assert audit._evaluate_gate(complete, has_copy_conflict=False)["decision"] == "GO"
    complete["versioned_daily_label_source"][
        "gate_pillar_satisfied_for_all_admitted_copies"
    ] = False
    assert audit._evaluate_gate(complete, has_copy_conflict=True)[
        "blocker_reason_codes"
    ] == ["missing_versioned_daily_label_source", "cross_category_copy_conflict"]


def test_modified_input_hash_is_rejected(monkeypatch, tmp_path):
    relative = "data/event_study_buyback.csv"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / relative).read_bytes() + b"tampered")
    monkeypatch.setattr(audit, "INPUT_PURPOSES", {relative: "test"})
    with pytest.raises(audit.AuditInputError, match="hash mismatch"):
        audit._validate_input_hashes(
            tmp_path, {relative: audit.EXPECTED_INPUT_SHA256[relative]}
        )


def test_missing_category_schema_is_rejected(tmp_path):
    path = tmp_path / "missing.csv"
    path.write_text("receipt_no,event_date\n1,2024-01-02\n", encoding="utf-8")
    with pytest.raises(audit.AuditInputError, match="missing required columns"):
        audit._read_csv(path, audit.REQUIRED_CATEGORY_COLUMNS)


def test_row_permutation_preserves_semantic_admission_and_overlap(
    monkeypatch, tmp_path
):
    first = _base_row()
    second = _base_row(
        id="2",
        receipt_no="20240102000002",
        stock_code="000002",
        t0_close="",
        error="fetch_failed",
    )
    sessions = {
        "KOSPI": [
            date.fromisoformat(value)
            for value in (
                "2024-01-02",
                "2024-01-03",
                "2024-01-04",
                "2024-01-05",
                "2024-01-08",
                "2024-01-09",
                "2024-01-10",
            )
        ],
        "KOSDAQ": [],
    }
    relative = "data/category.csv"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(audit, "CATEGORY_PATHS", {"test": relative})

    def write(rows):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=sorted(audit.REQUIRED_CATEGORY_COLUMNS)
            )
            writer.writeheader()
            writer.writerows(rows)

    write([first, second])
    forward, forward_receipts = audit._audit_categories(tmp_path, sessions)
    write([second, first])
    reverse, reverse_receipts = audit._audit_categories(tmp_path, sessions)
    assert forward == reverse
    assert audit._overlap_audit(forward_receipts) == audit._overlap_audit(
        reverse_receipts
    )


def test_overlap_conflict_and_category_dependent_admission_are_structural():
    sessions = {
        "KOSPI": [
            date.fromisoformat(value)
            for value in (
                "2024-01-02",
                "2024-01-03",
                "2024-01-04",
                "2024-01-05",
                "2024-01-08",
                "2024-01-09",
                "2024-01-10",
            )
        ]
    }
    valid = _base_row()
    invalid = _base_row(t0_close="", error="fetch_failed")
    copies = []
    for category, fields in (("alpha", valid), ("beta", invalid)):
        copies.append(
            {
                "category": category,
                "row_copy_id": f"{category}:{fields['receipt_no']}",
                "row_lexeme_sha256": audit._row_lexeme_sha256(fields),
                "fields": fields,
                "admission": audit._audit_row(fields, sessions),
            }
        )
    result = audit._overlap_audit({valid["receipt_no"]: copies})["receipts"][0]
    assert result["has_field_conflict"] is True
    assert result["category_dependent_admission"] is True
    assert result["canonical_copy_selected"] is False
    assert len(result["category_copies"]) == 2


def test_delayed_t0_is_excluded_even_when_prices_are_valid():
    days = [
        date.fromisoformat(value)
        for value in (
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
            "2024-01-08",
            "2024-01-09",
            "2024-01-10",
            "2024-01-11",
        )
    ]
    row = _base_row(
        t0_date="2024-01-03",
        **{"t+1_date": "2024-01-04", "t+5_date": "2024-01-10"},
    )
    result = audit._audit_row(row, {"KOSPI": days})
    assert result["admitted"] is False
    assert result["primary_exclusion_reason"] == "t0_session_mismatch"
    assert result["t0_delay_calendar_days"] == 1


def test_build_never_opens_sqlite_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("audit attempted database or network access")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert _payload()["ml_readiness_gate"]["decision"] == "NO_GO"


@pytest.mark.parametrize("target", ["existing", "data", "artifacts", "symlink"])
def test_output_guard_rejects_existing_and_protected_paths(tmp_path, target):
    root = tmp_path / "project"
    root.mkdir()
    if target == "existing":
        output = tmp_path / "existing.json"
        output.write_text("keep", encoding="utf-8")
    elif target == "symlink":
        (root / "artifacts").mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(root / "artifacts", target_is_directory=True)
        output = alias / "new.json"
    else:
        output = root / target / "new.json"
    with pytest.raises(ValueError, match="protected"):
        audit.require_new_output(output, project_root=root)


def test_exclusive_publication_does_not_replace_a_race_winner(tmp_path, monkeypatch):
    output = tmp_path / "race.json"
    real_link = audit.os.link

    def race(source, destination, **kwargs):
        output.write_text("another writer won", encoding="utf-8")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(audit.os, "link", race)
    with pytest.raises(FileExistsError):
        audit.write_new_json(output, {"ok": True})
    assert output.read_text(encoding="utf-8") == "another writer won"
    assert [item.name for item in tmp_path.iterdir()] == ["race.json"]


def test_parent_replacement_cannot_redirect_publication_into_artifacts(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    protected = project / "artifacts"
    protected.mkdir(parents=True)
    parent = tmp_path / "publish"
    parent.mkdir()
    displaced = tmp_path / "displaced"
    output = parent / "result.json"
    checked = audit.require_new_output(output, project_root=project)
    real_link = audit.os.link

    def replace_parent(source, destination, **kwargs):
        parent.rename(displaced)
        parent.symlink_to(protected, target_is_directory=True)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(audit.os, "link", replace_parent)
    with pytest.raises(ValueError, match="path changed"):
        audit.write_new_json(checked, {"ok": True}, project_root=project)
    assert not (protected / "result.json").exists()
    assert not (displaced / "result.json").exists()
    assert not any(path.name.startswith(".m0_7-") for path in displaced.iterdir())


def test_ancestor_replacement_before_child_open_cannot_redirect_to_artifacts(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    redirected_parent = project / "artifacts" / "redirect" / "publish"
    redirected_parent.mkdir(parents=True)
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "publish"
    parent.mkdir(parents=True)
    displaced = tmp_path / "displaced"
    output = parent / "result.json"
    checked = audit.require_new_output(output, project_root=project)
    real_open = audit.os.open
    attacked = False

    def replace_ancestor(path, flags, *args, **kwargs):
        nonlocal attacked
        if path == "publish" and not attacked:
            attacked = True
            ancestor.rename(displaced)
            ancestor.symlink_to(redirected_parent.parent, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(audit.os, "open", replace_ancestor)
    with pytest.raises(ValueError, match="protected|path changed"):
        audit.write_new_json(checked, {"ok": True}, project_root=project)
    assert attacked is True
    assert not (redirected_parent / "result.json").exists()
    assert not (displaced / "publish" / "result.json").exists()
    assert not any(
        path.name.startswith(".m0_7-") for path in (displaced / "publish").iterdir()
    )


def test_ancestor_symlink_is_rejected_when_that_component_opens(tmp_path, monkeypatch):
    project = tmp_path / "project"
    redirected_parent = project / "artifacts" / "redirect" / "publish"
    redirected_parent.mkdir(parents=True)
    ancestor = tmp_path / "ancestor"
    (ancestor / "publish").mkdir(parents=True)
    displaced = tmp_path / "displaced"
    output = ancestor / "publish" / "result.json"
    checked = audit.require_new_output(output, project_root=project)
    real_open = audit.os.open
    attacked = False

    def replace_component(path, flags, *args, **kwargs):
        nonlocal attacked
        if path == "ancestor" and not attacked:
            attacked = True
            ancestor.rename(displaced)
            ancestor.symlink_to(redirected_parent.parent, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(audit.os, "open", replace_component)
    with pytest.raises(ValueError, match="path changed"):
        audit.write_new_json(checked, {"ok": True}, project_root=project)
    assert attacked is True
    assert not (redirected_parent / "result.json").exists()
    assert not (displaced / "publish" / "result.json").exists()


def test_ancestor_replacement_during_link_rolls_back_displaced_file(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    redirected_parent = project / "artifacts" / "redirect" / "publish"
    redirected_parent.mkdir(parents=True)
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "publish"
    parent.mkdir(parents=True)
    displaced = tmp_path / "displaced"
    output = parent / "result.json"
    checked = audit.require_new_output(output, project_root=project)
    real_link = audit.os.link

    def replace_ancestor(source, destination, **kwargs):
        ancestor.rename(displaced)
        ancestor.symlink_to(redirected_parent.parent, target_is_directory=True)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(audit.os, "link", replace_ancestor)
    with pytest.raises(ValueError, match="path changed"):
        audit.write_new_json(checked, {"ok": True}, project_root=project)
    assert not (redirected_parent / "result.json").exists()
    assert not (displaced / "publish" / "result.json").exists()
    assert not any(
        path.name.startswith(".m0_7-") for path in (displaced / "publish").iterdir()
    )


def test_held_ancestor_moved_into_artifacts_is_rolled_back(tmp_path, monkeypatch):
    project = tmp_path / "project"
    protected = project / "artifacts"
    protected.mkdir(parents=True)
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "publish"
    parent.mkdir(parents=True)
    moved = protected / "moved"
    output = parent / "result.json"
    real_link = audit.os.link

    def move_ancestor(source, destination, **kwargs):
        ancestor.rename(moved)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(audit.os, "link", move_ancestor)
    with pytest.raises(ValueError, match="path changed"):
        audit.write_new_json(output, {"ok": True}, project_root=project)
    assert not (moved / "publish" / "result.json").exists()
    assert not any(
        path.name.startswith(".m0_7-") for path in (moved / "publish").iterdir()
    )


def test_fresh_nested_output_parents_remain_supported(tmp_path):
    output = tmp_path / "new" / "nested" / "result.json"
    digest = audit.write_new_json(output, {"ok": True})
    assert json.loads(output.read_text(encoding="utf-8")) == {"ok": True}
    assert digest == audit.sha256_file(output)
