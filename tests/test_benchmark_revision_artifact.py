from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from kdtb.data.benchmarks import BENCHMARK_CONTEXT_COLUMNS
from kdtb.research.baseline import sha256_file, write_json
from scripts.compare_benchmark_adjustment import build_comparison


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = PROJECT_ROOT / "artifacts/m0_3/benchmark_adjustment_comparison.json"


def test_committed_benchmark_comparison_regenerates_and_reconciles(tmp_path):
    recorded = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    regenerated = tmp_path / "comparison.json"
    write_json(regenerated, build_comparison())
    assert json.loads(regenerated.read_text(encoding="utf-8")) == recorded

    assert recorded["methodology"]["assignment"] == {
        "KOSDAQ": {"benchmark_symbol": "KOSDAQ", "pykrx_ticker_reference": "2001"},
        "KOSPI": {"benchmark_symbol": "KOSPI", "pykrx_ticker_reference": "1001"},
    }
    for category in recorded["categories"]:
        for metric in category["metrics"].values():
            assert metric["raw_unchanged_from_m0_2"] is True
            assert metric["abnormal_net_pct"] - metric["current_raw_net_pct"] == pytest.approx(
                metric["abnormal_minus_raw_pct_points"]
            )
        assert category["walk_forward"]["scored_folds"] >= 4

    learner = recorded["buyback_learner"]
    assert learner["raw_verdict"] == "positive_selective_edge"
    assert learner["abnormal_verdict"] == "no_selection_edge"
    for metric in learner["metrics"].values():
        assert metric["raw_unchanged_from_m0_2"] is True

    intraday = recorded["buyback_intraday"]
    assert intraday["positive_folds"] == {
        "scored": 10,
        "raw_uniform": 7,
        "abnormal_uniform": 5,
        "raw_timeaware": 9,
        "abnormal_timeaware": 7,
        "raw_timing_delta": 10,
        "abnormal_timing_delta": 9,
    }
    for metric in intraday["metrics"].values():
        assert metric["raw_unchanged_from_m0_2"] is True

    for group in ("inputs", "generator_sources"):
        for record in recorded[group]:
            assert sha256_file(PROJECT_ROOT / record["path"]) == record["sha256"]


def test_benchmark_enrichment_preserves_immutable_m0_2_input_lexemes(tmp_path):
    m0_2 = json.loads(
        (PROJECT_ROOT / "artifacts/m0_2/historical_cost_comparison.json").read_text(
            encoding="utf-8"
        )
    )
    records = m0_2["inputs"] + [
        {
            "path": "data/event_study_results.csv",
            "sha256": "2d7c433f2243dc9d33d7c9a2a8c1926a98dfa8c3c9b21eccd61658f9cb792ed5",
        },
        {
            "path": "data/event_study_filtered.csv",
            "sha256": "f869a589ef959094e00ffaaccb783fbcf4686cccd4e51624a7b1f3df14c388de",
        },
    ]
    for record in records:
        source = PROJECT_ROOT / record["path"]
        with source.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            preserved_fields = [
                column
                for column in (reader.fieldnames or [])
                if column not in BENCHMARK_CONTEXT_COLUMNS
            ]
            rows = list(reader)
        projected = tmp_path / source.name
        with projected.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=preserved_fields, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(
                {
                    column: row[column]
                    for column in preserved_fields
                }
                for row in rows
            )
        assert sha256_file(projected) == record["sha256"], record["path"]
