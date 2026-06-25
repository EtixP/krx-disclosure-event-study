"""Regex-based extractor for the standardized 단일판매ㆍ공급계약체결 disclosure form.

This is a deterministic alternative to LLM extraction. Per CLAUDE.md:
  "If numbers conflict with deterministic parser results, block trading."

So this parser serves two roles:
1. Standalone — lets us run the strategy without spending LLM tokens.
2. Cross-check — when both run, mismatches between LLM and parser
   should block the signal.

The form is highly standardized: "계약금액 총액(원) <number>", "최근 매출액(원) <number>",
"매출액 대비(%) <ratio>". First-match-wins on each pattern (the contract-section
fields appear before the counterparty-section fields).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from kdtb.schemas.extraction import Extraction

logger = logging.getLogger(__name__)

PARSER_VERSION = "deterministic_supply_contract_v1"

# `[\-\d,]+` matches either a literal dash (undisclosed) or digits+commas.
# Keeping them in one capture group lets us tell "value undisclosed" apart
# from "field not present at all" downstream.
NUM_OR_DASH = r"([\-\d,]+)"
NUM = r"([\d,]+)"
RATIO_NUM = r"([\-\d.]+)"

# Field patterns — order matters because we use the FIRST match for revenue
# (counterparty revenue appears later in the form).
CONTRACT_VALUE_PATTERNS = [
    r"계약금액\s*총액\s*\(\s*원\s*\)\s*" + NUM_OR_DASH,  # voluntary disclosure form
    r"계약금액\s*\(\s*원\s*\)\s*" + NUM_OR_DASH,         # mandatory disclosure form
    r"확정\s*계약금액\s*" + NUM_OR_DASH,
]
PRIOR_REVENUE_PATTERNS = [
    r"최근\s*매출액\s*\(\s*원\s*\)\s*" + NUM_OR_DASH,
]
RATIO_PATTERNS = [
    r"매출액\s*대비\s*\(\s*%\s*\)\s*" + RATIO_NUM,
]
COUNTERPARTY_PATTERNS = [
    r"계약상대방\s*([가-힣A-Za-z0-9\(\)\.\-\s]+?)\s*(?:최근|주요사업|회사와|\d+\.)",
]
CONTRACT_START_PATTERNS = [
    r"계약기간\s*(?:시작일)?\s*(\d{4}[-\.]\d{2}[-\.]\d{2})",
]
CONTRACT_END_PATTERNS = [
    r"종료일\s*(\d{4}[-\.]\d{2}[-\.]\d{2})",
]


def _first_match(patterns: list[str], text: str) -> Optional[str]:
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1).strip()
    return None


def _to_int_krw(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    cleaned = s.replace(",", "").strip()
    if not cleaned or cleaned in ("-", "0"):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _is_explicit_dash(s: Optional[str]) -> bool:
    if s is None:
        return False
    return s.replace(",", "").strip() == "-"


def _to_ratio(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        # Form reports as percentage; convert to fraction.
        return float(s) / 100.0
    except ValueError:
        return None


def parse_supply_contract(
    text: str,
    disclosure_id: int,
    report_name: str = "",
) -> Extraction:
    """Parse cleaned document text into an Extraction.

    Sets validation_status based on completeness:
    - "ok"     — contract_value AND prior_revenue AND computed ratio consistent.
    - "needs_manual_review" — some fields missing but event_type detectable.
    - "blocked" — parsing failed entirely (no contract value found).
    """
    is_cancellation = "해지" in report_name or "해제" in text[:500]
    is_revision = "정정" in report_name or "기재정정" in report_name

    raw_value_str = _first_match(CONTRACT_VALUE_PATTERNS, text)
    raw_revenue_str = _first_match(PRIOR_REVENUE_PATTERNS, text)
    contract_value = _to_int_krw(raw_value_str)
    prior_revenue = _to_int_krw(raw_revenue_str)
    value_undisclosed = _is_explicit_dash(raw_value_str)  # field present but redacted
    reported_ratio = _to_ratio(_first_match(RATIO_PATTERNS, text))
    counterparty = _first_match(COUNTERPARTY_PATTERNS, text)
    start_date = _first_match(CONTRACT_START_PATTERNS, text)
    end_date = _first_match(CONTRACT_END_PATTERNS, text)

    computed_ratio: Optional[float] = None
    if contract_value is not None and prior_revenue and prior_revenue > 0:
        computed_ratio = contract_value / prior_revenue

    # Prefer the reported ratio when present, else compute.
    ratio = reported_ratio if reported_ratio is not None else computed_ratio

    red_flags: list[str] = []
    if value_undisclosed:
        red_flags.append("value_undisclosed_by_company")
    elif contract_value is None:
        red_flags.append("missing_contract_value")
    if prior_revenue is None:
        red_flags.append("missing_revenue")
    if (
        reported_ratio is not None
        and computed_ratio is not None
        and abs(reported_ratio - computed_ratio) / max(reported_ratio, 1e-9) > 0.05
    ):
        red_flags.append("ratio_inconsistent")

    if value_undisclosed:
        # Real supply contract, company chose not to disclose the value.
        # Strategy will skip (ratio is None) but the row is informative, not garbage.
        status: str = "needs_manual_review"
        confidence = 0.5
        is_new = not (is_revision or is_cancellation)
    elif contract_value is None:
        status = "blocked"
        confidence = 0.0
        is_new = None
    elif red_flags:
        status = "needs_manual_review"
        confidence = 0.6
        is_new = not (is_revision or is_cancellation)
    else:
        status = "ok"
        confidence = 0.95  # deterministic parse with all fields present
        is_new = not (is_revision or is_cancellation)

    summary = f"공급계약 v={contract_value} rev={prior_revenue} ratio={ratio} counterparty={counterparty}"

    return Extraction(
        disclosure_id=disclosure_id,
        model_name=PARSER_VERSION,
        prompt_version="N/A",
        event_type="contract_cancellation" if is_cancellation
                   else "contract_revision" if is_revision
                   else "major_supply_contract",
        direction="negative" if is_cancellation else "positive" if not is_revision else "unclear",
        confidence=confidence,
        contract_value_krw=contract_value,
        prior_year_revenue_krw=prior_revenue,
        contract_to_revenue_ratio=ratio,
        is_new_contract=is_new,
        is_revision=is_revision,
        is_cancellation=is_cancellation,
        red_flags=red_flags,
        summary=summary[:500],
        raw_llm_output=None,
        validation_status=status,
        validation_errors=red_flags,
    )
