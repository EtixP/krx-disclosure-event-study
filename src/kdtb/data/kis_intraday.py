"""KIS Open API client and strict parser for prospective KRX minute bars."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import httpx

from kdtb.schemas.intraday_market_data import (
    KIS_MINUTE_ENDPOINT,
    KIS_MINUTE_TR_ID,
    IntradayBarObservation,
    IntradayCollectionTarget,
    make_intraday_bar_observation,
)

KIS_PRODUCTION_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_TOKEN_ENDPOINT = "/oauth2/tokenP"
KIS_MINUTE_PAGE_SIZE = 120
SEOUL = ZoneInfo("Asia/Seoul")
_DIGITS = re.compile(r"^[0-9]+$")


class KisIntradayError(RuntimeError):
    """Provider, transport, or source-shape failure."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    value = value.astimezone(timezone.utc)
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{name} must normalize to UTC")
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is not valid: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise KisIntradayError("KIS returned invalid strict JSON") from exc


@dataclass(frozen=True)
class KisMinuteRequest:
    stock_code: str
    target_date: date
    through_time: time
    page_number: int
    requested_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.stock_code, str) or not re.fullmatch(
            r"[0-9]{6}", self.stock_code
        ):
            raise ValueError("KIS stock code must contain exactly six digits")
        if isinstance(self.target_date, datetime) or not isinstance(
            self.target_date, date
        ):
            raise ValueError("target_date must be a date")
        if not isinstance(self.through_time, time):
            raise ValueError("through_time must be a time")
        if self.through_time.tzinfo is not None:
            raise ValueError("through_time is a Korea-local wall time")
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or self.page_number < 1
        ):
            raise ValueError("page_number must be a positive integer")
        object.__setattr__(
            self, "requested_at", _utc(self.requested_at, "requested_at")
        )

    def public_parameters(self) -> dict[str, str]:
        return {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": self.stock_code,
            "FID_INPUT_HOUR_1": self.through_time.strftime("%H%M%S"),
            "FID_INPUT_DATE_1": self.target_date.strftime("%Y%m%d"),
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_FAKE_TICK_INCU_YN": "",
        }

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "page_number": self.page_number,
                "requested_at": self.requested_at.isoformat(),
                "parameters": self.public_parameters(),
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class KisMinuteCapture:
    request: KisMinuteRequest
    received_at: datetime
    status_code: int
    raw_content: bytes
    source_url: str = KIS_PRODUCTION_BASE_URL + KIS_MINUTE_ENDPOINT

    def __post_init__(self) -> None:
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        if self.received_at < self.request.requested_at:
            raise ValueError("provider response cannot precede its request")
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("status_code must be a valid HTTP status")
        if not isinstance(self.raw_content, bytes):
            raise ValueError("raw_content must be exact response bytes")
        if self.source_url != KIS_PRODUCTION_BASE_URL + KIS_MINUTE_ENDPOINT:
            raise ValueError("capture source URL is not the verified KIS endpoint")

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw_content).hexdigest()


@dataclass(frozen=True)
class ParsedMinutePage:
    observations: tuple[IntradayBarObservation, ...]
    source_row_count: int


class IntradayBarSource(Protocol):
    def fetch_minute_page(
        self,
        stock_code: str,
        target_date: date,
        through_time: time,
        page_number: int,
    ) -> KisMinuteCapture: ...


def _integer(row: dict[str, object], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool):
        raise KisIntradayError(f"KIS row field {field} is not an integer string")
    text = str(value) if isinstance(value, int) else value
    if not isinstance(text, str) or _DIGITS.fullmatch(text) is None:
        raise KisIntradayError(f"KIS row field {field} is not an integer string")
    return int(text)


