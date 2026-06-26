"""Recent-negative-event blacklist for the risk engine.

When the strategy generates a long signal, the risk engine consults this
helper to check whether the subject stock has had a disclosure in one of
the negative-signal categories (halt_resumption, shareholder_change) within
the lookback window. If yes, the signal is rejected.

This converts the strongest Loop-10 negative findings (categories where the
long return is consistently negative across walk-forward windows and where
retail accounts can't short to monetize it) into a deterministic do-not-trade
filter.

Backed by SQLite; designed to be cheap to call once per signal evaluation —
one indexed query.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from kdtb.data.event_categories import CATEGORIES, DEFAULT_NEGATIVE_CATEGORIES

logger = logging.getLogger(__name__)


class EventBlacklist:
    """Queries the disclosures table for recent disclosures matching blacklist categories.

    Stateless beyond the SQLite connection — safe to share across signals.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        categories: Optional[list[str]] = None,
    ) -> None:
        self.conn = conn
        self.categories = list(categories or DEFAULT_NEGATIVE_CATEGORIES)
        # Validate every category is known up front — fail loud not silent.
        unknown = [c for c in self.categories if c not in CATEGORIES]
        if unknown:
            raise ValueError(
                f"Unknown blacklist categories: {unknown}. "
                f"Known categories: {sorted(CATEGORIES.keys())}"
            )

    def has_recent_negative_event(
        self,
        stock_code: str,
        now: datetime,
        lookback_days: int,
    ) -> Optional[str]:
        """Returns the matching category name if any blacklist event happened
        within [now - lookback_days, now] for this stock, else None.

        Inclusive lower bound, inclusive upper bound. Excludes the future.
        """
        if not stock_code or not self.categories or lookback_days <= 0:
            return None
        cutoff = now - timedelta(days=lookback_days)
        # OR the WHERE fragments together so one query finds any hit.
        # Each fragment is a static SQL string from CATEGORIES (no user input).
        joined = " OR ".join(f"({CATEGORIES[c]})" for c in self.categories)
        sql = f"""
            SELECT report_name, receipt_datetime
            FROM disclosures
            WHERE stock_code = ?
              AND receipt_datetime >= ?
              AND receipt_datetime <= ?
              AND ({joined})
            ORDER BY receipt_datetime DESC
            LIMIT 1
        """
        row = self.conn.execute(
            sql, (stock_code, cutoff.isoformat(), now.isoformat())
        ).fetchone()
        if row is None:
            return None
        # Map the report_name back to a category by re-checking against the patterns.
        report_name = row[0] if isinstance(row, tuple) else row["report_name"]
        for cat in self.categories:
            # Quick re-check using the same SQL pattern via LIKE in Python is hard,
            # but a Python equivalent of the pattern strings is sufficient for naming.
            if _matches_category(report_name, cat):
                return cat
        # Fallback: shouldn't reach here, but return the first category if we did.
        return self.categories[0]


def _matches_category(report_name: str, category: str) -> bool:
    """Python-side equivalent of the SQL LIKE patterns in CATEGORIES."""
    if category == "halt_resumption":
        return ("매매거래정지" in report_name) or ("거래재개" in report_name)
    if category == "shareholder_change":
        return "최대주주" in report_name and "변경" in report_name and "정정" not in report_name
    if category == "supply_contract":
        return report_name.startswith("단일판매") and "정정" not in report_name and "해지" not in report_name
    if category == "buyback":
        return "자기주식" in report_name and "취득" in report_name and "처분" not in report_name and "정정" not in report_name
    if category == "rights_offering":
        return "유상증자" in report_name and "정정" not in report_name
    if category == "bonus_issue":
        return "무상증자" in report_name and "정정" not in report_name
    if category == "convertible_bond":
        return "전환사채" in report_name and "발행" in report_name and "정정" not in report_name
    return False
