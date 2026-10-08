from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdtb.backtest.cost_model import CostModel
from kdtb.data.krx_calendar import (
    KRX_HOLIDAY_SOURCE,
    KRX_HOLIDAY_SOURCE_VERSION,
    KRXCalendarEvidenceError,
    krx_holiday_payload_sha256,
    parse_pinned_krx_holidays,
)
from kdtb.data.krx_session_hours import (
    KRXSessionHoursEvidenceError,
    krx_session_close,
    session_hours_payload_sha256,
)
from kdtb.schemas.experiment import (
    BenchmarkDefinition,
    CostAssumptions,
    ExecutionRule,
    require_utc,
)

SEOUL = ZoneInfo("Asia/Seoul")


def _canonical_json(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def outcome_id_for(*, decision_id: str, decision_sha256: str) -> str:
    return _sha256(f"m2.3:{decision_id}:{decision_sha256}")


def daily_close_observation_sha256(
    *,
    instrument_type: str,
    symbol: str,
    market: str,
    trading_date: date,
    close: float,
    source: str,
    source_version: str,
    available_at: datetime,
    captured_at: datetime,
) -> str:
    """Hash the complete normalized source record without claiming raw bytes."""

    payload = {
        "available_at": available_at.isoformat(),
        "captured_at": captured_at.isoformat(),
        "close": close,
        "instrument_type": instrument_type,
        "market": market,
        "observation_schema_version": "m2.3-daily-close-v1",
        "source": source,
        "source_version": source_version,
        "symbol": symbol,
        "trading_date": trading_date.isoformat(),
    }
    return _sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def trading_session_calendar_sha256(
    *,
    market: str,
    symbol: str,
    source: str,
    source_version: str,
    source_year: int,
    source_payload_utf8: str,
    source_payload_sha256: str,
    session_hours_source: str,
    session_hours_source_version: str,
    session_hours_source_payload_utf8: str,
    session_hours_source_payload_sha256: str,
    exception_policy_id: str,
    exception_policy_version: str,
    exception_evidence_sha256: str,
    window_start: date,
    window_end: date,
    sessions: tuple["TradingSession", ...],
    closed_weekdays: tuple[date, ...],
    captured_at: datetime,
) -> str:
    payload = {
        "calendar_schema_version": "m2.3-session-calendar-v1",
        "captured_at": captured_at.isoformat(),
        "complete": True,
        "market": market,
        "sessions": [
            {
                "close_at": session.close_at.isoformat(),
                "trading_date": session.trading_date.isoformat(),
            }
            for session in sessions
        ],
        "source": source,
        "source_payload_utf8": source_payload_utf8,
        "source_payload_sha256": source_payload_sha256,
        "source_version": source_version,
        "source_year": source_year,
        "session_hours_source": session_hours_source,
        "session_hours_source_version": session_hours_source_version,
        "session_hours_source_payload_utf8": session_hours_source_payload_utf8,
        "session_hours_source_payload_sha256": (session_hours_source_payload_sha256),
        "exception_policy_id": exception_policy_id,
        "exception_policy_version": exception_policy_version,
        "exception_evidence_sha256": exception_evidence_sha256,
        "symbol": symbol,
        "closed_weekdays": [value.isoformat() for value in closed_weekdays],
        "window_end": window_end.isoformat(),
        "window_start": window_start.isoformat(),
    }
    return _sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def frozen_session_offsets(
    entry_rule: ExecutionRule,
    exit_rule: ExecutionRule,
) -> tuple[int, int]:
    if (
        entry_rule.rule_id != "next_trading_day_close"
        or entry_rule.rule_version != "m2.1-v1"
    ):
        raise ValueError("unsupported frozen entry rule")
    if (
        exit_rule.rule_id != "trading_session_close"
        or exit_rule.rule_version != "m2.1-v1"
    ):
        raise ValueError("unsupported frozen exit rule")

    def parameter(rule: ExecutionRule, name: str) -> int:
        values = {item.name: item.value for item in rule.parameters}
        if set(values) != {name}:
            raise ValueError(f"{rule.rule_id} requires exactly {name}")
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{rule.rule_id} {name} is invalid")
        return value

    return (
        parameter(entry_rule, "session_offset"),
        parameter(exit_rule, "holding_sessions"),
    )


def frozen_roundtrip_cost_fraction(
    assumptions: CostAssumptions,
    *,
    buy_date: date,
    sell_date: date,
    market: str,
) -> float:
    """Apply only the cost implementation/version frozen by M2.1."""

    supported = (
        assumptions.model_id == "korean_equity_roundtrip"
        and assumptions.model_version == "m0.2-v1"
        and assumptions.tax_policy_id == "korean_equity_transaction_tax"
        and assumptions.tax_policy_version == "verified_2021_2026"
    )
    if not supported:
        raise ValueError("unsupported frozen cost model or tax-policy version")
    return CostModel(
        commission_per_side=assumptions.commission_per_side,
        vat_on_commission=assumptions.vat_on_commission,
        slippage_bps_per_side=assumptions.slippage_bps_per_side,
    ).roundtrip_cost(
        1.0,
        buy_date=buy_date,
        sell_date=sell_date,
        market=market,
    )


class DailyCloseObservation(BaseModel):
    """One source-bound close known no earlier than ``available_at``."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    observation_schema_version: Literal["m2.3-daily-close-v1"] = "m2.3-daily-close-v1"
    instrument_type: Literal["stock", "benchmark"]
    symbol: str = Field(min_length=1, max_length=64)
    market: Literal["KOSDAQ", "KOSPI"]
    trading_date: date
    close: float = Field(gt=0)
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=64)
    available_at: datetime
    captured_at: datetime
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("close")
    @classmethod
    def finite_close(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("daily close must be finite")
        return value

    @field_validator("available_at", "captured_at")
    @classmethod
    def utc_timestamps(cls, value: datetime, info) -> datetime:
        return require_utc(value, field=info.field_name)

    @model_validator(mode="after")
    def chronology_is_consistent(self) -> "DailyCloseObservation":
        if self.captured_at < self.available_at:
            raise ValueError("market observation cannot be captured before available")
        if self.available_at.astimezone(SEOUL).date() < self.trading_date:
            raise ValueError("daily close cannot be available before its trading date")
        expected_sha256 = daily_close_observation_sha256(
            instrument_type=self.instrument_type,
            symbol=self.symbol,
            market=self.market,
            trading_date=self.trading_date,
            close=self.close,
            source=self.source,
            source_version=self.source_version,
            available_at=self.available_at,
            captured_at=self.captured_at,
        )
        if self.observation_sha256 != expected_sha256:
            raise ValueError("daily close observation hash does not match its record")
        return self


class TradingSession(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    trading_date: date
    close_at: datetime

    @field_validator("close_at")
    @classmethod
    def utc_close(cls, value: datetime) -> datetime:
        return require_utc(value, field="session close_at")

    @model_validator(mode="after")
    def close_matches_date(self) -> "TradingSession":
        if self.close_at.astimezone(SEOUL).date() != self.trading_date:
            raise ValueError("session close timestamp must fall on its Korean date")
        if self.trading_date.weekday() >= 5:
            raise ValueError("trading sessions cannot fall on weekends")
        return self


class TradingSessionCalendar(BaseModel):
    """Source-bound, fully classified exchange-session window."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    calendar_schema_version: Literal["m2.3-session-calendar-v1"] = (
        "m2.3-session-calendar-v1"
    )
    market: Literal["KOSDAQ", "KOSPI"]
    symbol: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=64)
    source_year: int = Field(ge=2000, le=2100)
    source_payload_utf8: str = Field(min_length=2)
    source_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_hours_source: str = Field(min_length=1, max_length=128)
    session_hours_source_version: str = Field(min_length=1, max_length=64)
    session_hours_source_payload_utf8: str = Field(min_length=2)
    session_hours_source_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exception_policy_id: str = Field(min_length=1, max_length=128)
    exception_policy_version: str = Field(min_length=1, max_length=64)
    exception_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    window_start: date
    window_end: date
    sessions: tuple[TradingSession, ...] = Field(min_length=1)
    closed_weekdays: tuple[date, ...] = ()
    complete: Literal[True] = True
    captured_at: datetime
    calendar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("captured_at")
    @classmethod
    def utc_capture(cls, value: datetime) -> datetime:
        return require_utc(value, field="calendar captured_at")

    @model_validator(mode="after")
    def calendar_is_consistent(self) -> "TradingSessionCalendar":
        if self.window_start > self.window_end:
            raise ValueError("calendar window start cannot follow its end")
        if (
            self.source != KRX_HOLIDAY_SOURCE
            or self.source_version != KRX_HOLIDAY_SOURCE_VERSION
        ):
            raise ValueError("unsupported trading-session calendar source")
        if (
            self.window_start.year != self.source_year
            or self.window_end.year != self.source_year
        ):
            raise ValueError("calendar window must stay inside its source year")
        if self.source_payload_sha256 != krx_holiday_payload_sha256(
            self.source_payload_utf8
        ):
            raise ValueError(
                "calendar source payload hash does not match retained bytes"
            )
        try:
            holidays = parse_pinned_krx_holidays(
                self.source_payload_utf8,
                source_year=self.source_year,
            )
        except KRXCalendarEvidenceError as error:
            raise ValueError("calendar source evidence failed KRX parsing") from error
        if self.session_hours_source_payload_sha256 != session_hours_payload_sha256(
            self.session_hours_source_payload_utf8
        ):
            raise ValueError("session-hours payload hash does not match retained bytes")

        dates = tuple(session.trading_date for session in self.sessions)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            raise ValueError("calendar sessions must be unique and sorted")
        if self.closed_weekdays != tuple(sorted(self.closed_weekdays)) or len(
            self.closed_weekdays
        ) != len(set(self.closed_weekdays)):
            raise ValueError("closed weekdays must be unique and sorted")
        if any(
            date_value < self.window_start or date_value > self.window_end
            for date_value in (*dates, *self.closed_weekdays)
        ):
            raise ValueError("calendar dates must stay inside the complete window")
        if any(value.weekday() >= 5 for value in self.closed_weekdays):
            raise ValueError("closed weekdays must not contain weekends")
        if set(dates).intersection(self.closed_weekdays):
            raise ValueError("a date cannot be both an open and closed weekday")
        expected_weekdays = set()
        current = self.window_start
        while current <= self.window_end:
            if current.weekday() < 5:
                expected_weekdays.add(current)
            current += timedelta(days=1)
        if set(dates).union(self.closed_weekdays) != expected_weekdays:
            raise ValueError("calendar must classify every weekday in its window")
        retained_closures = {
            holiday.trading_date
            for holiday in holidays
            if self.window_start <= holiday.trading_date <= self.window_end
            and holiday.trading_date.weekday() < 5
        }
        expected_open_dates = tuple(sorted(expected_weekdays - retained_closures))
        expected_closed_dates = tuple(sorted(retained_closures))
        try:
            expected_sessions = tuple(
                TradingSession(
                    trading_date=trading_date,
                    close_at=datetime.combine(
                        trading_date,
                        krx_session_close(
                            trading_date,
                            source=self.session_hours_source,
                            source_version=self.session_hours_source_version,
                            source_payload_utf8=(
                                self.session_hours_source_payload_utf8
                            ),
                            source_payload_sha256=(
                                self.session_hours_source_payload_sha256
                            ),
                            exception_policy_id=self.exception_policy_id,
                            exception_policy_version=self.exception_policy_version,
                            exception_evidence_sha256=(self.exception_evidence_sha256),
                        ),
                        tzinfo=SEOUL,
                    ).astimezone(timezone.utc),
                )
                for trading_date in expected_open_dates
            )
        except KRXSessionHoursEvidenceError as error:
            raise ValueError("calendar session-hours evidence is incomplete") from error
        if (
            self.sessions != expected_sessions
            or self.closed_weekdays != expected_closed_dates
        ):
            raise ValueError(
                "calendar sessions and closures do not match retained KRX evidence"
            )
        expected_sha256 = trading_session_calendar_sha256(
            market=self.market,
            symbol=self.symbol,
            source=self.source,
            source_version=self.source_version,
            source_year=self.source_year,
            source_payload_utf8=self.source_payload_utf8,
            source_payload_sha256=self.source_payload_sha256,
            session_hours_source=self.session_hours_source,
            session_hours_source_version=self.session_hours_source_version,
            session_hours_source_payload_utf8=(self.session_hours_source_payload_utf8),
            session_hours_source_payload_sha256=(
                self.session_hours_source_payload_sha256
            ),
            exception_policy_id=self.exception_policy_id,
            exception_policy_version=self.exception_policy_version,
            exception_evidence_sha256=self.exception_evidence_sha256,
            window_start=self.window_start,
            window_end=self.window_end,
            sessions=self.sessions,
            closed_weekdays=self.closed_weekdays,
            captured_at=self.captured_at,
        )
        if self.calendar_sha256 != expected_sha256:
            raise ValueError("trading-session calendar hash does not match its record")
        return self


