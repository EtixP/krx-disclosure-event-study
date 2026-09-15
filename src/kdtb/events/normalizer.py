from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping

from kdtb.schemas.disclosure import Disclosure
from kdtb.schemas.economic_event import (
    CanonicalizationResult,
    DartReportRelations,
    EconomicEvent,
    EventAction,
    EventIssuer,
    EventSourceProvenance,
    EventStatus,
)

# These prefixes and meanings come from the OPEN DART list API documentation.
# The exact prefix is retained in raw provenance; stripping is used only to
# classify the underlying event type.
DART_UPDATE_PREFIX_ACTIONS: dict[str, EventAction] = {
    "[기재정정]": "correction",
    "[첨부정정]": "correction",
    "[첨부추가]": "amendment",
    "[변경등록]": "amendment",
    "[연장결정]": "amendment",
    "[발행조건확정]": "amendment",
    "[정정명령부과]": "correction_required",
    "[정정제출요구]": "correction_required",
}

_CANCELLATION_MARKERS = ("철회", "취소", "해지")
_COMPLETION_MARKERS = (
    "완료보고서",
    "결과보고서",
    "발행실적보고서",
    "취득결과보고서",
    "처분결과보고서",
)


def strip_dart_update_prefixes(report_name: str) -> tuple[str, tuple[str, ...]]:
    """Return the underlying title and every recognized leading DART prefix."""

    title = report_name.strip()
    prefixes: list[str] = []
    while True:
        prefix = next(
            (
                candidate
                for candidate in DART_UPDATE_PREFIX_ACTIONS
                if title.startswith(candidate)
            ),
            None,
        )
        if prefix is None:
            break
        prefixes.append(prefix)
        title = title[len(prefix) :].strip()
    return title, tuple(prefixes)


def classify_event_action(report_name: str) -> EventAction:
    """Classify how a filing changes an event without using future DART flags."""

    title, prefixes = strip_dart_update_prefixes(report_name)
    if any(marker in title for marker in _CANCELLATION_MARKERS):
        return "cancellation"
    if any(marker in title for marker in _COMPLETION_MARKERS):
        return "completion"
    actions = [DART_UPDATE_PREFIX_ACTIONS[prefix] for prefix in prefixes]
    if "correction_required" in actions:
        return "correction_required"
    if "correction" in actions:
        return "correction"
    if "amendment" in actions:
        return "amendment"
    return "original"


def canonical_event_type(report_name: str) -> str:
    """Map a normalized DART title to the existing research domain vocabulary."""

    title, _ = strip_dart_update_prefixes(report_name)
    if title.startswith("단일판매"):
        return "major_supply_contract"
    if "자기주식" in title and "취득" in title and "처분" not in title:
        return "share_buyback"
    if "유상증자" in title:
        return "rights_offering"
    if "무상증자" in title:
        return "bonus_issue"
    if "전환사채" in title and "발행" in title:
        return "convertible_bond"
    if "매매거래정지" in title or "거래재개" in title:
        return "halt_resumption"
    if "최대주주" in title and "변경" in title:
        return "shareholder_change"
    return "other"


class _DisjointSet:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def add(self, value: str) -> None:
        self.parent.setdefault(value, value)

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        # Deterministic root selection makes output independent of input order.
        primary, secondary = sorted((left_root, right_root))
        self.parent[secondary] = primary


