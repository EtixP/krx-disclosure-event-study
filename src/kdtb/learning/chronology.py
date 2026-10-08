"""Daily research chronology; observed benchmark dates are not an exchange calendar.

Reconstructed dates constrain historical admission. They do not establish the
original publication time, adjustment vintage, security membership, or a fill.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np
import pandas as pd

PINNED_BENCHMARK_SHA256 = (
    "a70c5f9ae3ae7b8349fe489b720bb01411ba60437b4a85927b87c6a6acc7e062"
)
IDENTITY_COLUMNS = ("id", "receipt_no", "corp_code", "stock_code")
SOURCE_DATES = ("event_date", "t0_date", "t+1_date", "t+5_date")
CHRONOLOGY_DATES = (
    "event_date",
    "t0_date",
    "feature_observation_date",
    "entry_date",
    "decision_date",
    "label_end_date",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def daily_date(value) -> pd.Timestamp:
    """Require an exact, timezone-free calendar day; never truncate intraday data."""
    if pd.isna(value) or value == "":
        raise ValueError("missing_date")
    if isinstance(value, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("malformed_date")
    try:
        result = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("malformed_date") from exc
    if pd.isna(result) or result.tz is not None or result != result.normalize():
        raise ValueError("malformed_date")
    return result


class ObservedSessionHorizon:
    """Hash-pinned observed index sessions, with explicit coverage limits.

    A modified/incomplete cache is rejected by its pin. A gap in the *original*
    captured series cannot be proved to be an exchange holiday by this class.
    """

    def __init__(
        self,
        path: str | Path = "data/benchmark_indices.csv",
        *,
        expected_sha256: str = PINNED_BENCHMARK_SHA256,
    ) -> None:
        self.path = Path(path)
        self.sha256 = sha256(self.path)
        if self.sha256 != expected_sha256:
            raise ValueError("benchmark_calendar_hash_mismatch")
        data = pd.read_csv(self.path, dtype={"date": "string", "market": "string"})
        if not {"date", "market", "close"}.issubset(data.columns) or data.empty:
            raise ValueError("invalid_benchmark_calendar_schema")
        try:
            data["date"] = data["date"].map(daily_date)
            prices = pd.to_numeric(data["close"], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_benchmark_calendar_values") from exc
        if (
            data["market"].isna().any()
            or not data["market"].isin(["KOSPI", "KOSDAQ"]).all()
            or data.duplicated(["market", "date"]).any()
            or not np.isfinite(prices).all()
            or (prices <= 0).any()
        ):
            raise ValueError("invalid_benchmark_calendar_values")
        self.sessions = {
            market: pd.DatetimeIndex(group["date"].sort_values())
            for market, group in data.groupby("market", sort=True)
        }
        if len(self.sessions) == 2 and not self.sessions["KOSPI"].equals(
            self.sessions["KOSDAQ"]
        ):
            raise ValueError("benchmark_calendar_market_gap")

    def window(self, market: str, event_date: pd.Timestamp) -> tuple[pd.Timestamp, ...]:
        dates = self.sessions.get(market)
        if dates is None:
            raise ValueError("unsupported_market")
        if event_date < dates[0]:
            raise ValueError("event_before_calendar_horizon")
        offset = dates.searchsorted(event_date)
        if offset + 5 >= len(dates):
            raise ValueError("insufficient_calendar_horizon")
        return dates[offset], dates[offset + 1], dates[offset + 5]

    def metadata(self) -> dict:
        return {
            "sha256": self.sha256,
            "semantics": "observed benchmark-session horizon; not authoritative exchange calendar",
            "markets": {
                key: {
                    "first": str(dates[0].date()),
                    "last": str(dates[-1].date()),
                    "sessions": len(dates),
                }
                for key, dates in self.sessions.items()
            },
        }


def require_chronology(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate caller-supplied chronology and return deterministic decision order."""
    required = [*IDENTITY_COLUMNS, "row_id", *CHRONOLOGY_DATES]
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError("chronology metadata required: " + ", ".join(missing))
    result = frame.copy()
    for col in [*IDENTITY_COLUMNS, "row_id"]:
        if not result[col].map(lambda x: isinstance(x, str) and bool(x.strip())).all():
            raise ValueError(f"chronology identity must be nonempty strings: {col}")
    if result["row_id"].duplicated().any():
        raise ValueError("duplicate_chronology_row_id")
    for col in CHRONOLOGY_DATES:
        try:
            result[col] = pd.to_datetime(result[col].map(daily_date))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid chronology date: {col}") from exc
    for source, alias in (
        ("t+1_date", "decision_date"),
        ("t+5_date", "label_end_date"),
    ):
        if source in result.columns:
            try:
                result[source] = pd.to_datetime(result[source].map(daily_date))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid chronology date: {source}") from exc
            if not result[source].equals(result[alias]):
                raise ValueError(f"contradictory chronology alias: {source} != {alias}")
    for alias, source in (("event_id", "id"), ("issuer_id", "corp_code")):
        if alias in result.columns and not result[alias].equals(result[source]):
            raise ValueError(f"contradictory identity alias: {alias} != {source}")
    valid = (
        (result["event_date"] <= result["t0_date"])
        & (result["t0_date"] == result["feature_observation_date"])
        & (result["feature_observation_date"] < result["decision_date"])
        & (result["entry_date"] == result["decision_date"])
        & (result["decision_date"] < result["label_end_date"])
    )
    if not valid.all():
        raise ValueError("reversed_or_inconsistent_chronology")
    return result.sort_values(["decision_date", "row_id"], kind="stable").reset_index(
        drop=True
    )
