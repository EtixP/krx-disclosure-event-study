from __future__ import annotations

from datetime import date

import httpx
import pytest

from kdtb.data.dart_client import DartApiError, DartClient


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
        1: [{"rcept_no": f"2026052800000{i}", "corp_code": "x", "corp_name": "x"} for i in range(1, 4)],
        2: [{"rcept_no": f"2026052800000{i}", "corp_code": "x", "corp_name": "x"} for i in range(4, 6)],
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


def test_list_disclosures_no_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})

    with _make_client(handler) as c:
        records = list(c.list_disclosures(date(2026, 5, 28)))
    assert records == []


def test_list_disclosures_raises_on_bad_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "020", "message": "등록되지 않은 키입니다."})

    with _make_client(handler) as c:
        with pytest.raises(DartApiError) as excinfo:
            list(c.list_disclosures(date(2026, 5, 28)))
    assert excinfo.value.status == "020"
