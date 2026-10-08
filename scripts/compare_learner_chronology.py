"""M0.6 bounded daily learner correction. No DB, network or historical writes."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path

import pandas as pd

from kdtb.learning.dataset import load_mock_trades
from kdtb.learning.walk_forward_trainer import run_walk_forward
from kdtb.research.baseline import sha256_file, summarize_learner, write_json
from kdtb.research.historical_learning import load_mock_trades as historical_trades

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "artifacts/m0_6/learner_chronology_v1.json"
SOURCES = (
    "scripts/compare_learner_chronology.py",
    "src/kdtb/learning/chronology.py",
    "src/kdtb/learning/dataset.py",
    "src/kdtb/learning/policy.py",
    "src/kdtb/learning/walk_forward_trainer.py",
    "src/kdtb/learning/features.py",
    "src/kdtb/research/historical_learning.py",
    "src/kdtb/research/baseline.py",
    "src/kdtb/backtest/cost_model.py",
    "src/kdtb/data/benchmarks.py",
    "pyproject.toml",
    "requirements.txt",
)
INPUTS = (
    "data/event_study_buyback.csv",
    "data/benchmark_indices.csv",
    "data/benchmark_indices.meta.json",
    "data/event_study_convertible_bond.csv",
    "artifacts/m0_3/benchmark_adjustment_comparison.json",
)


def records(paths):
    return [{"path": path, "sha256": sha256_file(ROOT / path)} for path in paths]


def _strict_summary(frame):
    report = run_walk_forward(frame, random_state=0)
    traded = [fold for fold in report.folds if fold.model_trades]
    matched_n = sum(fold.test_n for fold in traded)
    matched_pnl = sum(fold.always_pnl for fold in traded)
    n = report.total_model_trades
    all_n = sum(fold.test_n for fold in report.folds)
    mean = report.cumulative_model_pnl / n * 100 if n else 0.0
    matched_mean = matched_pnl / matched_n * 100 if matched_n else 0.0
    return {
        "mock_trades": len(frame),
        "promotions": report.n_promotions,
        "model_trades": n,
        "model_pnl_sum": report.cumulative_model_pnl,
        "model_mean_net_pct": mean,
        "always_all_trades": all_n,
        "always_all_mean_net_pct": (
            report.cumulative_always_pnl / all_n * 100 if all_n else 0.0
        ),
        "matched_always_trades": matched_n,
        "matched_always_mean_net_pct": matched_mean,
        "selection_lift_pct": mean - matched_mean,
        "folds_traded": len(traded),
        "testable_folds": len(report.folds),
        "folds": [asdict(fold) for fold in report.folds],
    }


def build_comparison():
    strict = load_mock_trades(
        "buyback", csv_path=str(ROOT / INPUTS[0]), calendar_path=str(ROOT / INPUTS[1])
    )
    old = historical_trades("buyback", csv_path=str(ROOT / INPUTS[0]))
    # Retain source-row order before the historical event-date quicksort. This
    # reproduces the legacy algorithm on precisely the admitted cohort.
    source = pd.read_csv(ROOT / INPUTS[0], dtype={"receipt_no": "string"})
    source_ids = source.loc[source.receipt_no.isin(strict.receipt_no), "receipt_no"]
    admitted_old = strict.set_index("receipt_no").loc[source_ids].reset_index()
    admitted_old = admitted_old.sort_values("event_date").reset_index(drop=True)
    legacy_all = summarize_learner(old, label="legacy_all", random_state=0)
    legacy_admitted = summarize_learner(
        admitted_old, label="legacy_admitted", random_state=0
    )
    corrected = _strict_summary(strict)
    cb = load_mock_trades(
        "convertible_bond",
        csv_path=str(ROOT / INPUTS[3]),
        calendar_path=str(ROOT / INPUTS[1]),
    )
    return {
        "schema_version": "m0.6_learner_chronology_v1",
        "milestone": "M0.6",
        "status": "exploratory_daily_reconstruction",
        "inputs": records(INPUTS),
        "generator_sources": records(SOURCES),
        "runtime": {
            key: importlib.metadata.version(key)
            for key in ("numpy", "pandas", "scikit-learn")
        },
        "methodology": {
            "reward": "stock minus matching benchmark return minus existing dated costs",
            "decision_date": "t+1_date",
            "feature_observation_date": "t0_date",
            "label_end_date": "t+5_date",
            "maturity": "label_end_date < cutoff; same-day excluded",
            "outer_boundaries": "Jan 1 / July 1 period starts, never inferred from label dates",
            "inner_split": "70/30 by unique candidate decision dates before maturity purging",
            "minimum_samples": "42 fit and 18 calibration after purging (existing min_train=60, val_frac=0.3)",
            "model_objective": "unchanged gradient boosting, threshold grid, PnL promotion, seed=0",
            "frozen_state": "fit labels before calibration; threshold labels before outer validation; promotion labels before test",
            "incumbent": "retain previously frozen incumbent when no challenger wins; initial policy NeverTrade",
            "units": "per-trade percent and additive reward; not compounded return",
            "legacy_admitted_order": "original CSV order, then historical event-date quicksort",
            "db_used": False,
        },
        "admission": strict.attrs["admission"],
        "convertible_bond_admission_diagnostic_only": cb.attrs["admission"],
        "results": {
            "legacy_all": legacy_all,
            "legacy_admitted": legacy_admitted,
            "strict_admitted": corrected,
        },
        "scientific_changes": [
            {
                "stage": "admission",
                "classification": "expected scientific change",
                "from": "legacy_all",
                "to": "legacy_admitted",
                "explanation": "exclude invalid reconstructed windows and unusable feature/entry/exit prices; surviving tie order can change under historical quicksort",
            },
            {
                "stage": "temporal_integrity",
                "classification": "expected scientific change",
                "from": "legacy_admitted",
                "to": "strict_admitted",
                "explanation": "decision-day cohorts, stable identity order, unique-date calibration and outcome-maturity purges",
            },
        ],
        "limitations": [
            "Reconstructed daily observations are not proof of original source availability or intraday executable fills.",
            "Pinned observed benchmark-session horizon is not authoritative proof of every exchange session.",
            "No price-generator repair, source-price rewrite, historical security master, adjustment-vintage or survivorship correction.",
            "Receipt-level duplicates across economic events, historical category/window inference and unversioned filings remain unresolved.",
            "Supply-contract strict learner refused: original-version extraction provenance unavailable, independent of DB hash.",
            "Current database hash mismatch is a separate pre-existing blocker; no database was read or reinitialized.",
            "Retrospectively inspected results are exploratory, not untouched out-of-sample or forward trading evidence.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON path; existing outputs are immutable",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(ROOT / "data")
        or output.is_relative_to(ROOT / "sources")
    ):
        parser.error("choose a new research output path outside protected data/sources")
    result = build_comparison()
    write_json(output, result)
    print(
        json.dumps(
            {
                key: {
                    metric: val[metric]
                    for metric in (
                        "mock_trades",
                        "model_trades",
                        "promotions",
                        "model_mean_net_pct",
                        "selection_lift_pct",
                    )
                }
                for key, val in result["results"].items()
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
