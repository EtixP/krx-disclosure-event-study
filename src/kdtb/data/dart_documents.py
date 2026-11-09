"""Pure decoding and lineage parsing of caller-supplied DART document bytes."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from html.parser import HTMLParser

from kdtb.schemas.economic_event import DartReportRelations

DART_VIEWER_BASE_URL = "https://dart.fss.or.kr"


def extract_text_from_document_zip(zip_bytes: bytes) -> str:
    """Decode the DART document ZIP into plain readable text."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = z.namelist()
        if not names:
            return ""
        raw = z.read(names[0])
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            html = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        html = raw.decode("utf-8", errors="replace")
    no_style = re.sub(
        r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE
    )
    no_script = re.sub(
        r"<script[^>]*>.*?</script>", " ", no_style, flags=re.DOTALL | re.IGNORECASE
    )
    stripped = re.sub(r"<[^>]+>", " ", no_script)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed


def decode_report_relations_html(raw_content: bytes) -> str:
    """Decode viewer bytes deterministically for lineage parsing and replay."""

    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw_content.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Relationship selectors and receipt numbers are ASCII. A one-byte fallback
    # preserves that markup without inventing or dropping undecodable bytes.
    return raw_content.decode("latin-1")


class _ReportRelationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active_select: str | None = None
        self.family: list[str] = []
        self.attachments: list[str] = []
        self.related: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select":
            select_id = attributes.get("id")
            self.active_select = (
                select_id if select_id in {"family", "att", "ref"} else None
            )
            return
        if tag != "option" or self.active_select is None:
            return
        value = attributes.get("value") or ""
        match = re.search(r"(?:^|[?&])rcpNo=(\d{14})(?:&|$)", value)
        if match is None:
            return
        if self.active_select == "family":
            target = self.family
        elif self.active_select == "att":
            target = self.attachments
        else:
            target = self.related
        receipt_no = match.group(1)
        if receipt_no not in target:
            target.append(receipt_no)

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.active_select = None


def parse_report_relations_html(
    html: str,
    rcept_no: str,
    *,
    source_url: str | None = None,
    raw_html_sha256: str | None = None,
) -> DartReportRelations:
    """Parse exact DART lineage from captured or freshly retrieved viewer HTML."""

    parser = _ReportRelationParser()
    parser.feed(html)
    resolved_source_url = source_url or (
        f"{DART_VIEWER_BASE_URL}/dsaf001/main.do?rcpNo={rcept_no}"
    )
    try:
        return DartReportRelations(
            receipt_no=rcept_no,
            family_receipt_nos=tuple(parser.family),
            attachment_receipt_nos=tuple(parser.attachments),
            related_receipt_nos=tuple(parser.related),
            source_url=resolved_source_url,
            raw_html_sha256=(
                raw_html_sha256
                if raw_html_sha256 is not None
                else hashlib.sha256(html.encode("utf-8")).hexdigest()
            ),
        )
    except ValueError as exc:
        raise ValueError(
            f"DART viewer response did not expose relationships for {rcept_no}"
        ) from exc
