from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import httpx
import pytest

from kdtb.data.dart_client import (
    DartApiError,
    DartClient,
    parse_report_relations_html,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _make_client(handler) -> DartClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return DartClient(api_key="testkey", client=http)


def test_requires_api_key():
    with pytest.raises(ValueError):
        DartClient(api_key="")


def test_list_disclosures_returns_records():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/list.json")
        assert request.url.params["crtfc_key"] == "testkey"
        assert request.url.params["bgn_de"] == "20260528"
        assert request.url.params["last_reprt_at"] == "N"
        return httpx.Response(
            200,
            json={
                "status": "000",
                "message": "정상",
                "page_no": 1,
                "total_page": 1,
                "list": [
                    {
                        "corp_code": "00126380",
                        "corp_name": "삼성전자",
                        "stock_code": "005930",
                        "corp_cls": "Y",
                        "report_nm": "단일판매·공급계약체결",
                        "rcept_no": "20260528000001",
                        "rcept_dt": "20260528",
                    }
                ],
            },
        )

    with _make_client(handler) as c:
        records = list(c.list_disclosures(date(2026, 5, 28)))
    assert len(records) == 1
    assert records[0]["rcept_no"] == "20260528000001"


def test_list_disclosures_paginates():
    pages = {
        1: [
            {"rcept_no": f"2026052800000{i}", "corp_code": "x", "corp_name": "x"}
            for i in range(1, 4)
        ],
        2: [
            {"rcept_no": f"2026052800000{i}", "corp_code": "x", "corp_name": "x"}
            for i in range(4, 6)
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page_no = int(request.url.params["page_no"])
        return httpx.Response(
            200,
            json={"status": "000", "total_page": 2, "list": pages[page_no]},
        )

    with _make_client(handler) as c:
        records = list(c.list_disclosures(date(2026, 5, 28)))
    assert len(records) == 5


def test_list_disclosure_pages_requests_supported_date_order_without_reordering():
    provider_items = [
        {"rcept_no": "20260528000002"},
        {"rcept_no": "20260528000001"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["page_count"] == "100"
        assert request.url.params["sort"] == "date"
        assert request.url.params["sort_mth"] == "asc"
        return httpx.Response(
            200,
            json={"status": "000", "total_page": 1, "list": provider_items},
        )

    with _make_client(handler) as client:
        pages = list(client.iter_disclosure_pages(date(2026, 5, 28)))

    assert pages == [tuple(provider_items)]


@pytest.mark.parametrize("page_count", [0, 101])
def test_list_disclosure_pages_rejects_provider_invalid_page_size(page_count):
    with _make_client(lambda _request: pytest.fail("request must not run")) as client:
        with pytest.raises(ValueError, match="between 1 and 100"):
            list(
                client.iter_disclosure_pages(
                    date(2026, 5, 28),
                    page_count=page_count,
                )
            )


def test_list_disclosures_no_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "013", "message": "조회된 데이타가 없습니다."}
        )

    with _make_client(handler) as c:
        records = list(c.list_disclosures(date(2026, 5, 28)))
    assert records == []


def test_list_disclosures_raises_on_bad_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "020", "message": "등록되지 않은 키입니다."}
        )

    with _make_client(handler) as c:
        with pytest.raises(DartApiError) as excinfo:
            list(c.list_disclosures(date(2026, 5, 28)))
    assert excinfo.value.status == "020"


def test_parse_report_relations_html():
    html = """
    <select id="family">
      <option value="null">select</option>
      <option value="rcpNo=20260826900745">correction</option>
      <option value="rcpNo=20220110900219">original</option>
    </select>
    <select id="ref">
      <option value="rcpNo=20260826900745&amp;dcmNo=123">current</option>
      <option value="rcpNo=20260326000528">withdrawal</option>
    </select>
    """

    result = parse_report_relations_html(html, "20260826900745")

    assert result.family_receipt_nos == (
        "20260826900745",
        "20220110900219",
    )
    assert result.related_receipt_nos == (
        "20260826900745",
        "20260326000528",
    )
    assert len(result.raw_html_sha256 or "") == 64
    assert result.attachment_receipt_nos == ()


def test_parse_captured_attachment_correction_bridges_att_to_parent_family():
    html = (FIXTURES / "dart_attachment_correction_20260713000496.html").read_text(
        encoding="utf-8"
    )

    result = parse_report_relations_html(html, "20260713000496")

    assert result.family_receipt_nos == (
        "20260713000482",
        "20260706100007",
        "20260626000007",
        "20260513000801",
    )
    assert result.attachment_receipt_nos == (
        "20260713000496",
        "20260514001047",
        "20260513000801",
    )
    assert result.related_receipt_nos == (
        "20260713000482",
        "20260706100007",
    )
    assert result.source_url.endswith("rcpNo=20260713000496")
    assert result.raw_html_sha256 == hashlib.sha256(html.encode("utf-8")).hexdigest()


def test_fetch_report_relations_uses_dart_viewer():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "dart.fss.or.kr"
        assert request.url.path == "/dsaf001/main.do"
        assert request.url.params["rcpNo"] == "20260826900745"
        return httpx.Response(
            200,
            text=(
                '<select id="family">'
                '<option value="rcpNo=20260826900745">update</option>'
                '<option value="rcpNo=20220110900219">original</option>'
                "</select>"
            ),
        )

    with _make_client(handler) as client:
        result = client.fetch_report_relations("20260826900745")

    assert result.family_receipt_nos[-1] == "20220110900219"
    assert result.source_url == (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260826900745"
    )
    assert len(result.raw_html_sha256 or "") == 64


def test_fetch_report_relations_capture_retains_exact_response_bytes():
    raw_content = (
        b'<select id="family">'
        b'<option value="rcpNo=20260826900745">update</option>'
        b'<option value="rcpNo=20220110900219">original</option>'
        b"</select>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw_content, request=request)

    with _make_client(handler) as client:
        capture = client.fetch_report_relations_capture("20260826900745")

    assert capture.raw_content == raw_content
    assert capture.relations.raw_html_sha256 == hashlib.sha256(raw_content).hexdigest()


def test_parse_report_relations_rejects_non_viewer_response():
    with pytest.raises(ValueError, match="did not expose relationships"):
        parse_report_relations_html("<html>temporary error</html>", "20260826900745")
