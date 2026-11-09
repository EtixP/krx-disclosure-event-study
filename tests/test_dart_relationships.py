from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kdtb.data.dart_documents import parse_report_relations_html

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_parse_report_relations_rejects_non_viewer_response():
    with pytest.raises(ValueError, match="did not expose relationships"):
        parse_report_relations_html("<html>temporary error</html>", "20260826900745")
