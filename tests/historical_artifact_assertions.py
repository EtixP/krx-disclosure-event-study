"""Separate immutable historical provenance from current execution provenance."""

from pathlib import Path

from kdtb.research.baseline import sha256_file


def assert_historical_source_record(root: Path, record: dict) -> None:
    archived = root / "artifacts/m0_6/legacy_sources" / record["path"]
    path = archived if archived.exists() else root / record["path"]
    assert sha256_file(path) == record["sha256"], record["path"]


def assert_current_replay(root: Path, recorded: dict, current: dict) -> None:
    # Scientific payload is EXACT; only the declared execution source list drifts.
    assert {k: v for k, v in current.items() if k != "generator_sources"} == {
        k: v for k, v in recorded.items() if k != "generator_sources"
    }
    assert current["generator_sources"] != recorded["generator_sources"]
    for record in current["generator_sources"]:
        assert sha256_file(root / record["path"]) == record["sha256"]
    for record in recorded["generator_sources"]:
        assert_historical_source_record(root, record)
