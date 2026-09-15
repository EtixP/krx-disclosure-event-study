from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kdtb.data.dart_client import parse_report_relations_html
from kdtb.events.normalizer import (
    classify_event_action,
    normalize_disclosures,
    project_legacy_event_study_rows,
)
from kdtb.schemas.disclosure import Disclosure
from kdtb.schemas.economic_event import DartReportRelations


FIXTURES = Path(__file__).parent / "fixtures"


def _disclosure(
    receipt_no: str,
    report_name: str,
    *,
    corp_code: str = "00126380",
    corp_name: str = "테스트회사",
    stock_code: str | None = "005930",
    receipt_datetime: datetime | None = None,
) -> Disclosure:
    return Disclosure(
        receipt_no=receipt_no,
        corp_code=corp_code,
        corp_name=corp_name,
        stock_code=stock_code,
        report_name=report_name,
        receipt_datetime=(
            receipt_datetime or datetime.strptime(receipt_no[:8], "%Y%m%d")
        ),
        market="KOSPI",
        raw_payload={"rcept_no": receipt_no, "rm": ""},
    )


def _relations(
    receipt_no: str,
    *,
    family: tuple[str, ...] = (),
    attachments: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    source_url: str | None = None,
    raw_html_sha256: str | None = None,
) -> DartReportRelations:
    return DartReportRelations(
        receipt_no=receipt_no,
        family_receipt_nos=family,
        attachment_receipt_nos=attachments,
        related_receipt_nos=related,
        source_url=(
            source_url or f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}"
        ),
        raw_html_sha256=raw_html_sha256 or "f" * 64,
    )


def test_correction_uses_dart_family_not_nearest_same_title():
    true_original = _disclosure("20220110900219", "단일판매ㆍ공급계약체결")
    unrelated_later_filing = _disclosure("20251208900227", "단일판매ㆍ공급계약체결")
    correction = _disclosure("20260826900745", "[기재정정]단일판매ㆍ공급계약체결")
    relation = _relations(
        correction.receipt_no,
        family=(true_original.receipt_no, correction.receipt_no),
        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={correction.receipt_no}",
        raw_html_sha256="a" * 64,
    )

    result = normalize_disclosures(
        [correction, unrelated_later_filing, true_original],
        relationships=[relation],
    )

    assert len(result.events) == 2
    assert result.receipt_to_economic_event_id[correction.receipt_no] == (
        "dart:20220110900219"
    )
    assert result.receipt_to_economic_event_id[unrelated_later_filing.receipt_no] == (
        "dart:20251208900227"
    )
    event = next(event for event in result.events if event.status == "amended")
    assert event.primary_receipt_no == true_original.receipt_no
    assert event.lineage_status == "complete"
    assert event.source_provenance[-1].relationship_source_receipt_no == (
        correction.receipt_no
    )
    assert event.source_provenance[-1].relationship_evidence_sha256 == "a" * 64


def test_missing_relationship_does_not_guess_correction_lineage():
    original = _disclosure("20250102000001", "단일판매ㆍ공급계약체결")
    correction = _disclosure("20250103000002", "[기재정정]단일판매ㆍ공급계약체결")

    result = normalize_disclosures([original, correction])

    assert len(result.events) == 2
    assert result.receipt_to_economic_event_id[original.receipt_no] != (
        result.receipt_to_economic_event_id[correction.receipt_no]
    )
    assert result.unresolved_update_receipt_nos == (correction.receipt_no,)
    correction_event = next(
        event
        for event in result.events
        if event.primary_receipt_no == correction.receipt_no
    )
    assert correction_event.lineage_status == "unresolved"


def test_attachment_amendment_merges_fields_and_retains_source_hashes():
    original = _disclosure("20250102000001", "주요사항보고서(유상증자결정)")
    amendment = _disclosure(
        "20250103000002",
        "[첨부추가]주요사항보고서(유상증자결정)",
        stock_code=None,
    )
    relation = _relations(
        amendment.receipt_no,
        family=(amendment.receipt_no, original.receipt_no),
    )

    result = normalize_disclosures(
        [original, amendment],
        relationships=[relation],
        normalized_fields_by_receipt={
            original.receipt_no: {"shares": 100, "price": 1_000},
            amendment.receipt_no: {"price": 900},
        },
    )
    event = result.events[0]

    assert classify_event_action(amendment.report_name) == "amendment"
    assert event.event_type == "rights_offering"
    assert event.status == "amended"
    assert event.issuer.stock_code == "005930"
    assert event.normalized_fields == {"shares": 100, "price": 900}
    assert [source.relationship for source in event.source_provenance] == [
        "primary",
        "dart_family",
    ]
    assert all(
        len(source.raw_payload_sha256) == 64 for source in event.source_provenance
    )


