"""Title-based event category SQL fragments.

Shared between the event-study runner (scripts/run_event_study.py) and
the risk engine's negative-event blacklist (src/kdtb/risk/event_blacklist.py).

Each fragment is a SQL WHERE clause fragment matching the `report_name`
column. The fragment is meant to be combined with stock_code / market
filters depending on the call site.
"""
from __future__ import annotations

CATEGORIES: dict[str, str] = {
    "supply_contract":   "report_name LIKE '단일판매%' AND report_name NOT LIKE '%정정%' AND report_name NOT LIKE '%해지%'",
    "buyback":           "report_name LIKE '%자기주식%취득%' AND report_name NOT LIKE '%처분%' AND report_name NOT LIKE '%정정%'",
    "rights_offering":   "report_name LIKE '%유상증자%' AND report_name NOT LIKE '%정정%'",
    "bonus_issue":       "report_name LIKE '%무상증자%' AND report_name NOT LIKE '%정정%'",
    "convertible_bond":  "report_name LIKE '%전환사채%' AND report_name LIKE '%발행%' AND report_name NOT LIKE '%정정%'",
    "halt_resumption":   "(report_name LIKE '%매매거래정지%' OR report_name LIKE '%거래재개%')",
    "shareholder_change":"report_name LIKE '%최대주주%변경%' AND report_name NOT LIKE '%정정%'",
}

# Categories empirically shown (Loop 10) to predict negative returns on long signals.
# When the risk engine sees a recent disclosure in one of these categories for the
# subject stock, it should reject a long signal.
DEFAULT_NEGATIVE_CATEGORIES: list[str] = [
    "halt_resumption",
    "shareholder_change",
]
