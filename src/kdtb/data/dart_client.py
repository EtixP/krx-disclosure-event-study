from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from datetime import date
from typing import Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

DART_BASE_URL = "https://opendart.fss.or.kr/api"

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
    ) -> None:
        if not api_key:
            raise ValueError("DART API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
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
                sleep = self.retry_backoff * (2 ** attempt)
                logger.warning(
                    "DART %s retry %d/%d after %.1fs: %s",
                    endpoint, attempt + 1, self.max_retries, sleep, e,
                )
                time.sleep(sleep)
        raise RuntimeError(f"DART {endpoint} failed after {self.max_retries} retries") from last_exc

    def list_disclosures(
        self,
        target_date: date,
        corp_cls: Optional[str] = None,
        page_count: int = 100,
    ) -> Iterator[dict]:
        """Yields raw disclosure records for the given date (DART field names preserved).

        corp_cls: Y=KOSPI, K=KOSDAQ, N=KONEX, E=other. None = all.
        """
        date_str = target_date.strftime("%Y%m%d")
        page_no = 1
        while True:
            params: dict = {
                "crtfc_key": self.api_key,
                "bgn_de": date_str,
                "end_de": date_str,
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
            for item in data.get("list", []):
                yield item
            total_page = int(data.get("total_page", 1))
            if page_no >= total_page:
                return
            page_no += 1

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
    no_style = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    no_script = re.sub(r"<script[^>]*>.*?</script>", " ", no_style, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<[^>]+>", " ", no_script)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed
