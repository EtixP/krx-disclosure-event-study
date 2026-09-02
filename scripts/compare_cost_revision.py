"""Generate the deterministic M0.2 before/after research comparison.

The "before" side is read from the immutable M0.1 pre-revision artifacts. The
"after" side reruns the important category, learner, and intraday analyses with
exact execution dates and the statutory date/market-aware cost schedule.

Usage:
    python -m scripts.compare_cost_revision
    python -m scripts.compare_cost_revision --output /tmp/m0_2_replay.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from kdtb.backtest.cost_model import TAX_REGIMES, CostModel
from kdtb.learning.dataset import load_mock_trades
from kdtb.learning.features import FEATURE_NAMES, extract_features
from kdtb.learning.walk_forward_trainer import make_folds
from kdtb.research.baseline import sha256_file, summarize_learner, write_json
from scripts.analyze_event_category import MIN_WINDOW_EVENTS, analyze
from scripts.run_intraday_walkforward import (
    INTRADAY_CUTOFF_MIN,
    _mins,
    apply_timeaware_returns,
)
from scripts.summarize_all_categories import CATEGORIES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BEFORE_DIR = PROJECT_ROOT / "artifacts/baselines/pre_revision"
DEFAULT_OUTPUT = PROJECT_ROOT / "artifacts/m0_2/historical_cost_comparison.json"
IMMUTABLE_ARTIFACT_SHA256 = (
    "b529d30ef58606ebff1fec2507a644b8851e6dc921c5bdb57ca5fdf4ded7dc77"
)
GENERATOR_SOURCES = (
    "src/kdtb/backtest/cost_model.py",
    "src/kdtb/backtest/metrics.py",
    "src/kdtb/learning/dataset.py",
    "src/kdtb/learning/features.py",
    "src/kdtb/learning/policy.py",
    "src/kdtb/learning/walk_forward_trainer.py",
    "scripts/analyze_event_category.py",
    "scripts/compare_cost_revision.py",
    "scripts/run_intraday_walkforward.py",
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _delta(after: float, before: float) -> float:
    return float(after) - float(before)


def _category_comparison(
    *, before_cross: dict[str, Any]
) -> list[dict[str, Any]]:
    prior = {row["category"]: row for row in before_cross["categories"]}
    rows = []
    for category in CATEGORIES:
        after = analyze(category, include_abnormal=False)
        before = prior[category]
        metrics = {}
        for label, before_value, after_value in (
            (
                "idealized_t1_mean_net_pct",
                before["aggregate"]["t1_net"]["mean_pct"],
                after["aggregate"]["t1_net"]["mean_pct"],
            ),
            (
                "idealized_t5_mean_net_pct",
                before["aggregate"]["t5_net"]["mean_pct"],
                after["aggregate"]["t5_net"]["mean_pct"],
            ),
            (
                "realistic_t1_to_t5_mean_net_pct",
                before["realistic"]["realistic"]["mean_pct"],
                after["realistic"]["realistic"]["mean_pct"],
            ),
        ):
            metrics[label] = {
                "before": before_value,
                "after": after_value,
                "delta_pct_points": _delta(after_value, before_value),
            }
        before_walk = before["walk_forward_summary"]
        after_scored = [
            fold
            for fold in after["walk_forward"]
            if fold.get("n", 0) >= MIN_WINDOW_EVENTS
        ]
        rows.append(
            {
                "category": category,
                "n_events": after["n_events"],
                "cost_fraction": after["cost_fraction"],
                "metrics": metrics,
                "walk_forward": {
                    "before_positive_folds": before_walk["positive_folds"],
                    "after_positive_folds": sum(
                        fold.get("mean_pct", 0) > 0 for fold in after_scored
                    ),
                    "scored_folds": len(after_scored),
                },
                "verdict": {
                    "before": before["verdict"],
                    "after": after["verdict"],
                },
            }
        )
    return rows


def _learner_comparison(*, before_dir: Path) -> dict[str, Any]:
    before = _load_json(before_dir / "learner_buyback_summary.json")["buyback"]
    frame = load_mock_trades("buyback", return_basis="raw")
    after = summarize_learner(frame, label="buyback", random_state=0)
    keys = (
        "model_mean_net_pct",
        "always_all_mean_net_pct",
        "matched_always_mean_net_pct",
        "selection_lift_pct",
    )
    return {
        "mock_trades": len(frame),
        "metrics": {
            key: {
                "before": before[key],
                "after": after[key],
                "delta_pct_points": _delta(after[key], before[key]),
            }
            for key in keys
        },
        "before_verdict": before["verdict"],
        "after_verdict": after["verdict"],
        "after_model_trades": after["model_trades"],
        "after_promotions": after["promotions"],
    }


def _corrected_intraday(before_dir: Path) -> dict[str, Any]:
    event_csv = PROJECT_ROOT / "data/event_study_buyback.csv"
    filing_times_csv = before_dir / "inputs/buyback_filing_times.csv"
    frame = pd.read_csv(event_csv, dtype={"receipt_no": "string"})
    frame = frame.dropna(subset=["t0_close", "t+1_close", "t+5_close"]).copy()
    frame = frame[frame["t+1_close"] > 0]
    times = pd.read_csv(
        filing_times_csv,
        dtype={"receipt_no": "string", "filing_time": "string"},
    )
    time_map = dict(zip(times["receipt_no"].astype(str), times["filing_time"].astype(str)))
    frame["filing_time"] = frame["receipt_no"].astype(str).map(time_map)
    frame["filing_mins"] = frame["filing_time"].map(_mins)
    frame = apply_timeaware_returns(frame, return_basis="raw")
    frame["event_date"] = pd.to_datetime(frame["event_date"])
    matched = frame[frame["filing_mins"].notna()].copy()

    folds = make_folds(matched.rename(columns={"ret_timeaware": "realized_net_return"}))
    scored = [fold for _, fold in folds if len(fold) >= MIN_WINDOW_EVENTS]
    uniform_mean = sum(fold["ret_uniform"].sum() for fold in scored) / len(matched) * 100
    timeaware_mean = (
        sum(fold["realized_net_return"].sum() for fold in scored)
        / len(matched)
        * 100
    )

    feature_frame = pd.DataFrame(
        [extract_features(row) for row in matched.to_dict("records")],
        columns=FEATURE_NAMES,
        index=matched.index,
    )
    learner_frame = pd.concat(
        [feature_frame, matched[["event_date"]].copy()], axis=1
    )
    learner_frame["realized_net_return"] = matched["ret_timeaware"].values
    learner_frame["label"] = (learner_frame["realized_net_return"] > 0).astype(int)
    learner = summarize_learner(
        learner_frame.sort_values("event_date").reset_index(drop=True),
        label="buyback_timeaware",
        random_state=0,
    )
    return {
        "events_with_prices": len(frame),
        "events_with_filing_time": len(matched),
        "uniform_mean_net_pct": uniform_mean,
        "timeaware_mean_net_pct": timeaware_mean,
        "entry_timing_delta_pct": timeaware_mean - uniform_mean,
        "uniform_positive_folds": sum(fold["ret_uniform"].mean() > 0 for fold in scored),
        "timeaware_positive_folds": sum(
            fold["realized_net_return"].mean() > 0 for fold in scored
        ),
        "scored_folds": len(scored),
        "learned_selector_model_mean_net_pct": learner["model_mean_net_pct"],
        "learned_selector_selection_lift_pct": learner["selection_lift_pct"],
    }


def _intraday_comparison(*, before_dir: Path) -> dict[str, Any]:
    before = _load_json(before_dir / "buyback_intraday_summary.json")
    after = _corrected_intraday(before_dir)
    keys = (
        "uniform_mean_net_pct",
        "timeaware_mean_net_pct",
        "entry_timing_delta_pct",
    )
    return {
        "events_with_filing_time": after["events_with_filing_time"],
        "metrics": {
            key: {
                "before": before["headline"][key],
                "after": after[key],
                "delta_pct_points": _delta(after[key], before["headline"][key]),
            }
            for key in keys
        },
        "positive_folds": {
            "uniform_before": before["headline"]["uniform_positive_folds"],
            "uniform_after": after["uniform_positive_folds"],
            "timeaware_before": before["headline"]["timeaware_positive_folds"],
            "timeaware_after": after["timeaware_positive_folds"],
            "scored_folds": after["scored_folds"],
        },
        "learned_selector": {
            "model_mean_net_pct_after": after[
                "learned_selector_model_mean_net_pct"
            ],
            "selection_lift_pct_after": after[
                "learned_selector_selection_lift_pct"
            ],
        },
    }


def build_comparison(*, before_dir: Path) -> dict[str, Any]:
    before_cross = _load_json(before_dir / "cross_category_summary.json")
    model = CostModel()
    input_paths = [
        PROJECT_ROOT / f"data/event_study_{category}.csv" for category in CATEGORIES
    ]
    return {
        "schema_version": 1,
        "milestone": "M0.2",
        "comparison": "pre_revision_flat_cost_vs_historical_cost_schedule",
        "assumptions": {
            **model.model_dump(),
            "slippage_semantics": "basis points per side",
            "same_notional_approximation": True,
            "tradability_bar_changed": False,
        },
        "tax_schedule": [
            {
                "start": regime.start.isoformat(),
                "end": regime.end.isoformat(),
                "kospi_transaction_tax": regime.kospi_transaction_tax,
                "kospi_special_rural_tax": regime.kospi_special_rural_tax,
                "kosdaq_transaction_tax": regime.kosdaq_transaction_tax,
                "kosdaq_special_rural_tax": regime.kosdaq_special_rural_tax,
            }
            for regime in TAX_REGIMES
        ],
        "inputs": [
            {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "sha256": sha256_file(path),
            }
            for path in input_paths
        ],
        "generator_sources": [
            {
                "path": relative_path,
                "sha256": sha256_file(PROJECT_ROOT / relative_path),
            }
            for relative_path in GENERATOR_SOURCES
        ],
        "categories": _category_comparison(before_cross=before_cross),
        "buyback_learner": _learner_comparison(before_dir=before_dir),
        "buyback_intraday": _intraday_comparison(before_dir=before_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-dir", type=Path, default=DEFAULT_BEFORE_DIR)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write a current-tree replay to a new path; the verified artifact is immutable",
    )
    return parser.parse_args()


def require_nonhistorical_output(output: Path) -> None:
    if output.resolve() == DEFAULT_OUTPUT.resolve():
        raise ValueError(
            "the verified M0.2 comparison is immutable; choose a different --output"
        )


def main() -> int:
    args = parse_args()
    if args.output is None:
        actual = sha256_file(DEFAULT_OUTPUT)
        if actual != IMMUTABLE_ARTIFACT_SHA256:
            raise ValueError(
                f"immutable M0.2 artifact hash mismatch: {actual}"
            )
        print(
            json.dumps(
                {
                    "artifact": str(DEFAULT_OUTPUT),
                    "sha256": actual,
                    "status": "verified_immutable_artifact",
                },
                indent=2,
            )
        )
        return 0
    require_nonhistorical_output(args.output)
    payload = build_comparison(before_dir=args.before_dir)
    write_json(args.output, payload)
    print(json.dumps({"output": str(args.output), "categories": len(payload["categories"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
