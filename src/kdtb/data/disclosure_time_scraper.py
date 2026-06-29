"""Scrape exact disclosure filing times from the DART website.

The OPEN DART OpenAPI returns only the filing DATE (rcept_dt = YYYYMMDD), not
the time. But the DART website's daily disclosure list
(dsac001/mainAll.do?selectDate=YYYYMMDD) renders an HH:MM time column per row,
for historical dates as well as today. Each row pairs a time cell with the
disclosure's receipt number (rcpNo), so we can recover precise filing times
for any past disclosure and match them back to our stored rows by receipt_no.

This is the prerequisite for any intraday / execution-speed analysis: it tells
us whether a given event published during market hours (09:00-15:30 KST, so the
same-day close is tradable) or after the close (earliest entry is the next day).
"""
from __future__ import annotations

import logging
import re
import time as _time
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

LIST_URL = "https://dart.fss.or.kr/dsac001/mainAll.do"
_ROW_TIME = re.compile(r"<td>\s*([0-2]\d:[0-5]\d)\s*</td>")
_ROW_RCPT = re.compile(r"rcpNo=(\d{14})")
_TOTAL = re.compile(r"([\d,]+)\s*건")
_PAGE_SIZE = 100


def parse_times(html: str) -> dict[str, str]:
    """Extract {receipt_no: 'HH:MM'} from one DART list-page HTML."""
    out: dict[str, str] = {}
    for tr in html.split("<tr>"):
        mt = _ROW_TIME.search(tr)
        mr = _ROW_RCPT.search(tr)
        if mt and mr:
            out[mr.group(1)] = mt.group(1)
    return out


def total_count(html: str) -> Optional[int]:
    m = _TOTAL.search(html)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


class DisclosureTimeScraper:
    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        sleep: float = 0.25,
        max_pages: int = 12,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}
        )
        self._owns = client is None
        self.sleep = sleep
        self.max_pages = max_pages

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> "DisclosureTimeScraper":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _get(self, ymd: str, page: int) -> str:
        r = self._client.get(LIST_URL, params={"selectDate": ymd, "currentPage": page})
        r.raise_for_status()
        return r.text

    def scrape_date(self, d: date) -> dict[str, str]:
        """Return {receipt_no: 'HH:MM'} for every disclosure filed on date d."""
        ymd = d.strftime("%Y%m%d")
        first = self._get(ymd, 1)
        out = parse_times(first)
        total = total_count(first)
        pages = min(self.max_pages, (total + _PAGE_SIZE - 1) // _PAGE_SIZE) if total else 1
        for p in range(2, pages + 1):
            _time.sleep(self.sleep)
            try:
                out.update(parse_times(self._get(ymd, p)))
            except httpx.HTTPError as e:
                logger.warning("DART list %s page %d failed: %s", ymd, p, e)
                break
        return out
