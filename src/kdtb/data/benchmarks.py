"""Broad-market benchmark history and exact-date event alignment.

KRX defines the KOSPI and KOSDAQ indices.  Naver Finance's public domestic-
index history endpoint is used as the delivery source because the unauthenticated
KRX endpoint currently used by pykrx does not return index history reliably.

Alignment is intentionally strict: a stock observation date is looked up by
that exact calendar date in its market's benchmark.  Missing index observations
remain missing (or raise in strict mode); they are never filled or treated as a
zero return.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping

import httpx
import pandas as pd


BENCHMARK_SOURCE = "NAVER_FINANCE_DOMESTIC_INDEX_DAILY"
BENCHMARK_ENDPOINT = "https://m.stock.naver.com/api/index/{symbol}/price"


@dataclass(frozen=True)
class BenchmarkSpec:
    """The fixed broad-market benchmark assigned to one listing market."""

    market: str
    symbol: str
    name: str
    pykrx_ticker: str


BENCHMARKS: Mapping[str, BenchmarkSpec] = {
    "KOSPI": BenchmarkSpec("KOSPI", "KOSPI", "KOSPI", "1001"),
    "KOSDAQ": BenchmarkSpec("KOSDAQ", "KOSDAQ", "KOSDAQ", "2001"),
}

OBSERVATIONS: tuple[tuple[str, str, str | None], ...] = (
    ("t0", "t0_date", None),
    ("t1", "t+1_date", "ret_1d"),
    ("t2", "t+2_date", "ret_2d"),
    ("t5", "t+5_date", "ret_5d"),
)

BENCHMARK_CONTEXT_COLUMNS = (
    "benchmark_source",
    "benchmark_symbol",
    "benchmark_t0_close",
    "benchmark_t1_close",
    "benchmark_t2_close",
    "benchmark_t5_close",
    "benchmark_ret_1d",
    "benchmark_ret_2d",
    "benchmark_ret_5d",
    "abnormal_ret_1d",
    "abnormal_ret_2d",
    "abnormal_ret_5d",
    "benchmark_alignment",
)


class BenchmarkDataError(ValueError):
    """Raised when benchmark source data or exact-date alignment is invalid."""


def _number(value: Any) -> float:
    return float(str(value).replace(",", ""))


def _is_missing(value: Any) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    return bool(pd.isna(value))


class NaverBenchmarkClient:
    """Fetch normalized KOSPI/KOSDAQ daily closes from Naver Finance."""

    def __init__(
        self,
        *,
        request_get: Callable[..., Any] | None = None,
        sleep_seconds: float = 0.05,
        page_size: int = 30,
    ) -> None:
        self._request_get = request_get or httpx.get
        self.sleep_seconds = sleep_seconds
        self.page_size = page_size

    def fetch(self, market: str, start: date, end: date) -> pd.DataFrame:
        """Return normalized exact daily closes for ``market`` in [start, end]."""
        normalized_market = str(market).upper()
        if normalized_market not in BENCHMARKS:
            raise BenchmarkDataError(f"unsupported benchmark market: {market!r}")
        if start > end:
            raise BenchmarkDataError("benchmark start date must not exceed end date")

        spec = BENCHMARKS[normalized_market]
        rows: list[dict[str, Any]] = []
        page = 1
        # Thirty trading days per page.  The generous calendar-based guard
        # catches a malformed endpoint that repeats pages forever.
        max_pages = max(4, ((end - start).days // 20) + 10)
        while page <= max_pages:
            response = self._request_get(
                BENCHMARK_ENDPOINT.format(symbol=spec.symbol),
                params={"pageSize": self.page_size, "page": page},
                headers={"User-Agent": "kdtb-benchmark-research/1.0"},
                timeout=30.0,
            )
            try:
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise BenchmarkDataError(
                    f"benchmark fetch failed for {spec.symbol} page {page}"
                ) from exc
            if not isinstance(payload, list):
                raise BenchmarkDataError(
                    f"benchmark response for {spec.symbol} page {page} is not a list"
                )
            if not payload:
                break

            parsed_dates: list[date] = []
            for item in payload:
                try:
                    observed = date.fromisoformat(str(item["localTradedAt"]))
                    close = _number(item["closePrice"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise BenchmarkDataError(
                        f"malformed benchmark row for {spec.symbol} page {page}"
                    ) from exc
                parsed_dates.append(observed)
                if start <= observed <= end:
                    if not math.isfinite(close) or close <= 0:
                        raise BenchmarkDataError(
                            f"non-finite or non-positive {spec.symbol} close on {observed}"
                        )
                    rows.append(
                        {
                            "date": observed.isoformat(),
                            "market": spec.market,
                            "benchmark_symbol": spec.symbol,
                            "close": close,
                            "source": BENCHMARK_SOURCE,
                        }
                    )

            if min(parsed_dates) <= start:
                break
            page += 1
            if self.sleep_seconds:
                time.sleep(self.sleep_seconds)
        else:
            raise BenchmarkDataError(
                f"benchmark pagination guard reached for {spec.symbol}"
            )

        frame = pd.DataFrame(
            rows,
            columns=["date", "market", "benchmark_symbol", "close", "source"],
        )
        if frame.empty:
            return frame
        if frame.duplicated(["market", "date"]).any():
            raise BenchmarkDataError(
                f"duplicate benchmark dates returned for {spec.symbol}"
            )
        return frame.sort_values(["market", "date"]).reset_index(drop=True)

    def fetch_all(self, start: date, end: date) -> pd.DataFrame:
        """Fetch the fixed broad index for every supported listing market."""
        frames = [self.fetch(market, start, end) for market in BENCHMARKS]
        return pd.concat(frames, ignore_index=True)


def normalize_benchmark_history(history: pd.DataFrame) -> pd.DataFrame:
    """Validate the normalized benchmark-history boundary used by alignment."""
    required = ["date", "market", "benchmark_symbol", "close", "source"]
    missing = [column for column in required if column not in history.columns]
    if missing:
        raise BenchmarkDataError(
            "benchmark history is missing columns: " + ", ".join(missing)
        )
    out = history[required].copy()
    out["date"] = pd.to_datetime(out["date"], errors="raise").dt.normalize()
    out["market"] = out["market"].astype(str).str.upper()
    unsupported = sorted(set(out["market"]) - set(BENCHMARKS))
    if unsupported:
        raise BenchmarkDataError(
            "benchmark history has unsupported markets: " + ", ".join(unsupported)
        )
    out["close"] = pd.to_numeric(out["close"], errors="raise")
    if not out["close"].map(math.isfinite).all() or (out["close"] <= 0).any():
        raise BenchmarkDataError(
            "benchmark history contains non-finite or non-positive closes"
        )
    if out.duplicated(["market", "date"]).any():
        raise BenchmarkDataError("benchmark history contains duplicate market dates")
    for market, sub in out.groupby("market"):
        spec = BENCHMARKS[market]
        if set(sub["benchmark_symbol"]) != {spec.symbol}:
            raise BenchmarkDataError(
                f"benchmark symbol mismatch for {market}: expected {spec.symbol}"
            )
        if set(sub["source"]) != {BENCHMARK_SOURCE}:
            raise BenchmarkDataError(f"benchmark source mismatch for {market}")
    return out.sort_values(["market", "date"]).reset_index(drop=True)


def add_benchmark_context(
    events: pd.DataFrame,
    history: pd.DataFrame,
    *,
    strict: bool = False,
) -> pd.DataFrame:
    """Add exact-date benchmark and abnormal returns to event-study rows.

    ``t0_date``/``t+N_date`` are the stock's actual recorded observation dates.
    The benchmark is looked up on those dates directly; the function never uses
    an index-series row offset, nearest match, forward fill, or backward fill.
    """
    required = ["market"] + [date_column for _, date_column, _ in OBSERVATIONS]
    missing = [column for column in required if column not in events.columns]
    if missing:
        raise BenchmarkDataError(
            "event data is missing benchmark-alignment columns: "
            + ", ".join(missing)
        )
    normalized = normalize_benchmark_history(history)
    close_by_key = {
        (row.market, row.date): float(row.close)
        for row in normalized.itertuples(index=False)
    }

    out = events.copy()
    output_rows: list[dict[str, Any]] = []
    missing_keys: set[tuple[str, str]] = set()
    for event in out.to_dict("records"):
        market = str(event.get("market", "")).upper()
        if market not in BENCHMARKS:
            raise BenchmarkDataError(f"unsupported event market: {market!r}")
        spec = BENCHMARKS[market]
        context: dict[str, Any] = {
            "benchmark_source": BENCHMARK_SOURCE,
            "benchmark_symbol": spec.symbol,
        }
        absent = False
        for token, date_column, _ in OBSERVATIONS:
            value = event.get(date_column)
            benchmark_close: float | None = None
            if not _is_missing(value):
                observed = pd.Timestamp(value).normalize()
                benchmark_close = close_by_key.get((market, observed))
                if benchmark_close is None:
                    absent = True
                    missing_keys.add((market, observed.date().isoformat()))
            context[f"benchmark_{token}_close"] = benchmark_close

        benchmark_t0 = context["benchmark_t0_close"]
        for token, _, stock_return_column in OBSERVATIONS[1:]:
            benchmark_end = context[f"benchmark_{token}_close"]
            benchmark_return = (
                benchmark_end / benchmark_t0 - 1.0
                if benchmark_t0 is not None and benchmark_end is not None
                else None
            )
            horizon = stock_return_column.removeprefix("ret_")
            context[f"benchmark_ret_{horizon}"] = benchmark_return
            stock_return = event.get(stock_return_column)
            context[f"abnormal_ret_{horizon}"] = (
                float(stock_return) - benchmark_return
                if not _is_missing(stock_return)
                and benchmark_return is not None
                else None
            )
        context["benchmark_alignment"] = "missing" if absent else "complete"
        output_rows.append(context)

    if strict and missing_keys:
        examples = ", ".join(
            f"{market}:{observed}" for market, observed in sorted(missing_keys)[:8]
        )
        raise BenchmarkDataError(
            f"missing exact-date benchmark observations ({len(missing_keys)}): {examples}"
        )

    context_frame = pd.DataFrame(output_rows, index=out.index)
    for column in BENCHMARK_CONTEXT_COLUMNS:
        out[column] = context_frame[column] if column in context_frame else None
    return out


def require_benchmark_columns(
    frame: pd.DataFrame,
    *,
    tokens: tuple[str, ...] = ("t0", "t1", "t2", "t5"),
) -> None:
    """Fail if an analysis would silently use incomplete benchmark context."""
    required = ["benchmark_source", "benchmark_symbol", "benchmark_alignment"]
    required.extend(f"benchmark_{token}_close" for token in tokens)
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise BenchmarkDataError(
            "benchmark-adjusted analysis requires columns: " + ", ".join(missing)
        )
    incomplete = frame[required].isna().any(axis=1) | (
        frame["benchmark_alignment"] != "complete"
    )
    close_columns = [f"benchmark_{token}_close" for token in tokens]
    finite = frame[close_columns].apply(
        lambda column: pd.to_numeric(column, errors="coerce").map(math.isfinite)
    )
    incomplete |= ~finite.all(axis=1)
    if incomplete.any():
        raise BenchmarkDataError(
            f"benchmark-adjusted analysis has {int(incomplete.sum())} rows with "
            "missing exact-date benchmark data"
        )
