"""Decision-time feature extraction.

Every feature here must be knowable AT THE MOMENT WE DECIDE TO TRADE. Because
the mock trade enters at the T+1 close, everything from the event day and
earlier is fair game (the event-day close `t0_close`, the contract terms, the
calendar). Nothing computed from T+1 or later may appear — that would be
look-ahead.

The feature order is FIXED. A trained model is scored on exactly this order.
New features must be appended at the END so previously-pickled models stay
loadable.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Optional

FEATURE_NAMES: list[str] = [
    "market_is_kospi",            # 1.0 if KOSPI else 0.0
    "day_of_week",                # 0=Mon .. 4=Fri
    "month",                      # 1..12
    "log_t0_close",               # log of event-day close (price-level / penny-stock proxy)
    "contract_to_revenue_ratio",  # supply-contract only; 0.0 otherwise
    "log_contract_value",         # supply-contract only; 0.0 otherwise
    "counterparty_ordinal",       # gov=0, unknown/other=1, large_corp=2, foreign=3
    "has_extraction",             # 1.0 if the row had parser-extracted fields
]

# Ordinal ranking loosely follows the Loop-6 finding that government
# counterparties underperform; the tree can split on it freely.
_COUNTERPARTY_ORDINAL: dict[str, int] = {
    "government": 0,
    "unknown": 1,
    "other_korean_corp": 1,
    "large_corp_korean": 2,
    "foreign": 3,
}


def _safe_log(x: Optional[float]) -> float:
    if x is None:
        return 0.0
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return 0.0
    if xf <= 0 or math.isnan(xf):
        return 0.0
    return math.log(xf)


def _to_date(ev) -> Optional[date]:
    if ev is None:
        return None
    if isinstance(ev, datetime):
        return ev.date()
    if isinstance(ev, date):
        return ev
    if isinstance(ev, str):
        try:
            return datetime.strptime(ev[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    # pandas Timestamp duck-types as having .date()
    try:
        return ev.date()
    except AttributeError:
        return None


def _is_missing(v) -> bool:
    if v is None:
        return True
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def extract_features(row: dict) -> list[float]:
    """Map one event row (dict-like) to the fixed-order feature vector."""
    market = str(row.get("market") or "").upper()
    ev_dt = _to_date(row.get("event_date"))
    dow = float(ev_dt.weekday()) if ev_dt else 0.0
    month = float(ev_dt.month) if ev_dt else 0.0

    ratio = row.get("contract_to_revenue_ratio")
    value = row.get("contract_value_krw")
    cp = row.get("counterparty_type")

    ratio_present = not _is_missing(ratio)
    cp_present = cp is not None and not (isinstance(cp, float) and math.isnan(cp))
    has_ext = 1.0 if (ratio_present or cp_present) else 0.0

    return [
        1.0 if market == "KOSPI" else 0.0,
        dow,
        month,
        _safe_log(row.get("t0_close")),
        float(ratio) if ratio_present else 0.0,
        _safe_log(value),
        float(_COUNTERPARTY_ORDINAL.get(str(cp), 1)) if cp_present else 1.0,
        has_ext,
    ]
