"""Build the M0.7 immutable-evidence historical ML-readiness audit.

This audit is deliberately self-contained.  It opens only the pinned local
files named below, never a database or network client, and never repairs a
source row.  Category copies remain distinct even when they share a receipt.
"""

from __future__ import annotations

import argparse
import bisect
import csv
from datetime import date
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = ROOT / "artifacts/m0_7/historical_data_readiness_v1.json"
SCHEMA_VERSION = "m0.7_historical_data_readiness_v1"

CATEGORY_PATHS = {
    "supply_contract": "data/event_study_supply_contract.csv",
    "buyback": "data/event_study_buyback.csv",
    "rights_offering": "data/event_study_rights_offering.csv",
    "bonus_issue": "data/event_study_bonus_issue.csv",
    "convertible_bond": "data/event_study_convertible_bond.csv",
    "halt_resumption": "data/event_study_halt_resumption.csv",
    "shareholder_change": "data/event_study_shareholder_change.csv",
}
SUPPORTING_INPUTS = {
    "benchmark_sessions": "data/benchmark_indices.csv",
    "benchmark_metadata": "data/benchmark_indices.meta.json",
    "buyback_filing_times": (
        "artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv"
    ),
    "price_adjustment_audit": "artifacts/m0_4/price_adjustment_audit.json",
}
INPUT_PURPOSES = {
    **{
        path: f"frozen {category} category-row copies"
        for category, path in CATEGORY_PATHS.items()
    },
    SUPPORTING_INPUTS["benchmark_sessions"]: (
        "hash-pinned observed benchmark-session horizon"
    ),
    SUPPORTING_INPUTS["benchmark_metadata"]: "benchmark capture metadata",
    SUPPORTING_INPUTS["buyback_filing_times"]: "pinned buyback exact filing times",
    SUPPORTING_INPUTS["price_adjustment_audit"]: (
        "pinned prior provider-route and adjustment-policy conclusion"
    ),
}
EXPECTED_INPUT_SHA256 = {
    "data/event_study_supply_contract.csv": (
        "115ee82455c22309e4c2aea052d4d0f7a1012946c52cc74e19a02865ec648bb2"
    ),
    "data/event_study_buyback.csv": (
        "0eb9f0c29a75243c132f89ce60026e3d41f25a6609db270a10513706c8c21d27"
    ),
    "data/event_study_rights_offering.csv": (
        "a572efb8abceeee5889f24472521ac93caaa4ec04f946e1c6f997a45c4e2b6b3"
    ),
    "data/event_study_bonus_issue.csv": (
        "76edf1f5b37a42fd9b38b19ec2a711acf421ba9abf00ee66e78095747ed2d03a"
    ),
    "data/event_study_convertible_bond.csv": (
        "39d9afbb8f585e8f7cc54907c57cc21c0a016d6cceaad83b03c238d68725168c"
    ),
    "data/event_study_halt_resumption.csv": (
        "f9e4061c8dc73e08a0231fdc204f9fcd1417646bc7ef9209665699c78365a20c"
    ),
    "data/event_study_shareholder_change.csv": (
        "350c3427a5bc4ab93084e4adbfdbb8a9796158908db8702812afe2eeb470194f"
    ),
    "data/benchmark_indices.csv": (
        "a70c5f9ae3ae7b8349fe489b720bb01411ba60437b4a85927b87c6a6acc7e062"
    ),
    "data/benchmark_indices.meta.json": (
        "d9ba4ba90f0a16afdc0772aac04bd27636beed41c47acc958cda11c85c3ed282"
    ),
    "artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv": (
        "0c61572d3e128a82b0b7d79c236541d0f5940db7a4fe1d1dd3ae061fa0ee99e5"
    ),
    "artifacts/m0_4/price_adjustment_audit.json": (
        "1acf9366a9147fa78835270d90db7dad8fae9a4a8e9c90018e1a4fa92bac254b"
    ),
}
GENERATOR_SOURCE_PATHS = ("scripts/audit_historical_data_readiness.py",)

