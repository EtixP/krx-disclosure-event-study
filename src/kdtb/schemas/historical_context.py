from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdtb.backtest.metrics import compute
from kdtb.research.inference import issuer_clustered_mean_ci
from kdtb.schemas.disclosure import Market
from kdtb.schemas.economic_event import EventStatus, LineageStatus

HistoricalContextStatus = Literal["available", "insufficient_history"]
_SEOUL = ZoneInfo("Asia/Seoul")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("historical-context timestamps must be timezone-aware")
    return value


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("historical-context metrics must be finite")
    return value


def _outcomes_sha256(values: tuple["HistoricalComparableOutcome", ...]) -> str:
    payload = json.dumps(
        [value.model_dump(mode="json") for value in values],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HistoricalContextDataset(BaseModel):
    """Exact committed research-data vintage and query cutoff."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    dataset_id: str
    dataset_version: Literal["m1.4-v1"] = "m1.4-v1"
    category: str
    source_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_row_count: int = Field(ge=0)
    source_max_outcome_date: date | None
    query_cutoff_receipt_no: str = Field(pattern=r"^[0-9]{14}$")
    outcomes_strictly_before: date

    @field_validator("dataset_id", "category", "source_path")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("historical-context dataset labels must not be empty")
        return value


class HistoricalContextSelection(BaseModel):
    """Auditable funnel from the pinned source file to comparable outcomes."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    rule: Literal[
        "same event type/category and market; receipt before current cutoff; "
        "exclude current canonical lineage; complete T+1/T+5 stock outcome; "
        "T+5 strictly before Seoul assessment date"
    ] = (
        "same event type/category and market; receipt before current cutoff; "
        "exclude current canonical lineage; complete T+1/T+5 stock outcome; "
        "T+5 strictly before Seoul assessment date"
    )
    source_rows: int = Field(ge=0)
    same_market_rows: int = Field(ge=0)
    prior_receipt_rows: int = Field(ge=0)
    current_lineage_rows: int = Field(ge=0)
    missing_stock_outcome_rows: int = Field(ge=0)
    future_or_same_day_outcome_rows: int = Field(ge=0)
    selected_rows: int = Field(ge=0)
    selected_receipt_nos: tuple[str, ...]
    selected_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("selected_receipt_nos")
    @classmethod
    def valid_receipts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) != 14 or not value.isdigit() for value in values):
            raise ValueError("selected receipt numbers must contain 14 digits")
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("selected receipt numbers must be sorted and unique")
        return values

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "HistoricalContextSelection":
        if self.selected_rows != len(self.selected_receipt_nos):
            raise ValueError("selected row count must match selected receipts")
        if self.same_market_rows > self.source_rows:
            raise ValueError("same-market rows cannot exceed source rows")
        if self.prior_receipt_rows > self.same_market_rows:
            raise ValueError("prior rows cannot exceed same-market rows")
        if (
            self.current_lineage_rows
            + self.missing_stock_outcome_rows
            + self.future_or_same_day_outcome_rows
            + self.selected_rows
            != self.prior_receipt_rows
        ):
            raise ValueError("historical-context selection funnel does not reconcile")
        return self


