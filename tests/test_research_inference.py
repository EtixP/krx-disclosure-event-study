from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from kdtb.research.baseline import sha256_file, write_json
from kdtb.research.inference import issuer_clustered_mean_ci, tail_sensitivity
from scripts.analyze_research_inference import VERIFIED_UPSTREAM_SHA256, build_report
from scripts.subgroup_analysis import summarize


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = PROJECT_ROOT / "artifacts/m0_5/research_inference.json"


def test_cluster_bootstrap_resamples_issuers_not_repeated_rows():
    base = issuer_clustered_mean_ci(
        [1.0, -1.0],
        ["A", "B"],
        n_resamples=2_000,
        random_state=17,
    )
    repeated = issuer_clustered_mean_ci(
        [1.0] * 20 + [-1.0] * 20,
        ["A"] * 20 + ["B"] * 20,
        n_resamples=2_000,
        random_state=17,
    )

    for field in (
        "estimate",
        "ci_lower",
        "ci_upper",
        "bootstrap_standard_error",
    ):
        assert repeated[field] == base[field]
    assert repeated["n_events"] == 40
    assert repeated["n_issuers"] == base["n_issuers"] == 2
    assert repeated["repeated_event_rows"] == 38
    assert repeated["largest_issuer_cluster"] == 20


def test_cluster_bootstrap_is_seed_stable_and_rejects_invalid_clusters():
    first = issuer_clustered_mean_ci(
        [0.1, 0.2, -0.1, 0.05],
        ["A", "A", "B", "C"],
        n_resamples=1_000,
        random_state=3,
    )
    second = issuer_clustered_mean_ci(
        [0.1, 0.2, -0.1, 0.05],
        ["A", "A", "B", "C"],
        n_resamples=1_000,
        random_state=3,
    )
    assert first == second

    with pytest.raises(ValueError, match="at least two issuers"):
        issuer_clustered_mean_ci([0.1, 0.2], ["A", "A"])
    with pytest.raises(ValueError, match="non-finite"):
        issuer_clustered_mean_ci([0.1, np.nan], ["A", "B"])
    with pytest.raises(ValueError, match="missing"):
        issuer_clustered_mean_ci([0.1, 0.2], ["A", None])
    with pytest.raises(ValueError, match="n_resamples"):
        issuer_clustered_mean_ci([0.1, 0.2], ["A", "B"], n_resamples=2.5)


def test_tail_sensitivity_reports_both_one_sided_and_symmetric_stress():
    result = tail_sensitivity(np.arange(1.0, 101.0), fractions=(0.05,))
    assert result["full_sample"] == {
        "n": 100,
        "mean": 50.5,
        "median": 50.5,
        "win_fraction": 1.0,
    }
    scenarios = {row["scenario"]: row for row in result["scenarios"]}
    assert scenarios["exclude_top"]["n"] == 95
    assert scenarios["exclude_top"]["mean"] == 48.0
    assert scenarios["exclude_bottom"]["mean"] == 53.0
    assert scenarios["symmetric_trim"]["n"] == 90
    assert scenarios["symmetric_trim"]["mean"] == 50.5
    assert all(row["removed_per_selected_tail"] == 5 for row in scenarios.values())


def test_committed_inference_report_regenerates_and_is_semantic(tmp_path):
    recorded = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    regenerated = tmp_path / "research_inference.json"
    write_json(regenerated, build_report())
    assert json.loads(regenerated.read_text(encoding="utf-8")) == recorded

    assert recorded["methodology"] == {
        "confidence_level": 0.95,
        "dependence_unit": "issuer_stock_code",
        "headline_estimand": "event_weighted_mean_realistic_t1_to_t5_net_return",
        "interval": "percentile_cluster_bootstrap",
        "n_resamples": 10_000,
        "random_state": 0,
        "tail_fractions": [0.01, 0.05],
        "tail_scenarios": ["exclude_top", "exclude_bottom", "symmetric_trim"],
    }
    categories = recorded["category_headline_inference"]
    assert len(categories) == 7
    assert all(row["hypothesis_status"] == "exploratory" for row in categories)
    for row in categories:
        abnormal = row["realistic_t1_to_t5"]["abnormal_net"]
        assert abnormal["resampling_unit"] == "issuer"
        assert abnormal["n_issuers"] < abnormal["n_events"]
        assert abnormal["repeated_event_rows"] == (
            abnormal["n_events"] - abnormal["n_issuers"]
        )
        reconciled = row["point_reconciliation"]
        assert reconciled["current_headline"]["n"] == abnormal["n_events"]
        assert reconciled["independent_formula"]["n"] == abnormal["n_events"]

    governance = recorded["hypothesis_governance"]
    assert governance["current_classification"]["confirmatory"] == []
    assert governance["current_classification"]["pre_specified"] == []
    assert governance["current_classification"]["exploratory"]
    assert "not multiplicity-adjusted" in governance["multiple_testing"]

    timing = recorded["buyback_timing_inference"]
    assert timing["hypothesis_status"] == "exploratory"
    assert timing["sample"] == {
        "event_date_max": "2026-06-19",
        "event_date_min": "2021-06-28",
        "events": 3_562,
        "filing_time_input": (
            "artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv"
        ),
        "issuers": 937,
    }
    abnormal = timing["all_matched_event_inference"]["abnormal"]
    assert abnormal["timeaware_entry"]["ci_excludes_zero"] is False
    assert abnormal["paired_entry_timing_delta"]["ci_excludes_zero"] is True
    assert abnormal["paired_entry_timing_delta"]["n_issuers"] == 937

    for tail_name in (
        "timeaware_raw_net",
        "timeaware_abnormal_net",
        "paired_entry_timing_delta_raw",
        "paired_entry_timing_delta_abnormal",
    ):
        tail = timing["tail_sensitivity"][tail_name]
        top_five = [
            row
            for row in tail["scenarios"]
            if row["scenario"] == "exclude_top" and row["fraction"] == 0.05
        ]
        assert len(top_five) == 1
        assert tail["full_sample"]["mean_pct"] > 0
        assert top_five[0]["mean_pct"] < 0

    for group in ("inputs", "generator_sources", "verified_upstream_artifacts"):
        for record in recorded[group]:
            assert sha256_file(PROJECT_ROOT / record["path"]) == record["sha256"]
    assert {
        record["path"]: record["sha256"]
        for record in recorded["verified_upstream_artifacts"]
    } == VERIFIED_UPSTREAM_SHA256


def test_subgroup_summary_is_explicitly_exploratory():
    frame = pd.DataFrame(
        {
            "_t1_net": [0.01] * 20,
            "_t5_net": [0.02] * 20,
            "_t1_abnormal_net": [0.005] * 20,
            "_t5_abnormal_net": [0.01] * 20,
        }
    )
    result = summarize(frame, "screened subgroup")
    assert result is not None
    assert result["hypothesis_status"] == "exploratory"
