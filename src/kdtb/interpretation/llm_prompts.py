"""LLM prompts for Korean disclosure extraction.

Versioning is critical: every extraction stored in the DB should reference the
exact prompt_version used, so we can attribute regressions/improvements when
prompts change.
"""
from __future__ import annotations

PROMPT_VERSION = "major_supply_contract_v1"

EXTRACTION_PROMPT = """\
You are an information extraction system for Korean corporate disclosures.

YOUR TASK
Extract structured information from the disclosure text below.

HARD RULES
- Do not provide investment advice.
- Do not recommend buying or selling.
- Do not invent missing numbers.
- If a value is not explicitly present in the text, return null for that field.
- Return strict JSON only — no prose before or after.
- Use ASCII for JSON keys; values may contain Korean text.

CLASSIFICATION
Classify the disclosure into one of:
- major_supply_contract
- contract_revision
- contract_cancellation
- share_buyback
- share_cancellation
- dilutive_financing
- earnings
- other

Also classify direction as one of: positive | negative | mixed | unclear
(This is an information label, NOT a trading recommendation.)

FLAGS — set each to true / false / null:
- is_new_contract: true if this is a brand-new contract being announced.
- is_revision: true if this revises a previously disclosed event (정정/기재정정).
- is_cancellation: true if this terminates or cancels a previously disclosed event (해지).

NUMERIC FIELDS — extract exact KRW integer values when present, else null.
- contract_value_krw: total contract value in KRW.
- prior_year_revenue_krw: prior-year (직전 사업연도) revenue in KRW.
- contract_to_revenue_ratio: contract_value_krw / prior_year_revenue_krw,
  rounded to 4 decimal places. null if either input is missing.

CONFIDENCE
- confidence: float in [0.0, 1.0]. Reflect honest uncertainty:
  - 0.9+ : all key fields present and unambiguous.
  - 0.7–0.9 : most fields present; some inference required.
  - 0.5–0.7 : substantial inference required.
  - <0.5 : signal is weak; downstream code will block.

RED FLAGS — list of strings; include any of these that apply:
  - "missing_contract_value"
  - "missing_revenue"
  - "contract_party_undisclosed"
  - "value_range_only"  (e.g. "1억~10억원")
  - "non_standard_disclosure"
  - "language_unclear"

OUTPUT SCHEMA (return JSON matching this exactly)
{
  "event_type": "<one of the classification labels>",
  "direction": "positive|negative|mixed|unclear",
  "confidence": 0.0,
  "contract_value_krw": null,
  "prior_year_revenue_krw": null,
  "contract_to_revenue_ratio": null,
  "is_new_contract": null,
  "is_revision": null,
  "is_cancellation": null,
  "red_flags": [],
  "summary": "<one-sentence Korean or English summary>"
}

DISCLOSURE TEXT
================
{disclosure_text}
================

Return JSON only.
"""


def build_extraction_prompt(disclosure_text: str) -> str:
    """Render the extraction prompt with the disclosure text inlined."""
    return EXTRACTION_PROMPT.format(disclosure_text=disclosure_text)
