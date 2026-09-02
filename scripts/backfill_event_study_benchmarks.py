"""Attach exact-date KOSPI/KOSDAQ benchmark context to event-study CSVs.

The script fetches each broad index once, writes a normalized source cache, and
then derives benchmark and abnormal returns for every event row.  Existing raw
stock prices/returns are preserved byte-for-value; only benchmark columns are
added or replaced.

Examples:
    python -m scripts.backfill_event_study_benchmarks --refresh
    python -m scripts.backfill_event_study_benchmarks --files data/event_study_buyback.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from kdtb.data.benchmarks import (
    BENCHMARK_ENDPOINT,
    BENCHMARK_CONTEXT_COLUMNS,
    BENCHMARK_SOURCE,
    NaverBenchmarkClient,
    add_benchmark_context,
    normalize_benchmark_history,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = PROJECT_ROOT / "data/benchmark_indices.csv"
DEFAULT_META = PROJECT_ROOT / "data/benchmark_indices.meta.json"


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary_path = Path(handle.name)
        frame.to_csv(handle, index=False)
    os.replace(temporary_path, path)


def _csv_value(value: object) -> str:
    """Serialize only newly derived cells; existing CSV lexemes stay untouched."""
    if value is None or pd.isna(value):
        return ""
    return str(value)


def enrich_event_csv(
    path: Path, history: pd.DataFrame, *, strict: bool = True
) -> tuple[int, int]:
    """Append/replace benchmark columns without reserializing source fields."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    preserved_fields = [
        column for column in fieldnames if column not in BENCHMARK_CONTEXT_COLUMNS
    ]
    source = pd.DataFrame(
        [{column: row.get(column, "") for column in preserved_fields} for row in rows]
    )
    enriched = add_benchmark_context(source, history, strict=strict)

    output_fields = preserved_fields + list(BENCHMARK_CONTEXT_COLUMNS)
    output_rows: list[dict[str, str]] = []
    for source_row, (_, derived_row) in zip(rows, enriched.iterrows(), strict=True):
        output = {column: source_row.get(column, "") for column in preserved_fields}
        output.update(
            {
                column: _csv_value(derived_row[column])
                for column in BENCHMARK_CONTEXT_COLUMNS
            }
        )
        output_rows.append(output)

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary_path = Path(handle.name)
        writer = csv.DictWriter(
            handle, fieldnames=output_fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(output_rows)
    os.replace(temporary_path, path)

    complete = sum(
        row["benchmark_alignment"] == "complete" for row in output_rows
    )
    return len(output_rows), complete


def _atomic_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        temporary_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary_path, path)


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _event_date_bounds(paths: list[Path]) -> tuple[date, date]:
    observed: list[pd.Timestamp] = []
    date_columns = ["t0_date", "t+1_date", "t+2_date", "t+5_date"]
    for path in paths:
        frame = pd.read_csv(path, usecols=date_columns)
        values = pd.to_datetime(frame.stack(), errors="coerce").dropna()
        if not values.empty:
            observed.extend([values.min(), values.max()])
    if not observed:
        raise ValueError("event-study files contain no observation dates")
    return min(observed).date(), max(observed).date()


def _default_files() -> list[Path]:
    return sorted((PROJECT_ROOT / "data").glob("event_study_*.csv"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", nargs="*", default=None)
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--meta", default=str(DEFAULT_META))
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="refetch broad-index history instead of using the normalized cache",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = [_resolve(value) for value in args.files] if args.files else _default_files()
    if not paths:
        raise FileNotFoundError("no event-study CSVs found")
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"event-study CSV not found: {missing[0]}")

    start, end = _event_date_bounds(paths)
    cache_path = _resolve(args.cache)
    meta_path = _resolve(args.meta)
    if args.refresh or not cache_path.exists():
        print(f"fetching KOSPI/KOSDAQ benchmark closes for {start}..{end}")
        history = NaverBenchmarkClient().fetch_all(start, end)
        history = normalize_benchmark_history(history)
        serializable = history.copy()
        serializable["date"] = serializable["date"].dt.date.astype(str)
        _atomic_csv(serializable, cache_path)
        _atomic_json(
            {
                "schema_version": 1,
                "source": BENCHMARK_SOURCE,
                "endpoint": BENCHMARK_ENDPOINT,
                "markets": ["KOSPI", "KOSDAQ"],
                "start": start.isoformat(),
                "end": end.isoformat(),
                "rows": len(serializable),
                "retrieved_at_utc": datetime.now(UTC).isoformat(),
                "alignment_policy": "exact stock observation date; no fill",
            },
            meta_path,
        )
    else:
        history = normalize_benchmark_history(pd.read_csv(cache_path))

    print(f"benchmark cache: {cache_path.relative_to(PROJECT_ROOT)} ({len(history)} rows)")
    for path in paths:
        row_count, complete = enrich_event_csv(path, history, strict=True)
        print(
            f"{path.relative_to(PROJECT_ROOT)}: {row_count} rows, "
            f"{complete} exact-date aligned"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
