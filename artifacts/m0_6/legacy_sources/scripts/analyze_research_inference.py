"""Generate the deterministic M0.5 research-inference report.

The report adds issuer-clustered pointwise confidence intervals to the seven
headline category effects and fixed tail stress tests to the historical
buyback timing result. It does not convert exploratory historical findings
into confirmatory evidence.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.data.benchmarks import require_benchmark_columns
from kdtb.learning.walk_forward_trainer import make_folds
from kdtb.research.baseline import sha256_file, write_json
from kdtb.research.inference import issuer_clustered_mean_ci, tail_sensitivity
from scripts.analyze_event_category import analyze
from scripts.run_intraday_walkforward import apply_timeaware_returns, _mins
from scripts.summarize_all_categories import CATEGORIES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILING_TIMES = (
    PROJECT_ROOT
    / "artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "artifacts/m0_5/research_inference.json"
CONFIDENCE_LEVEL = 0.95
N_RESAMPLES = 10_000
RANDOM_STATE = 0
TAIL_FRACTIONS = (0.01, 0.05)
GENERATOR_SOURCES = (
    "pyproject.toml",
    "requirements.txt",
    "src/kdtb/backtest/cost_model.py",
    "src/kdtb/backtest/metrics.py",
    "src/kdtb/data/benchmarks.py",
    "src/kdtb/learning/walk_forward_trainer.py",
    "src/kdtb/research/baseline.py",
    "src/kdtb/research/inference.py",
    "scripts/analyze_event_category.py",
    "scripts/analyze_research_inference.py",
    "scripts/run_intraday_walkforward.py",
    "scripts/summarize_all_categories.py",
)
VERIFIED_UPSTREAM_SHA256 = {
    "artifacts/baselines/pre_revision/manifest.json": (
        "7618e0d62028d44fea96edc496adb7fc0ada1f6a063ca3aee1e680f8b8c81776"
    ),
    "artifacts/m0_2/historical_cost_comparison.json": (
        "b529d30ef58606ebff1fec2507a644b8851e6dc921c5bdb57ca5fdf4ded7dc77"
    ),
    "artifacts/m0_3/benchmark_adjustment_comparison.json": (
        "0ac51cc563114f2b57c0fc6987a5984889fb4dd78bfb4643f6c9882879820abf"
    ),
    "artifacts/m0_4/price_adjustment_audit.json": (
        "1acf9366a9147fa78835270d90db7dad8fae9a4a8e9c90018e1a4fa92bac254b"
    ),
}


def _event_csv(category: str) -> Path:
    return PROJECT_ROOT / f"data/event_study_{category}.csv"


def _file_record(path: Path) -> dict[str, str]:
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "sha256": sha256_file(path),
    }


def _verified_upstream_records() -> list[dict[str, str]]:
    records = []
    for relative_path, expected in VERIFIED_UPSTREAM_SHA256.items():
        record = _file_record(PROJECT_ROOT / relative_path)
        if record["sha256"] != expected:
            raise ValueError(
                f"verified upstream artifact changed: {relative_path}; "
                f"expected {expected}, got {record['sha256']}"
            )
        records.append(record)
    return records


def _as_pct(summary: dict[str, Any]) -> dict[str, Any]:
    scaled = dict(summary)
    for key in ("estimate", "ci_lower", "ci_upper", "bootstrap_standard_error"):
        scaled[f"{key}_pct"] = float(scaled.pop(key) * 100.0)
    return scaled


def _tail_as_pct(summary: dict[str, Any]) -> dict[str, Any]:
    def scale_distribution(row: dict[str, Any]) -> dict[str, Any]:
        scaled = dict(row)
        scaled["mean_pct"] = float(scaled.pop("mean") * 100.0)
        scaled["median_pct"] = float(scaled.pop("median") * 100.0)
        scaled["win_rate_pct"] = float(scaled.pop("win_fraction") * 100.0)
        return scaled

    return {
        "full_sample": scale_distribution(summary["full_sample"]),
        "quantiles_pct": {
            name: float(value * 100.0)
            for name, value in summary["quantiles"].items()
        },
        "scenarios": [scale_distribution(row) for row in summary["scenarios"]],
    }


def _category_scenario_frame(category: str) -> pd.DataFrame:
    frame = pd.read_csv(
        _event_csv(category),
        dtype={
            "corp_code": "string",
            "receipt_no": "string",
            "stock_code": "string",
        },
    )
    frame = frame.dropna(subset=["ret_5d", "t+1_close", "t+5_close"]).copy()
    required = ("stock_code", "t+1_date", "t+5_date", "market")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"{category} lacks inference columns: {', '.join(missing)}")
    if frame[list(required)].isna().any().any():
        raise ValueError(f"{category} contains missing inference provenance")
    if (frame[["t+1_close", "t+5_close"]] <= 0).any().any():
        raise ValueError(f"{category} contains non-positive scenario closes")
    require_benchmark_columns(frame, tokens=("t1", "t5"))

    model = CostModel()
    costs = model.roundtrip_cost_fractions(
        buy_dates=frame["t+1_date"],
        sell_dates=frame["t+5_date"],
        markets=frame["market"],
    )
    stock_gross = frame["t+5_close"] / frame["t+1_close"] - 1.0
    benchmark_gross = (
        frame["benchmark_t5_close"] / frame["benchmark_t1_close"] - 1.0
    )
    frame["realistic_raw_net"] = stock_gross - costs
    frame["realistic_abnormal_net"] = stock_gross - benchmark_gross - costs
    values = frame[["realistic_raw_net", "realistic_abnormal_net"]].to_numpy()
    if not np.isfinite(values).all():
        raise ValueError(f"{category} inference returns contain non-finite values")
    return frame


def _reconcile_category_point(
    category: str, frame: pd.DataFrame
) -> dict[str, Any]:
    current = analyze(category)
    if "error" in current:
        raise ValueError(f"headline analysis failed for {category}: {current['error']}")
    expected = {
        "raw_mean_pct": current["realistic"]["realistic"]["mean_pct"],
        "abnormal_mean_pct": current["realistic_abnormal"]["realistic"]["mean_pct"],
        "n": current["realistic_abnormal"]["realistic"]["n"],
        "verdict": current["verdict"],
    }
    observed = {
        "raw_mean_pct": float(frame["realistic_raw_net"].mean() * 100.0),
        "abnormal_mean_pct": float(
            frame["realistic_abnormal_net"].mean() * 100.0
        ),
        "n": len(frame),
    }
    if observed["n"] != expected["n"]:
        raise ValueError(f"{category} realistic event-count reconciliation failed")
    for key in ("raw_mean_pct", "abnormal_mean_pct"):
        if not math.isclose(observed[key], expected[key], abs_tol=0.00005):
            raise ValueError(f"{category} {key} reconciliation failed")
    return {"current_headline": expected, "independent_formula": observed}


def _category_inference(
    category: str,
    *,
    confidence_level: float,
    n_resamples: int,
    random_state: int,
) -> dict[str, Any]:
    frame = _category_scenario_frame(category)
    issuers = frame["stock_code"].tolist()
    return {
        "category": category,
        "hypothesis_id": f"historical_category_screen:{category}",
        "hypothesis_status": "exploratory",
        "point_reconciliation": _reconcile_category_point(category, frame),
        "realistic_t1_to_t5": {
            "raw_net": _as_pct(
                issuer_clustered_mean_ci(
                    frame["realistic_raw_net"].to_numpy(),
                    issuers,
                    confidence_level=confidence_level,
                    n_resamples=n_resamples,
                    random_state=random_state,
                )
            ),
            "abnormal_net": _as_pct(
                issuer_clustered_mean_ci(
                    frame["realistic_abnormal_net"].to_numpy(),
                    issuers,
                    confidence_level=confidence_level,
                    n_resamples=n_resamples,
                    random_state=random_state,
                )
            ),
        },
    }


def _load_buyback_timing(filing_times_path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        _event_csv("buyback"),
        dtype={
            "corp_code": "string",
            "receipt_no": "string",
            "stock_code": "string",
        },
    )
    frame = frame.dropna(subset=["t0_close", "t+1_close", "t+5_close"]).copy()
    frame = frame[frame["t+1_close"] > 0].copy()
    times = pd.read_csv(
        filing_times_path,
        dtype={"receipt_no": "string", "filing_time": "string"},
    )
    if times["receipt_no"].isna().any() or times["receipt_no"].duplicated().any():
        raise ValueError("pinned filing times require unique receipt numbers")
    time_map = dict(zip(times["receipt_no"], times["filing_time"]))
    frame["filing_time"] = frame["receipt_no"].map(time_map)
    frame["filing_mins"] = frame["filing_time"].map(_mins)
    matched = frame[frame["filing_mins"].notna()].copy()
    if matched.empty:
        raise ValueError("no buyback rows match the pinned filing-time input")
    matched = apply_timeaware_returns(
        matched,
        CostModel(),
        return_basis="abnormal",
    )
    matched["event_date"] = pd.to_datetime(matched["event_date"])
    return matched


def _legacy_timing_headline(frame: pd.DataFrame, basis: str) -> dict[str, Any]:
    timeaware = f"ret_timeaware_{basis}"
    uniform = f"ret_uniform_{basis}"
    fold_input = frame.rename(columns={timeaware: "realized_net_return"})
    folds = make_folds(fold_input)
    scored = [fold for _, fold in folds if len(fold) >= 5]
    uniform_mean = sum(fold[uniform].sum() for fold in scored) / len(frame)
    timeaware_mean = (
        sum(fold["realized_net_return"].sum() for fold in scored) / len(frame)
    )
    return {
        "uniform_mean_pct": float(uniform_mean * 100.0),
        "timeaware_mean_pct": float(timeaware_mean * 100.0),
        "paired_delta_pct": float((timeaware_mean - uniform_mean) * 100.0),
        "uniform_positive_folds": sum(fold[uniform].mean() > 0 for fold in scored),
        "timeaware_positive_folds": sum(
            fold["realized_net_return"].mean() > 0 for fold in scored
        ),
        "delta_positive_folds": sum(
            (fold["realized_net_return"] - fold[uniform]).mean() > 0
            for fold in scored
        ),
        "generated_folds": len(folds),
        "scored_folds": len(scored),
    }


def _effect_ci(
    values: pd.Series,
    issuers: list[str],
    *,
    confidence_level: float,
    n_resamples: int,
    random_state: int,
) -> dict[str, Any]:
    return _as_pct(
        issuer_clustered_mean_ci(
            values.to_numpy(),
            issuers,
            confidence_level=confidence_level,
            n_resamples=n_resamples,
            random_state=random_state,
        )
    )


def _buyback_timing_inference(
    filing_times_path: Path,
    *,
    confidence_level: float,
    n_resamples: int,
    random_state: int,
) -> dict[str, Any]:
    frame = _load_buyback_timing(filing_times_path)
    issuers = frame["stock_code"].astype(str).tolist()
    effects: dict[str, Any] = {}
    tails: dict[str, Any] = {}
    for basis in ("raw", "abnormal"):
        uniform = frame[f"ret_uniform_{basis}"]
        timeaware = frame[f"ret_timeaware_{basis}"]
        delta = timeaware - uniform
        effects[basis] = {
            "uniform_entry": _effect_ci(
                uniform,
                issuers,
                confidence_level=confidence_level,
                n_resamples=n_resamples,
                random_state=random_state,
            ),
            "timeaware_entry": _effect_ci(
                timeaware,
                issuers,
                confidence_level=confidence_level,
                n_resamples=n_resamples,
                random_state=random_state,
            ),
            "paired_entry_timing_delta": _effect_ci(
                delta,
                issuers,
                confidence_level=confidence_level,
                n_resamples=n_resamples,
                random_state=random_state,
            ),
        }
        tails[f"timeaware_{basis}_net"] = _tail_as_pct(
            tail_sensitivity(timeaware.to_numpy(), fractions=TAIL_FRACTIONS)
        )
        tails[f"paired_entry_timing_delta_{basis}"] = _tail_as_pct(
            tail_sensitivity(delta.to_numpy(), fractions=TAIL_FRACTIONS)
        )

    return {
        "hypothesis_id": "historical_buyback_entry_timing",
        "hypothesis_status": "exploratory",
        "sample": {
            "events": len(frame),
            "issuers": int(frame["stock_code"].nunique()),
            "filing_time_input": str(filing_times_path.relative_to(PROJECT_ROOT)),
            "event_date_min": frame["event_date"].min().date().isoformat(),
            "event_date_max": frame["event_date"].max().date().isoformat(),
        },
        "legacy_headline_reconciliation": {
            basis: _legacy_timing_headline(frame, basis)
            for basis in ("raw", "abnormal")
        },
        "all_matched_event_inference": effects,
        "tail_sensitivity": tails,
        "interpretation_boundary": (
            "historical exploratory timing effect; not confirmatory alpha and not "
            "evidence of executable closing-auction fills"
        ),
    }


def _hypothesis_governance() -> dict[str, Any]:
    return {
        "status_definitions": {
            "pre_specified": (
                "hypothesis and decision rule frozen before inspecting the "
                "evaluation outcomes"
            ),
            "exploratory": (
                "hypothesis formed, selected, or repeatedly examined using the "
                "same historical sample"
            ),
            "confirmatory": (
                "pre-specified hypothesis evaluated once on data not used to form it"
            ),
        },
        "current_classification": {
            "pre_specified": [],
            "exploratory": [
                "historical_category_screen:*",
                "historical_buyback_entry_timing",
                "historical_shareholder_change_blacklist",
                "legacy_subgroup_screens:*",
            ],
            "confirmatory": [],
        },
        "analysis_procedure_status": "fixed_reanalysis_not_preregistration",
        "multiple_testing": (
            "Intervals are pointwise descriptive intervals for an exploratory "
            "family of seven categories. They are not multiplicity-adjusted and "
            "must not be read as familywise confirmatory tests. Formal correction "
            "belongs with a prospectively frozen hypothesis family."
        ),
        "promotion_rule": (
            "No historical category or subgroup becomes a strategy from these "
            "intervals; confirmatory status requires untouched future data."
        ),
    }


def build_report(
    *,
    filing_times_path: Path = DEFAULT_FILING_TIMES,
    confidence_level: float = CONFIDENCE_LEVEL,
    n_resamples: int = N_RESAMPLES,
    random_state: int = RANDOM_STATE,
) -> dict[str, Any]:
    category_rows = [
        _category_inference(
            category,
            confidence_level=confidence_level,
            n_resamples=n_resamples,
            random_state=random_state,
        )
        for category in CATEGORIES
    ]
    input_paths = [_event_csv(category) for category in CATEGORIES]
    input_paths.append(filing_times_path)
    return {
        "schema_version": 1,
        "milestone": "M0.5",
        "methodology": {
            "headline_estimand": "event_weighted_mean_realistic_t1_to_t5_net_return",
            "dependence_unit": "issuer_stock_code",
            "interval": "percentile_cluster_bootstrap",
            "confidence_level": confidence_level,
            "n_resamples": n_resamples,
            "random_state": random_state,
            "tail_fractions": list(TAIL_FRACTIONS),
            "tail_scenarios": [
                "exclude_top",
                "exclude_bottom",
                "symmetric_trim",
            ],
        },
        "hypothesis_governance": _hypothesis_governance(),
        "category_headline_inference": category_rows,
        "buyback_timing_inference": _buyback_timing_inference(
            filing_times_path,
            confidence_level=confidence_level,
            n_resamples=n_resamples,
            random_state=random_state,
        ),
        "result_change": (
            "point estimates are unchanged; inference now measures issuer "
            "dependence and separates raw, abnormal, and paired timing tails"
        ),
        "inputs": [_file_record(path) for path in input_paths],
        "generator_sources": [
            _file_record(PROJECT_ROOT / path) for path in GENERATOR_SOURCES
        ],
        "verified_upstream_artifacts": _verified_upstream_records(),
        "runtime_versions": {
            "python": sys.version.split()[0],
            "numpy": importlib.metadata.version("numpy"),
            "pandas": importlib.metadata.version("pandas"),
        },
    }


def _scenario_mean(tail: dict[str, Any], name: str, fraction: float) -> float:
    match = [
        row
        for row in tail["scenarios"]
        if row["scenario"] == name and row["fraction"] == fraction
    ]
    if len(match) != 1:
        raise ValueError(f"missing tail scenario {name} {fraction}")
    return match[0]["mean_pct"]


def _print_report(report: dict[str, Any]) -> None:
    print("\nM0.5 issuer-clustered headline inference (all hypotheses exploratory)")
    print(f"{'category':>20} {'n':>6} {'issuers':>8} {'abn mean':>10} {'pointwise 95% CI':>25}")
    for row in report["category_headline_inference"]:
        ci = row["realistic_t1_to_t5"]["abnormal_net"]
        print(
            f"{row['category']:>20} {ci['n_events']:>6} {ci['n_issuers']:>8} "
            f"{ci['estimate_pct']:>+9.3f}% "
            f"[{ci['ci_lower_pct']:+.3f}%, {ci['ci_upper_pct']:+.3f}%]"
        )
    timing = report["buyback_timing_inference"]
    delta = timing["all_matched_event_inference"]["abnormal"][
        "paired_entry_timing_delta"
    ]
    tail = timing["tail_sensitivity"]["paired_entry_timing_delta_abnormal"]
    print("\nExploratory buyback paired entry-timing delta (abnormal):")
    print(
        f"  mean {delta['estimate_pct']:+.3f}% | pointwise 95% CI "
        f"[{delta['ci_lower_pct']:+.3f}%, {delta['ci_upper_pct']:+.3f}%]"
    )
    print(
        "  excluding top 5%: "
        f"{_scenario_mean(tail, 'exclude_top', 0.05):+.3f}%"
    )
    print("  status: exploratory; no confirmatory historical claim")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filing-times", type=Path, default=DEFAULT_FILING_TIMES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resamples", type=int, default=N_RESAMPLES)
    parser.add_argument("--random-state", type=int, default=RANDOM_STATE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(
        filing_times_path=args.filing_times,
        n_resamples=args.resamples,
        random_state=args.random_state,
    )
    write_json(args.output, report)
    _print_report(report)
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
