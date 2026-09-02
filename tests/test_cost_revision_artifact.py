from __future__ import annotations

import json
from pathlib import Path

import pytest

from kdtb.research.baseline import sha256_file
from scripts.compare_cost_revision import DEFAULT_OUTPUT, require_nonhistorical_output


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = PROJECT_ROOT / "artifacts/m0_2/historical_cost_comparison.json"
EXPECTED_SHA256 = "b529d30ef58606ebff1fec2507a644b8851e6dc921c5bdb57ca5fdf4ded7dc77"


def test_committed_cost_revision_artifact_is_immutable_and_reconciles():
    assert sha256_file(ARTIFACT) == EXPECTED_SHA256
    recorded = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    assert [row["start"] for row in recorded["tax_schedule"]] == [
        "2021-01-01",
        "2023-01-01",
        "2024-01-01",
        "2025-01-01",
        "2026-01-01",
    ]
    for category in recorded["categories"]:
        for metric in category["metrics"].values():
            assert metric["after"] - metric["before"] == pytest.approx(
                metric["delta_pct_points"]
            )

    learner = recorded["buyback_learner"]
    assert learner["before_verdict"] == "no_selection_edge"
    assert learner["after_verdict"] == "positive_selective_edge"
    intraday = recorded["buyback_intraday"]
    assert intraday["metrics"]["entry_timing_delta_pct"]["delta_pct_points"] == 0


def test_cost_revision_generator_refuses_to_overwrite_verified_artifact():
    with pytest.raises(ValueError, match="immutable"):
        require_nonhistorical_output(DEFAULT_OUTPUT)
