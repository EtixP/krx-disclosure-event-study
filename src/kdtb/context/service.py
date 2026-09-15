from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from numbers import Integral
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.backtest.metrics import compute
from kdtb.data.benchmarks import BENCHMARK_SOURCE, require_benchmark_columns
from kdtb.events.chronology import SEOUL, latest_receipt_no, validate_event_available_at
from kdtb.research.inference import issuer_clustered_mean_ci
from kdtb.schemas.economic_event import EconomicEvent
from kdtb.schemas.historical_context import (
    HistoricalComparableOutcome,
    HistoricalContextDataset,
    HistoricalContextMetrics,
    HistoricalContextResult,
    HistoricalContextSelection,
)

EVENT_TYPE_CATEGORIES: Mapping[str, str] = MappingProxyType(
    {
        "major_supply_contract": "supply_contract",
        "share_buyback": "buyback",
        "rights_offering": "rights_offering",
        "bonus_issue": "bonus_issue",
        "convertible_bond": "convertible_bond",
        "halt_resumption": "halt_resumption",
        "shareholder_change": "shareholder_change",
    }
)

_IDENTIFIER_DTYPES = {
    "receipt_no": "string",
    "corp_code": "string",
    "stock_code": "string",
}
_STOCK_OUTCOME_COLUMNS = (
    "corp_code",
    "stock_code",
    "event_date",
    "t+1_date",
    "t+5_date",
    "t+1_close",
    "t+5_close",
)
_REQUIRED_COLUMNS = (
    "receipt_no",
    "market",
    *_STOCK_OUTCOME_COLUMNS,
    "benchmark_source",
    "benchmark_symbol",
    "benchmark_t1_close",
    "benchmark_t5_close",
    "benchmark_alignment",
)


class HistoricalContextError(ValueError):
    """Raised when historical context cannot be reproduced safely."""


