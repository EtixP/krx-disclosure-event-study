"""Pinned KRX session-hours evidence and exceptional-session policy.

The annual KRX holiday response proves whether a weekday is open, but it does
not contain session hours.  This module retains the separate official evidence
used for the regular 15:30 KST close and fails closed for the known 2026 CSAT
date until KRX publishes and this repository pins that date's exact hours.
"""

from __future__ import annotations

import gzip
import hashlib
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, time
from html.parser import HTMLParser
from xml.etree import ElementTree


KRX_REGULAR_HOURS_SOURCE = "KRX_GLOBAL_STOCK_TRADING_HOURS"
KRX_REGULAR_HOURS_SOURCE_VERSION = "glb0602020204-2026-09-16-v1"
KRX_REGULAR_HOURS_SOURCE_URL = (
    "https://global.krx.co.kr/contents/GLB/06/0602/0602020204/" "GLB0602020204T1.jsp"
)
KRX_REGULAR_HOURS_PAYLOAD_SHA256 = (
    "c8b1bdbc2bbe1bb243c6208c742aee71fccfb965f11cd0a26e2f0b01c9b24242"
)
_KRX_REGULAR_HOURS_PAYLOAD_GZIP_BASE64 = (
    "H4sIAAAAAAACE81XX2/bNhB/zoB+B07okPTBsSU7ktzaAoI0a4FtaNDlYXukyJNFlBYFkk5q"
    "YOjn2Euf9jX2kfohdqRkO3JSJ6i9YbAB/tHd7+5+vBNPz757tv5Nvn/97uL696tLUtq5zH"
    "DDjUTSajYNoAr8DlCeTaywErI3UuVUkp/e/0b+IJN+s4kyfS+Ek1zxZdZic3FDmKTGTAMOR"
    "syqnpfvSWFsGBDBpwFLQ0iilI/5iEVFPIyLQZwU6ZixcJTGEQs81tGEO9+OcLTZtaZcVDPy"
    "Vi20mfRxp3nC/Xj0kFWaS2gNAmM5SxMY5WcMogKidDhICz4uIprktBi2BhHHaxGzmM+pXk6"
    "DjtlgZcFS7+KRU2C0tkJV2w6utldSSs60WtTtutkhxi4lTINbwW35kkRnP7wK/jsB9LHr1M"
    "S2x9loW71BsiUxTNUIhDpIg5KmptU0QOIuqIWZ0ktMi/JrCmtyyoacjSTO9dqfu/Ynts2pX"
    "c5odesDCAjOGo9GQXaloYen9wEsMWAMnsLXfHtAPwqyd0XR847icSvj3K61YEBsE8QOMNTV"
    "HDTRwEDUdkuSZ4P05XBAegTH0QCf8gdo2BHrmsdDA+8kIZeKfVgFT2jFSU6NI7fd6qsNX2"
    "peA1a7uIGu2n6cDXxoYxwPzBkCf/pW4PucbcoC+XsPs4Wkmjw5EZ+aO+EZjgflYdwQ/I3A"
    "2zzEWIDK2P9pBfogXbDxgbMJgUd7AD9AwnlhQbc07hNx3KR5NDh0xPsB7/XO2S8DRv8OH0"
    "8AxunmWsOFazaaheumsIdxzZRhGh3H2fNTL/Cj0MZelELyk+PHGpnjF698E9ZfgzhDjuGt7"
    "qhSFrAxw16v87QUnEPVo9IG2Ze//v7y+U+EwufdG5wUSmNJwq4iJcIQ/8765C+l0xan6bM"
    "w1oaeVXt3+ZGV2H0Ctk9ScLp8WovHVGWhsndbS5omRREnYxoVQ0hCPg7TkMZJnpyNojBat5"
    "YIp+TqUKTIfqV2oRu7uNrsXy1yKRhG3XhFREUoYwpFKwz1VmBuYNdCmre9a/gI/t+oG9DV"
    "HD3zqdtiYHYjPeZOhB1LP9McSX1Nl60BR6FVHv2cWQd7aVw6CFN6ZFWQtcrJL6gWvriHaC"
    "xBS07UwSwB76MThVONazxzSupOeATRVkRsg51XS4/lgNaHxQHmhlRYacZgu0xyYHRhwBlEeM"
    "XYQmtwRCEwbsDHGpgFju21yzLSoqA04EmqOXqCIxeOR+N0XJq19wjlc1HhMWvP8sa5SX91"
    "jvcr6PmpS4xr9/VxcvxYYmDldKpmlaI4yqypqR3oj33RdNEbuPZNgF9RzUfYP0l9AGiiDQA"
    "A"
)