class HistoricalComparableOutcome(BaseModel):
    """One exact Phase 0 input/output row retained for metric auditing."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    receipt_no: str = Field(pattern=r"^[0-9]{14}$")
    corp_code: str = Field(pattern=r"^[0-9]{8}$")
    stock_code: str = Field(pattern=r"^[0-9A-Z]{6}$")
    event_date: date
    market: Market
    entry_date: date
    exit_date: date
    stock_gross_return: float
    benchmark_gross_return: float
    modeled_cost_fraction: float = Field(ge=0)
    abnormal_net_return: float

    _finite_returns = field_validator(
        "stock_gross_return",
        "benchmark_gross_return",
        "modeled_cost_fraction",
        "abnormal_net_return",
    )(_finite)

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> "HistoricalComparableOutcome":
        if not self.corp_code or not self.stock_code:
            raise ValueError("historical outcomes require issuer identifiers")
        if not self.event_date <= self.entry_date <= self.exit_date:
            raise ValueError("historical outcome dates are not chronological")
        expected_abnormal = (
            self.stock_gross_return
            - self.benchmark_gross_return
            - self.modeled_cost_fraction
        )
        if self.abnormal_net_return != expected_abnormal:
            raise ValueError(
                "abnormal net return must equal stock return minus benchmark "
                "return minus modeled cost"
            )
        return self


class HistoricalContextMetrics(BaseModel):
    """Verified Phase 0 summary of realistic benchmark-adjusted outcomes."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    horizon: Literal["t+1_close_to_t+5_close"] = "t+1_close_to_t+5_close"
    return_basis: Literal[
        "stock_return_minus_benchmark_return_minus_dated_roundtrip_cost"
    ] = "stock_return_minus_benchmark_return_minus_dated_roundtrip_cost"
    cost_adjusted: Literal[True] = True
    cost_model: Literal["verified_phase0_dated_same_notional"] = (
        "verified_phase0_dated_same_notional"
    )
    benchmark_source: Literal["NAVER_FINANCE_DOMESTIC_INDEX_DAILY"] = (
        "NAVER_FINANCE_DOMESTIC_INDEX_DAILY"
    )
    benchmark_assignment: Literal["listing_market_broad_price_index"] = (
        "listing_market_broad_price_index"
    )
    sample_size: int = Field(ge=2)
    issuer_count: int = Field(ge=2)
    mean_stock_gross_return: float
    mean_benchmark_gross_return: float
    mean_modeled_cost_fraction: float = Field(ge=0)
    mean_abnormal_net_return: float
    median_abnormal_net_return: float
    std_abnormal_net_return: float = Field(ge=0)
    positive_fraction: float = Field(ge=0, le=1)
    profit_factor: float | None = Field(default=None, ge=0)
    ci_lower: float
    ci_upper: float
    confidence_level: float = Field(gt=0, lt=1)
    n_resamples: int = Field(ge=2)
    random_state: int
    resampling_unit: Literal["issuer"] = "issuer"
    estimand: Literal["event_weighted_mean"] = "event_weighted_mean"
    interval: Literal["percentile_cluster_bootstrap"] = "percentile_cluster_bootstrap"

    _finite_metrics = field_validator(
        "mean_stock_gross_return",
        "mean_benchmark_gross_return",
        "mean_modeled_cost_fraction",
        "mean_abnormal_net_return",
        "median_abnormal_net_return",
        "std_abnormal_net_return",
        "positive_fraction",
        "ci_lower",
        "ci_upper",
    )(_finite)

    @field_validator("profit_factor")
    @classmethod
    def finite_profit_factor(cls, value: float | None) -> float | None:
        return _finite(value) if value is not None else None

    @model_validator(mode="after")
    def valid_interval(self) -> "HistoricalContextMetrics":
        if self.issuer_count > self.sample_size:
            raise ValueError("issuer count cannot exceed sample size")
        if self.ci_lower > self.ci_upper:
            raise ValueError("confidence interval bounds are reversed")
        return self


