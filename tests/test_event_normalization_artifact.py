from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.audit_event_normalization import GENERATOR_SOURCES, build_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = PROJECT_ROOT / "artifacts/m1_1/normalization_prevalence.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_committed_normalization_prevalence_regenerates_and_is_semantic(
    monkeypatch,
):
    monkeypatch.chdir(PROJECT_ROOT)
    recorded = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    assert build_report(Path("data/kdtb.db")) == recorded
    assert recorded["corpus"] == {
        "disclosures": 1_209_249,
        "first_receipt_datetime": "2021-06-28T00:00:00",
        "last_receipt_datetime": "2026-08-26T00:00:00",
    }
    assert recorded["update_prevalence"] == {
        "filings": 203_626,
        "fraction_of_corpus": 0.16839046,
    }
    assert recorded["update_actions"] == {
        "amendment": 7_191,
        "correction": 196_363,
        "correction_required": 72,
    }
    assert recorded["same_issuer_title_date_collisions"]["groups"] == 54_526
    assert (
        "not duplicate labels"
        in recorded["same_issuer_title_date_collisions"]["warning"]
    )
    assert "not lineage" in recorded["title_based_linkage_diagnostic_only"]["warning"]

    source_records = {
        row["path"]: row["sha256"] for row in recorded["generator_sources"]
    }
    assert tuple(source_records) == GENERATOR_SOURCES
    for path, expected in source_records.items():
        assert _sha256(PROJECT_ROOT / path) == expected