KRX_EXCEPTION_POLICY_ID = "KRX_CSAT_EXCEPTION_FAIL_CLOSED"
KRX_EXCEPTION_POLICY_VERSION = "2026-v1"
KRX_2025_CSAT_NOTICE_URL = (
    "https://kind.krx.co.kr/external/2025/10/30/000102/" "20251030000137/99303.htm"
)
KRX_2025_CSAT_NOTICE_SHA256 = (
    "f34bd2f0873506c767e4a1985c9885b0be0920bf564564d87460f61b5395df3d"
)
_KRX_2025_CSAT_NOTICE_GZIP_BASE64 = (
    "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"
    "1h71kYz3jzBTfwMyjer9cCMAAA=="
)

MOE_2026_CSAT_SOURCE_URL = (
    "https://www.moe.go.kr/boardCnts/fileDown.do?m=020402&s=moe&"
    "fileSeq=140d81510a9ce0417c52813a89eb8de9"
)
MOE_2026_CSAT_ATTACHMENT_SHA256 = (
    "c88311906018668e42dc60327a38b37d375a35863c7b453b5bd81d86e4505efb"
)
MOE_2026_CSAT_SECTION_SHA256 = (
    "bffe6fb0a75171165a4648eb8efba35e7ab5e15a356df652d8036f9758ddaaa1"
)
MOE_2026_CSAT_FRAGMENT_SHA256 = (
    "5fbe288655aa0067d786817914e492e14eed45c93df7017ad203bae438cf1989"
)
_MOE_2026_CSAT_FRAGMENT_GZIP_BASE64 = (
    "H4sIAAAAAAACE91YX2/TVhT/Kld+Ao01vo6TOFWD5LZBRQsNajMh7aVykpvEw7Gta4fQPpUR"
    "JjaQqLSNtgymThpCIJBgUNgDT3wUHmvnO+zcP05iRreuChvbk88595zj8+93fOU5N1BnfX"
    "S567jBLNAlpROG/mwm0+/3ZzqW2/C6Mw1v5iLNdPp+18loKsYZ36JWm1p+R0F2s6RoWC/o"
    "RjavGwpiR+fp2cUV0iopBRAE4bpDJK+y8zaZp8S6yLmG5/S67pjvEtomTUaenmOB0Z6LGh2"
    "LjjxmsTwJ6w5/NzZyumEUCwa8aqNKm4RyR26vWyfUdtu1dZ+UlJo5XykrKCSXwwsQNwiq59"
    "fM5cW1+WqtVj0nTs44Xr+kgGRpbfXsYnlVQY7XEHE1qec3LJ/nUlKWPZekMlkoVyoKosQnV"
    "rhELB4EBoHXX3BDqA9PlJMgbRDHWfWtBgTHfdc9FvUZ23FkjjqE75nNL3tBOK5EsIH6djPs"
    "wHHBwHlFcCvEqXklxZxfrVY+r0GGHWK3O+w9uaKeTdg/aPnUC0mDe0cZ4d/3AhRCNqEZLED"
    "BeaRWqwValclgW1CjC3bYqUG9hI4DkuolQh1WVtDoeE7TdBsdj5puc7XKZXCcBHHeXDGZEt"
    "1ICZiG6dhtl7dGKEi+Uj5TEwrVVisgoXwL3ZhgZRJeLzxn0bbtIoe0WOENqAEVFeF06PmSq"
    "nth6HUlI81tN2WNdTyy5jS35lRizRlpHVL5bCDX6sKYxHt3o0cPo0fXWB+sZDI7ViDeIuA"
    "w0QnStEOrzgaMTZxNw/X3zwdOMBD06hU7CDkOxAgv2hS82R44X6qunP2iulwzYTId2yVi7"
    "udXyuZnqXIvlJdr5RWuc5F5G0M1kSyD47GUY0hMouSW5MyJ5NhkJLrALve6kpNz9hcbIzv"
    "tjaHnk41xWlO1wvCH3ejaILo1QNHNTWDi6ztM9O39aO9+fOPucHsnvrc5lxEG4gkuhQdWx"
    "oC0LUqt9ZSElwEQNBr2STqwN6ClWl6V5UogKiR1KyDMC/AaBj5IwJbT5JhzXwU1KzjhTc9"
    "z45ZjteEwW8xqbCVkZMDpOLnI/yiqn4fSI4zjH79HuBjfe30C8HEy3t5CrPK3v4m2HkY3r"
    "h9S/D9zHd345RhWaDptzmLtI+t0ZmI5iATYN8dsNilrIHvyDODzNKIzYz1Y9y7TY8/kMzai"
    "J/Xe/SIl6RawVkhpprZqjuUutyqnD9+qoj2NhKCH7Nno5eaHWrXG/3jT6tPGOlYPW7WwZGH"
    "DSpQD7tEsOmwbTAeTupqCJNbfhaSQjCGJcXESkpoxCUlNVdUUJnNZVRthUtOLajZvHH39qh"
    "9BJwbP4r0raPj1fvxg852GaLwhmDXk4LfrU2qIphv/pY7kPmRHhjuD+KfHo1b8+gK211E/"
    "X1gtjvycOLIRVkdGB/tP4tsv3rwS9x54PrgSb18bXt062H8c7wD9BL3d3Dl4vg+z8Uk82B"
    "tevXfw/DUg8+3mLmI6Pz89heKvHg/v3HzzCk7AiTBG0hrI6OUADW9twWih6Ltnp9BxkoNx"
    "1EZjCxt+OHgq98dxsj559BAK4/tBdGuXYQOQEt95OKXbgp7L/ztAeG9kuj4RWl7T/7nQsK7"
    "jQlGb9lUGT/cqYxjytjX9q0xG/sWQ0zZSOu5g5bWCnupePgdwSrdPiCb6p6vZ7EQDP8WpD"
    "qbbZ2D2j+XvXER/Bx3VX6ZcEgAA"
)

