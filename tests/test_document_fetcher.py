from __future__ import annotations

import io
import zipfile

from kdtb.data.dart_client import extract_text_from_document_zip


def _make_zip(name: str, content: str, encoding: str = "utf-8") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, content.encode(encoding))
    return buf.getvalue()


def test_extracts_text_from_utf8_html():
    html = "<html><body><p>안녕 <b>세계</b></p></body></html>"
    out = extract_text_from_document_zip(_make_zip("doc.xml", html))
    assert "안녕" in out
    assert "세계" in out
    assert "<b>" not in out


def test_strips_style_and_script_blocks():
    html = """<html><head><style>p { color: red; }</style>
    <script>alert(1)</script></head><body>real content</body></html>"""
    out = extract_text_from_document_zip(_make_zip("doc.xml", html))
    assert "real content" in out
    assert "alert" not in out
    assert "color: red" not in out


def test_handles_cp949_fallback():
    html = "<html><body>한국어 인코딩</body></html>"
    out = extract_text_from_document_zip(_make_zip("doc.xml", html, encoding="cp949"))
    assert "한국어" in out


def test_collapses_whitespace():
    html = "<p>a</p>\n\n\n<p>b</p>\t\t<p>c</p>"
    out = extract_text_from_document_zip(_make_zip("doc.xml", html))
    assert out == "a b c"


def test_empty_zip_returns_empty():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        pass
    assert extract_text_from_document_zip(buf.getvalue()) == ""
