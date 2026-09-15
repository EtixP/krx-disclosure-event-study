from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from kdtb.schemas.disclosure import Market

EventAction = Literal[
    "original",
    "correction",
    "amendment",
    "correction_required",
    "completion",
    "cancellation",
]
EventStatus = Literal[
    "active",
    "amended",
    "correction_required",
    "completed",
    "cancelled",
]
LineageStatus = Literal["self_contained", "complete", "partial", "unresolved"]


def _validate_receipt_no(value: str) -> str:
    if len(value) != 14 or not value.isdigit():
        raise ValueError("DART receipt numbers must contain exactly 14 digits")
    return value


def _require_string_mapping_keys(value: object) -> object:
    """Keep arbitrary field values inside JSON's unambiguous object-key domain."""

    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("normalized field mapping keys must be strings")
            _require_string_mapping_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _require_string_mapping_keys(nested)
    return value


class DartReportRelations(BaseModel):
    """Authoritative relationships parsed from a DART report-viewer page.

    ``family_receipt_nos`` is the viewer's revision-family selector. The
    ``attachment_receipt_nos`` field captures its ``att`` selector, which can
    contain the requested receipt while ``family`` contains the parent report
    chain. The broader ``related_receipt_nos`` list is used conservatively only
    for cancellation/completion documents by the normalizer.
    """

    receipt_no: str
    family_receipt_nos: tuple[str, ...] = ()
    attachment_receipt_nos: tuple[str, ...] = ()
    related_receipt_nos: tuple[str, ...] = ()
    source_url: str
    raw_html_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _receipt_no = field_validator("receipt_no")(_validate_receipt_no)

    @field_validator(
        "family_receipt_nos", "attachment_receipt_nos", "related_receipt_nos"
    )
    @classmethod
    def validate_receipt_nos(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            value = _validate_receipt_no(value)
            if value not in result:
                result.append(value)
        return tuple(result)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "dart.fss.or.kr"
            or parsed.path != "/dsaf001/main.do"
        ):
            raise ValueError("relationship source must be a DART report-viewer URL")
        receipt_values = parse_qs(parsed.query).get("rcpNo", [])
        if len(receipt_values) != 1:
            raise ValueError("relationship source URL must identify one DART receipt")
        _validate_receipt_no(receipt_values[0])
        return value

    @model_validator(mode="after")
    def current_receipt_is_present(self) -> "DartReportRelations":
        source_receipt = parse_qs(urlparse(self.source_url).query)["rcpNo"][0]
        if source_receipt != self.receipt_no:
            raise ValueError("relationship source URL receipt does not match response")
        if self.receipt_no not in {
            *self.family_receipt_nos,
            *self.attachment_receipt_nos,
            *self.related_receipt_nos,
        }:
            raise ValueError(
                "DART relationship response does not contain the requested receipt"
            )
        return self


class EventIssuer(BaseModel):
    corp_code: str
    corp_name: str
    stock_code: str | None = None


class EventSourceProvenance(BaseModel):
    receipt_no: str
    report_name: str
    receipt_timestamp: datetime
    source: str
    raw_url: str | None = None
    action: EventAction
    relationship: Literal[
        "primary",
        "dart_family",
        "dart_attachment",
        "dart_related",
        "unresolved_update",
    ]
    relationship_source_receipt_no: str | None = None
    relationship_source_url: str | None = None
    relationship_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    raw_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dart_remarks: str | None = None

    _receipt_no = field_validator("receipt_no")(_validate_receipt_no)

    @field_validator("relationship_source_receipt_no")
    @classmethod
    def validate_optional_receipt_no(cls, value: str | None) -> str | None:
        return _validate_receipt_no(value) if value is not None else None

    @field_validator("relationship_source_url")
    @classmethod
    def validate_optional_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "dart.fss.or.kr"
            or parsed.path != "/dsaf001/main.do"
        ):
            raise ValueError("relationship evidence must use a DART viewer URL")
        return value

    @model_validator(mode="after")
    def validate_relationship_evidence(self) -> "EventSourceProvenance":
        evidence = (
            self.relationship_source_receipt_no,
            self.relationship_source_url,
            self.relationship_evidence_sha256,
        )
        if self.relationship in {"dart_family", "dart_attachment", "dart_related"}:
            if any(value is None for value in evidence):
                raise ValueError(
                    "linked provenance requires complete relationship evidence"
                )
            source_receipt = parse_qs(
                urlparse(self.relationship_source_url or "").query
            ).get("rcpNo", [])
            if source_receipt != [self.relationship_source_receipt_no]:
                raise ValueError("relationship evidence URL and receipt must agree")
        elif any(value is not None for value in evidence):
            raise ValueError("unlinked provenance cannot claim relationship evidence")
        return self


class EconomicEvent(BaseModel):
    """One economic event with all source filings retained as provenance."""

    economic_event_id: str
    event_type: str
    issuer: EventIssuer
    market: Market
    primary_receipt_no: str
    related_receipt_nos: tuple[str, ...] = ()
    original_timestamp: datetime
    latest_update_timestamp: datetime
    status: EventStatus
    normalized_fields: dict[str, Any] = Field(default_factory=dict)
    source_provenance: tuple[EventSourceProvenance, ...]
    lineage_status: LineageStatus
    normalization_version: str = "m1.1-v1"

    _primary_receipt_no = field_validator("primary_receipt_no")(_validate_receipt_no)

    @field_validator("normalized_fields", mode="before")
    @classmethod
    def validate_normalized_field_keys(cls, value: object) -> object:
        return _require_string_mapping_keys(value)

    @field_validator("related_receipt_nos")
    @classmethod
    def validate_related_receipt_nos(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            value = _validate_receipt_no(value)
            if value not in result:
                result.append(value)
        return tuple(result)

    @model_validator(mode="after")
    def validate_event_invariants(self) -> "EconomicEvent":
        if self.economic_event_id != f"dart:{self.primary_receipt_no}":
            raise ValueError(
                "economic_event_id must derive from the authoritative primary receipt"
            )
        if self.primary_receipt_no in self.related_receipt_nos:
            raise ValueError("primary receipt cannot also be a related receipt")
        if self.original_timestamp > self.latest_update_timestamp:
            raise ValueError("original_timestamp cannot follow latest_update_timestamp")
        receipts = [source.receipt_no for source in self.source_provenance]
        if len(receipts) != len(set(receipts)):
            raise ValueError("source provenance receipt numbers must be unique")
        event_receipts = {self.primary_receipt_no, *self.related_receipt_nos}
        if not set(receipts).issubset(event_receipts):
            raise ValueError("source provenance must belong to the event receipt set")
        if self.lineage_status == "complete":
            if set(receipts) != event_receipts:
                raise ValueError(
                    "complete lineage requires provenance for every receipt"
                )
            if any(
                source.relationship == "unresolved_update"
                for source in self.source_provenance
            ):
                raise ValueError(
                    "complete lineage cannot contain unresolved provenance"
                )
            if not any(
                source.relationship
                in {"dart_family", "dart_attachment", "dart_related"}
                for source in self.source_provenance
            ):
                raise ValueError(
                    "complete lineage requires linked relationship evidence"
                )
        return self


class CanonicalizationResult(BaseModel):
    events: tuple[EconomicEvent, ...]
    receipt_to_economic_event_id: dict[str, str]
    unresolved_update_receipt_nos: tuple[str, ...] = ()