KRX_EXCEPTION_EVIDENCE_SHA256 = (
    "3f4e9638be2203d151fca73e9638d3b31623c52d165dcfd62e2678eb658502b1"
)


class KRXSessionHoursEvidenceError(ValueError):
    """Raised when exact KRX close evidence is invalid or unavailable."""


class _TableCellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if tag in {"th", "td"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None:
            self.cells.append(" ".join("".join(self._cell).split()))
            self._cell = None


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.replace("\xa0", " ").split())
        if normalized:
            self.parts.append(normalized)


@dataclass(frozen=True)
class KRXCSATExceptionEvidence:
    precedent_date: date
    precedent_regular_close: time
    precedent_exception_close: time
    unresolved_2026_date: date


def _retained_bytes(value: str, expected_sha256: str) -> bytes:
    try:
        payload = gzip.decompress(b64decode(value))
    except Exception as error:
        raise RuntimeError("retained session-hours evidence is corrupt") from error
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("retained session-hours evidence hash is inconsistent")
    return payload


KRX_REGULAR_HOURS_PAYLOAD_UTF8 = _retained_bytes(
    _KRX_REGULAR_HOURS_PAYLOAD_GZIP_BASE64,
    KRX_REGULAR_HOURS_PAYLOAD_SHA256,
).decode("utf-8")
_KRX_2025_CSAT_NOTICE_UTF8 = _retained_bytes(
    _KRX_2025_CSAT_NOTICE_GZIP_BASE64,
    KRX_2025_CSAT_NOTICE_SHA256,
).decode("utf-8")
_MOE_2026_CSAT_FRAGMENT_UTF8 = _retained_bytes(
    _MOE_2026_CSAT_FRAGMENT_GZIP_BASE64,
    MOE_2026_CSAT_FRAGMENT_SHA256,
).decode("utf-8")


def session_hours_payload_sha256(payload_utf8: str) -> str:
    return hashlib.sha256(payload_utf8.encode("utf-8")).hexdigest()