class HistoricalContextResult(BaseModel):
    """Reproducible historical outcome context; never a recommendation."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    context_version: Literal["m1.4-v1"] = "m1.4-v1"
    economic_event_id: str
    event_type: str
    market: Market
    event_status: EventStatus
    lineage_status: LineageStatus
    assessed_at: datetime
    status: HistoricalContextStatus
    dataset: HistoricalContextDataset
    selection: HistoricalContextSelection
    comparables: tuple[HistoricalComparableOutcome, ...]
    metrics: HistoricalContextMetrics | None
    limitations: tuple[str, ...]
    is_trade_recommendation: Literal[False] = False
    trading_recommendation: None = None

    _assessed_at = field_validator("assessed_at")(_aware)

    @model_validator(mode="after")
    def result_is_consistent(self) -> "HistoricalContextResult":
        if (
            not self.economic_event_id.startswith("dart:")
            or not self.economic_event_id[5:].isdigit()
        ):
            raise ValueError("historical context requires a canonical DART event ID")
        if len(self.economic_event_id.removeprefix("dart:")) != 14:
            raise ValueError("historical context requires a canonical DART event ID")
        if (
            self.economic_event_id.removeprefix("dart:")
            > self.dataset.query_cutoff_receipt_no
        ):
            raise ValueError(
                "dataset receipt cutoff cannot predate the canonical event"
            )
        if (
            self.assessed_at.astimezone(_SEOUL).date()
            != self.dataset.outcomes_strictly_before
        ):
            raise ValueError(
                "dataset outcome cutoff must match the Seoul assessment date"
            )
        if self.dataset.source_row_count != self.selection.source_rows:
            raise ValueError("dataset and selection source-row counts must match")
        if len(self.comparables) != self.selection.selected_rows:
            raise ValueError("comparable outcomes must match the selected rows")
        if tuple(item.receipt_no for item in self.comparables) != (
            self.selection.selected_receipt_nos
        ):
            raise ValueError("comparable outcome order must match selected receipts")
        if _outcomes_sha256(self.comparables) != self.selection.selected_rows_sha256:
            raise ValueError("selected-row hash must match comparable outcomes")
        if any(item.market != self.market for item in self.comparables):
            raise ValueError("comparable outcome market must match the current event")
        if any(
            item.receipt_no >= self.dataset.query_cutoff_receipt_no
            for item in self.comparables
        ):
            raise ValueError("historical context contains a non-prior receipt")
        if any(
            item.exit_date >= self.dataset.outcomes_strictly_before
            for item in self.comparables
        ):
            raise ValueError("historical context contains an unavailable outcome")
        if self.status == "available":
            if self.metrics is None:
                raise ValueError("available context requires metrics")
            if self.metrics.sample_size != len(self.comparables):
                raise ValueError("metric sample size must match comparables")
            if self.metrics.issuer_count != len(
                {item.stock_code for item in self.comparables}
            ):
                raise ValueError("metric issuer count must match comparables")
            abnormal = [item.abnormal_net_return for item in self.comparables]
            trade_metrics = compute(abnormal)
            inference = issuer_clustered_mean_ci(
                abnormal,
                [item.stock_code for item in self.comparables],
                confidence_level=self.metrics.confidence_level,
                n_resamples=self.metrics.n_resamples,
                random_state=self.metrics.random_state,
            )
            expected_metrics = {
                "mean_stock_gross_return": sum(
                    item.stock_gross_return for item in self.comparables
                )
                / len(self.comparables),
                "mean_benchmark_gross_return": sum(
                    item.benchmark_gross_return for item in self.comparables
                )
                / len(self.comparables),
                "mean_modeled_cost_fraction": sum(
                    item.modeled_cost_fraction for item in self.comparables
                )
                / len(self.comparables),
                "mean_abnormal_net_return": trade_metrics.mean_return,
                "median_abnormal_net_return": trade_metrics.median_return,
                "std_abnormal_net_return": trade_metrics.std_return,
                "positive_fraction": trade_metrics.win_rate,
                "profit_factor": trade_metrics.profit_factor,
                "ci_lower": inference["ci_lower"],
                "ci_upper": inference["ci_upper"],
            }
            for field, expected in expected_metrics.items():
                if getattr(self.metrics, field) != expected:
                    raise ValueError(
                        f"metric {field} must match the comparable outcomes"
                    )
        elif self.metrics is not None:
            raise ValueError("insufficient context cannot claim inferred metrics")
        elif (
            len(self.comparables) >= 2
            and len({item.stock_code for item in self.comparables}) >= 2
        ):
            raise ValueError("sufficient historical context must include metrics")
        return self