def test_captured_attachment_correction_traces_parent_original():
    relation = parse_report_relations_html(
        (FIXTURES / "dart_attachment_correction_20260713000496.html").read_text(
            encoding="utf-8"
        ),
        "20260713000496",
    )
    original = _disclosure("20260513000801", "주요사항보고서(회사합병결정)")
    first_attachment = _disclosure(
        "20260514001047", "[첨부정정]주요사항보고서(회사합병결정)"
    )
    parent_correction = _disclosure(
        "20260626000007", "[기재정정]주요사항보고서(회사합병결정)"
    )
    current_attachment = _disclosure(
        "20260713000496",
        "[첨부정정]주요사항보고서(회사합병결정)",
        receipt_datetime=datetime(2026, 7, 14),
    )

    result = normalize_disclosures(
        [current_attachment, original, first_attachment, parent_correction],
        relationships=[relation],
    )
    event = result.events[0]

    assert event.primary_receipt_no == original.receipt_no
    assert result.receipt_to_economic_event_id[current_attachment.receipt_no] == (
        f"dart:{original.receipt_no}"
    )
    assert event.latest_update_timestamp == datetime(2026, 7, 14)
    assert event.lineage_status == "partial"
    current_source = next(
        source
        for source in event.source_provenance
        if source.receipt_no == current_attachment.receipt_no
    )
    assert current_source.relationship == "dart_attachment"
    assert current_source.relationship_source_url == relation.source_url
    assert current_source.relationship_evidence_sha256 == relation.raw_html_sha256


def test_relationship_evidence_is_required_before_normalization():
    receipt_no = "20250103000002"
    with pytest.raises(ValidationError):
        DartReportRelations(
            receipt_no=receipt_no,
            family_receipt_nos=(receipt_no, "20250102000001"),
        )

    unevidenced = DartReportRelations.model_construct(
        receipt_no=receipt_no,
        family_receipt_nos=(receipt_no, "20250102000001"),
        attachment_receipt_nos=(),
        related_receipt_nos=(),
        source_url=None,
        raw_html_sha256=None,
    )
    with pytest.raises(ValueError, match="relationship evidence is required"):
        normalize_disclosures(
            [
                _disclosure("20250102000001", "단일판매ㆍ공급계약체결"),
                _disclosure(receipt_no, "[기재정정]단일판매ㆍ공급계약체결"),
            ],
            relationships=[unevidenced],
        )

    valid_relation = _relations(
        receipt_no,
        family=(receipt_no, "20250102000001"),
    )
    event = normalize_disclosures(
        [
            _disclosure("20250102000001", "단일판매ㆍ공급계약체결"),
            _disclosure(receipt_no, "[기재정정]단일판매ㆍ공급계약체결"),
        ],
        relationships=[valid_relation],
    ).events[0]
    invalid_event = event.model_dump()
    invalid_event["source_provenance"][1]["relationship_source_url"] = None
    with pytest.raises(ValidationError, match="complete relationship evidence"):
        event.__class__.model_validate(invalid_event)


def test_cancellation_follows_related_document_then_revision_family():
    original = _disclosure("20260209000679", "증권신고서(지분증권)")
    correction = _disclosure("20260224003990", "[기재정정]증권신고서(지분증권)")
    withdrawal = _disclosure("20260326000528", "철회신고서")
    relations = [
        _relations(
            correction.receipt_no,
            family=(original.receipt_no, correction.receipt_no),
        ),
        _relations(
            withdrawal.receipt_no,
            family=(withdrawal.receipt_no,),
            related=(withdrawal.receipt_no, correction.receipt_no),
        ),
    ]

    event = normalize_disclosures(
        [withdrawal, correction, original], relationships=relations
    ).events[0]

    assert event.primary_receipt_no == original.receipt_no
    assert event.related_receipt_nos == (correction.receipt_no, withdrawal.receipt_no)
    assert event.status == "cancelled"
    assert event.lineage_status == "complete"
    assert event.source_provenance[-1].relationship == "dart_related"