def parse_pinned_krx_regular_close(payload_utf8: str) -> time:
    """Parse the regular-session close from the exact retained KRX page."""

    if payload_utf8 != KRX_REGULAR_HOURS_PAYLOAD_UTF8 or (
        session_hours_payload_sha256(payload_utf8) != KRX_REGULAR_HOURS_PAYLOAD_SHA256
    ):
        raise KRXSessionHoursEvidenceError(
            "KRX regular-hours payload does not match retained source bytes"
        )
    parser = _TableCellParser()
    parser.feed(payload_utf8)
    expected = (
        "Regular market session",
        "Order receipt",
        "08:30 - 15:30",
        "Trading",
        "09:00 - 15:30",
    )
    try:
        start = parser.cells.index(expected[0])
    except ValueError as error:
        raise KRXSessionHoursEvidenceError(
            "KRX regular-session row is missing"
        ) from error
    if tuple(parser.cells[start : start + len(expected)]) != expected:
        raise KRXSessionHoursEvidenceError(
            "KRX regular-session row has an unsupported shape"
        )
    return time(15, 30)


def parse_pinned_krx_csat_exception() -> KRXCSATExceptionEvidence:
    """Parse the official precedent and the official 2026 CSAT date."""

    notice = _TextParser()
    notice.feed(_KRX_2025_CSAT_NOTICE_UTF8)
    required_notice_parts = {
        "대학수학능력시험일(2025년 11월 13일) 출근시간 조정에 따라 "
        "유가증권시장의 거래시간이 다음과 같이 임시 변경됨을 알려드립니다.",
        "ㅇ 정규시장 : 09:00~15:30(경쟁대량매매는 15:00까지)",
        "ㅇ 정규시장 : 10:00~16:30(경쟁대량매매는 16:00까지)",
        "3. 시행일 : 2025. 11. 13 (목)",
    }
    if not required_notice_parts.issubset(notice.parts):
        raise KRXSessionHoursEvidenceError(
            "retained KRX CSAT notice has an unsupported shape"
        )

    try:
        root = ElementTree.fromstring(_MOE_2026_CSAT_FRAGMENT_UTF8)
    except ElementTree.ParseError as error:
        raise KRXSessionHoursEvidenceError(
            "retained Ministry of Education evidence is invalid XML"
        ) from error
    moe_text = "".join(
        element.text or "" for element in root.iter() if element.tag.endswith("}t")
    )
    if "2026년 11월 19일(목)에 시행됩니다" not in moe_text:
        raise KRXSessionHoursEvidenceError(
            "retained Ministry of Education evidence omits the 2026 CSAT date"
        )
    combined = hashlib.sha256(
        _KRX_2025_CSAT_NOTICE_UTF8.encode("utf-8")
        + b"\0"
        + _MOE_2026_CSAT_FRAGMENT_UTF8.encode("utf-8")
    ).hexdigest()
    if combined != KRX_EXCEPTION_EVIDENCE_SHA256:
        raise KRXSessionHoursEvidenceError(
            "KRX exception evidence hash is inconsistent"
        )
    return KRXCSATExceptionEvidence(
        precedent_date=date(2025, 11, 13),
        precedent_regular_close=time(15, 30),
        precedent_exception_close=time(16, 30),
        unresolved_2026_date=date(2026, 11, 19),
    )


def krx_session_close(
    trading_date: date,
    *,
    source: str,
    source_version: str,
    source_payload_utf8: str,
    source_payload_sha256: str,
    exception_policy_id: str,
    exception_policy_version: str,
    exception_evidence_sha256: str,
) -> time:
    """Return an evidenced regular close or reject a known exception date."""

    if (
        source != KRX_REGULAR_HOURS_SOURCE
        or source_version != KRX_REGULAR_HOURS_SOURCE_VERSION
        or source_payload_sha256 != KRX_REGULAR_HOURS_PAYLOAD_SHA256
        or exception_policy_id != KRX_EXCEPTION_POLICY_ID
        or exception_policy_version != KRX_EXCEPTION_POLICY_VERSION
        or exception_evidence_sha256 != KRX_EXCEPTION_EVIDENCE_SHA256
    ):
        raise KRXSessionHoursEvidenceError(
            "unsupported KRX session-hours evidence version"
        )
    regular_close = parse_pinned_krx_regular_close(source_payload_utf8)
    exception = parse_pinned_krx_csat_exception()
    if trading_date == exception.unresolved_2026_date:
        raise KRXSessionHoursEvidenceError(
            "exact KRX close time is unavailable for the 2026 CSAT session"
        )
    return regular_close