REQUIRED_CATEGORY_COLUMNS = {
    "id",
    "receipt_no",
    "corp_code",
    "corp_name",
    "stock_code",
    "report_name",
    "event_date",
    "market",
    "t0_close",
    "t+1_close",
    "t+2_close",
    "t+5_close",
    "error",
    "t0_date",
    "t+1_date",
    "t+2_date",
    "t+5_date",
    "benchmark_source",
    "benchmark_symbol",
    "benchmark_t0_close",
    "benchmark_t1_close",
    "benchmark_t2_close",
    "benchmark_t5_close",
}
DATE_COLUMNS = ("event_date", "t0_date", "t+1_date", "t+5_date")
ADMISSION_PRICE_COLUMNS = ("t0_close", "t+1_close", "t+5_close")
PRIMARY_REASON_ORDER = (
    "missing_date",
    "malformed_date",
    "reversed_window",
    "unsupported_market",
    "calendar_horizon",
    "t0_session_mismatch",
    "entry_session_mismatch",
    "label_session_mismatch",
    "invalid_stock_price",
    "source_row_error",
    "missing_identity",
)
DEFECT_FLAG_ORDER = (
    "missing_date",
    "malformed_date",
    "reversed_window",
    "unsupported_market",
    "calendar_horizon",
    "t0_session_mismatch",
    "entry_session_mismatch",
    "label_session_mismatch",
    "invalid_stock_price",
    "source_row_error",
    "missing_identity",
)
GATE_BLOCKER_CODES = (
    "missing_point_in_time_security_identity",
    "missing_point_in_time_disclosure_evidence",
    "missing_versioned_daily_label_source",
    "cross_category_copy_conflict",
)
PILLAR_BLOCKER_CODES = {
    "point_in_time_security_identity": "missing_point_in_time_security_identity",
    "point_in_time_disclosure_evidence": "missing_point_in_time_disclosure_evidence",
    "versioned_daily_label_source": "missing_versioned_daily_label_source",
}
INTRADAY_INFORMATION_CODE = "historical_intraday_unavailable_prospective_only"


