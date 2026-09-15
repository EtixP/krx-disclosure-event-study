from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Iterator, Optional

import httpx

from kdtb.schemas.economic_event import DartReportRelations

logger = logging.getLogger(__name__)

DART_BASE_URL = "https://opendart.fss.or.kr/api"
DART_VIEWER_BASE_URL = "https://dart.fss.or.kr"

CORP_CLS_TO_MARKET = {
    "Y": "KOSPI",
    "K": "KOSDAQ",
    "N": "KONEX",
    "E": "OTHER",
}


class DartApiError(RuntimeError):
    """Non-success status returned by OPEN DART (e.g. invalid key, rate limit)."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(f"DART API status={status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class DartRelationshipCapture:
    """Parsed viewer relationships plus the exact response bytes that prove them."""

    relations: DartReportRelations
    raw_content: bytes


class DartClient:
    """OPEN DART API client.

    Endpoints used:
    - list.json    — disclosure list by date
    - document.xml — full disclosure document (zipped XML)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DART_BASE_URL,
        timeout: float = 30.0,
        client: Optional[httpx.Client] = None,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        viewer_base_url: str = DART_VIEWER_BASE_URL,
    ) -> None:
        if not api_key:
            raise ValueError("DART API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.viewer_base_url = viewer_base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DartClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _get(self, endpoint: str, params: dict) -> httpx.Response:
        url = f"{self.base_url}/{endpoint}"
        return self._get_url(url, params, endpoint)

    def _get_url(self, url: str, params: dict, label: str) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                r = self._client.get(url, params=params)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"server {r.status_code}", request=r.request, response=r
                    )
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_exc = e
                sleep = self.retry_backoff * (2**attempt)
                logger.warning(
                    "DART %s retry %d/%d after %.1fs: %s",
                    label,
                    attempt + 1,
                    self.max_retries,
                    sleep,
                    e,
                )
                time.sleep(sleep)
        raise RuntimeError(
            f"DART {label} failed after {self.max_retries} retries"
        ) from last_exc

    def list_disclosures(
        self,
        target_date: date,
        corp_cls: Optional[str] = None,
        page_count: int = 100,
    ) -> Iterator[dict]:
        """Yields raw disclosure records for the given date (DART field names preserved).

        corp_cls: Y=KOSPI, K=KOSDAQ, N=KONEX, E=other. None = all.
        """

        for page in self.iter_disclosure_pages(
            target_date,
            corp_cls=corp_cls,
            page_count=page_count,
        ):
            yield from page

    def iter_disclosure_pages(
        self,
        target_date: date,
        corp_cls: Optional[str] = None,
        page_count: int = 100,
    ) -> Iterator[tuple[dict, ...]]:
        """Yield complete API pages so callers can persist each page atomically.

        OPEN DART documents a maximum of 100 records per page and supports
        ascending receipt-date sorting. It does not expose a receipt-number
        tie-breaker, so callers must not assume that ``rcept_no`` is monotonic
        within a date. Receipt-key idempotency makes repeated observations safe.
        """

        if not 1 <= page_count <= 100:
            raise ValueError("OPEN DART page_count must be between 1 and 100")
        if corp_cls not in {None, "Y", "K", "N", "E"}:
            raise ValueError("unsupported OPEN DART corp_cls")
        date_str = target_date.strftime("%Y%m%d")
        page_no = 1
        while True:
            params: dict = {
                "crtfc_key": self.api_key,
                "bgn_de": date_str,
                "end_de": date_str,
                # Explicitly retain originals and corrections. OPEN DART's
                # documented default is N, but normalization must not depend
                # on an implicit provider default.
                "last_reprt_at": "N",
                "sort": "date",
                "sort_mth": "asc",
                "page_no": page_no,
                "page_count": page_count,
            }
            if corp_cls:
                params["corp_cls"] = corp_cls
            r = self._get("list.json", params)
            r.raise_for_status()
            data = r.json()
            status = data.get("status")
            if status == "013":  # no results
                return
            if status != "000":
                raise DartApiError(status, data.get("message", ""))
            items = data.get("list", [])
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise ValueError("OPEN DART disclosure list returned malformed records")
            yield tuple(items)
            total_page = int(data.get("total_page", 1))
            if page_no >= total_page:
                return
            page_no += 1

    def fetch_report_relations(self, rcept_no: str) -> DartReportRelations:
        """Fetch authoritative family, attachment, and related receipt IDs.

        The list API labels updates but does not return their original receipt.
        DART's public report viewer exposes that lineage in its ``family``,
        ``att``, and ``ref`` selectors. Callers should cache the result beside
        raw source data before normalization.
        """

        return self.fetch_report_relations_capture(rcept_no).relations

    def fetch_report_relations_capture(self, rcept_no: str) -> DartRelationshipCapture:
        """Fetch relationships while retaining the exact bytes used as evidence."""

        if len(rcept_no) != 14 or not rcept_no.isdigit():
            raise ValueError("DART receipt numbers must contain exactly 14 digits")
        url = f"{self.viewer_base_url}/dsaf001/main.do"
        response = self._get_url(url, {"rcpNo": rcept_no}, "report relations")
        response.raise_for_status()
        raw_content = response.content
        relations = parse_report_relations_html(
            decode_report_relations_html(raw_content),
            rcept_no,
            source_url=str(response.request.url),
            raw_html_sha256=hashlib.sha256(raw_content).hexdigest(),
        )
        return DartRelationshipCapture(
            relations=relations,
            raw_content=raw_content,
        )

    def fetch_document(self, rcept_no: str) -> bytes:
        """Returns raw bytes of the disclosure document (ZIP containing HTML masquerading as XML)."""
        params = {"crtfc_key": self.api_key, "rcept_no": rcept_no}
        r = self._get("document.xml", params)
        r.raise_for_status()
        return r.content

    def fetch_document_text(self, rcept_no: str) -> str:
        """Fetches the document and returns plain text (HTML tags stripped, UTF-8 decoded).

        DART returns a ZIP containing an HTML file. Despite the endpoint name 'document.xml'
        and the inner file's .xml extension, the content is HTML. Encoding is UTF-8.
        """
        return extract_text_from_document_zip(self.fetch_document(rcept_no))


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
