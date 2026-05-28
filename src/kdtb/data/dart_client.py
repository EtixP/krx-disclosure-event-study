from __future__ import annotations

from datetime import date


class DartClient:
    """Placeholder for OPEN DART API client (Milestone 2)."""

    def __init__(self, api_key: str, base_url: str = "https://opendart.fss.or.kr/api") -> None:
        if not api_key:
            raise ValueError("DART API key is required")
        self.api_key = api_key
        self.base_url = base_url

    def list_disclosures(self, target_date: date, market: str | None = None) -> list[dict]:
        raise NotImplementedError("Implemented in Milestone 2")

    def fetch_document(self, receipt_no: str) -> dict:
        raise NotImplementedError("Implemented in Milestone 2")