class AuditInputError(ValueError):
    """Raised when pinned audit evidence is missing, modified or malformed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _row_lexeme_sha256(row: Mapping[str, str]) -> str:
    return hashlib.sha256(_canonical_bytes(dict(row))).hexdigest()


def _file_record(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "purpose": INPUT_PURPOSES[relative],
    }


def _validate_input_hashes(root: Path, expected: Mapping[str, str]) -> None:
    required = set(INPUT_PURPOSES)
    if set(expected) != required:
        raise AuditInputError(
            "expected input pins do not enumerate the exact audit inputs"
        )
    for relative in sorted(required):
        path = root / relative
        if not path.is_file():
            raise AuditInputError(f"missing pinned input: {relative}")
        actual = sha256_file(path)
        if actual != expected[relative]:
            raise AuditInputError(f"pinned input hash mismatch: {relative}")


def _read_csv(path: Path, required: set[str]) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        if header is None:
            raise AuditInputError(f"missing CSV header: {path.name}")
        missing = sorted(required - set(header))
        if missing:
            raise AuditInputError(
                f"missing required columns in {path.name}: {', '.join(missing)}"
            )
        if len(header) != len(set(header)):
            raise AuditInputError(f"duplicate CSV columns: {path.name}")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise AuditInputError(f"malformed CSV row: {path.name}")
            rows.append({key: value for key, value in row.items()})
    return list(header), rows


def _strict_day(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("malformed_date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("malformed_date") from error


def _valid_positive_number(value: str) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _benchmark_sessions(root: Path) -> tuple[dict[str, list[date]], dict[str, Any]]:
    path = root / SUPPORTING_INPUTS["benchmark_sessions"]
    header, rows = _read_csv(path, {"date", "market", "close", "source"})
    if not rows:
        raise AuditInputError("benchmark session input is empty")
    sessions: dict[str, list[date]] = {"KOSPI": [], "KOSDAQ": []}
    seen: set[tuple[str, date]] = set()
    for row in rows:
        market = row["market"]
        if market not in sessions or not _valid_positive_number(row["close"]):
            raise AuditInputError("invalid benchmark session value")
        try:
            observed = _strict_day(row["date"])
        except ValueError as error:
            raise AuditInputError("invalid benchmark session date") from error
        key = (market, observed)
        if key in seen:
            raise AuditInputError("duplicate benchmark session")
        seen.add(key)
        sessions[market].append(observed)
    for values in sessions.values():
        values.sort()
    if not sessions["KOSPI"] or sessions["KOSPI"] != sessions["KOSDAQ"]:
        raise AuditInputError("benchmark market session horizons differ")
    return sessions, {
        "columns": header,
        "rows": len(rows),
        "semantics": (
            "hash-pinned observed benchmark sessions; not an authoritative exchange calendar"
        ),
        "markets": {
            market: {
                "first": values[0].isoformat(),
                "last": values[-1].isoformat(),
                "sessions": len(values),
            }
            for market, values in sorted(sessions.items())
        },
    }


def _expected_window(
    sessions: Mapping[str, list[date]], market: str, event_day: date
) -> tuple[date, date, date]:
    if market not in sessions:
        raise ValueError("unsupported_market")
    values = sessions[market]
    offset = bisect.bisect_left(values, event_day)
    if offset + 5 >= len(values):
        raise ValueError("calendar_horizon")
    return values[offset], values[offset + 1], values[offset + 5]


def _audit_row(
    row: Mapping[str, str], sessions: Mapping[str, list[date]]
) -> dict[str, Any]:
    flags: set[str] = set()
    if any(not row.get(column, "").strip() for column in DATE_COLUMNS):
        flags.add("missing_date")

    parsed: dict[str, date] = {}
    for column in DATE_COLUMNS:
        value = row.get(column, "")
        if not value:
            continue
        try:
            parsed[column] = _strict_day(value)
        except ValueError:
            flags.add("malformed_date")

    if all(column in parsed for column in DATE_COLUMNS):
        event_day, t0_day, entry_day, label_day = (
            parsed[column] for column in DATE_COLUMNS
        )
        if not event_day <= t0_day < entry_day < label_day:
            flags.add("reversed_window")
        try:
            expected = _expected_window(sessions, row.get("market", ""), event_day)
        except ValueError as error:
            flags.add(str(error))
        else:
            if t0_day != expected[0]:
                flags.add("t0_session_mismatch")
            if entry_day != expected[1]:
                flags.add("entry_session_mismatch")
            if label_day != expected[2]:
                flags.add("label_session_mismatch")

    if not all(
        _valid_positive_number(row.get(column, ""))
        for column in ADMISSION_PRICE_COLUMNS
    ):
        flags.add("invalid_stock_price")
    if row.get("error", "").strip():
        flags.add("source_row_error")
    if any(
        not row.get(column, "").strip()
        for column in ("id", "receipt_no", "corp_code", "stock_code")
    ):
        flags.add("missing_identity")

    primary_reason = next(
        (reason for reason in PRIMARY_REASON_ORDER if reason in flags), None
    )
    delay_days = None
    if "t0_session_mismatch" in flags and {"event_date", "t0_date"} <= parsed.keys():
        delay_days = (parsed["t0_date"] - parsed["event_date"]).days
    return {
        "admitted": primary_reason is None,
        "primary_exclusion_reason": primary_reason,
        "defect_flags": [flag for flag in DEFECT_FLAG_ORDER if flag in flags],
        "t0_delay_calendar_days": delay_days,
    }


def _audit_categories(
    root: Path, sessions: Mapping[str, list[date]]
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    summaries: dict[str, Any] = {}
    by_receipt: dict[str, list[dict[str, Any]]] = {}
    for category, relative in CATEGORY_PATHS.items():
        header, rows = _read_csv(root / relative, REQUIRED_CATEGORY_COLUMNS)
        receipt_counts: dict[str, int] = {}
        audited_rows: list[dict[str, Any]] = []
        for fields in rows:
            receipt = fields["receipt_no"]
            receipt_counts[receipt] = receipt_counts.get(receipt, 0) + 1
            admission = _audit_row(fields, sessions)
            copy = {
                "category": category,
                "row_copy_id": f"{category}:{receipt}",
                "row_lexeme_sha256": _row_lexeme_sha256(fields),
                "fields": fields,
                "admission": admission,
            }
            audited_rows.append(copy)
            by_receipt.setdefault(receipt, []).append(copy)
        duplicate_receipts = sorted(
            receipt for receipt, count in receipt_counts.items() if count != 1
        )
        if duplicate_receipts:
            raise AuditInputError(
                f"category has non-unique receipt copies: {category}: {duplicate_receipts[:3]}"
            )

        admitted = sum(copy["admission"]["admitted"] for copy in audited_rows)
        primary_counts = {
            reason: sum(
                copy["admission"]["primary_exclusion_reason"] == reason
                for copy in audited_rows
            )
            for reason in PRIMARY_REASON_ORDER
        }
        defect_counts = {
            flag: sum(
                flag in copy["admission"]["defect_flags"] for copy in audited_rows
            )
            for flag in DEFECT_FLAG_ORDER
        }
        exclusions = [
            {
                "receipt_no": copy["fields"]["receipt_no"],
                "row_copy_id": copy["row_copy_id"],
                "row_lexeme_sha256": copy["row_lexeme_sha256"],
                "primary_exclusion_reason": copy["admission"][
                    "primary_exclusion_reason"
                ],
                "defect_flags": copy["admission"]["defect_flags"],
                "event_date": copy["fields"]["event_date"] or None,
                "t0_date": copy["fields"]["t0_date"] or None,
                "t0_delay_calendar_days": copy["admission"]["t0_delay_calendar_days"],
            }
            for copy in audited_rows
            if not copy["admission"]["admitted"]
        ]
        exclusions.sort(
            key=lambda item: (item["row_copy_id"], item["row_lexeme_sha256"])
        )
        semantic_hashes = sorted(copy["row_lexeme_sha256"] for copy in audited_rows)
        summaries[category] = {
            "path": relative,
            "columns": header,
            "row_copy_count": len(rows),
            "unique_receipt_count": len(receipt_counts),
            "admitted_row_copies": admitted,
            "excluded_row_copies": len(rows) - admitted,
            "primary_reason_counts": primary_counts,
            "nonexclusive_defect_counts": defect_counts,
            "ordered_primary_exclusions": exclusions,
            "sorted_row_lexeme_set_sha256": hashlib.sha256(
                _canonical_bytes(semantic_hashes)
            ).hexdigest(),
        }
    return summaries, by_receipt


def _overlap_audit(by_receipt: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    overlaps = []
    for receipt, copies in by_receipt.items():
        if len(copies) < 2:
            continue
        ordered = sorted(copies, key=lambda copy: copy["category"])
        fields = sorted({key for copy in ordered for key in copy["fields"]})
        conflicts = []
        for field in fields:
            values = {
                copy["category"]: copy["fields"].get(field, "") for copy in ordered
            }
            if len(set(values.values())) > 1:
                conflicts.append({"field": field, "values_by_category": values})
        overlaps.append(
            {
                "receipt_no": receipt,
                "categories": [copy["category"] for copy in ordered],
                "category_copies": ordered,
                "field_conflicts": conflicts,
                "has_field_conflict": bool(conflicts),
                "category_dependent_admission": len(
                    {copy["admission"]["admitted"] for copy in ordered}
                )
                > 1,
                "canonical_copy_selected": False,
            }
        )
    overlaps.sort(key=lambda item: item["receipt_no"])
    divergent = [item for item in overlaps if item["has_field_conflict"]]
    return {
        "cross_category_unique_receipts": len(overlaps),
        "cross_category_row_copies": sum(
            len(item["category_copies"]) for item in overlaps
        ),
        "field_conflict_receipts": len(divergent),
        "category_dependent_admission_receipts": sum(
            item["category_dependent_admission"] for item in overlaps
        ),
        "canonicalization_policy": (
            "retain every category copy; no canonical copy is selected without new source evidence"
        ),
        "receipts": overlaps,
    }


def _population(
    category_summaries: Mapping[str, Any],
    by_receipt: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    receipt_states = {
        receipt: [copy["admission"]["admitted"] for copy in copies]
        for receipt, copies in by_receipt.items()
    }
    row_copies = sum(item["row_copy_count"] for item in category_summaries.values())
    admitted = sum(item["admitted_row_copies"] for item in category_summaries.values())
    return {
        "row_copies": {
            "input": row_copies,
            "admitted": admitted,
            "excluded": row_copies - admitted,
            "interpretation": "category-row copies; not independent events",
        },
        "unique_receipts": {
            "input": len(receipt_states),
            "at_least_one_copy_admitted": sum(
                any(states) for states in receipt_states.values()
            ),
            "every_copy_admitted": sum(
                all(states) for states in receipt_states.values()
            ),
            "interpretation": "receipt identities collapsed only for population accounting",
        },
    }


def _delayed_t0(category_summaries: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for category, summary in category_summaries.items():
        for item in summary["ordered_primary_exclusions"]:
            delay = item["t0_delay_calendar_days"]
            if delay is not None and "t0_session_mismatch" in item["defect_flags"]:
                rows.append({"category": category, **item})
    rows.sort(
        key=lambda item: (
            -(item["t0_delay_calendar_days"] or 0),
            item["row_copy_id"],
        )
    )
    return {
        "affected_row_copies": len(rows),
        "maximum_calendar_days": rows[0]["t0_delay_calendar_days"] if rows else None,
        "longest_examples": rows[:10],
        "admission_policy": (
            "T0 must equal the first observed benchmark session on or after the event date; arbitrary later prices are excluded"
        ),
    }


def _filing_time_coverage(root: Path, buyback_rows: int) -> dict[str, Any]:
    _, rows = _read_csv(
        root / SUPPORTING_INPUTS["buyback_filing_times"], {"receipt_no", "filing_time"}
    )
    times: dict[str, str] = {}
    buckets = {
        "pre_open_before_09_00": 0,
        "session_09_00_through_15_29": 0,
        "exact_close_15_30": 0,
        "after_close_after_15_30": 0,
    }
    for row in rows:
        receipt = row["receipt_no"]
        value = row["filing_time"]
        if receipt in times:
            raise AuditInputError("duplicate pinned buyback filing time")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise AuditInputError("invalid pinned buyback filing time")
        times[receipt] = value
        hour, minute = (int(part) for part in value.split(":"))
        minute_of_day = hour * 60 + minute
        if minute_of_day < 9 * 60:
            buckets["pre_open_before_09_00"] += 1
        elif minute_of_day < 15 * 60 + 30:
            buckets["session_09_00_through_15_29"] += 1
        elif minute_of_day == 15 * 60 + 30:
            buckets["exact_close_15_30"] += 1
        else:
            buckets["after_close_after_15_30"] += 1
    if len(times) > buyback_rows:
        raise AuditInputError("filing-time input exceeds buyback population")
    return {
        "buyback_row_copies": buyback_rows,
        "exact_time_supplied": len(times),
        "exact_time_not_supplied": buyback_rows - len(times),
        "mutually_exclusive_time_buckets": buckets,
        "same_day_close_constraint": (
            "filings at or after 15:30 cannot justify the same-day close as an executable entry"
        ),
    }


def _identifier_shape(by_receipt: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    counts = {
        "six_digit_numeric": 0,
        "six_character_alphanumeric": 0,
        "missing": 0,
        "other": 0,
    }
    for copies in by_receipt.values():
        for copy in copies:
            value = copy["fields"]["stock_code"]
            if not value:
                counts["missing"] += 1
            elif re.fullmatch(r"\d{6}", value):
                counts["six_digit_numeric"] += 1
            elif re.fullmatch(r"[A-Za-z0-9]{6}", value):
                counts["six_character_alphanumeric"] += 1
            else:
                counts["other"] += 1
    return {
        "row_copy_counts": counts,
        "semantics": (
            "retrospectively stored DART stock_code lexemes; not proof of event-date listing, instrument or tradability"
        ),
    }


def _prior_provider_conclusion(root: Path) -> dict[str, Any]:
    try:
        prior = json.loads(
            (root / SUPPORTING_INPUTS["price_adjustment_audit"]).read_text(
                encoding="utf-8"
            )
        )
        methodology = prior["methodology"]
        return {
            key: methodology[key]
            for key in (
                "provider_route",
                "pykrx_adjusted_argument",
                "pykrx_version_pin",
                "research_policy",
            )
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AuditInputError("invalid pinned price-adjustment audit") from error


def _evidence_assessment(
    admitted_row_copies: int, provider: Mapping[str, Any]
) -> dict[str, Any]:
    unavailable = "not_supplied_by_pinned_inputs"
    return {
        "point_in_time_security_identity": {
            "gate_pillar_satisfied_for_all_admitted_copies": False,
            "admitted_row_copies_without_pillar": admitted_row_copies,
            "retrospective_fields_present": ["corp_code", "stock_code", "market"],
            "effective_dated_listing_status": unavailable,
            "effective_dated_market": unavailable,
            "effective_dated_instrument_type": unavailable,
            "security_code_support_history": unavailable,
            "listing_and_delisting_intervals": unavailable,
            "event_date_trading_eligibility": unavailable,
            "claim_boundary": (
                "retrospective DART identity is not an authoritative security master"
            ),
        },
        "point_in_time_disclosure_evidence": {
            "gate_pillar_satisfied_for_all_admitted_copies": False,
            "admitted_row_copies_without_pillar": admitted_row_copies,
            "receipt_identity_present": True,
            "original_document_version_bytes": unavailable,
            "document_byte_hash": unavailable,
            "relationship_and_lineage_capture": unavailable,
            "contemporaneous_observation_timestamp": unavailable,
            "decision_time_availability_for_all_categories": unavailable,
            "database_inference_used": False,
        },
        "versioned_daily_label_source": {
            "gate_pillar_satisfied_for_all_admitted_copies": False,
            "admitted_row_copies_without_pillar": admitted_row_copies,
            "frozen_close_observations_present": ["T0", "T+1", "T+2", "T+5"],
            "raw_ohlcv_provider_response_bytes": unavailable,
            "raw_response_hash": unavailable,
            "capture_timestamp": unavailable,
            "request_boundary": unavailable,
            "volume": unavailable,
            "adjustment_vintage": unavailable,
            "prior_pinned_provider_conclusion": dict(provider),
            "claim_boundary": (
                "frozen closes prove earlier research consumption, not a raw-bar vintage or executable price"
            ),
        },
    }


def _evaluate_gate(
    evidence: Mapping[str, Mapping[str, Any]], *, has_copy_conflict: bool
) -> dict[str, Any]:
    blockers = [
        code
        for pillar, code in PILLAR_BLOCKER_CODES.items()
        if not evidence[pillar]["gate_pillar_satisfied_for_all_admitted_copies"]
    ]
    if has_copy_conflict:
        blockers.append("cross_category_copy_conflict")
    if any(code not in GATE_BLOCKER_CODES for code in blockers):
        raise ValueError("unregistered ML gate blocker code")
    return {
        "decision": "NO_GO" if blockers else "GO",
        "blocker_reason_codes": blockers,
        "reason_code_order": list(GATE_BLOCKER_CODES),
        "semantics": (
            "GO requires every admitted row copy to satisfy all three evidence pillars and requires no cross-category copy conflict"
        ),
        "all_three_evidence_pillars_required_per_admitted_copy": True,
        "cross_category_copy_conflict_is_independent_defect": True,
        "historical_intraday_is_daily_gate_input": False,
    }


def build_audit(
    *,
    root: Path = ROOT,
    expected_input_sha256: Mapping[str, str] = EXPECTED_INPUT_SHA256,
) -> dict[str, Any]:
    """Build the deterministic audit after verifying every consumed input."""
    root = root.resolve()
    _validate_input_hashes(root, expected_input_sha256)
    sessions, benchmark = _benchmark_sessions(root)
    categories, by_receipt = _audit_categories(root, sessions)
    population = _population(categories, by_receipt)
    overlaps = _overlap_audit(by_receipt)
    provider = _prior_provider_conclusion(root)
    admitted = population["row_copies"]["admitted"]
    filing_times = _filing_time_coverage(root, categories["buyback"]["row_copy_count"])
    benchmark_meta = json.loads(
        (root / SUPPORTING_INPUTS["benchmark_metadata"]).read_text(encoding="utf-8")
    )
    inputs = [_file_record(root, relative) for relative in sorted(INPUT_PURPOSES)]
    sources = [
        {
            "path": relative,
            "bytes": (root / relative).stat().st_size,
            "sha256": sha256_file(root / relative),
            "purpose": "artifact generator source",
        }
        for relative in GENERATOR_SOURCE_PATHS
    ]
    evidence = _evidence_assessment(admitted, provider)
    gate = _evaluate_gate(
        evidence, has_copy_conflict=bool(overlaps["field_conflict_receipts"])
    )
    executable = Path(sys.executable).absolute()
    try:
        command_executable = executable.relative_to(root).as_posix()
    except ValueError:
        command_executable = executable.as_posix()
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": "M0.7",
        "status": "historical_ml_readiness_audit",
        "generation": {
            "command": [
                command_executable,
                "-m",
                "scripts.audit_historical_data_readiness",
                "--output",
                "/tmp/m0_7_historical_data_readiness_v1.json",
            ],
            "working_directory": ".",
            "runtime": {
                "python_executable": command_executable,
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "pandas": importlib.metadata.version("pandas"),
                "numpy": importlib.metadata.version("numpy"),
            },
            "database_used": False,
            "network_used": False,
        },
        "inputs": inputs,
        "generator_sources": sources,
        "scope": {
            "reconciliation_boundary": (
                "exact frozen local CSV lexemes and pinned supporting inputs only"
            ),
            "upstream_dart_completeness_claimed": False,
            "category_population_completeness_claimed": False,
            "source_rows_repaired_or_rewritten": False,
        },
        "benchmark_session_evidence": {
            **benchmark,
            "metadata": benchmark_meta,
        },
        "primary_exclusion_reason_order": list(PRIMARY_REASON_ORDER),
        "nonexclusive_defect_flag_order": list(DEFECT_FLAG_ORDER),
        "categories": categories,
        "population": population,
        "cross_category_overlaps": overlaps,
        "delayed_t0_windows": _delayed_t0(categories),
        "buyback_filing_time_coverage": filing_times,
        "security_identifier_shape": _identifier_shape(by_receipt),
        "evidence": evidence,
        "ml_readiness_gate": gate,
        "informational_status": [
            {
                "code": INTRADAY_INFORMATION_CODE,
                "status": "prospective_collection_only",
                "gate_blocker": False,
                "detail": (
                    "the prospective KIS path cannot reconstruct historical spreads, latency, order books or fills"
                ),
            }
        ],
        "recommended_next_bounded_milestone": {
            "scope": (
                "establish source feasibility for effective-dated security identity and a versioned raw daily outcome source for buybacks"
            ),
            "document_features_wait_for": (
                "exact document-version bytes, lineage and decision-time availability"
            ),
            "model_training_allowed_now": False,
        },
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def require_new_output(path: Path, *, project_root: Path = ROOT) -> Path:
    """Require a new non-symlink output outside protected repository trees."""
    if path.is_symlink():
        raise ValueError("choose a new output outside protected historical trees")
    resolved = path.resolve(strict=False)
    protected = tuple(
        (project_root / name).resolve() for name in ("data", "sources", "artifacts")
    )
    if resolved.exists() or any(resolved.is_relative_to(root) for root in protected):
        raise ValueError("choose a new output outside protected historical trees")
    return resolved


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _close_directory_chain(chain: list[tuple[str, int, tuple[int, int]]]) -> None:
    for _, descriptor, _ in reversed(chain):
        os.close(descriptor)


def _open_directory_chain(
    path: Path, *, create: bool
) -> list[tuple[str, int, tuple[int, int]]]:
    """Open each absolute directory component relative to its held parent."""
    if not path.is_absolute() or path.anchor != "/":
        raise ValueError("output parent must resolve to an absolute POSIX path")
    flags = _directory_open_flags()
    root_fd = os.open("/", flags)
    root_stat = os.fstat(root_fd)
    chain = [("/", root_fd, (root_stat.st_dev, root_stat.st_ino))]
    try:
        for component in path.parts[1:]:
            parent_fd = chain[-1][1]
            try:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                except FileExistsError:
                    # A competing creator is acceptable only if the no-follow
                    # open below proves it made a real directory.
                    pass
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            child_stat = os.fstat(child_fd)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child_fd)
                raise ValueError("output path component is not a directory")
            chain.append((component, child_fd, (child_stat.st_dev, child_stat.st_ino)))
        return chain
    except Exception:
        _close_directory_chain(chain)
        raise


def _visible_chain_matches(
    path: Path, expected: list[tuple[str, int, tuple[int, int]]]
) -> bool:
    try:
        visible = _open_directory_chain(path, create=False)
    except (OSError, ValueError):
        return False
    try:
        return [item[2] for item in visible] == [item[2] for item in expected]
    finally:
        _close_directory_chain(visible)


def write_new_json(
    path: Path, payload: Mapping[str, Any], *, project_root: Path = ROOT
) -> str:
    """Publish through a held no-follow path chain and refuse any replacement."""
    output = require_new_output(path, project_root=project_root)
    try:
        chain = _open_directory_chain(output.parent, create=True)
    except OSError as error:
        raise ValueError("output path changed after safety validation") from error
    parent_fd = chain[-1][1]

    encoded = _json_bytes(payload)
    temporary_name = f".m0_7-{secrets.token_hex(16)}.tmp"
    temporary_fd = None
    published = False
    try:
        visible_output = require_new_output(path, project_root=project_root)
        if output != visible_output or not _visible_chain_matches(output.parent, chain):
            raise ValueError("output path changed before publication")
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(temporary_fd, "wb", closefd=True) as handle:
            temporary_fd = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(
            temporary_name,
            output.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        published = True

        if not _visible_chain_matches(output.parent, chain):
            raise ValueError("output path changed during publication")
        linked = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        visible = os.stat(output, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino) != (visible.st_dev, visible.st_ino):
            raise ValueError("output path changed during publication")
    except Exception:
        if published:
            os.unlink(output.name, dir_fd=parent_fd)
        raise
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        _close_directory_chain(chain)
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON path outside data, sources and artifacts",
    )
    args = parser.parse_args()
    output = require_new_output(args.output)
    result = build_audit()
    output_sha256 = write_new_json(output, result)
    print(
        json.dumps(
            {
                "decision": result["ml_readiness_gate"]["decision"],
                "blocker_reason_codes": result["ml_readiness_gate"][
                    "blocker_reason_codes"
                ],
                "output": str(output),
                "sha256": output_sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
