from __future__ import annotations

import json

from kdtb.interpretation.extraction_validator import validate_llm_output


def _good_json(**overrides) -> str:
    defaults = {
        "event_type": "major_supply_contract",
        "direction": "positive",
        "confidence": 0.9,
        "contract_value_krw": 85_000_000_000,
        "prior_year_revenue_krw": 600_000_000_000,
        "contract_to_revenue_ratio": 0.1417,
        "is_new_contract": True,
        "is_revision": False,
        "is_cancellation": False,
        "red_flags": [],
        "summary": "ok",
    }
    defaults.update(overrides)
    return json.dumps(defaults)


def test_validates_clean_output():
    ext = validate_llm_output(_good_json(), 1, "claude-3-5-sonnet", "v1")
    assert ext.validation_status == "ok"
    assert ext.event_type == "major_supply_contract"
    assert ext.contract_to_revenue_ratio == 0.1417


def test_blocks_malformed_json():
    ext = validate_llm_output("not json {{", 1, "x", "v1")
    assert ext.validation_status == "blocked"
    assert any("json_decode_error" in e for e in ext.validation_errors)


def test_blocks_inconsistent_ratio():
    # reported ratio 0.5 but value/revenue = 0.14
    bad = _good_json(contract_to_revenue_ratio=0.5)
    ext = validate_llm_output(bad, 1, "x", "v1")
    assert ext.validation_status == "blocked"
    assert any("ratio_inconsistent" in e for e in ext.validation_errors)


def test_blocks_negative_value():
    bad = _good_json(contract_value_krw=-1_000)
    ext = validate_llm_output(bad, 1, "x", "v1")
    assert ext.validation_status == "blocked"


def test_blocks_cancellation_and_new_contract_both_true():
    bad = _good_json(is_new_contract=True, is_cancellation=True)
    ext = validate_llm_output(bad, 1, "x", "v1")
    assert ext.validation_status == "blocked"
    assert any("cancellation_and_new_contract_both_true" in e for e in ext.validation_errors)


def test_flags_missing_contract_value_as_review():
    review = _good_json(contract_value_krw=None, contract_to_revenue_ratio=None)
    ext = validate_llm_output(review, 1, "x", "v1")
    assert ext.validation_status == "needs_manual_review"
    assert "missing_contract_value" in ext.validation_errors


def test_low_confidence_flagged_as_review():
    low = _good_json(confidence=0.3)
    ext = validate_llm_output(low, 1, "x", "v1")
    assert ext.validation_status == "needs_manual_review"
    assert any("low_confidence" in e for e in ext.validation_errors)


def test_blocks_schema_violation():
    # confidence > 1 violates schema
    bad = _good_json(confidence=1.5)
    ext = validate_llm_output(bad, 1, "x", "v1")
    assert ext.validation_status == "blocked"
    assert any("schema_error" in e for e in ext.validation_errors)
