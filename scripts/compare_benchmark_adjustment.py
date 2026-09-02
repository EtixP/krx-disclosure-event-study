"""Generate the deterministic M0.3 raw-versus-abnormal comparison artifact."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from kdtb.data.benchmarks import BENCHMARKS, BENCHMARK_SOURCE, normalize_benchmark_history
from kdtb.learning.dataset import load_mock_trades
from kdtb.learning.features import FEATURE_NAMES, extract_features
from kdtb.learning.walk_forward_trainer import make_folds
from kdtb.research.baseline import sha256_file, write_json
from kdtb.research.baseline import summarize_learner
from scripts.analyze_event_category import MIN_WINDOW_EVENTS, analyze
from scripts.run_intraday_walkforward import _mins, apply_timeaware_returns
from scripts.summarize_all_categories import CATEGORIES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_M0_2 = PROJECT_ROOT / "artifacts/m0_2/historical_cost_comparison.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "artifacts/m0_3/benchmark_adjustment_comparison.json"
BENCHMARK_CACHE = PROJECT_ROOT / "data/benchmark_indices.csv"
BENCHMARK_META = PROJECT_ROOT / "data/benchmark_indices.meta.json"
GENERATOR_SOURCES = (
    "src/kdtb/backtest/cost_model.py",
    "src/kdtb/backtest/metrics.py",
    "src/kdtb/data/benchmarks.py",
    "src/kdtb/learning/dataset.py",
    "src/kdtb/learning/features.py",
    "src/kdtb/learning/policy.py",
    "src/kdtb/learning/walk_forward_trainer.py",
    "scripts/analyze_event_category.py",
    "scripts/backfill_event_study_benchmarks.py",
    "scripts/compare_benchmark_adjustment.py",
    "scripts/run_intraday_walkforward.py",
    "scripts/summarize_all_categories.py",
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _metric(raw: float, abnormal: float, prior_raw: float) -> dict[str, Any]:
    raw_value = float(raw)
    abnormal_value = float(abnormal)
    prior_value = float(prior_raw)
    return {
        "m0_2_raw_net_pct": prior_value,
        "current_raw_net_pct": raw_value,
        "abnormal_net_pct": abnormal_value,
        "abnormal_minus_raw_pct_points": abnormal_value - raw_value,
        "raw_unchanged_from_m0_2": bool(abs(raw_value - prior_value) < 1e-8),
    }


def _positive_folds(folds: list[dict[str, Any]]) -> tuple[int, int]:
    scored = [fold for fold in folds if fold.get("n", 0) >= MIN_WINDOW_EVENTS]
    return sum(fold.get("mean_pct", 0) > 0 for fold in scored), len(scored)


def _learner_comparison(prior: dict[str, Any]) -> dict[str, Any]:
    raw = summarize_learner(
        load_mock_trades("buyback", return_basis="raw"),
        label="buyback_raw",
        random_state=0,
    )
    abnormal = summarize_learner(
        load_mock_trades("buyback", return_basis="abnormal"),
        label="buyback_abnormal",
        random_state=0,
    )
    keys = (
        "model_mean_net_pct",
        "always_all_mean_net_pct",
        "matched_always_mean_net_pct",
        "selection_lift_pct",
    )
    return {
        "mock_trades": abnormal["mock_trades"],
        "metrics": {
            key: _metric(
                raw[key], abnormal[key], prior["metrics"][key]["after"]
            )
            for key in keys
        },
        "raw_verdict": raw["verdict"],
        "abnormal_verdict": abnormal["verdict"],
        "raw_model_trades": raw["model_trades"],
        "abnormal_model_trades": abnormal["model_trades"],
        "raw_promotions": raw["promotions"],
        "abnormal_promotions": abnormal["promotions"],
    }


def _intraday_summary(return_basis: str) -> dict[str, Any]:
    frame = pd.read_csv(
        PROJECT_ROOT / "data/event_study_buyback.csv",
        dtype={"receipt_no": "string"},
    )
    frame = frame.dropna(subset=["t0_close", "t+1_close", "t+5_close"]).copy()
    frame = frame[frame["t+1_close"] > 0]
    times = pd.read_csv(
        PROJECT_ROOT
        / "artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv",
        dtype={"receipt_no": "string", "filing_time": "string"},
    )
    time_map = dict(zip(times["receipt_no"].astype(str), times["filing_time"].astype(str)))
    frame["filing_time"] = frame["receipt_no"].astype(str).map(time_map)
    frame["filing_mins"] = frame["filing_time"].map(_mins)
    frame = apply_timeaware_returns(frame, return_basis=return_basis)
    frame["event_date"] = pd.to_datetime(frame["event_date"])
    matched = frame[frame["filing_mins"].notna()].copy()
    folds = make_folds(matched.rename(columns={"ret_timeaware": "realized_net_return"}))
    scored = [fold for _, fold in folds if len(fold) >= MIN_WINDOW_EVENTS]
    uniform_mean = sum(fold["ret_uniform"].sum() for fold in scored) / len(matched) * 100
    timeaware_mean = (
        sum(fold["realized_net_return"].sum() for fold in scored) / len(matched) * 100
    )
    feature_frame = pd.DataFrame(
        [extract_features(row) for row in matched.to_dict("records")],
        columns=FEATURE_NAMES,
        index=matched.index,
    )
    learner_frame = pd.concat([feature_frame, matched[["event_date"]].copy()], axis=1)
    learner_frame["realized_net_return"] = matched["ret_timeaware"].values
    learner_frame["label"] = (learner_frame["realized_net_return"] > 0).astype(int)
    learner = summarize_learner(
        learner_frame.sort_values("event_date").reset_index(drop=True),
        label=f"buyback_timeaware_{return_basis}",
        random_state=0,
    )
    return {
        "events_with_filing_time": len(matched),
        "uniform_mean_net_pct": uniform_mean,
        "timeaware_mean_net_pct": timeaware_mean,
        "entry_timing_delta_pct": timeaware_mean - uniform_mean,
        "uniform_positive_folds": sum(fold["ret_uniform"].mean() > 0 for fold in scored),
        "timeaware_positive_folds": sum(
            fold["realized_net_return"].mean() > 0 for fold in scored
        ),
        "timing_delta_positive_folds": sum(
            (fold["realized_net_return"] - fold["ret_uniform"]).mean() > 0
            for fold in scored
        ),
        "scored_folds": len(scored),
        "learner": learner,
    }


def _intraday_comparison(prior: dict[str, Any]) -> dict[str, Any]:
    raw = _intraday_summary("raw")
    abnormal = _intraday_summary("abnormal")
    metric_keys = (
        "uniform_mean_net_pct",
        "timeaware_mean_net_pct",
        "entry_timing_delta_pct",
    )
    return {
        "events_with_filing_time": abnormal["events_with_filing_time"],
        "metrics": {
            key: _metric(raw[key], abnormal[key], prior["metrics"][key]["after"])
            for key in metric_keys
        },
        "positive_folds": {
            "scored": abnormal["scored_folds"],
            "raw_uniform": raw["uniform_positive_folds"],
            "abnormal_uniform": abnormal["uniform_positive_folds"],
            "raw_timeaware": raw["timeaware_positive_folds"],
            "abnormal_timeaware": abnormal["timeaware_positive_folds"],
            "raw_timing_delta": raw["timing_delta_positive_folds"],
            "abnormal_timing_delta": abnormal["timing_delta_positive_folds"],
        },
        "learned_selector": {
            "raw_model_mean_net_pct": raw["learner"]["model_mean_net_pct"],
            "abnormal_model_mean_net_pct": abnormal["learner"]["model_mean_net_pct"],
            "raw_selection_lift_pct": raw["learner"]["selection_lift_pct"],
            "abnormal_selection_lift_pct": abnormal["learner"]["selection_lift_pct"],
            "raw_verdict": raw["learner"]["verdict"],
            "abnormal_verdict": abnormal["learner"]["verdict"],
        },
    }


def build_comparison(*, m0_2_path: Path = DEFAULT_M0_2) -> dict[str, Any]:
    prior = _load_json(m0_2_path)
    prior_by_category = {row["category"]: row for row in prior["categories"]}
    history = normalize_benchmark_history(pd.read_csv(BENCHMARK_CACHE))
    categories = []
    for category in CATEGORIES:
        result = analyze(category)
        old = prior_by_category[category]
        aggregate = result["aggregate"]
        raw_realistic = result["realistic"]["realistic"]["mean_pct"]
        abnormal_realistic = result["realistic_abnormal"]["realistic"]["mean_pct"]
        raw_positive, scored = _positive_folds(result["walk_forward"])
        abnormal_positive, abnormal_scored = _positive_folds(
            result["walk_forward_abnormal"]
        )
        if scored != abnormal_scored:
            raise ValueError(f"walk-forward fold count mismatch for {category}")
        categories.append(
            {
                "category": category,
                "n_events": result["n_events"],
                "metrics": {
                    "idealized_t1": _metric(
                        aggregate["t1_net"]["mean_pct"],
                        aggregate["t1_abnormal_net"]["mean_pct"],
                        old["metrics"]["idealized_t1_mean_net_pct"]["after"],
                    ),
                    "idealized_t5": _metric(
                        aggregate["t5_net"]["mean_pct"],
                        aggregate["t5_abnormal_net"]["mean_pct"],
                        old["metrics"]["idealized_t5_mean_net_pct"]["after"],
                    ),
                    "realistic_t1_to_t5": _metric(
                        raw_realistic,
                        abnormal_realistic,
                        old["metrics"]["realistic_t1_to_t5_mean_net_pct"]["after"],
                    ),
                },
                "walk_forward": {
                    "scored_folds": scored,
                    "raw_positive_folds": raw_positive,
                    "abnormal_positive_folds": abnormal_positive,
                },
                "verdict": {
                    "raw": result["verdict_raw"],
                    "abnormal": result["verdict"],
                },
            }
        )

    input_paths = [
        PROJECT_ROOT / f"data/event_study_{category}.csv" for category in CATEGORIES
    ] + [
        BENCHMARK_CACHE,
        BENCHMARK_META,
        m0_2_path,
        PROJECT_ROOT / "artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv",
    ]
    ranges = {}
    for market, sub in history.groupby("market"):
        ranges[str(market)] = {
            "rows": len(sub),
            "start": sub["date"].min().date().isoformat(),
            "end": sub["date"].max().date().isoformat(),
        }
    return {
        "schema_version": 1,
        "milestone": "M0.3",
        "comparison": "historical_cost_raw_returns_vs_broad_market_abnormal_returns",
        "methodology": {
            "benchmark_source": BENCHMARK_SOURCE,
            "assignment": {
                market: {
                    "benchmark_symbol": spec.symbol,
                    "pykrx_ticker_reference": spec.pykrx_ticker,
                }
                for market, spec in BENCHMARKS.items()
            },
            "return_formula": "stock_simple_return - benchmark_simple_return",
            "alignment": "same exact stock observation entry/exit dates; no fill",
            "transaction_costs": "subtracted once after benchmark adjustment",
            "decision_time_use": "outcome attribution only; not a decision feature",
            "benchmark_ranges": ranges,
        },
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
        "categories": categories,
        "buyback_learner": _learner_comparison(prior["buyback_learner"]),
        "buyback_intraday": _intraday_comparison(prior["buyback_intraday"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--m0-2", type=Path, default=DEFAULT_M0_2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_comparison(m0_2_path=args.m0_2)
    write_json(args.output, payload)
    print(json.dumps({"output": str(args.output), "categories": len(payload["categories"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