def test_historical_batch_and_recomputed_live_snapshot_are_identical():
    original = _disclosure("20250102000001", "단일판매ㆍ공급계약체결")
    correction = _disclosure("20250103000002", "[기재정정]단일판매ㆍ공급계약체결")
    cancellation = _disclosure("20250104000003", "단일판매ㆍ공급계약해지")
    relationships = [
        _relations(
            correction.receipt_no,
            family=(original.receipt_no, correction.receipt_no),
        ),
        _relations(
            cancellation.receipt_no,
            family=(cancellation.receipt_no,),
            related=(
                original.receipt_no,
                correction.receipt_no,
                cancellation.receipt_no,
            ),
        ),
    ]

    batch = normalize_disclosures(
        [original, correction, cancellation], relationships=relationships
    )
    observed: list[Disclosure] = []
    live_snapshot = None
    for disclosure in (original, correction, cancellation):
        observed.append(disclosure)
        live_snapshot = normalize_disclosures(
            observed,
            relationships=[
                relation
                for relation in relationships
                if relation.receipt_no in {item.receipt_no for item in observed}
            ],
        )

    assert live_snapshot == batch


def test_as_of_does_not_leak_future_viewer_family_members():
    original = _disclosure("20250102000001", "단일판매ㆍ공급계약체결")
    future_correction = _disclosure(
        "20250103000002", "[기재정정]단일판매ㆍ공급계약체결"
    )
    relation_as_seen_later = _relations(
        original.receipt_no,
        family=(future_correction.receipt_no, original.receipt_no),
    )

    result = normalize_disclosures(
        [original, future_correction],
        relationships=[relation_as_seen_later],
        as_of=datetime(2025, 1, 2, 23, 59),
    )

    assert len(result.events) == 1
    assert result.events[0].related_receipt_nos == ()
    assert result.events[0].status == "active"


def test_later_state_dart_remarks_do_not_change_decision_time_status():
    original = _disclosure("20250102000001", "증권신고서(지분증권)")
    original.raw_payload["rm"] = "정철"

    event = normalize_disclosures([original]).events[0]

    assert event.status == "active"
    assert event.source_provenance[0].dart_remarks == "정철"


def test_completion_document_closes_related_event():
    original = _disclosure("20250102000001", "자기주식취득결정")
    completion = _disclosure("20250110000002", "자기주식취득결과보고서")
    relation = _relations(
        completion.receipt_no,
        family=(completion.receipt_no,),
        related=(original.receipt_no, completion.receipt_no),
    )

    event = normalize_disclosures(
        [completion, original], relationships=[relation]
    ).events[0]

    assert event.primary_receipt_no == original.receipt_no
    assert event.status == "completed"
    assert event.lineage_status == "complete"


def test_legacy_projection_preserves_receipt_rows_and_fixed_width_codes():
    original = _disclosure("20250102000001", "단일판매ㆍ공급계약체결")
    correction = _disclosure("20250103000002", "[기재정정]단일판매ㆍ공급계약체결")
    result = normalize_disclosures([original, correction])

    rows = project_legacy_event_study_rows([original, correction], result)

    assert len(rows) == 2
    assert [row["receipt_no"] for row in rows] == [
        original.receipt_no,
        correction.receipt_no,
    ]
    assert all(row["corp_code"] == "00126380" for row in rows)
    assert all(row["stock_code"] == "005930" for row in rows)
    assert "economic_event_id" not in rows[0]


def test_relationships_cannot_connect_different_issuers():
    first = _disclosure("20250102000001", "철회신고서", corp_code="00000001")
    second = _disclosure("20250101000001", "증권신고서", corp_code="00000002")
    relation = _relations(
        first.receipt_no,
        family=(first.receipt_no,),
        related=(first.receipt_no, second.receipt_no),
    )

    try:
        normalize_disclosures([first, second], relationships=[relation])
    except ValueError as exc:
        assert "different issuers" in str(exc)
    else:
        raise AssertionError("cross-issuer DART relationship must fail")