class ForwardOutcome(BaseModel):
    """One prospective realized outcome, separate from its frozen decision."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    outcome_schema_version: Literal["m2.3-v1"] = "m2.3-v1"
    outcome_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_assessed_at: datetime
    experiment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    experiment_version: int = Field(ge=1)
    experiment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_receipt_no: str = Field(pattern=r"^[0-9]{14}$")
    event_date: date
    issuer_stock_code: str = Field(pattern=r"^[0-9]{6}$")
    market: Literal["KOSDAQ", "KOSPI"]
    entry_rule: ExecutionRule
    exit_rule: ExecutionRule
    benchmark: BenchmarkDefinition
    costs: CostAssumptions
    session_calendar: TradingSessionCalendar
    benchmark_sessions: tuple[DailyCloseObservation, ...] = Field(min_length=1)
    stock_entry: DailyCloseObservation
    stock_exit: DailyCloseObservation
    benchmark_entry: DailyCloseObservation
    benchmark_exit: DailyCloseObservation
    stock_gross_return: float
    benchmark_gross_return: float
    roundtrip_cost_fraction: float = Field(ge=0)
    raw_net_return: float
    abnormal_net_return: float
    evaluated_at: datetime
    sample_kind: Literal["prospective_forward"] = "prospective_forward"
    authorizes_execution: Literal[False] = False
    is_decision: Literal[False] = False
    is_outcome: Literal[True] = True

    @field_validator("decision_assessed_at", "evaluated_at")
    @classmethod
    def utc_timestamps(cls, value: datetime, info) -> datetime:
        return require_utc(value, field=info.field_name)

    @field_validator(
        "stock_gross_return",
        "benchmark_gross_return",
        "roundtrip_cost_fraction",
        "raw_net_return",
        "abnormal_net_return",
    )
    @classmethod
    def finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("forward outcome metrics must be finite")
        return value

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> "ForwardOutcome":
        if self.outcome_id != outcome_id_for(
            decision_id=self.decision_id,
            decision_sha256=self.decision_sha256,
        ):
            raise ValueError("outcome ID does not match its frozen decision")

        observations = (
            self.stock_entry,
            self.stock_exit,
            self.benchmark_entry,
            self.benchmark_exit,
        )
        if any(observation.market != self.market for observation in observations):
            raise ValueError("outcome observations must use the decision market")
        if (
            self.stock_entry.instrument_type != "stock"
            or self.stock_exit.instrument_type != "stock"
            or self.stock_entry.symbol != self.issuer_stock_code
            or self.stock_exit.symbol != self.issuer_stock_code
        ):
            raise ValueError("stock observations do not match the decision issuer")
        if (
            self.stock_entry.source != self.stock_exit.source
            or self.stock_entry.source_version != self.stock_exit.source_version
        ):
            raise ValueError("stock entry and exit must use one source version")

        mapping = next(
            (
                item
                for item in self.benchmark.market_mappings
                if item.market == self.market
            ),
            None,
        )
        if mapping is None:
            raise ValueError("frozen benchmark has no mapping for the decision market")
        if (
            self.benchmark.alignment != "exact_trading_date"
            or self.benchmark.return_type != "price_return"
            or self.benchmark.missing_data_policy != "fail"
        ):
            raise ValueError("unsupported frozen benchmark semantics")
        if (
            self.session_calendar.market != self.market
            or self.session_calendar.symbol != mapping.symbol
            or not (
                self.session_calendar.window_start
                <= self.event_date
                <= self.session_calendar.window_end
            )
        ):
            raise ValueError("session calendar does not match the frozen benchmark")

        try:
            entry_offset, holding_sessions = frozen_session_offsets(
                self.entry_rule,
                self.exit_rule,
            )
        except ValueError as error:
            raise ValueError("outcome contains unsupported frozen rules") from error
        future_sessions = tuple(
            session
            for session in self.session_calendar.sessions
            if session.trading_date > self.event_date
        )
        entry_index = entry_offset - 1
        exit_index = entry_index + holding_sessions
        if len(future_sessions) <= exit_index:
            raise ValueError(
                "complete session window does not cover the frozen horizon"
            )
        required_sessions = future_sessions[: exit_index + 1]
        if tuple(
            observation.trading_date for observation in self.benchmark_sessions
        ) != tuple(session.trading_date for session in required_sessions):
            raise ValueError("benchmark session evidence is incomplete or shifted")
        if (
            self.benchmark_entry != self.benchmark_sessions[entry_index]
            or self.benchmark_exit != self.benchmark_sessions[exit_index]
        ):
            raise ValueError("selected benchmark observations violate frozen rules")
        if any(
            observation.instrument_type != "benchmark"
            or observation.symbol != mapping.symbol
            or observation.market != self.market
            or observation.source != self.benchmark.source
            or observation.source_version != self.benchmark.source_version
            for observation in self.benchmark_sessions
        ):
            raise ValueError("benchmark observations do not match the frozen benchmark")

        if self.stock_entry.trading_date >= self.stock_exit.trading_date:
            raise ValueError("outcome exit must follow entry")
        if (
            self.benchmark_entry.trading_date != self.stock_entry.trading_date
            or self.benchmark_exit.trading_date != self.stock_exit.trading_date
        ):
            raise ValueError(
                "benchmark observations must use exact stock trading dates"
            )
        if any(
            observation.available_at < session.close_at
            for observation, session in zip(
                self.benchmark_sessions,
                required_sessions,
                strict=True,
            )
        ):
            raise ValueError("benchmark close cannot be available before session close")
        selected_sessions = (
            required_sessions[entry_index],
            required_sessions[exit_index],
        )
        if any(
            observation.available_at < session.close_at
            for observation, session in zip(
                (self.stock_entry, self.stock_exit),
                selected_sessions,
                strict=True,
            )
        ):
            raise ValueError("stock close cannot be available before session close")
        if self.session_calendar.captured_at > self.evaluated_at:
            raise ValueError("outcome cannot use a calendar captured after evaluation")
        if any(
            observation.available_at <= self.decision_assessed_at
            for observation in (*observations, *self.benchmark_sessions)
        ):
            raise ValueError(
                "outcome observations must become available after decision"
            )
        if any(
            observation.captured_at > self.evaluated_at
            for observation in (*observations, *self.benchmark_sessions)
        ):
            raise ValueError("outcome cannot use market data captured after evaluation")

        expected_stock = self.stock_exit.close / self.stock_entry.close - 1.0
        expected_benchmark = (
            self.benchmark_exit.close / self.benchmark_entry.close - 1.0
        )
        expected_cost = frozen_roundtrip_cost_fraction(
            self.costs,
            buy_date=self.stock_entry.trading_date,
            sell_date=self.stock_exit.trading_date,
            market=self.market,
        )
        expected_raw_net = expected_stock - expected_cost
        expected_abnormal_net = expected_stock - expected_benchmark - expected_cost
        actual = (
            self.stock_gross_return,
            self.benchmark_gross_return,
            self.roundtrip_cost_fraction,
            self.raw_net_return,
            self.abnormal_net_return,
        )
        expected = (
            expected_stock,
            expected_benchmark,
            expected_cost,
            expected_raw_net,
            expected_abnormal_net,
        )
        if actual != expected:
            raise ValueError("forward outcome metrics do not match prices and costs")
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self)

    def sha256(self) -> str:
        return _sha256(self.canonical_json())