def parse_kis_minute_capture(
    capture: KisMinuteCapture,
    target: IntradayCollectionTarget,
) -> ParsedMinutePage:
    """Parse exact retained response bytes and reject cross-target data."""

    request = capture.request
    if target.stock_code is None:
        raise KisIntradayError("a missing-stock target cannot have a provider capture")
    if (
        request.stock_code != target.stock_code
        or request.target_date != target.target_date
    ):
        raise KisIntradayError("KIS request does not match its immutable event target")
    if capture.status_code != 200:
        raise KisIntradayError(f"KIS HTTP status {capture.status_code}")
    body = _strict_json(capture.raw_content)
    if not isinstance(body, dict):
        raise KisIntradayError("KIS response must be a JSON object")
    if body.get("rt_cd") != "0":
        code = body.get("msg_cd")
        raise KisIntradayError(f"KIS rejected minute request with code {code!r}")
    rows = body.get("output2")
    if not isinstance(rows, list):
        raise KisIntradayError("KIS response output2 must be an array")
    if len(rows) > KIS_MINUTE_PAGE_SIZE:
        raise KisIntradayError(
            f"KIS response exceeds the documented {KIS_MINUTE_PAGE_SIZE}-row limit"
        )

    observations: list[IntradayBarObservation] = []
    seen_timestamps: dict[datetime, IntradayBarObservation] = {}
    through = datetime.combine(request.target_date, request.through_time, tzinfo=SEOUL)
    for index, untyped_row in enumerate(rows):
        if not isinstance(untyped_row, dict) or any(
            not isinstance(key, str) for key in untyped_row
        ):
            raise KisIntradayError("KIS minute rows must be string-keyed objects")
        row = untyped_row
        date_text = row.get("stck_bsop_date")
        time_text = row.get("stck_cntg_hour")
        if not isinstance(date_text, str) or not re.fullmatch(r"[0-9]{8}", date_text):
            raise KisIntradayError("KIS row has no exact business date")
        if not isinstance(time_text, str) or not re.fullmatch(r"[0-9]{6}", time_text):
            raise KisIntradayError("KIS row has no exact market time")
        try:
            market_timestamp = datetime.strptime(
                date_text + time_text, "%Y%m%d%H%M%S"
            ).replace(tzinfo=SEOUL)
        except ValueError as exc:
            raise KisIntradayError(
                "KIS row contains an invalid market timestamp"
            ) from exc
        if market_timestamp.date() != request.target_date:
            raise KisIntradayError(
                "KIS row date differs from the requested market date"
            )
        if market_timestamp > through:
            raise KisIntradayError("KIS row is later than the requested page cursor")

        values = {
            "target_id": target.target_id,
            "stock_code": target.stock_code,
            "market_timestamp": market_timestamp,
            "captured_at": capture.received_at,
            "capture_sha256": capture.raw_sha256,
            "raw_row_index": index,
            "open": _integer(row, "stck_oprc"),
            "high": _integer(row, "stck_hgpr"),
            "low": _integer(row, "stck_lwpr"),
            "close": _integer(row, "stck_prpr"),
            "volume": _integer(row, "cntg_vol"),
            "cumulative_volume": _integer(row, "acml_vol"),
        }
        try:
            observation = make_intraday_bar_observation(**values)
        except ValueError as exc:
            raise KisIntradayError("KIS minute row violates bar invariants") from exc
        duplicate = seen_timestamps.get(market_timestamp)
        if (
            duplicate is not None
            and duplicate.identity_payload() != observation.identity_payload()
        ):
            raise KisIntradayError(
                "KIS response has conflicting rows for one timestamp"
            )
        if duplicate is None:
            seen_timestamps[market_timestamp] = observation
            observations.append(observation)

    return ParsedMinutePage(
        observations=tuple(observations),
        source_row_count=len(rows),
    )


class KisOpenApiClient:
    """Minimal authenticated client for the verified KIS dated-minute endpoint."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 15.0,
    ) -> None:
        if (
            not isinstance(app_key, str)
            or not isinstance(app_secret, str)
            or not app_key.strip()
            or not app_secret.strip()
        ):
            raise ValueError("KIS app key and secret are required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self._app_key = app_key
        self._app_secret = app_secret
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=KIS_PRODUCTION_BASE_URL,
            timeout=timeout_seconds,
        )
        self._access_token: str | None = None

    def __enter__(self) -> "KisOpenApiClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _now(self) -> datetime:
        return _utc(self._clock(), "KIS client clock")

    def _token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        response = self._client.post(
            KIS_TOKEN_ENDPOINT,
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise KisIntradayError(f"KIS token HTTP status {response.status_code}")
        body = _strict_json(response.content)
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise KisIntradayError("KIS token response omitted access_token")
        self._access_token = token
        return token

    def fetch_minute_page(
        self,
        stock_code: str,
        target_date: date,
        through_time: time,
        page_number: int,
    ) -> KisMinuteCapture:
        request = KisMinuteRequest(
            stock_code=stock_code,
            target_date=target_date,
            through_time=through_time,
            page_number=page_number,
            requested_at=self._now(),
        )
        response = self._client.get(
            KIS_MINUTE_ENDPOINT,
            params=request.public_parameters(),
            headers={
                "Accept": "application/json",
                "authorization": f"Bearer {self._token()}",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
                "tr_id": KIS_MINUTE_TR_ID,
                "custtype": "P",
            },
        )
        return KisMinuteCapture(
            request=request,
            received_at=self._now(),
            status_code=response.status_code,
            raw_content=response.content,
        )
