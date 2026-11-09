"""Pure candidate features on caller-declared, pre-publication buyback inputs.

These checks establish consistency, not public availability, source vintage,
security identity, complete sessions, eligibility or predictive value. Historical
availability describes the supplied vintage, never the date of later retrieval.
There is no source loader, calendar inference, admission or learner integration.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from kdtb.schemas.session import TradingSession, require_utc

_KST = ZoneInfo("Asia/Seoul")
_SERIES = ("stock_close", "index_close", "stock_traded_value_krw")


def _identifier(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("identifiers must be nonempty and trimmed")
    return value


def _number(value, *, positive: bool = False):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError("numbers must be Python int/float, never bool or string")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("numbers must be finite") from error
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError("numbers must be finite and positive/nonnegative")
    return result


def _paired(value, available_at, *, field: str) -> None:
    if (value is None) != (available_at is None):
        raise ValueError(
            f"{field} value and availability must both exist or both be null"
        )


class BuybackFeatureFacts(BaseModel):
    """One declared ordinary-share security and the terms applicable at cutoff."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision_id: str
    issuer_id: str
    ordinary_security_id: str
    market: Literal["KOSPI", "KOSDAQ"]
    decision_at: datetime
    original_publication_lower_at: datetime
    original_publication_upper_at: datetime
    terms_available_at: datetime
    announced_ordinary_shares: int | None
    announced_ordinary_value_krw: float | None
    listed_ordinary_shares: int | None
    listed_shares_reference_at: datetime
    listed_shares_available_at: datetime | None

    @field_validator("decision_id", "issuer_id", "ordinary_security_id")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _identifier(value)

    @field_validator(
        "announced_ordinary_shares", "listed_ordinary_shares", mode="before"
    )
    @classmethod
    def quantities(cls, value):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("share quantities must be nonnegative strict integers")
        return value

    @field_validator("announced_ordinary_value_krw", mode="before")
    @classmethod
    def nonnegative_krw(cls, value):
        return _number(value)

    @field_validator(
        "decision_at",
        "original_publication_lower_at",
        "original_publication_upper_at",
        "terms_available_at",
        "listed_shares_reference_at",
        "listed_shares_available_at",
    )
    @classmethod
    def utc_timestamps(cls, value, info):
        return None if value is None else require_utc(value, field=info.field_name)

    @model_validator(mode="after")
    def declared_chronology(self) -> BuybackFeatureFacts:
        local = self.decision_at.astimezone(_KST)
        if (local.hour, local.minute, local.second, local.microsecond) != (8, 55, 0, 0):
            raise ValueError("decision must be exactly 08:55:00 KST")
        lower, upper = (
            self.original_publication_lower_at,
            self.original_publication_upper_at,
        )
        if not lower <= upper < self.decision_at:
            raise ValueError("original publication interval must precede decision")
        if not lower <= self.terms_available_at < self.decision_at:
            raise ValueError(
                "applicable terms must be available from original lower bound before decision"
            )
        _paired(
            self.listed_ordinary_shares,
            self.listed_shares_available_at,
            field="listed shares",
        )
        if (
            self.listed_shares_available_at is not None
            and self.listed_shares_available_at >= self.decision_at
        ):
            raise ValueError("listed-share vintage must precede decision")
        return self