@dataclass(frozen=True)
class HistoricalContextPolicy:
    confidence_level: float = 0.95
    n_resamples: int = 10_000
    random_state: int = 0

    def __post_init__(self) -> None:
        confidence = self.confidence_level
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 < float(confidence) < 1
        ):
            raise ValueError(
                "confidence_level must be finite and strictly between 0 and 1"
            )
        if (
            isinstance(self.n_resamples, bool)
            or not isinstance(self.n_resamples, Integral)
            or self.n_resamples < 2
        ):
            raise ValueError("n_resamples must be an integer of at least two")
        if isinstance(self.random_state, bool) or not isinstance(
            self.random_state, Integral
        ):
            raise TypeError("random_state must be an integer")
        object.__setattr__(self, "confidence_level", float(confidence))
        object.__setattr__(self, "n_resamples", int(self.n_resamples))
        object.__setattr__(self, "random_state", int(self.random_state))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _present(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    result = pd.Series(True, index=frame.index, dtype=bool)
    for column in columns:
        values = frame[column]
        available = values.notna()
        if isinstance(values.dtype, pd.StringDtype) or values.dtype == object:
            available &= values.astype("string").str.strip().ne("").fillna(False)
        result &= available
    return result


def _parse_dates(values: pd.Series, *, label: str) -> pd.Series:
    present = values.notna() & values.astype("string").str.strip().ne("").fillna(False)
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    try:
        parsed.loc[present] = pd.to_datetime(
            values.loc[present], format="%Y-%m-%d", errors="raise"
        )
    except (TypeError, ValueError) as error:
        raise HistoricalContextError(f"{label} contains an invalid date") from error
    return parsed


def _positive_numbers(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    for column in columns:
        try:
            values = pd.to_numeric(frame[column], errors="raise")
        except (TypeError, ValueError) as error:
            raise HistoricalContextError(
                f"{column} contains a non-numeric value"
            ) from error
        if not values.map(math.isfinite).all() or (values <= 0).any():
            raise HistoricalContextError(
                f"{column} contains a non-finite or non-positive value"
            )
        frame[column] = values.astype(float)


def _selected_rows_sha256(
    outcomes: tuple[HistoricalComparableOutcome, ...],
) -> str:
    payload = json.dumps(
        [outcome.model_dump(mode="json") for outcome in outcomes],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HistoricalContextService:
    """Query committed Phase 0 event studies without using future outcomes."""

    def __init__(
        self,
        *,
        data_dir: str | Path = "data",
        dataset_id: str = "committed-phase0-event-study",
        policy: HistoricalContextPolicy | None = None,
    ) -> None:
        if not dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        self.data_dir = Path(data_dir)
        self.dataset_id = dataset_id
        self.policy = policy or HistoricalContextPolicy()

    def _load(self, category: str) -> tuple[pd.DataFrame, Path, str]:
        path = self.data_dir / f"event_study_{category}.csv"
        if not path.is_file():
            raise HistoricalContextError(
                f"historical context source is missing: {path}"
            )
        before_hash = _sha256_file(path)
        frame = pd.read_csv(path, dtype=_IDENTIFIER_DTYPES)
        after_hash = _sha256_file(path)
        if before_hash != after_hash:
            raise HistoricalContextError(
                "historical context source changed while it was being read"
            )
        missing = [column for column in _REQUIRED_COLUMNS if column not in frame]
        if missing:
            raise HistoricalContextError(
                "historical context source is missing columns: " + ", ".join(missing)
            )
        if (
            frame["receipt_no"].isna().any()
            or not frame["receipt_no"].str.fullmatch(r"[0-9]{14}").all()
        ):
            raise HistoricalContextError(
                "historical context receipt numbers must contain exactly 14 digits"
            )
        if frame["receipt_no"].duplicated().any():
            raise HistoricalContextError(
                "historical context source contains duplicate receipt numbers"
            )
        if frame["market"].isna().any():
            raise HistoricalContextError(
                "historical context source contains a missing market"
            )
        markets = set(frame["market"].astype(str))
        if not markets.issubset({"KOSPI", "KOSDAQ"}):
            raise HistoricalContextError(
                "historical context source contains an unsupported market"
            )
        return frame, path, before_hash

    def query(
        self,
        event: EconomicEvent,
        *,
        assessed_at: datetime,
    ) -> HistoricalContextResult:
        """Return deterministic context known strictly before ``assessed_at``."""

        validate_event_available_at(event, assessed_at)
        if event.lineage_status not in {"complete", "self_contained"}:
            raise HistoricalContextError(
                "historical context requires complete or self-contained current-event lineage"
            )
        category = EVENT_TYPE_CATEGORIES.get(event.event_type)
        if category is None:
            raise HistoricalContextError(
                f"no verified historical category for event type {event.event_type!r}"
            )
        if event.market not in {"KOSPI", "KOSDAQ"}:
            raise HistoricalContextError(
                f"no verified benchmark/cost context for market {event.market!r}"
            )

        cutoff_receipt_no = latest_receipt_no(event)
        cutoff_date = assessed_at.astimezone(SEOUL).date()
        frame, source_path, source_sha256 = self._load(category)
        source_exit_dates = _parse_dates(frame["t+5_date"], label="t+5_date")

        same_market = frame["market"].eq(event.market)
        prior_receipt = same_market & frame["receipt_no"].lt(cutoff_receipt_no)
        current_receipts = {
            event.primary_receipt_no,
            *event.related_receipt_nos,
            *(source.receipt_no for source in event.source_provenance),
        }
        current_lineage = prior_receipt & frame["receipt_no"].isin(current_receipts)
        prior = frame.loc[prior_receipt & ~current_lineage].copy()
        stock_complete = _present(prior, _STOCK_OUTCOME_COLUMNS)
        missing_stock_outcomes = int((~stock_complete).sum())
        complete = prior.loc[stock_complete].copy()
        complete["_event_date"] = _parse_dates(
            complete["event_date"], label="event_date"
        )
        complete["_entry_date"] = _parse_dates(complete["t+1_date"], label="t+1_date")
        complete["_exit_date"] = _parse_dates(complete["t+5_date"], label="t+5_date")
        outcome_available = complete["_exit_date"].dt.date < cutoff_date
        unavailable_outcomes = int((~outcome_available).sum())
        selected = complete.loc[outcome_available].copy()
        selected = selected.sort_values("receipt_no").reset_index(drop=True)

        if not selected.empty:
            require_benchmark_columns(selected, tokens=("t1", "t5"))
            if set(selected["benchmark_source"].astype(str)) != {BENCHMARK_SOURCE}:
                raise HistoricalContextError(
                    "historical context source uses an unverified benchmark provider"
                )
            if set(selected["benchmark_symbol"].astype(str)) != {event.market}:
                raise HistoricalContextError(
                    "historical context benchmark does not match the listing market"
                )
            _positive_numbers(
                selected,
                (
                    "t+1_close",
                    "t+5_close",
                    "benchmark_t1_close",
                    "benchmark_t5_close",
                ),
            )

        cost_model = CostModel()
        outcomes: list[HistoricalComparableOutcome] = []
        for row in selected.to_dict(orient="records"):
            entry_date = row["_entry_date"].date()
            exit_date = row["_exit_date"].date()
            stock_gross = row["t+5_close"] / row["t+1_close"] - 1.0
            benchmark_gross = (
                row["benchmark_t5_close"] / row["benchmark_t1_close"] - 1.0
            )
            cost = cost_model.roundtrip_cost(
                1.0,
                buy_date=entry_date,
                sell_date=exit_date,
                market=event.market,
            )
            outcomes.append(
                HistoricalComparableOutcome(
                    receipt_no=row["receipt_no"],
                    corp_code=row["corp_code"],
                    stock_code=row["stock_code"],
                    event_date=row["_event_date"].date(),
                    market=event.market,
                    entry_date=entry_date,
                    exit_date=exit_date,
                    stock_gross_return=stock_gross,
                    benchmark_gross_return=benchmark_gross,
                    modeled_cost_fraction=cost,
                    abnormal_net_return=stock_gross - benchmark_gross - cost,
                )
            )
        comparable_outcomes = tuple(outcomes)
        receipt_nos = tuple(item.receipt_no for item in comparable_outcomes)

        selection = HistoricalContextSelection(
            source_rows=len(frame),
            same_market_rows=int(same_market.sum()),
            prior_receipt_rows=int(prior_receipt.sum()),
            current_lineage_rows=int(current_lineage.sum()),
            missing_stock_outcome_rows=missing_stock_outcomes,
            future_or_same_day_outcome_rows=unavailable_outcomes,
            selected_rows=len(comparable_outcomes),
            selected_receipt_nos=receipt_nos,
            selected_rows_sha256=_selected_rows_sha256(comparable_outcomes),
        )
        max_source_outcome = source_exit_dates.max()
        dataset = HistoricalContextDataset(
            dataset_id=self.dataset_id,
            category=category,
            source_path=source_path.as_posix(),
            source_sha256=source_sha256,
            source_row_count=len(frame),
            source_max_outcome_date=(
                max_source_outcome.date() if not pd.isna(max_source_outcome) else None
            ),
            query_cutoff_receipt_no=cutoff_receipt_no,
            outcomes_strictly_before=cutoff_date,
        )

        issuers = [item.stock_code for item in comparable_outcomes]
        if len(comparable_outcomes) < 2 or len(set(issuers)) < 2:
            status = "insufficient_history"
            metrics = None
        else:
            abnormal = [item.abnormal_net_return for item in comparable_outcomes]
            trade_metrics = compute(abnormal)
            inference = issuer_clustered_mean_ci(
                abnormal,
                issuers,
                confidence_level=self.policy.confidence_level,
                n_resamples=self.policy.n_resamples,
                random_state=self.policy.random_state,
            )
            metrics = HistoricalContextMetrics(
                sample_size=len(comparable_outcomes),
                issuer_count=len(set(issuers)),
                mean_stock_gross_return=sum(
                    item.stock_gross_return for item in comparable_outcomes
                )
                / len(comparable_outcomes),
                mean_benchmark_gross_return=sum(
                    item.benchmark_gross_return for item in comparable_outcomes
                )
                / len(comparable_outcomes),
                mean_modeled_cost_fraction=sum(
                    item.modeled_cost_fraction for item in comparable_outcomes
                )
                / len(comparable_outcomes),
                mean_abnormal_net_return=trade_metrics.mean_return,
                median_abnormal_net_return=trade_metrics.median_return,
                std_abnormal_net_return=trade_metrics.std_return,
                positive_fraction=trade_metrics.win_rate,
                profit_factor=trade_metrics.profit_factor,
                ci_lower=inference["ci_lower"],
                ci_upper=inference["ci_upper"],
                confidence_level=inference["confidence_level"],
                n_resamples=inference["n_resamples"],
                random_state=inference["random_state"],
            )
            status = "available"

        return HistoricalContextResult(
            economic_event_id=event.economic_event_id,
            event_type=event.event_type,
            market=event.market,
            event_status=event.status,
            lineage_status=event.lineage_status,
            assessed_at=assessed_at,
            status=status,
            dataset=dataset,
            selection=selection,
            comparables=comparable_outcomes,
            metrics=metrics,
            limitations=(
                "Historical outcomes are exploratory receipt-level Phase 0 observations, not canonical-lineage backfill.",
                "Only T+5 outcomes strictly before the Seoul assessment date are eligible.",
                "The interval clusters by issuer but does not remove common-date or overlapping-window dependence.",
                "No trading recommendation was evaluated.",
            ),
        )
