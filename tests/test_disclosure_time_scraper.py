from __future__ import annotations

from datetime import date

import httpx

from kdtb.data.disclosure_time_scraper import (
    DisclosureTimeScraper,
    parse_times,
    total_count,
)

_SAMPLE_ROW = (
    '<tr> <td> 16:24 </td> <td class="tL"> 에코글로우 </td> '
    '<td class="tL"> <a href="/dsaf001/main.do?rcpNo=20260626901432" '
    'title="단일판매ㆍ공급계약체결 공시뷰어 새창">단일판매ㆍ공급계약체결</a> </td> </tr>'
    '<tr> <td> 09:05 </td> <td> 삼성전자 </td> '
    '<td> <a href="x?rcpNo=20260626800111">자기주식취득</a> </td> </tr>'
)


def test_parse_times_pairs_rcptno_and_time():
    out = parse_times(_SAMPLE_ROW)
    assert out["20260626901432"] == "16:24"
    assert out["20260626800111"] == "09:05"


def test_parse_times_empty_on_no_rows():
    assert parse_times("<html><body>no rows</body></html>") == {}


def test_total_count_parsed():
    assert total_count("<span>총 490건</span>") == 490
    assert total_count("1,234건 조회") == 1234
    assert total_count("no count here") is None


def test_scrape_date_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        # one page, total fits on it
        return httpx.Response(200, text="총 2건 " + _SAMPLE_ROW)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with DisclosureTimeScraper(client=client) as s:
        out = s.scrape_date(date(2026, 6, 26))
    assert out == {"20260626901432": "16:24", "20260626800111": "09:05"}


def test_scrape_date_paginates():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        page = int(request.url.params["currentPage"])
        if page == 1:
            # claim 150 total -> 2 pages
            return httpx.Response(200, text='총 150건 <tr> <td> 10:00 </td> <a href="x?rcpNo=20260626000001">r</a> </tr>')
        return httpx.Response(200, text='<tr> <td> 11:30 </td> <a href="x?rcpNo=20260626000002">r</a> </tr>')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with DisclosureTimeScraper(client=client, sleep=0.0) as s:
        out = s.scrape_date(date(2026, 6, 26))
    assert out == {"20260626000001": "10:00", "20260626000002": "11:30"}
    assert calls["n"] == 2
