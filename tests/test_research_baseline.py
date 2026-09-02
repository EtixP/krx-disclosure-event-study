from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from kdtb.research.baseline import (
    SnapshotVerificationError,
    capture_buyback_filing_times,
    sha256_file,
    verify_snapshot,
    write_json,
)
from scripts.verify_research_state import require_nonhistorical_output


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = PROJECT_ROOT / "artifacts/baselines/pre_revision"
EXPECTED_MANIFEST_SHA256 = (
    "7618e0d62028d44fea96edc496adb7fc0ada1f6a063ca3aee1e680f8b8c81776"
)


def _update_artifact_hash(snapshot_dir: Path, artifact_path: Path) -> None:
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = next(
        item for item in manifest["artifacts"] if item["path"] == artifact_path.name
    )
    record["bytes"] = artifact_path.stat().st_size
    record["sha256"] = sha256_file(artifact_path)
    write_json(manifest_path, manifest)


def test_canonical_json_write_is_byte_stable(tmp_path):
    payload = {"z": np.float64(1.25), "a": {"value": np.int64(3)}}
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    write_json(first, payload)
    write_json(second, payload)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_text(encoding="utf-8").startswith('{\n  "a"')


def test_capture_buyback_times_is_filtered_and_sorted(tmp_path):
    event_csv = tmp_path / "events.csv"
    event_csv.write_text(
        "receipt_no,event_date\n"
        "20240102000002,2024-01-02\n"
        "20240102000001,2024-01-02\n"
        "20240102000003,2024-01-02\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "source.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE disclosures (receipt_no TEXT, filing_time TEXT)"
        )
        connection.executemany(
            "INSERT INTO disclosures VALUES (?, ?)",
            [
                ("20240102000002", "15:10"),
                ("20240102000001", "09:05"),
                ("20240102000003", None),
                ("20240102099999", "10:00"),
            ],
        )
    output = tmp_path / "buyback_filing_times.csv"

    count = capture_buyback_filing_times(
        db_path=db_path, event_csv=event_csv, output_path=output
    )

    assert count == 2
    assert output.read_text(encoding="utf-8") == (
        "receipt_no,filing_time\n"
        "20240102000001,09:05\n"
        "20240102000002,15:10\n"
    )


def test_committed_snapshot_is_internally_consistent():
    assert sha256_file(BASELINE_DIR / "manifest.json") == EXPECTED_MANIFEST_SHA256
    result = verify_snapshot(output_dir=BASELINE_DIR)

    assert result["internal_consistency"] == "passed"
    assert result["artifacts_verified"] == 4
    assert result["external_inputs_checked"] is False


def test_baseline_generator_refuses_to_overwrite_verified_snapshot():
    with pytest.raises(ValueError, match="immutable"):
        require_nonhistorical_output(BASELINE_DIR, mutating=True)
    require_nonhistorical_output(BASELINE_DIR, mutating=False)


def test_verification_rejects_arithmetic_tampering_even_with_updated_hash(tmp_path):
    copied = tmp_path / "snapshot"
    shutil.copytree(BASELINE_DIR, copied)

    intraday_path = copied / "buyback_intraday_summary.json"
    intraday = json.loads(intraday_path.read_text(encoding="utf-8"))
    intraday["headline"]["entry_timing_delta_pct"] += 1.0
    write_json(intraday_path, intraday)

    _update_artifact_hash(copied, intraday_path)

    with pytest.raises(
        SnapshotVerificationError, match="intraday headline delta arithmetic"
    ):
        verify_snapshot(output_dir=copied)


def test_external_input_hashes_are_optional_but_checkable(tmp_path):
    result = verify_snapshot(output_dir=BASELINE_DIR)
    assert result["external_inputs_checked"] is False

    with pytest.raises(SnapshotVerificationError, match="external input is missing"):
        verify_snapshot(
            output_dir=BASELINE_DIR,
            project_root=tmp_path,
            check_external_inputs=True,
        )


def test_verification_rejects_contradictory_category_count_after_rehash(tmp_path):
    copied = tmp_path / "snapshot"
    shutil.copytree(BASELINE_DIR, copied)

    cross_path = copied / "cross_category_summary.json"
    cross = json.loads(cross_path.read_text(encoding="utf-8"))
    cross["categories"][0]["n_events"] = 1
    write_json(cross_path, cross)
    _update_artifact_hash(copied, cross_path)

    with pytest.raises(
        SnapshotVerificationError,
        match="category supply_contract",
    ):
        verify_snapshot(output_dir=copied)


def test_verification_rejects_contradictory_category_fold_count_after_rehash(
    tmp_path,
):
    copied = tmp_path / "snapshot"
    shutil.copytree(BASELINE_DIR, copied)

    cross_path = copied / "cross_category_summary.json"
    cross = json.loads(cross_path.read_text(encoding="utf-8"))
    cross["categories"][0]["walk_forward"][0]["n"] += 1
    write_json(cross_path, cross)
    _update_artifact_hash(copied, cross_path)

    with pytest.raises(
        SnapshotVerificationError,
        match="fold event counts must sum to n_events",
    ):
        verify_snapshot(output_dir=copied)


def test_metrics_implementation_is_provenance_hashed_and_checked(tmp_path):
    manifest = json.loads(
        (BASELINE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    metrics_path = "src/kdtb/backtest/metrics.py"
    metrics_record = next(
        record
        for record in manifest["generator_sources"]
        if record["path"] == metrics_path
    )
    current_metrics = PROJECT_ROOT / metrics_path
    assert metrics_record["sha256"] == sha256_file(current_metrics)

    copied = tmp_path / "snapshot"
    shutil.copytree(BASELINE_DIR, copied)
    copied_manifest_path = copied / "manifest.json"
    copied_manifest = json.loads(copied_manifest_path.read_text(encoding="utf-8"))
    copied_manifest["external_inputs"] = []
    copied_manifest["generator_sources"] = [metrics_record]
    write_json(copied_manifest_path, copied_manifest)

    changed_project = tmp_path / "changed_project"
    changed_metrics = changed_project / metrics_path
    changed_metrics.parent.mkdir(parents=True)
    changed_metrics.write_text("# deliberately changed\n", encoding="utf-8")

    with pytest.raises(SnapshotVerificationError, match="generator source"):
        verify_snapshot(
            output_dir=copied,
            project_root=changed_project,
            check_external_inputs=True,
        )
