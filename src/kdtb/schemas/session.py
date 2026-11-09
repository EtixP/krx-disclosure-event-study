"""Caller-declared sessions; validation does not attest an exchange calendar."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

SEOUL = ZoneInfo("Asia/Seoul")


def require_utc(value: datetime, *, field: str = "experiment timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    return value.astimezone(timezone.utc)


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
