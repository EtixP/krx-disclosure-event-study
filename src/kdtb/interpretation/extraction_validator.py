"""Validate LLM output against the Extraction schema and sanity rules.

The LLM returns JSON; we parse, validate the shape with Pydantic, then run
cross-field sanity checks (e.g. ratio = value/revenue). Anything suspicious
flips `validation_status` to "needs_manual_review" or "blocked".
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import ValidationError

from kdtb.schemas.extraction import Extraction

logger = logging.getLogger(__name__)

# If the LLM-reported ratio differs from value/revenue by more than this
# fraction, we flag it. Catches both bad arithmetic and inconsistent fields.
RATIO_TOLERANCE = 0.05


def validate_llm_output(
    raw_output: str,
    disclosure_id: int,
    model_name: str,
    prompt_version: str,
) -> Extraction:
    """Parse and validate raw LLM output. Always returns an Extraction.

    The Extraction's validation_status reflects the outcome:
    - "ok" — passes all checks, safe for the strategy engine.
    - "needs_manual_review" — recoverable issue (missing fields, low confidence).
    - "blocked" — hard failure (unparseable, schema-invalid, inconsistent numbers).
    """
    errors: list[str] = []

    try:
        parsed = json.loads(raw_output.strip())
    except json.JSONDecodeError as e:
        return _blocked(
            disclosure_id, model_name, prompt_version, raw_output,
            [f"json_decode_error:{e.msg}"],
        )

    parsed.setdefault("disclosure_id", disclosure_id)
    parsed.setdefault("model_name", model_name)
    parsed.setdefault("prompt_version", prompt_version)
    parsed["raw_llm_output"] = raw_output

    try:
        ext = Extraction(**parsed)
    except ValidationError as e:
        return _blocked(
            disclosure_id, model_name, prompt_version, raw_output,
            [f"schema_error:{err['loc']}:{err['msg']}" for err in e.errors()],
        )

    cross = _cross_field_checks(ext)
    if cross:
        errors.extend(cross)
        ext.validation_status = "blocked"
        ext.validation_errors = errors
        return ext

    soft = _soft_checks(ext)
    if soft:
        ext.validation_status = "needs_manual_review"
        ext.validation_errors = soft
    else:
        ext.validation_status = "ok"
        ext.validation_errors = []
    return ext


def _cross_field_checks(ext: Extraction) -> list[str]:
    """Hard inconsistencies. Trigger "blocked" status — no trading."""
    errs: list[str] = []
    v = ext.contract_value_krw
    r = ext.prior_year_revenue_krw
    ratio = ext.contract_to_revenue_ratio
    if v is not None and v < 0:
        errs.append("contract_value_negative")
    if r is not None and r <= 0:
        errs.append("revenue_non_positive")
    if v is not None and r is not None and ratio is not None:
        expected = v / r
        if expected > 0 and abs(ratio - expected) / expected > RATIO_TOLERANCE:
            errs.append(f"ratio_inconsistent:reported={ratio:.4f}_computed={expected:.4f}")
    if ext.is_cancellation and ext.is_new_contract:
        errs.append("cancellation_and_new_contract_both_true")
    return errs


def _soft_checks(ext: Extraction) -> list[str]:
    """Recoverable issues. Trigger "needs_manual_review"."""
    warnings: list[str] = []
    if ext.event_type == "major_supply_contract":
        if ext.contract_value_krw is None:
            warnings.append("missing_contract_value")
        if ext.prior_year_revenue_krw is None:
            warnings.append("missing_revenue")
        if ext.is_new_contract is None:
            warnings.append("is_new_contract_unknown")
    if ext.confidence < 0.5:
        warnings.append(f"low_confidence:{ext.confidence:.2f}")
    return warnings


def _blocked(
    disclosure_id: int,
    model_name: str,
    prompt_version: str,
    raw_output: str,
    errors: list[str],
) -> Extraction:
    """Build a 'blocked' Extraction when we can't parse or validate the output."""
    return Extraction(
        disclosure_id=disclosure_id,
        model_name=model_name,
        prompt_version=prompt_version,
        event_type="other",
        direction="unclear",
        confidence=0.0,
        raw_llm_output=raw_output,
        validation_status="blocked",
        validation_errors=errors,
    )
