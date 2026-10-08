"""Deterministic pre-revision research snapshot generation and verification.

This module is deliberately a consumer of the existing research machinery. It
does not change event definitions, costs, entries, learner features, or model
selection. Its job is to preserve what the current implementation reports so
later methodology milestones can explain their result changes.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from kdtb.backtest.cost_model import TRADABILITY_BAR_PCT
from kdtb.learning.dataset import load_mock_trades
from kdtb.learning.features import FEATURE_NAMES, extract_features
from kdtb.learning.walk_forward_trainer import make_folds, run_walk_forward
from scripts.analyze_event_category import MIN_WINDOW_EVENTS, analyze
from scripts.run_intraday_walkforward import INTRADAY_CUTOFF_MIN, _mins
from scripts.summarize_all_categories import CATEGORIES
from scripts.train_learner import _synthetic_edge_df


SCHEMA_VERSION = 1
DEFAULT_SNAPSHOT_NAME = "pre_revision"
DEFAULT_OUTPUT_DIR = Path("artifacts/baselines/pre_revision")
BUYBACK_TIMES_INPUT = Path("inputs/buyback_filing_times.csv")

# Frozen pre-M0.2 assumption. This module intentionally reproduces the named
# pre-revision snapshot; current research uses the dated CostModel instead.
PRE_REVISION_COST_FRACTION = 0.00313
PRE_REVISION_COST_MODEL = {
    "commission_per_side": 0.00015,
    "tx_tax_on_sell": 0.0018,
    "vat_on_commission": 0.10,
    "slippage_bps": 5.0,
}

ARTIFACT_FILENAMES = (
    "cross_category_summary.json",
    "buyback_intraday_summary.json",
    "learner_buyback_summary.json",
    "shareholder_change_summary.json",
)

GENERATOR_SOURCE_PATHS = (
    "src/kdtb/research/baseline.py",
    "src/kdtb/backtest/cost_model.py",
    "src/kdtb/backtest/metrics.py",
    "src/kdtb/learning/dataset.py",
    "src/kdtb/learning/features.py",
    "src/kdtb/learning/policy.py",
    "src/kdtb/learning/walk_forward_trainer.py",
    "scripts/analyze_event_category.py",
    "scripts/run_intraday_walkforward.py",
    "scripts/summarize_all_categories.py",
    "scripts/train_learner.py",
    "scripts/verify_research_state.py",
)


class SnapshotVerificationError(ValueError):
    """Raised when a snapshot is incomplete or internally inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, *, display_path: str) -> dict[str, Any]:
    return {
        "path": display_path,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _clean_number(value: Any, digits: int = 10) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"non-finite number cannot be snapshotted: {number}")
        rounded = round(number, digits)
        return 0.0 if rounded == 0 else rounded
    return value


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (np.integer, np.floating, float)):
        return _clean_number(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write canonical, byte-stable JSON with an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        _json_ready(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        handle.write(encoded)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def _header(snapshot_name: str, artifact: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot": snapshot_name,
        "artifact": artifact,
    }


def _event_csv_path(project_root: Path, category: str) -> Path:
    return project_root / "data" / f"event_study_{category}.csv"


def _category_summary(result: dict[str, Any]) -> dict[str, Any]:
    valid_folds = [
        fold
        for fold in result["walk_forward"]
        if fold.get("n", 0) >= MIN_WINDOW_EVENTS
    ]
    positive_folds = sum(fold.get("mean_pct", 0) > 0 for fold in valid_folds)
    return {
        "category": result["category"],
        "source": result["csv_path"],
        "n_events": result["n_events"],
        "n_unique_stocks": result["n_unique_stocks"],
        "aggregate": result["aggregate"],
        "realistic": result["realistic"],
        "walk_forward": result["walk_forward"],
        "walk_forward_summary": {
            "generated_folds": len(result["walk_forward"]),
            "scored_folds": len(valid_folds),
            "positive_folds": positive_folds,
            "minimum_events_to_score": MIN_WINDOW_EVENTS,
        },
        "by_market": result["by_market"],
        "verdict": result["verdict"],
    }


def build_cross_category_summary(
    project_root: Path, snapshot_name: str
) -> dict[str, Any]:
    category_results: list[dict[str, Any]] = []
    for category in CATEGORIES:
        csv_path = _event_csv_path(project_root, category)
        if not csv_path.exists():
            raise FileNotFoundError(f"required event-study input is missing: {csv_path}")
        result = analyze(
            category,
            str(csv_path),
            flat_cost_fraction=PRE_REVISION_COST_FRACTION,
            include_abnormal=False,
        )
        if "error" in result:
            raise ValueError(f"{category} analysis failed: {result['error']}")
        result["csv_path"] = str(csv_path.relative_to(project_root))
        category_results.append(_category_summary(result))

    return {
        **_header(snapshot_name, "cross_category_summary"),
        "methodology": {
            "cost_model": PRE_REVISION_COST_MODEL,
            "roundtrip_cost_fraction": PRE_REVISION_COST_FRACTION,
            "tradability_bar_pct": TRADABILITY_BAR_PCT,
            "realistic_entry": "t+1_close",
            "exit": "t+5_close",
            "walk_forward_frequency": "half_year",
        },
        "category_order": list(CATEGORIES),
        "categories": category_results,
    }


def build_shareholder_change_summary(
    cross_category: dict[str, Any], snapshot_name: str
) -> dict[str, Any]:
    matching = [
        item
        for item in cross_category["categories"]
        if item["category"] == "shareholder_change"
    ]
    if len(matching) != 1:
        raise ValueError("cross-category result must contain one shareholder_change row")
    return {
        **_header(snapshot_name, "shareholder_change_summary"),
        "category_summary": matching[0],
        "current_product_use": "default long-side event blacklist",
        "status": "provisional_pending_methodology_corrections",
    }


def _fold_payload(fold: Any) -> dict[str, Any]:
    return {
        "fold_index": fold.fold_index,
        "period": fold.period,
        "train_n": fold.train_n,
        "test_n": fold.test_n,
        "model_pnl_sum": fold.model_pnl,
        "always_pnl_sum": fold.always_pnl,
        "never_pnl_sum": fold.never_pnl,
        "model_trades": fold.model_trades,
        "promoted": fold.promoted,
        "champion_version": fold.champion_version,
        "champion_is_learned": fold.champion_is_learned,
    }


def _learner_verdict(
    *, model_trades: int, breadth: float, selection_lift_pct: float, model_mean_pct: float
) -> str:
    if model_trades == 0:
        return "learned_to_abstain"
    if breadth < 0.5:
        return "insufficient_breadth"
    if selection_lift_pct <= 0:
        return "no_selection_edge"
    if model_mean_pct > TRADABILITY_BAR_PCT:
        return "positive_selective_edge"
    return "marginal_selection"


def summarize_learner(
    frame: pd.DataFrame, *, label: str, random_state: int
) -> dict[str, Any]:
    input_folds = make_folds(frame)
    report = run_walk_forward(frame, random_state=random_state)
    total_model_trades = report.total_model_trades
    model_mean_pct = (
        report.cumulative_model_pnl / total_model_trades * 100
        if total_model_trades
        else 0.0
    )
    always_trades = sum(fold.test_n for fold in report.folds)
    always_mean_pct = (
        report.cumulative_always_pnl / always_trades * 100 if always_trades else 0.0
    )
    traded_folds = [fold for fold in report.folds if fold.model_trades > 0]
    matched_always_pnl = sum(fold.always_pnl for fold in traded_folds)
    matched_always_n = sum(fold.test_n for fold in traded_folds)
    matched_always_mean_pct = (
        matched_always_pnl / matched_always_n * 100 if matched_always_n else 0.0
    )
    selection_lift_pct = model_mean_pct - matched_always_mean_pct
    breadth = len(traded_folds) / len(report.folds) if report.folds else 0.0

    return {
        "label": label,
        "random_state": random_state,
        "features": list(FEATURE_NAMES),
        "mock_trades": len(frame),
        "input_folds": len(input_folds),
        "testable_folds": len(report.folds),
        "folds_traded": len(traded_folds),
        "traded_periods": [fold.period for fold in traded_folds],
        "promotions": report.n_promotions,
        "model_trades": total_model_trades,
        "model_pnl_sum": report.cumulative_model_pnl,
        "model_mean_net_pct": model_mean_pct,
        "always_all_trades": always_trades,
        "always_all_pnl_sum": report.cumulative_always_pnl,
        "always_all_mean_net_pct": always_mean_pct,
        "matched_always_trades": matched_always_n,
        "matched_always_pnl_sum": matched_always_pnl,
        "matched_always_mean_net_pct": matched_always_mean_pct,
        "selection_lift_pct": selection_lift_pct,
        "breadth_fraction": breadth,
        "tradability_bar_pct": TRADABILITY_BAR_PCT,
        "verdict": _learner_verdict(
            model_trades=total_model_trades,
            breadth=breadth,
            selection_lift_pct=selection_lift_pct,
            model_mean_pct=model_mean_pct,
        ),
        "folds": [_fold_payload(fold) for fold in report.folds],
    }


def build_learner_buyback_summary(
    project_root: Path, snapshot_name: str, *, random_state: int = 0
) -> dict[str, Any]:
    buyback_csv = _event_csv_path(project_root, "buyback")
    buyback = load_mock_trades(
        "buyback",
        csv_path=str(buyback_csv),
        flat_cost_fraction=PRE_REVISION_COST_FRACTION,
        return_basis="raw",
    )
    synthetic = _synthetic_edge_df(seed=random_state)
    return {
        **_header(snapshot_name, "learner_buyback_summary"),
        "buyback": summarize_learner(
            buyback, label="buyback", random_state=random_state
        ),
        "synthetic_control": summarize_learner(
            synthetic, label="synthetic_planted_edge", random_state=random_state
        ),
    }


def capture_buyback_filing_times(
    *, db_path: Path, event_csv: Path, output_path: Path
) -> int:
    """Pin the minimal public filing-time input needed by the intraday baseline."""
    if not db_path.exists():
        raise FileNotFoundError(f"local source database is missing: {db_path}")
    receipts = pd.read_csv(
        event_csv, usecols=["receipt_no"], dtype={"receipt_no": "string"}
    )["receipt_no"].dropna()
    receipt_numbers = sorted(set(receipts.astype(str)))

    found: dict[str, str] = {}
    with sqlite3.connect(db_path) as connection:
        for start in range(0, len(receipt_numbers), 500):
            chunk = receipt_numbers[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                "SELECT receipt_no, filing_time FROM disclosures "
                f"WHERE filing_time IS NOT NULL AND receipt_no IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update((str(receipt_no), str(filing_time)) for receipt_no, filing_time in rows)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=output_path.parent, delete=False
    ) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["receipt_no", "filing_time"])
        writer.writerows(sorted(found.items()))
        temporary_path = Path(handle.name)
    os.replace(temporary_path, output_path)
    return len(found)


def _load_timeaware_from_pinned_input(
    *, event_csv: Path, filing_times_csv: Path
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.read_csv(event_csv, dtype={"receipt_no": "string"})
    frame = frame.dropna(subset=["t0_close", "t+1_close", "t+5_close"]).copy()
    frame = frame[frame["t+1_close"] > 0]
    frame["receipt_no"] = frame["receipt_no"].astype(str)

    times = pd.read_csv(
        filing_times_csv,
        dtype={"receipt_no": "string", "filing_time": "string"},
    )
    if times["receipt_no"].duplicated().any():
        raise ValueError("pinned buyback filing times contain duplicate receipt numbers")
    time_map = dict(zip(times["receipt_no"].astype(str), times["filing_time"].astype(str)))
    frame["filing_time"] = frame["receipt_no"].map(time_map)
    frame["filing_mins"] = frame["filing_time"].map(_mins)

    cost = PRE_REVISION_COST_FRACTION
    frame["ret_uniform"] = (
        (frame["t+5_close"] - frame["t+1_close"]) / frame["t+1_close"] - cost
    )
    intraday = frame["filing_mins"].notna() & (
        frame["filing_mins"] < INTRADAY_CUTOFF_MIN
    )
    entry = np.where(intraday, frame["t0_close"], frame["t+1_close"])
    frame["entry_mode"] = np.where(intraday, "intraday", "afterclose")
    frame["ret_timeaware"] = (frame["t+5_close"] - entry) / entry - cost
    frame["event_date"] = pd.to_datetime(frame["event_date"])
    matched = frame[frame["filing_mins"].notna()].copy()
    return frame, matched


def _mean_pct(series: pd.Series) -> float:
    return float(series.mean() * 100)


def _mean_without_top_fraction(series: pd.Series, fraction: float) -> float:
    ordered = series.dropna().sort_values()
    remove_n = int(len(ordered) * fraction)
    kept = ordered.iloc[:-remove_n] if remove_n else ordered
    return _mean_pct(kept)


def build_buyback_intraday_summary(
    project_root: Path,
    filing_times_csv: Path,
    snapshot_name: str,
    *,
    random_state: int = 0,
) -> dict[str, Any]:
    event_csv = _event_csv_path(project_root, "buyback")
    frame, matched = _load_timeaware_from_pinned_input(
        event_csv=event_csv, filing_times_csv=filing_times_csv
    )
    if matched.empty:
        raise ValueError("no buyback events matched the pinned filing-time input")

    folds = make_folds(matched.rename(columns={"ret_timeaware": "realized_net_return"}))
    fold_rows: list[dict[str, Any]] = []
    for period, fold in folds:
        uniform_pct = _mean_pct(fold["ret_uniform"])
        timeaware_pct = _mean_pct(fold["realized_net_return"])
        fold_rows.append(
            {
                "period": period,
                "n": len(fold),
                "scored": len(fold) >= MIN_WINDOW_EVENTS,
                "uniform_sum_net_return": float(fold["ret_uniform"].sum()),
                "timeaware_sum_net_return": float(
                    fold["realized_net_return"].sum()
                ),
                "uniform_mean_net_pct": uniform_pct,
                "timeaware_mean_net_pct": timeaware_pct,
                "delta_pct": timeaware_pct - uniform_pct,
                "intraday_fraction": float((fold["entry_mode"] == "intraday").mean()),
            }
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
        random_state=random_state,
    )

    scored_rows = [row for row in fold_rows if row["scored"]]
    # Preserve the existing script's exact headline aggregation: folds with
    # fewer than MIN_WINDOW_EVENTS do not contribute PnL, while the denominator
    # remains the full matched sample. The first three-event fold is therefore
    # captured but not silently reinterpreted by the baseline harness.
    uniform_mean_pct = (
        sum(row["uniform_sum_net_return"] for row in scored_rows)
        / len(matched)
        * 100
    )
    timeaware_mean_pct = (
        sum(row["timeaware_sum_net_return"] for row in scored_rows)
        / len(matched)
        * 100
    )
    return {
        **_header(snapshot_name, "buyback_intraday_summary"),
        "methodology": {
            "intraday_cutoff_kst": f"{INTRADAY_CUTOFF_MIN // 60:02d}:{INTRADAY_CUTOFF_MIN % 60:02d}",
            "intraday_entry": "t0_close",
            "after_close_or_unknown_entry": "t+1_close",
            "exit": "t+5_close",
            "roundtrip_cost_fraction": PRE_REVISION_COST_FRACTION,
            "random_state": random_state,
        },
        "coverage": {
            "events_with_prices": len(frame),
            "events_with_filing_time": len(matched),
            "coverage_fraction": len(matched) / len(frame),
            "intraday_events": int((matched["entry_mode"] == "intraday").sum()),
            "intraday_fraction": float((matched["entry_mode"] == "intraday").mean()),
        },
        "headline": {
            "uniform_mean_net_pct": uniform_mean_pct,
            "timeaware_mean_net_pct": timeaware_mean_pct,
            "entry_timing_delta_pct": timeaware_mean_pct - uniform_mean_pct,
            "uniform_positive_folds": sum(
                row["uniform_mean_net_pct"] > 0 for row in scored_rows
            ),
            "timeaware_positive_folds": sum(
                row["timeaware_mean_net_pct"] > 0 for row in scored_rows
            ),
            "delta_positive_folds": sum(row["delta_pct"] > 0 for row in scored_rows),
            "generated_folds": len(fold_rows),
            "scored_folds": len(scored_rows),
        },
        "tail_diagnostics": {
            "timeaware_median_net_pct": float(matched["ret_timeaware"].median() * 100),
            "timeaware_win_rate_pct": float((matched["ret_timeaware"] > 0).mean() * 100),
            "timeaware_mean_excluding_top_5pct_pct": _mean_without_top_fraction(
                matched["ret_timeaware"], 0.05
            ),
        },
        "folds": fold_rows,
        "learned_selector": learner,
        "status": "real_delta_untradable_level",
    }


def _runtime_versions() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "numpy": importlib.metadata.version("numpy"),
        "pandas": importlib.metadata.version("pandas"),
        "scikit-learn": importlib.metadata.version("scikit-learn"),
    }


def _records_for_paths(project_root: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    records = []
    for relative in paths:
        path = project_root / relative
        if not path.exists():
            raise FileNotFoundError(f"snapshot source is missing: {path}")
        records.append(file_record(path, display_path=relative))
    return records


def generate_snapshot(
    *,
    project_root: Path,
    output_dir: Path,
    snapshot_name: str = DEFAULT_SNAPSHOT_NAME,
    filing_times_source: Path | None = None,
) -> dict[str, Any]:
    """Generate all JSON artifacts from pinned inputs, then verify them."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filing_times = output_dir / BUYBACK_TIMES_INPUT
    if filing_times_source is not None:
        if not filing_times_source.exists():
            raise FileNotFoundError(
                f"requested pinned intraday input is missing: {filing_times_source}"
            )
        filing_times.parent.mkdir(parents=True, exist_ok=True)
        if filing_times_source.resolve() != filing_times.resolve():
            shutil.copyfile(filing_times_source, filing_times)
    if not filing_times.exists():
        raise FileNotFoundError(
            f"pinned intraday input is missing: {filing_times}. "
            "Capture it once with --refresh-buyback-times-from-db or pass "
            "--filing-times-input."
        )

    cross = build_cross_category_summary(project_root, snapshot_name)
    artifacts = {
        "cross_category_summary.json": cross,
        "buyback_intraday_summary.json": build_buyback_intraday_summary(
            project_root, filing_times, snapshot_name
        ),
        "learner_buyback_summary.json": build_learner_buyback_summary(
            project_root, snapshot_name
        ),
        "shareholder_change_summary.json": build_shareholder_change_summary(
            cross, snapshot_name
        ),
    }
    for filename, payload in artifacts.items():
        write_json(output_dir / filename, payload)

    external_data_paths = [
        f"data/event_study_{category}.csv" for category in CATEGORIES
    ]
    with filing_times.open(encoding="utf-8") as handle:
        filing_time_records = sum(1 for _ in handle) - 1

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot": snapshot_name,
        "artifact": "manifest",
        "generation_command": "python -m scripts.verify_research_state --generate",
        "verification_semantics": (
            "artifact hashes and arithmetic invariants; no equality assertion against "
            "future corrected research outputs"
        ),
        "runtime": _runtime_versions(),
        "artifacts": [
            file_record(output_dir / filename, display_path=filename)
            for filename in ARTIFACT_FILENAMES
        ],
        "pinned_inputs": [
            {
                **file_record(
                    filing_times, display_path=BUYBACK_TIMES_INPUT.as_posix()
                ),
                "records": filing_time_records,
                "source": "data/kdtb.db disclosures(receipt_no, filing_time)",
            }
        ],
        "external_inputs": _records_for_paths(project_root, external_data_paths),
        "generator_sources": _records_for_paths(project_root, GENERATOR_SOURCE_PATHS),
    }
    write_json(output_dir / "manifest.json", manifest)
    return verify_snapshot(output_dir=output_dir)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise SnapshotVerificationError(f"cannot read {path}: {error}") from error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotVerificationError(message)


def _close(left: float, right: float, tolerance: float = 1e-7) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)


def _verify_finite(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _verify_finite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _verify_finite(item, f"{path}[{index}]")
    elif isinstance(value, float):
        _require(math.isfinite(value), f"non-finite value at {path}")


def _verify_learner(summary: dict[str, Any], label: str) -> None:
    _require(summary["testable_folds"] == len(summary["folds"]), f"{label}: fold count")
    _require(summary["folds_traded"] <= summary["testable_folds"], f"{label}: breadth")
    expected_lift = (
        summary["model_mean_net_pct"] - summary["matched_always_mean_net_pct"]
    )
    _require(
        _close(summary["selection_lift_pct"], expected_lift),
        f"{label}: selection lift arithmetic",
    )
    expected_breadth = (
        summary["folds_traded"] / summary["testable_folds"]
        if summary["testable_folds"]
        else 0.0
    )
    _require(
        _close(summary["breadth_fraction"], expected_breadth),
        f"{label}: breadth arithmetic",
    )
    _require(summary["random_state"] == 0, f"{label}: deterministic random state")


def _verify_category_summary(category: dict[str, Any]) -> None:
    """Reconcile every category event count against its derived partitions."""
    label = f"category {category['category']}"
    event_count = category["n_events"]
    _require(
        isinstance(event_count, int) and event_count > 0,
        f"{label}: positive integer event count",
    )
    unique_stocks = category["n_unique_stocks"]
    _require(
        isinstance(unique_stocks, int) and 0 < unique_stocks <= event_count,
        f"{label}: unique-stock count",
    )

    for metric_name, metric in category["aggregate"].items():
        _require(
            metric["n"] == event_count,
            f"{label}: aggregate {metric_name} count must equal n_events",
        )

    for scenario_name, scenario in category["realistic"].items():
        _require(
            scenario["n"] == event_count,
            f"{label}: execution scenario {scenario_name} count must equal n_events",
        )

    market_count = sum(market["n"] for market in category["by_market"].values())
    _require(
        market_count == event_count,
        f"{label}: market counts must sum to n_events",
    )

    folds = category["walk_forward"]
    walk = category["walk_forward_summary"]
    _require(
        walk["generated_folds"] == len(folds),
        f"{label}: generated fold count",
    )
    _require(
        sum(fold["n"] for fold in folds) == event_count,
        f"{label}: fold event counts must sum to n_events",
    )
    minimum_events = walk["minimum_events_to_score"]
    _require(
        isinstance(minimum_events, int) and minimum_events > 0,
        f"{label}: minimum events to score",
    )
    scored_folds = [fold for fold in folds if fold["n"] >= minimum_events]
    positive_folds = [fold for fold in scored_folds if fold["mean_pct"] > 0]
    _require(
        walk["scored_folds"] == len(scored_folds),
        f"{label}: scored fold count",
    )
    _require(
        walk["positive_folds"] == len(positive_folds),
        f"{label}: positive fold count",
    )


def _verify_recorded_files(
    *, records: list[dict[str, Any]], base_dir: Path, label: str
) -> None:
    for record in records:
        path = base_dir / record["path"]
        _require(path.exists(), f"{label} is missing: {path}")
        _require(path.stat().st_size == record["bytes"], f"{label} size mismatch: {path}")
        _require(sha256_file(path) == record["sha256"], f"{label} hash mismatch: {path}")


def verify_snapshot(
    *,
    output_dir: Path,
    project_root: Path | None = None,
    check_external_inputs: bool = False,
) -> dict[str, Any]:
    """Verify artifact integrity and arithmetic without pinning future results."""
    manifest = _load_json(output_dir / "manifest.json")
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "manifest schema version")
    snapshot_name = manifest.get("snapshot")
    _require(bool(snapshot_name), "manifest snapshot name")

    _verify_recorded_files(
        records=manifest["artifacts"], base_dir=output_dir, label="artifact"
    )
    _verify_recorded_files(
        records=manifest["pinned_inputs"], base_dir=output_dir, label="pinned input"
    )
    _require(len(manifest["pinned_inputs"]) == 1, "one pinned input is required")
    pinned_record = manifest["pinned_inputs"][0]
    pinned_path = output_dir / pinned_record["path"]
    pinned_times = pd.read_csv(
        pinned_path,
        dtype={"receipt_no": "string", "filing_time": "string"},
    )
    _require(
        list(pinned_times.columns) == ["receipt_no", "filing_time"],
        "pinned input columns",
    )
    _require(len(pinned_times) == pinned_record["records"], "pinned input row count")
    _require(not pinned_times.isna().any().any(), "pinned input missing values")
    receipt_numbers = pinned_times["receipt_no"].astype(str).tolist()
    _require(len(receipt_numbers) == len(set(receipt_numbers)), "pinned input duplicates")
    _require(receipt_numbers == sorted(receipt_numbers), "pinned input sort order")
    _require(
        bool(
            pinned_times["filing_time"]
            .astype(str)
            .str.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d")
            .all()
        ),
        "pinned input filing-time format",
    )
    if check_external_inputs:
        _require(project_root is not None, "project_root is required to check inputs")
        _verify_recorded_files(
            records=manifest["external_inputs"],
            base_dir=project_root,
            label="external input",
        )
        _verify_recorded_files(
            records=manifest["generator_sources"],
            base_dir=project_root,
            label="generator source",
        )

    payloads = {
        filename: _load_json(output_dir / filename) for filename in ARTIFACT_FILENAMES
    }
    for filename, payload in payloads.items():
        _require(payload.get("schema_version") == SCHEMA_VERSION, f"{filename}: schema")
        _require(payload.get("snapshot") == snapshot_name, f"{filename}: snapshot name")
        _verify_finite(payload, filename)

    cross = payloads["cross_category_summary.json"]
    categories = cross["categories"]
    recorded_order = cross["category_order"]
    _require(
        [item["category"] for item in categories] == recorded_order,
        "cross-category order or membership",
    )
    _require(len(recorded_order) == len(set(recorded_order)), "duplicate categories")
    _require(len(recorded_order) > 0, "empty category baseline")
    for category in categories:
        _verify_category_summary(category)

    shareholder = payloads["shareholder_change_summary.json"]
    shareholder_cross = next(
        item for item in categories if item["category"] == "shareholder_change"
    )
    _require(
        shareholder["category_summary"] == shareholder_cross,
        "shareholder artifact must equal its cross-category row",
    )

    learner = payloads["learner_buyback_summary.json"]
    _verify_learner(learner["buyback"], "buyback learner")
    _verify_learner(learner["synthetic_control"], "synthetic learner")

    intraday = payloads["buyback_intraday_summary.json"]
    coverage = intraday["coverage"]
    headline = intraday["headline"]
    _require(coverage["events_with_prices"] > 0, "intraday event count")
    _require(
        _close(
            coverage["coverage_fraction"],
            coverage["events_with_filing_time"] / coverage["events_with_prices"],
        ),
        "intraday coverage arithmetic",
    )
    _require(
        _close(
            headline["entry_timing_delta_pct"],
            headline["timeaware_mean_net_pct"] - headline["uniform_mean_net_pct"],
        ),
        "intraday headline delta arithmetic",
    )
    _require(
        headline["generated_folds"] == len(intraday["folds"]),
        "intraday generated fold count",
    )
    _require(
        sum(row["n"] for row in intraday["folds"])
        == coverage["events_with_filing_time"],
        "intraday fold membership",
    )
    scored_intraday_folds = [row for row in intraday["folds"] if row["scored"]]
    _require(
        headline["scored_folds"] == len(scored_intraday_folds),
        "intraday scored fold count",
    )
    expected_uniform = (
        sum(row["uniform_sum_net_return"] for row in scored_intraday_folds)
        / coverage["events_with_filing_time"]
        * 100
    )
    expected_timeaware = (
        sum(row["timeaware_sum_net_return"] for row in scored_intraday_folds)
        / coverage["events_with_filing_time"]
        * 100
    )
    _require(
        _close(headline["uniform_mean_net_pct"], expected_uniform),
        "intraday uniform headline aggregation",
    )
    _require(
        _close(headline["timeaware_mean_net_pct"], expected_timeaware),
        "intraday time-aware headline aggregation",
    )
    for key in (
        "uniform_positive_folds",
        "timeaware_positive_folds",
        "delta_positive_folds",
    ):
        _require(0 <= headline[key] <= headline["scored_folds"], f"intraday {key}")
    _verify_learner(intraday["learned_selector"], "intraday learner")

    return {
        "snapshot": snapshot_name,
        "schema_version": SCHEMA_VERSION,
        "artifacts_verified": len(payloads),
        "categories_verified": len(categories),
        "internal_consistency": "passed",
        "external_inputs_checked": check_external_inputs,
    }