class BuybackHistoryObservation(BaseModel):
    """Aligned stock/index session with separately declared numeric vintages."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ordinary_security_id: str
    market: Literal["KOSPI", "KOSDAQ"]
    session: TradingSession
    stock_close: float | None
    stock_close_available_at: datetime | None
    index_close: float | None
    index_close_available_at: datetime | None
    stock_traded_value_krw: float | None
    stock_traded_value_krw_available_at: datetime | None

    @field_validator("ordinary_security_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("stock_close", "index_close", mode="before")
    @classmethod
    def positive_prices(cls, value):
        return _number(value, positive=True)

    @field_validator("stock_traded_value_krw", mode="before")
    @classmethod
    def nonnegative_krw(cls, value):
        return _number(value)

    @field_validator(
        "stock_close_available_at",
        "index_close_available_at",
        "stock_traded_value_krw_available_at",
    )
    @classmethod
    def utc_timestamps(cls, value, info):
        return None if value is None else require_utc(value, field=info.field_name)

    @model_validator(mode="after")
    def declared_vintages(self) -> BuybackHistoryObservation:
        for field in _SERIES:
            available = getattr(self, f"{field}_available_at")
            _paired(getattr(self, field), available, field=field)
            if available is not None and available < self.session.close_at:
                raise ValueError(
                    "session observations cannot be available before close"
                )
        return self


def _snapshot(value, model_type):
    if type(value) is not model_type:
        raise TypeError(f"expected a {model_type.__name__} instance")
    # Dump first so bypassed nested models are also rebuilt and revalidated.
    return model_type.model_validate(value.model_dump(warnings=False))


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("derived features and aggregates must be finite")
    return value


def _ratio(numerator, denominator) -> float:
    try:
        return _finite(numerator / denominator)
    except OverflowError as error:
        raise ValueError("derived ratio must be finite") from error


def _feature(value, reasons, formula: str, unit: str = "return_fraction") -> dict:
    return {"value": value, "reasons": reasons, "formula": formula, "unit": unit}


def _price_features(history, field: str, symbol: str) -> tuple[dict, dict]:
    prices = [getattr(row, field) for row in history]
    reasons = [f"INCOMPLETE_{field.upper()}_21"] if None in prices else []
    total = volatility = None
    if not reasons:
        total = _finite(_ratio(prices[-1], prices[0]) - 1)
        returns = [_finite(_ratio(b, a) - 1) for a, b in zip(prices, prices[1:])]
        # statistics.stdev avoids naive squared-deviation overflow and uses ddof=1.
        try:
            volatility = _finite(statistics.stdev(returns))
        except OverflowError as error:
            raise ValueError("derived volatility must be finite") from error
    return (
        _feature(
            total, list(reasons), f"{symbol}20/{symbol}0 - 1; all 21 closes required"
        ),
        _feature(
            volatility,
            list(reasons),
            f"sample_sd({symbol}k/{symbol}(k-1)-1, k=1..20, ddof=1)",
            "unannualized_daily_return_fraction",
        ),
    )


def calculate_buyback_features(
    facts: BuybackFeatureFacts,
    history: Sequence[BuybackHistoryObservation],
    *,
    next_session: TradingSession,
) -> dict:
    """Calculate six candidate features; no admission or trade decision follows."""
    facts = _snapshot(facts, BuybackFeatureFacts)
    next_session = _snapshot(next_session, TradingSession)
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise TypeError("history must be a finite sequence of typed observations")
    rows = sorted(
        [_snapshot(row, BuybackHistoryObservation) for row in history],
        key=lambda row: row.session.trading_date,
    )
    if len(rows) != 21:
        raise ValueError("history must contain exactly 21 declared sessions")
    dates = [row.session.trading_date for row in rows]
    closes = [row.session.close_at for row in rows]
    if len(set(dates)) != 21 or len(set(closes)) != 21 or closes != sorted(closes):
        raise ValueError(
            "session dates and closes must be unique and strictly increasing"
        )
    for row in rows:
        if (row.ordinary_security_id, row.market) != (
            facts.ordinary_security_id,
            facts.market,
        ):
            raise ValueError("history security/market mismatch")
        for field in _SERIES:
            available = getattr(row, f"{field}_available_at")
            if available is not None and available >= facts.decision_at:
                raise ValueError("observation vintage must strictly precede decision")
    reference = rows[-1].session
    if (
        next_session.trading_date <= reference.trading_date
        or next_session.close_at <= reference.close_at
    ):
        raise ValueError("next session must follow reference session")
    if not (
        reference.close_at < facts.original_publication_lower_at
        and facts.original_publication_upper_at <= next_session.close_at
    ):
        raise ValueError(
            "original publication interval does not resolve this reference close"
        )
    if facts.listed_shares_reference_at != reference.close_at:
        raise ValueError("listed-shares reference must equal pre-publication close")

    share_reasons = []
    if facts.announced_ordinary_shares is None:
        share_reasons.append("MISSING_ANNOUNCED_ORDINARY_SHARES")
    if facts.listed_ordinary_shares is None:
        share_reasons.append("MISSING_LISTED_ORDINARY_SHARES")
    elif facts.listed_ordinary_shares == 0:
        share_reasons.append("ZERO_LISTED_ORDINARY_SHARES")
    share_fraction = (
        None
        if share_reasons
        else _ratio(facts.announced_ordinary_shares, facts.listed_ordinary_shares)
    )

    value_reasons = []
    if facts.announced_ordinary_value_krw is None:
        value_reasons.append("MISSING_ANNOUNCED_ORDINARY_VALUE_KRW")
    turnover = [row.stock_traded_value_krw for row in rows[1:]]
    median = None
    if None in turnover:
        value_reasons.append("INCOMPLETE_STOCK_TRADED_VALUE_KRW_20")
    else:
        ordered = sorted(turnover)
        # Exact internal accumulation avoids sum overflow and tiny midpoint-step loss.
        median = _finite(statistics.mean(ordered[9:11]))
        if median == 0:
            if ordered[10] > 0:
                raise ValueError("positive median turnover is not representable")
            value_reasons.append("ZERO_MEDIAN_STOCK_TRADED_VALUE_KRW_20")
    value_ratio = (
        None if value_reasons else _ratio(facts.announced_ordinary_value_krw, median)
    )
    stock_return, stock_volatility = _price_features(rows, "stock_close", "P")
    market_return, market_volatility = _price_features(rows, "index_close", "B")
    return {
        "schema_version": "m0.19-declared-buyback-features-v1",
        "scope": "OFFLINE_DECLARED_INPUT_CALCULATIONS_ONLY_NO_SOURCE_ADMISSION",
        "decision_id": facts.decision_id,
        "issuer_id": facts.issuer_id,
        "ordinary_security_id": facts.ordinary_security_id,
        "market": facts.market,
        "decision_at": facts.decision_at.isoformat(),
        "original_publication_interval": {
            "lower_at": facts.original_publication_lower_at.isoformat(),
            "upper_at": facts.original_publication_upper_at.isoformat(),
        },
        "terms_available_at": facts.terms_available_at.isoformat(),
        "reference_session": reference.model_dump(mode="json"),
        "next_session": next_session.model_dump(mode="json"),
        "window": {
            "first_session": rows[0].session.model_dump(mode="json"),
            "declared_session_count": 21,
            "daily_return_count": 20,
            "turnover_session_count": 20,
            "completeness": "CALLER_DECLARED_ADJACENT_SESSIONS_NOT_SOURCE_PROVEN",
        },
        "features": {
            "announced_share_fraction": _feature(
                share_fraction,
                share_reasons,
                "announced_ordinary_shares/listed_ordinary_shares_at_R",
                "share_fraction",
            ),
            "announced_value_to_median_turnover_20": _feature(
                value_ratio,
                value_reasons,
                "announced_ordinary_value_krw/median(V1..V20); excludes V0",
                "ratio_of_KRW_to_session_KRW_turnover",
            ),
            "stock_return_20": stock_return,
            "market_return_20": market_return,
            "stock_daily_volatility_20": stock_volatility,
            "market_daily_volatility_20": market_volatility,
        },
    }