def _raw_payload_sha256(disclosure: Disclosure) -> str:
    encoded = json.dumps(
        disclosure.raw_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp_from_receipt_no(receipt_no: str) -> datetime:
    return datetime.strptime(receipt_no[:8], "%Y%m%d")


def _advance_status(status: EventStatus, action: EventAction) -> EventStatus:
    if status == "cancelled":
        return status
    if action == "cancellation":
        return "cancelled"
    if action == "completion":
        return "completed"
    if action == "correction_required":
        return "correction_required"
    if action in {"correction", "amendment"}:
        if status == "completed":
            return status
        return "amended"
    return status


def normalize_disclosures(
    disclosures: Iterable[Disclosure],
    *,
    relationships: Iterable[DartReportRelations] = (),
    normalized_fields_by_receipt: Mapping[str, Mapping[str, Any]] | None = None,
    as_of: datetime | None = None,
) -> CanonicalizationResult:
    """Canonicalize a historical batch or the disclosures observed by a live loop.

    This is deliberately one pure entry point for both modes. A future watcher
    should persist the raw filing first and call this function with the affected
    source component. Relationships newer than the source being processed are
    ignored, so a DART page fetched later cannot introduce a future filing into
    a historical replay.

    Revision-family and attachment links are authoritative. Broader DART
    ``related document`` links are followed only for explicit
    cancellation/completion documents. No same-title or nearest-date guess is
    made when relationship data is absent.
    """

    fields_by_receipt = normalized_fields_by_receipt or {}
    source_by_receipt: dict[str, Disclosure] = {}
    for disclosure in disclosures:
        if as_of is not None and disclosure.receipt_datetime > as_of:
            continue
        existing = source_by_receipt.get(disclosure.receipt_no)
        if existing is not None and existing.model_dump() != disclosure.model_dump():
            raise ValueError(
                f"conflicting disclosures for receipt {disclosure.receipt_no}"
            )
        source_by_receipt[disclosure.receipt_no] = disclosure

    relation_by_receipt: dict[str, DartReportRelations] = {}
    for relation in relationships:
        if not relation.source_url or not relation.raw_html_sha256:
            raise ValueError(
                f"DART relationship evidence is required for {relation.receipt_no}"
            )
        existing = relation_by_receipt.get(relation.receipt_no)
        if existing is not None and existing != relation:
            raise ValueError(
                f"conflicting DART relationships for {relation.receipt_no}"
            )
        relation_by_receipt[relation.receipt_no] = relation

    graph = _DisjointSet(source_by_receipt)
    family_nodes: set[str] = set()
    attachment_nodes: set[str] = set()
    related_nodes: set[str] = set()
    family_evidence: dict[str, set[str]] = defaultdict(set)
    attachment_evidence: dict[str, set[str]] = defaultdict(set)
    related_evidence: dict[str, set[str]] = defaultdict(set)
    relationship_used_for_update: set[str] = set()

    for receipt_no, disclosure in source_by_receipt.items():
        relation = relation_by_receipt.get(receipt_no)
        if relation is None:
            continue
        action = classify_event_action(disclosure.report_name)

        # Receipt numbers sort chronologically by their date/sequence. Filtering
        # here prevents a current DART page from leaking later family members.
        family = tuple(
            value for value in relation.family_receipt_nos if value <= receipt_no
        )
        for value in family:
            graph.union(receipt_no, value)
            family_evidence[value].add(receipt_no)
        if any(value != receipt_no for value in family):
            family_nodes.update(family)
            relationship_used_for_update.add(receipt_no)

        attachments = tuple(
            value for value in relation.attachment_receipt_nos if value <= receipt_no
        )
        for value in attachments:
            graph.union(receipt_no, value)
            attachment_evidence[value].add(receipt_no)
        attachment_nodes.update(attachments)
        if any(value != receipt_no for value in attachments):
            relationship_used_for_update.add(receipt_no)

        if action in {"cancellation", "completion"}:
            related = tuple(
                value for value in relation.related_receipt_nos if value <= receipt_no
            )
            for value in related:
                graph.union(receipt_no, value)
                related_nodes.add(value)
                related_evidence[value].add(receipt_no)
            if len(related) > 1:
                relationship_used_for_update.add(receipt_no)

    nodes_by_root: dict[str, set[str]] = defaultdict(set)
    for receipt_no in graph.parent:
        nodes_by_root[graph.find(receipt_no)].add(receipt_no)

    events: list[EconomicEvent] = []
    receipt_to_event: dict[str, str] = {}
    unresolved: list[str] = []

    for component_nodes in nodes_by_root.values():
        component_sources = sorted(
            (
                source_by_receipt[value]
                for value in component_nodes
                if value in source_by_receipt
            ),
            key=lambda source: (source.receipt_datetime, source.receipt_no),
        )
        if not component_sources:
            continue
        corp_codes = {source.corp_code for source in component_sources}
        if len(corp_codes) != 1:
            raise ValueError(
                "DART relationship connected different issuers: "
                + ", ".join(sorted(corp_codes))
            )

        primary_receipt_no = min(component_nodes)
        primary_source = source_by_receipt.get(primary_receipt_no)
        representative = primary_source or component_sources[0]
        latest_source = component_sources[-1]

        event_fields: dict[str, Any] = {}
        status: EventStatus = "active"
        provenance: list[EventSourceProvenance] = []
        component_has_update = False
        component_has_relationship = False

        for source in component_sources:
            action = classify_event_action(source.report_name)
            component_has_update |= action != "original"
            component_has_relationship |= (
                source.receipt_no in relationship_used_for_update
            )
            status = _advance_status(status, action)
            if source.receipt_no in fields_by_receipt:
                event_fields.update(dict(fields_by_receipt[source.receipt_no]))

            if source.receipt_no == primary_receipt_no:
                relationship = "primary"
                relationship_source_receipt_no = None
            elif source.receipt_no in family_nodes:
                relationship = "dart_family"
                relationship_source_receipt_no = min(family_evidence[source.receipt_no])
            elif source.receipt_no in attachment_nodes:
                relationship = "dart_attachment"
                relationship_source_receipt_no = min(
                    attachment_evidence[source.receipt_no]
                )
            elif source.receipt_no in related_nodes:
                relationship = "dart_related"
                relationship_source_receipt_no = min(
                    related_evidence[source.receipt_no]
                )
            else:
                relationship = "unresolved_update"
                relationship_source_receipt_no = None

            relationship_evidence = (
                relation_by_receipt.get(relationship_source_receipt_no)
                if relationship_source_receipt_no is not None
                else None
            )

            dart_remarks = source.raw_payload.get("rm")
            provenance.append(
                EventSourceProvenance(
                    receipt_no=source.receipt_no,
                    report_name=source.report_name,
                    receipt_timestamp=source.receipt_datetime,
                    source=source.source,
                    raw_url=source.raw_url,
                    action=action,
                    relationship=relationship,
                    relationship_source_receipt_no=relationship_source_receipt_no,
                    relationship_source_url=(
                        relationship_evidence.source_url
                        if relationship_evidence is not None
                        else None
                    ),
                    relationship_evidence_sha256=(
                        relationship_evidence.raw_html_sha256
                        if relationship_evidence is not None
                        else None
                    ),
                    raw_payload_sha256=_raw_payload_sha256(source),
                    dart_remarks=(
                        str(dart_remarks) if dart_remarks not in {None, ""} else None
                    ),
                )
            )

        if component_has_update and not component_has_relationship:
            lineage_status = "unresolved"
            unresolved.extend(
                source.receipt_no
                for source in component_sources
                if classify_event_action(source.report_name) != "original"
            )
        elif component_has_update and (
            primary_source is None
            or any(value not in source_by_receipt for value in component_nodes)
        ):
            lineage_status = "partial"
        elif component_has_update:
            lineage_status = "complete"
        else:
            lineage_status = "self_contained"

        market = next(
            (
                source.market
                for source in reversed(component_sources)
                if source.market != "OTHER"
            ),
            latest_source.market,
        )
        original_timestamp = (
            primary_source.receipt_datetime
            if primary_source is not None
            else _timestamp_from_receipt_no(primary_receipt_no)
        )
        event = EconomicEvent(
            economic_event_id=f"dart:{primary_receipt_no}",
            event_type=canonical_event_type(representative.report_name),
            issuer=EventIssuer(
                corp_code=latest_source.corp_code,
                corp_name=latest_source.corp_name,
                stock_code=next(
                    (
                        source.stock_code
                        for source in reversed(component_sources)
                        if source.stock_code is not None
                    ),
                    None,
                ),
            ),
            market=market,
            primary_receipt_no=primary_receipt_no,
            related_receipt_nos=tuple(sorted(component_nodes - {primary_receipt_no})),
            original_timestamp=original_timestamp,
            latest_update_timestamp=latest_source.receipt_datetime,
            status=status,
            normalized_fields=event_fields,
            source_provenance=tuple(provenance),
            lineage_status=lineage_status,
        )
        events.append(event)
        for receipt_no in component_nodes:
            if receipt_no in source_by_receipt:
                receipt_to_event[receipt_no] = event.economic_event_id

    events.sort(key=lambda event: (event.original_timestamp, event.primary_receipt_no))
    return CanonicalizationResult(
        events=tuple(events),
        receipt_to_economic_event_id=dict(sorted(receipt_to_event.items())),
        unresolved_update_receipt_nos=tuple(sorted(set(unresolved))),
    )


def project_legacy_event_study_rows(
    disclosures: Iterable[Disclosure],
    canonicalization: CanonicalizationResult,
    *,
    include_economic_event_id: bool = False,
) -> list[dict[str, Any]]:
    """Project source filings without deduplicating legacy event-study rows.

    Existing studies are receipt-level historical artifacts. This adapter keeps
    their row cardinality and fields intact while allowing an explicit future
    migration to attach ``economic_event_id``.
    """

    rows: list[dict[str, Any]] = []
    for disclosure in disclosures:
        row: dict[str, Any] = {
            "id": disclosure.id,
            "receipt_no": disclosure.receipt_no,
            "corp_code": disclosure.corp_code,
            "corp_name": disclosure.corp_name,
            "stock_code": disclosure.stock_code,
            "report_name": disclosure.report_name,
            "event_date": disclosure.receipt_datetime.date().isoformat(),
            "market": disclosure.market,
        }
        if include_economic_event_id:
            row["economic_event_id"] = canonicalization.receipt_to_economic_event_id[
                disclosure.receipt_no
            ]
        rows.append(row)
    return rows
