"""Backfill exact observation dates into committed event-study CSVs.

M0.2 needs the actual buy and sell dates to select the statutory sell-tax
regime. Older event-study rows retained prices but discarded the corresponding
pykrx index dates. This one-time migration fetches each stock once, reconstructs
the same t0/t+1/t+2/t+5 observation positions, and appends their dates without
changing the recorded prices or returns.

Usage:
    python -m scripts.backfill_event_study_dates
    python -m scripts.backfill_event_study_dates --sleep 0.05
"""
from __future__ import annotations

import argparse
import csv
import os
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from kdtb.data.event_categories import CATEGORIES
from kdtb.data.market_data_client import (
    RESEARCH_PRICE_ADJUSTMENT,
    MarketDataClient,
)


DATE_COLUMNS = ("t0_date", "t+1_date", "t+2_date", "t+5_date")
CLOSE_TO_DATE = {
    "t0_close": "t0_date",
    "t+1_close": "t+1_date",
    "t+2_close": "t+2_date",
    "t+5_close": "t+5_date",
}
DEFAULT_PATHS = tuple(
    [Path(f"data/event_study_{category}.csv") for category in CATEGORIES]
    + [Path("data/event_study_results.csv")]
)


def _event_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _load(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def _write(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def backfill(paths: list[Path], *, sleep_seconds: float) -> dict[str, int]:
    loaded: dict[Path, tuple[list[str], list[dict[str, str]]]] = {}
    stock_dates: dict[str, set[date]] = defaultdict(set)
    for path in paths:
        fieldnames, rows = _load(path)
        required = {"stock_code", "event_date", *CLOSE_TO_DATE}
        missing = required.difference(fieldnames)
        if missing:
            raise ValueError(f"{path} is missing required columns: {sorted(missing)}")
        loaded[path] = (fieldnames, rows)
        for row in rows:
            if any(row.get(close) for close in CLOSE_TO_DATE):
                stock_dates[row["stock_code"]].add(_event_date(row["event_date"]))

    client = MarketDataClient()
    reconstructed: dict[tuple[str, date], dict[str, str | float | None]] = {}
    failures: list[str] = []
    for index, (stock_code, dates) in enumerate(sorted(stock_dates.items()), 1):
        start = min(dates) - timedelta(days=3)
        end = max(dates) + timedelta(days=12)
        try:
            prices = client.fetch_ohlcv(
                stock_code,
                start,
                end,
                adjustment=RESEARCH_PRICE_ADJUSTMENT,
            )
        except Exception as error:  # preserve evidence and fail before any write
            failures.append(f"{stock_code}:{type(error).__name__}:{error}")
            continue
        for event_date in dates:
            reconstructed[(stock_code, event_date)] = client.event_returns(
                prices, event_date
            )
        if index % 100 == 0:
            print(f"reconstructed {index}/{len(stock_dates)} stocks", flush=True)
        if sleep_seconds:
            time.sleep(sleep_seconds)

    if failures:
        preview = "; ".join(failures[:5])
        raise RuntimeError(
            f"date reconstruction failed for {len(failures)} stocks; {preview}"
        )

    unresolved: list[str] = []
    changed_rows = 0
    for path, (fieldnames, rows) in loaded.items():
        for row_number, row in enumerate(rows, 2):
            if not any(row.get(close) for close in CLOSE_TO_DATE):
                continue
            result = reconstructed[(row["stock_code"], _event_date(row["event_date"]))]
            row_changed = False
            for close_column, date_column in CLOSE_TO_DATE.items():
                reconstructed_date = result.get(date_column)
                if row.get(close_column) and not reconstructed_date:
                    unresolved.append(f"{path}:{row_number}:{close_column}")
                value = str(reconstructed_date) if reconstructed_date else ""
                if row.get(date_column, "") != value:
                    row[date_column] = value
                    row_changed = True
            changed_rows += int(row_changed)
    if unresolved:
        preview = "; ".join(unresolved[:10])
        raise RuntimeError(
            f"{len(unresolved)} recorded closes lack reconstructed dates; {preview}"
        )
    for path, (fieldnames, rows) in loaded.items():
        output_fields = fieldnames + [c for c in DATE_COLUMNS if c not in fieldnames]
        _write(path, output_fields, rows)
    return {"files": len(loaded), "stocks": len(stock_dates), "rows_changed": changed_rows}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--sleep", type=float, default=0.05)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = args.paths or [path for path in DEFAULT_PATHS if path.exists()]
    result = backfill(paths, sleep_seconds=args.sleep)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
