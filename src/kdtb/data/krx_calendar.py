"""Pinned official KRX holiday evidence for prospective 2026 outcomes.

The payload below is the UTF-8 response body captured from KRX's annual
holiday grid on 2026-09-15.  M2.3 supports only this pinned evidence version;
an updated year or provider shape requires a new explicit parser/version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date


KRX_HOLIDAY_SOURCE = "KRX_MARKET_HOLIDAY_CALENDAR"
KRX_HOLIDAY_SOURCE_VERSION = "mkd01100305-2026-v1"
KRX_HOLIDAY_SOURCE_URL = (
    "https://open.krx.co.kr/contents/MKD/01/0110/01100305/MKD01100305.jsp"
)
KRX_HOLIDAY_SOURCE_YEAR = 2026

# Exact provider response body (no trailing newline), retrieved with
# search_bas_yy=2026 and gridTp=KRX.
KRX_HOLIDAY_PAYLOAD_2026_UTF8 = (
    '{"block1":['
    '{"calnd_dd":"2026-01-01","dy_tp_cd":"THU",'
    '"calnd_dd_dy":"2026-01-01","kr_dy_tp":"목요일","holdy_nm":"신정"},'
    '{"calnd_dd":"2026-02-16","dy_tp_cd":"MON",'
    '"calnd_dd_dy":"2026-02-16","kr_dy_tp":"월요일","holdy_nm":"설날"},'
    '{"calnd_dd":"2026-02-17","dy_tp_cd":"TUE",'
    '"calnd_dd_dy":"2026-02-17","kr_dy_tp":"화요일","holdy_nm":"설날"},'
    '{"calnd_dd":"2026-02-18","dy_tp_cd":"WED",'
    '"calnd_dd_dy":"2026-02-18","kr_dy_tp":"수요일","holdy_nm":"설날"},'
    '{"calnd_dd":"2026-03-02","dy_tp_cd":"MON",'
    '"calnd_dd_dy":"2026-03-02","kr_dy_tp":"월요일",'
    '"holdy_nm":"삼일절(대체휴일)"},'
    '{"calnd_dd":"2026-05-01","dy_tp_cd":"FRI",'
    '"calnd_dd_dy":"2026-05-01","kr_dy_tp":"금요일",'
    '"holdy_nm":"근로자의날"},'
    '{"calnd_dd":"2026-05-05","dy_tp_cd":"TUE",'
    '"calnd_dd_dy":"2026-05-05","kr_dy_tp":"화요일",'
    '"holdy_nm":"어린이날"},'
    '{"calnd_dd":"2026-05-25","dy_tp_cd":"MON",'
    '"calnd_dd_dy":"2026-05-25","kr_dy_tp":"월요일",'
    '"holdy_nm":"석가탄신일(대체휴일)"},'
    '{"calnd_dd":"2026-06-03","dy_tp_cd":"WED",'
    '"calnd_dd_dy":"2026-06-03","kr_dy_tp":"수요일",'
    '"holdy_nm":"임시공휴일"},'
    '{"calnd_dd":"2026-07-17","dy_tp_cd":"FRI",'
    '"calnd_dd_dy":"2026-07-17","kr_dy_tp":"금요일",'
    '"holdy_nm":"제헌절"},'
    '{"calnd_dd":"2026-08-17","dy_tp_cd":"MON",'
    '"calnd_dd_dy":"2026-08-17","kr_dy_tp":"월요일",'
    '"holdy_nm":"광복절(대체휴일)"},'
    '{"calnd_dd":"2026-09-24","dy_tp_cd":"THU",'
    '"calnd_dd_dy":"2026-09-24","kr_dy_tp":"목요일","holdy_nm":"추석"},'
    '{"calnd_dd":"2026-09-25","dy_tp_cd":"FRI",'
    '"calnd_dd_dy":"2026-09-25","kr_dy_tp":"금요일","holdy_nm":"추석"},'
    '{"calnd_dd":"2026-10-05","dy_tp_cd":"MON",'
    '"calnd_dd_dy":"2026-10-05","kr_dy_tp":"월요일",'
    '"holdy_nm":"개천절(대체휴일)"},'
    '{"calnd_dd":"2026-10-09","dy_tp_cd":"FRI",'
    '"calnd_dd_dy":"2026-10-09","kr_dy_tp":"금요일",'
    '"holdy_nm":"한글날"},'
    '{"calnd_dd":"2026-12-25","dy_tp_cd":"FRI",'
    '"calnd_dd_dy":"2026-12-25","kr_dy_tp":"금요일",'
    '"holdy_nm":"성탄절"},'
    '{"calnd_dd":"2026-12-31","dy_tp_cd":"THU",'
    '"calnd_dd_dy":"2026-12-31","kr_dy_tp":"목요일",'
    '"holdy_nm":"연말휴장일"}'
    "]}"
)
KRX_HOLIDAY_PAYLOAD_2026_SHA256 = (
    "89ccce131de8d0c4baa6a30d62b7d2e8e3bdc872c71a21d7d81d4b667330d384"
)


class KRXCalendarEvidenceError(ValueError):
    """Raised when retained KRX holiday bytes or their schema are invalid."""


@dataclass(frozen=True)
class KRXHoliday:
    trading_date: date
    name: str


_DAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_KOREAN_DAYS = (
    "월요일",
    "화요일",
    "수요일",
    "목요일",
    "금요일",
    "토요일",
    "일요일",
)
_ROW_KEYS = {"calnd_dd", "dy_tp_cd", "calnd_dd_dy", "kr_dy_tp", "holdy_nm"}


def krx_holiday_payload_sha256(payload_utf8: str) -> str:
    return hashlib.sha256(payload_utf8.encode("utf-8")).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def parse_pinned_krx_holidays(
    payload_utf8: str,
    *,
    source_year: int,
) -> tuple[KRXHoliday, ...]:
    """Validate the retained provider bytes and parse the versioned KRX shape."""

    if source_year != KRX_HOLIDAY_SOURCE_YEAR:
        raise KRXCalendarEvidenceError("unsupported KRX holiday source year")
    payload_sha256 = krx_holiday_payload_sha256(payload_utf8)
    if payload_sha256 != KRX_HOLIDAY_PAYLOAD_2026_SHA256:
        raise KRXCalendarEvidenceError(
            "KRX holiday payload does not match the pinned source bytes"
        )
    if payload_utf8 != KRX_HOLIDAY_PAYLOAD_2026_UTF8:
        raise KRXCalendarEvidenceError(
            "KRX holiday payload differs from retained source bytes"
        )

    try:
        payload = json.loads(
            payload_utf8,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError) as error:
        raise KRXCalendarEvidenceError(
            "KRX holiday payload is not strict JSON"
        ) from error
    if not isinstance(payload, dict) or set(payload) != {"block1"}:
        raise KRXCalendarEvidenceError("unexpected KRX holiday response envelope")
    rows = payload["block1"]
    if not isinstance(rows, list) or not rows:
        raise KRXCalendarEvidenceError("KRX holiday response has no holiday rows")

    holidays: list[KRXHoliday] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise KRXCalendarEvidenceError("unexpected KRX holiday row shape")
        try:
            observed = date.fromisoformat(row["calnd_dd"])
        except (TypeError, ValueError) as error:
            raise KRXCalendarEvidenceError("invalid KRX holiday date") from error
        if observed.year != source_year or row["calnd_dd_dy"] != observed.isoformat():
            raise KRXCalendarEvidenceError("inconsistent KRX holiday date fields")
        weekday = observed.weekday()
        if (
            row["dy_tp_cd"] != _DAY_CODES[weekday]
            or row["kr_dy_tp"] != _KOREAN_DAYS[weekday]
        ):
            raise KRXCalendarEvidenceError("inconsistent KRX holiday weekday fields")
        name = row["holdy_nm"]
        if not isinstance(name, str) or not name.strip():
            raise KRXCalendarEvidenceError("KRX holiday name is missing")
        holidays.append(KRXHoliday(trading_date=observed, name=name))

    dates = tuple(item.trading_date for item in holidays)
    if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise KRXCalendarEvidenceError("KRX holidays must be unique and sorted")
    return tuple(holidays)


# Catch accidental edits to the retained bytes at import time.
if (
    krx_holiday_payload_sha256(KRX_HOLIDAY_PAYLOAD_2026_UTF8)
    != KRX_HOLIDAY_PAYLOAD_2026_SHA256
):
    raise RuntimeError("pinned KRX holiday payload hash is inconsistent")
