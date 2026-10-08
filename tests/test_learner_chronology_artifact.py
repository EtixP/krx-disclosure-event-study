from __future__ import annotations

import json
from pathlib import Path

from kdtb.research.baseline import sha256_file, write_json
from scripts.compare_learner_chronology import build_comparison
from scripts.replay_historical_learner_sources import replay

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts/m0_6/learner_chronology_v1.json"


def test_chronology_artifact_reproduces_and_records_actual_current_sources(tmp_path):
    recorded = json.loads(ARTIFACT.read_text())
    regenerated = tmp_path / "chronology.json"
    write_json(regenerated, build_comparison())
    assert regenerated.read_bytes() == ARTIFACT.read_bytes()
    for kind in ("inputs", "generator_sources"):
        for record in recorded[kind]:
            assert sha256_file(ROOT / record["path"]) == record["sha256"]
    admission = recorded["admission"]
    assert (
        admission["input_rows"]
        == admission["admitted_rows"] + admission["excluded_rows"]
    )
    assert sum(admission["reason_counts"].values()) == admission["excluded_rows"]
    results = recorded["results"]
    assert (
        results["legacy_admitted"]["mock_trades"]
        == results["strict_admitted"]["mock_trades"]
    )
    assert results["strict_admitted"]["mock_trades"] == admission["admitted_rows"]
    for fold in results["strict_admitted"]["folds"]:
        assert (
            fold["promotion_n"] + fold["promotion_purged_n"]
            == fold["promotion_candidates_n"]
        )
        for stage in ("fit", "calibration"):
            diag = fold["fit_diagnostics"]
            assert (
                diag[f"{stage}_n"] + diag[f"{stage}_purged_n"]
                == diag[f"{stage}_candidates_n"]
            )
        if fold["champion_trained_before"] is not None:
            assert (
                fold["champion_trained_before"]
                < fold["champion_selected_before"]
                <= fold["test_period_start"]
            )
    assert recorded["methodology"]["db_used"] is False


def test_archived_sources_execute_exact_historical_payloads_without_db():
    result = replay()
    assert result["db_exposed"] is False
    assert all(
        result["results"][key]["byte_identical"]
        for key in ("m0_1_learner", "m0_3", "m0_5")
    )
    assert result["results"]["m0_2"]["scientific_payload_identical"] is True
