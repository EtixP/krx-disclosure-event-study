"""Generate the deterministic M0.4 corporate-action price-policy audit."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from kdtb.data.market_data_client import RESEARCH_PRICE_ADJUSTMENT
from kdtb.research.baseline import sha256_file, write_json
from scripts.analyze_event_category import MIN_WINDOW_EVENTS, analyze


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_M0_3 = PROJECT_ROOT / "artifacts/m0_3/benchmark_adjustment_comparison.json"
DEFAULT_OBSERVATIONS = PROJECT_ROOT / "artifacts/m0_4/provider_observations.json"
DEFAULT_RIGHT_DROP_CALENDAR = (
    PROJECT_ROOT / "artifacts/m0_4/dart_right_drop_calendar.json"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "artifacts/m0_4/price_adjustment_audit.json"
CORPORATE_CATEGORIES = ("bonus_issue", "rights_offering")
RIGHT_DROP_QUERY = """
SELECT receipt_no, stock_code, report_name, receipt_datetime
FROM disclosures
WHERE report_name LIKE '%권리락%'
  AND stock_code IS NOT NULL
  AND trim(stock_code) <> ''
ORDER BY stock_code, receipt_datetime, receipt_no
""".strip()
GENERATOR_SOURCES = (
    "pyproject.toml",
    "requirements.txt",
    "src/kdtb/backtest/cost_model.py",
    "src/kdtb/backtest/metrics.py",
    "src/kdtb/data/benchmarks.py",
    "src/kdtb/data/market_data_client.py",
    "scripts/analyze_event_category.py",
    "scripts/audit_price_adjustments.py",
    "scripts/backfill_event_study_dates.py",
    "scripts/run_event_study.py",
)
HORIZONS = (
    ("t0_date", "t0_close"),
    ("t+1_date", "t+1_close"),
    ("t+2_date", "t+2_close"),
    ("t+5_date", "t+5_close"),
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_category(category: str) -> pd.DataFrame:
    return pd.read_csv(
        PROJECT_ROOT / f"data/event_study_{category}.csv",
        dtype={
            "corp_code": "string",
            "receipt_no": "string",
            "stock_code": "string",
        },
    )


def _complete(frame: pd.DataFrame) -> pd.Series:
    return frame[[close for _, close in HORIZONS]].notna().all(axis=1)


def _return_summary(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "n": len(frame),
        "raw_mean_pct": {
            horizon: float(frame[column].mean() * 100)
            for horizon, column in (
                ("t1", "ret_1d"),
                ("t2", "ret_2d"),
                ("t5", "ret_5d"),
            )
        },
        "abnormal_mean_pct": {
            horizon: float(frame[column].mean() * 100)
            for horizon, column in (
                ("t1", "abnormal_ret_1d"),
                ("t2", "abnormal_ret_2d"),
                ("t5", "abnormal_ret_5d"),
            )
        },
    }


def capture_right_drop_calendar(database_path: Path) -> dict[str, Any]:
    """Capture the raw DART rights-drop slice without making the audit DB-bound."""
    uri = database_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(RIGHT_DROP_QUERY).fetchall()
        table_summary = connection.execute(
            """
            SELECT COUNT(*), MIN(receipt_datetime), MAX(receipt_datetime)
            FROM disclosures
            """
        ).fetchone()

    events = [
        {
            "receipt_no": str(receipt_no),
            "stock_code": str(stock_code),
            "report_name": str(report_name),
            "receipt_datetime": str(receipt_datetime),
            "event_date": pd.Timestamp(receipt_datetime).date().isoformat(),
        }
        for receipt_no, stock_code, report_name, receipt_datetime in rows
    ]
    return {
        "schema_version": 1,
        "source_snapshot": {
            "kind": "local_dart_sqlite_snapshot",
            "table": "disclosures",
            "table_rows": int(table_summary[0]),
            "receipt_datetime_min": str(table_summary[1]),
            "receipt_datetime_max": str(table_summary[2]),
            "query": RIGHT_DROP_QUERY,
        },
        "events": events,
    }


def _normalize_right_drop_calendar(payload: dict[str, Any]) -> list[dict[str, str]]:
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported right-drop calendar schema")
    events = []
    receipt_nos: set[str] = set()
    stock_dates: set[tuple[str, str]] = set()
    for raw in payload.get("events", []):
        receipt_no = str(raw["receipt_no"])
        stock_code = str(raw["stock_code"])
        report_name = str(raw["report_name"])
        receipt_datetime = str(raw["receipt_datetime"])
        event_date = str(raw["event_date"])
        if "권리락" not in report_name:
            raise ValueError(f"non-rights-drop event in calendar: {receipt_no}")
        if pd.Timestamp(receipt_datetime).date().isoformat() != event_date:
            raise ValueError(f"calendar date mismatch: {receipt_no}")
        if receipt_no in receipt_nos:
            raise ValueError(f"duplicate right-drop receipt: {receipt_no}")
        if (stock_code, event_date) in stock_dates:
            raise ValueError(f"duplicate right-drop stock/date: {stock_code} {event_date}")
        receipt_nos.add(receipt_no)
        stock_dates.add((stock_code, event_date))
        events.append(
            {
                "receipt_no": receipt_no,
                "stock_code": stock_code,
                "report_name": report_name,
                "receipt_datetime": receipt_datetime,
                "event_date": event_date,
            }
        )
    expected_order = sorted(
        events,
        key=lambda event: (
            event["stock_code"],
            event["receipt_datetime"],
            event["receipt_no"],
        ),
    )
    if events != expected_order:
        raise ValueError("right-drop calendar is not in deterministic source order")
    if not events:
        raise ValueError("right-drop calendar is empty")
    return events


def _committed_right_drop_calendar(
    frames: dict[str, pd.DataFrame],
) -> list[dict[str, str]]:
    events: dict[str, dict[str, str]] = {}
    for frame in frames.values():
        rows = frame[frame["report_name"].str.contains("권리락", na=False)]
        for _, row in rows.iterrows():
            receipt_no = str(row["receipt_no"])
            event = {
                "receipt_no": receipt_no,
                "stock_code": str(row["stock_code"]),
                "report_name": str(row["report_name"]),
                "event_date": str(row["event_date"]),
            }
            previous = events.setdefault(receipt_no, event)
            if previous != event:
                raise ValueError(f"conflicting committed right-drop event: {receipt_no}")
    return sorted(
        events.values(),
        key=lambda event: (
            event["stock_code"],
            event["event_date"],
            event["receipt_no"],
        ),
    )


def _right_drop_crossings(
    frame: pd.DataFrame, right_drop_calendar: list[dict[str, str]]
) -> list[dict[str, Any]]:
    events_by_stock: dict[str, list[dict[str, str]]] = {}
    for event in right_drop_calendar:
        events_by_stock.setdefault(event["stock_code"], []).append(event)
    crossings = []
    for _, row in frame[_complete(frame)].iterrows():
        event_date = pd.Timestamp(row["event_date"])
        start = pd.Timestamp(row["t0_date"])
        end = pd.Timestamp(row["t+5_date"])
        hits = sorted(
            (
                event
                for event in events_by_stock.get(str(row["stock_code"]), [])
                if start <= pd.Timestamp(event["event_date"]) <= end
                and pd.Timestamp(event["event_date"]) != event_date
            ),
            key=lambda event: (event["event_date"], event["receipt_no"]),
        )
        if hits:
            crossings.append(
                {
                    "receipt_no": str(row["receipt_no"]),
                    "event_date": str(row["event_date"]),
                    "report_name": str(row["report_name"]),
                    "stock_code": str(row["stock_code"]),
                    "t0_date": str(row["t0_date"]),
                    "t+5_date": str(row["t+5_date"]),
                    "right_drop_events": hits,
                }
            )
    return crossings


def _crossing_receipts(crossings: list[dict[str, Any]]) -> set[str]:
    return {row["receipt_no"] for row in crossings}


def _row_closes(row: pd.Series) -> dict[str, float]:
    return {str(row[date]): float(row[close]) for date, close in HORIZONS}


def _simple_returns(closes: dict[str, float]) -> tuple[float, float, float]:
    values = list(closes.values())
    return tuple(value / values[0] - 1.0 for value in values[1:])


def _provider_case_audit(
    cases: list[dict[str, Any]], frames: dict[str, pd.DataFrame]
) -> list[dict[str, Any]]:
    audited = []
    for case in cases:
        matches = []
        for category, frame in frames.items():
            selected = frame[
                (frame["stock_code"].astype(str) == case["stock_code"])
                & (frame["event_date"].astype(str) == case["event_date"])
            ]
            for _, row in selected.iterrows():
                if _row_closes(row) == {
                    date: float(value)
                    for date, value in case["stored_closes"].items()
                }:
                    matches.append(category)
        stored = {date: float(value) for date, value in case["stored_closes"].items()}
        fresh = {date: float(value) for date, value in case["fresh_closes"].items()}
        if stored.keys() != fresh.keys():
            raise ValueError(f"provider case date mismatch: {case['case_id']}")
        factors = [fresh[date] / stored[date] for date in stored]
        if any(abs(factor - factors[0]) > 1e-12 for factor in factors[1:]):
            relationship = "non_uniform_revision"
        elif abs(factors[0] - 1.0) < 1e-12:
            relationship = "exact_match"
        else:
            relationship = "uniform_scale_revision"
        stored_returns = _simple_returns(stored)
        fresh_returns = _simple_returns(fresh)
        audited.append(
            {
                "case_id": case["case_id"],
                "matched_category_rows": len(matches),
                "matched_categories": sorted(set(matches)),
                "relationship": relationship,
                "scale_factor": factors[0] if relationship != "non_uniform_revision" else None,
                "max_absolute_return_change": max(
                    abs(current - previous)
                    for previous, current in zip(stored_returns, fresh_returns)
                ),
            }
        )
    return audited


def _positive_folds(folds: list[dict[str, Any]]) -> tuple[int, int]:
    scored = [fold for fold in folds if fold.get("n", 0) >= MIN_WINDOW_EVENTS]
    return sum(fold.get("mean_pct", 0) > 0 for fold in scored), len(scored)


def _current_headline(category: str) -> dict[str, Any]:
    result = analyze(category)
    raw_positive, scored = _positive_folds(result["walk_forward"])
    abnormal_positive, abnormal_scored = _positive_folds(
        result["walk_forward_abnormal"]
    )
    if scored != abnormal_scored:
        raise ValueError(f"walk-forward fold count mismatch for {category}")
    return {
        "n_events": result["n_events"],
        "metrics": {
            "idealized_t1": {
                "current_raw_net_pct": result["aggregate"]["t1_net"]["mean_pct"],
                "abnormal_net_pct": result["aggregate"]["t1_abnormal_net"]["mean_pct"],
            },
            "idealized_t5": {
                "current_raw_net_pct": result["aggregate"]["t5_net"]["mean_pct"],
                "abnormal_net_pct": result["aggregate"]["t5_abnormal_net"]["mean_pct"],
            },
            "realistic_t1_to_t5": {
                "current_raw_net_pct": result["realistic"]["realistic"]["mean_pct"],
                "abnormal_net_pct": result["realistic_abnormal"]["realistic"]["mean_pct"],
            },
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


def build_audit(
    *,
    m0_3_path: Path = DEFAULT_M0_3,
    observations_path: Path = DEFAULT_OBSERVATIONS,
    right_drop_calendar_path: Path = DEFAULT_RIGHT_DROP_CALENDAR,
) -> dict[str, Any]:
    prior = _load_json(m0_3_path)
    observations = _load_json(observations_path)
    right_drop_payload = _load_json(right_drop_calendar_path)
    dart_right_drops = _normalize_right_drop_calendar(right_drop_payload)
    frames = {category: _load_category(category) for category in CORPORATE_CATEGORIES}
    committed_right_drops = _committed_right_drop_calendar(frames)
    committed_receipts = {event["receipt_no"] for event in committed_right_drops}
    dart_receipts = {event["receipt_no"] for event in dart_right_drops}
    if not committed_receipts <= dart_receipts:
        missing = sorted(committed_receipts - dart_receipts)
        raise ValueError(
            f"pinned DART calendar omits committed right-drop receipts: {missing[:3]}"
        )
    dart_by_receipt = {event["receipt_no"]: event for event in dart_right_drops}
    for committed in committed_right_drops:
        raw = dart_by_receipt[committed["receipt_no"]]
        for field in ("stock_code", "report_name", "event_date"):
            if committed[field] != raw[field]:
                raise ValueError(
                    "committed/raw right-drop mismatch for "
                    f"{committed['receipt_no']} field {field}"
                )
    prior_by_category = {row["category"]: row for row in prior["categories"]}
    categories = []
    committed_crossings_by_category: dict[str, list[dict[str, Any]]] = {}
    dart_crossings_by_category: dict[str, list[dict[str, Any]]] = {}
    for category, frame in frames.items():
        complete = frame[_complete(frame)].copy()
        right_drops = complete[
            complete["report_name"].str.contains("권리락", na=False)
        ]
        committed_crossings = _right_drop_crossings(frame, committed_right_drops)
        dart_crossings = _right_drop_crossings(frame, dart_right_drops)
        committed_crossings_by_category[category] = committed_crossings
        dart_crossings_by_category[category] = dart_crossings
        if not _crossing_receipts(committed_crossings) <= _crossing_receipts(
            dart_crossings
        ):
            raise ValueError(
                f"raw DART crossings omit committed-union crossings for {category}"
            )
        current = _current_headline(category)
        prior_category = prior_by_category[category]
        comparisons = []
        for name, values in current["metrics"].items():
            prior_values = prior_category["metrics"][name]
            comparisons.append(
                values["current_raw_net_pct"] == prior_values["current_raw_net_pct"]
                and values["abnormal_net_pct"] == prior_values["abnormal_net_pct"]
            )
        categories.append(
            {
                "category": category,
                "rows": len(frame),
                "complete_rows": len(complete),
                "unique_stocks": int(frame["stock_code"].nunique()),
                "all_complete_returns": _return_summary(complete),
                "explicit_right_drop_returns": _return_summary(right_drops),
                "right_drop_crossings": {
                    "committed_category_union": committed_crossings,
                    "pinned_local_dart": dart_crossings,
                },
                "m0_3_headline": current,
                "m0_3_headline_unchanged": bool(
                    all(comparisons)
                    and current["n_events"] == prior_category["n_events"]
                    and current["walk_forward"] == prior_category["walk_forward"]
                    and current["verdict"] == prior_category["verdict"]
                ),
            }
        )

    input_paths = [
        PROJECT_ROOT / f"data/event_study_{category}.csv"
        for category in CORPORATE_CATEGORIES
    ] + [m0_3_path, observations_path, right_drop_calendar_path]
    committed_counts = {
        category: len(crossings)
        for category, crossings in committed_crossings_by_category.items()
    }
    dart_counts = {
        category: len(crossings)
        for category, crossings in dart_crossings_by_category.items()
    }
    committed_crossing_receipts = set().union(
        *(
            _crossing_receipts(crossings)
            for crossings in committed_crossings_by_category.values()
        )
    )
    dart_crossing_receipts = set().union(
        *(
            _crossing_receipts(crossings)
            for crossings in dart_crossings_by_category.values()
        )
    )
    return {
        "schema_version": 2,
        "milestone": "M0.4",
        "methodology": {
            "research_policy": RESEARCH_PRICE_ADJUSTMENT.value,
            "provider_route": RESEARCH_PRICE_ADJUSTMENT.source,
            "pykrx_adjusted_argument": RESEARCH_PRICE_ADJUSTMENT.pykrx_adjusted,
            "reason": (
                "announcement returns use a continuity-preserving adjusted series; "
                "stored observations pin the provider vintage"
            ),
            "pykrx_version_pin": "1.2.8",
        },
        "provider_observation_summary": {
            "captured_at": observations["captured_at"],
            "pykrx_version": observations["pykrx_version"],
            "unadjusted_probe": observations["unadjusted_probe"],
            "cases": _provider_case_audit(observations["cases"], frames),
        },
        "right_drop_calendar_summary": {
            "committed_category_union": {
                "records": len(committed_right_drops),
                "crossed_complete_windows": sum(committed_counts.values()),
                "crossed_complete_windows_by_category": committed_counts,
            },
            "pinned_local_dart": {
                "records": len(dart_right_drops),
                "crossed_complete_windows": sum(dart_counts.values()),
                "crossed_complete_windows_by_category": dart_counts,
                "source_snapshot": right_drop_payload["source_snapshot"],
            },
            "comparison": {
                "dart_records_absent_from_committed_categories": len(
                    dart_receipts - committed_receipts
                ),
                "dart_crossed_windows_beyond_committed_union": len(
                    dart_crossing_receipts - committed_crossing_receipts
                ),
            },
        },
        "categories": categories,
        "result_change": "none; the prior implicit pykrx default was adjusted=True",
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
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--m0-3", type=Path, default=DEFAULT_M0_3)
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument(
        "--right-drop-calendar", type=Path, default=DEFAULT_RIGHT_DROP_CALENDAR
    )
    parser.add_argument(
        "--capture-right-drops-from-db",
        type=Path,
        help="refresh the pinned calendar from a local DART SQLite database",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.capture_right_drops_from_db is not None:
        write_json(
            args.right_drop_calendar,
            capture_right_drop_calendar(args.capture_right_drops_from_db),
        )
    payload = build_audit(
        m0_3_path=args.m0_3,
        observations_path=args.observations,
        right_drop_calendar_path=args.right_drop_calendar,
    )
    write_json(args.output, payload)
    print(
        json.dumps(
            {"output": str(args.output), "categories": len(payload["categories"])},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
