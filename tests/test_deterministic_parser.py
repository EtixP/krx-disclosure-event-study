from __future__ import annotations

from kdtb.interpretation.deterministic_parser import parse_supply_contract, PARSER_VERSION


VOLUNTARY_DISCLOSURE_TEXT = """
아이티센엔텍/단일판매ㆍ공급계약체결(자율공시)/(2026.05.27)단일판매ㆍ공급계약체결(자율공시)
단일판매ㆍ공급계약 체결(자율공시) 1. 판매ㆍ공급계약 내용 2026년 정보시스템 통합유지관리 용역사업
2. 계약내역 조건부 계약여부 미해당 확정 계약금액 47,945,884,091 조건부 계약금액 -
계약금액 총액(원) 47,945,884,091 최근 매출액(원) 572,026,600,945 매출액 대비(%) 8.38
3. 계약상대방 국민건강보험공단 - 최근 매출액(원) 131,914,169,762,370 - 주요사업
"""

MANDATORY_DISCLOSURE_TEXT = """
삼성중공업/단일판매ㆍ공급계약체결/(2026.05.27)
1. 계약명 유조선 2척 2. 계약내역 계약금액(원) 278,400,000,000 최근매출액(원) 10,650,000,000,000
매출액대비(%) 2.6 대규모법인여부 해당 3. 계약상대 버뮤다 지역 선주
"""

REVISION_DISCLOSURE_TEXT = """
[기재정정]단일판매ㆍ공급계약체결 1. 정정사유 계약금액 변경
계약금액 총액(원) 1,000,000,000 최근 매출액(원) 100,000,000,000 매출액 대비(%) 1.0
"""


def test_parses_voluntary_form():
    ext = parse_supply_contract(VOLUNTARY_DISCLOSURE_TEXT, disclosure_id=1, report_name="단일판매ㆍ공급계약체결(자율공시)")
    assert ext.validation_status == "ok"
    assert ext.contract_value_krw == 47_945_884_091
    assert ext.prior_year_revenue_krw == 572_026_600_945
    assert abs(ext.contract_to_revenue_ratio - 0.0838) < 0.001
    assert ext.is_new_contract is True
    assert ext.is_revision is False
    assert ext.is_cancellation is False
    assert ext.event_type == "major_supply_contract"
    assert ext.model_name == PARSER_VERSION


def test_parses_mandatory_form():
    ext = parse_supply_contract(MANDATORY_DISCLOSURE_TEXT, disclosure_id=2, report_name="단일판매ㆍ공급계약체결")
    assert ext.validation_status == "ok"
    assert ext.contract_value_krw == 278_400_000_000
    assert ext.prior_year_revenue_krw == 10_650_000_000_000
    assert abs(ext.contract_to_revenue_ratio - 0.026) < 0.001
    assert ext.event_type == "major_supply_contract"


def test_first_match_wins_for_revenue():
    """Counterparty revenue (131 trillion) appears AFTER company revenue (572 billion).
    Make sure we extract the company's, not the counterparty's."""
    ext = parse_supply_contract(VOLUNTARY_DISCLOSURE_TEXT, disclosure_id=1)
    assert ext.prior_year_revenue_krw == 572_026_600_945
    assert ext.prior_year_revenue_krw != 131_914_169_762_370


def test_revision_marked_correctly():
    ext = parse_supply_contract(REVISION_DISCLOSURE_TEXT, disclosure_id=3, report_name="[기재정정]단일판매ㆍ공급계약체결")
    assert ext.is_revision is True
    assert ext.event_type == "contract_revision"
    assert ext.is_new_contract is False


def test_returns_blocked_when_no_value():
    """No contract value anywhere → blocked status."""
    ext = parse_supply_contract("그냥 다른 공시 내용입니다.", disclosure_id=4, report_name="단일판매")
    assert ext.validation_status == "blocked"
    assert ext.contract_value_krw is None
    assert "missing_contract_value" in ext.red_flags


def test_computes_ratio_when_only_value_and_revenue_present():
    text = "계약금액 총액(원) 5,000,000,000 최근 매출액(원) 100,000,000,000"
    ext = parse_supply_contract(text, disclosure_id=5, report_name="단일판매")
    assert ext.contract_value_krw == 5_000_000_000
    assert ext.prior_year_revenue_krw == 100_000_000_000
    assert abs(ext.contract_to_revenue_ratio - 0.05) < 1e-6


def test_flags_inconsistent_ratio():
    """Reported ratio doesn't match computed."""
    text = "계약금액 총액(원) 1,000,000,000 최근 매출액(원) 100,000,000,000 매출액 대비(%) 50.0"
    ext = parse_supply_contract(text, disclosure_id=6, report_name="단일판매")
    assert "ratio_inconsistent" in ext.red_flags
    assert ext.validation_status == "needs_manual_review"
