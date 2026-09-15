from __future__ import annotations

import sqlite3
from typing import Any

from kdtb.events.normalizer import DART_UPDATE_PREFIX_ACTIONS


def _prefix_values_sql() -> tuple[str, tuple[str, ...]]:
    rows = ",".join("(?,?)" for _ in DART_UPDATE_PREFIX_ACTIONS)
    params = tuple(
        value
        for prefix, action in DART_UPDATE_PREFIX_ACTIONS.items()
        for value in (prefix, action)
    )
    return rows, params


def audit_normalization_prevalence(conn: sqlite3.Connection) -> dict[str, Any]:
    """Measure update/collision prevalence without assigning canonical lineage."""

    total, first_date, last_date = conn.execute(
        """
        SELECT COUNT(*), MIN(receipt_datetime), MAX(receipt_datetime)
        FROM disclosures
        """
    ).fetchone()

    values_sql, prefix_params = _prefix_values_sql()
    prefix_rows = conn.execute(
        f"""
        WITH update_prefixes(prefix, action) AS (VALUES {values_sql})
        SELECT u.prefix, u.action, COUNT(d.receipt_no) AS filings
        FROM update_prefixes u
        LEFT JOIN disclosures d
          ON substr(d.report_name, 1, length(u.prefix)) = u.prefix
        GROUP BY u.prefix, u.action
        ORDER BY u.prefix
        """,
        prefix_params,
    ).fetchall()

    action_counts: dict[str, int] = {}
    prefix_counts: dict[str, dict[str, Any]] = {}
    for prefix, action, filings in prefix_rows:
        count = int(filings)
        prefix_counts[prefix] = {"action": action, "filings": count}
        if count:
            action_counts[action] = action_counts.get(action, 0) + count
    update_filings = sum(action_counts.values())

    exact_title_row = conn.execute(
        f"""
        WITH update_prefixes(prefix, action) AS (VALUES {values_sql}),
        normalized AS MATERIALIZED (
            SELECT
                d.receipt_no,
                d.corp_code,
                CASE
                    WHEN u.prefix IS NULL THEN trim(d.report_name)
                    ELSE trim(substr(d.report_name, length(u.prefix) + 1))
                END AS normalized_title,
                u.action
            FROM disclosures d
            LEFT JOIN update_prefixes u
              ON substr(d.report_name, 1, length(u.prefix)) = u.prefix
        ),
        groups AS MATERIALIZED (
            SELECT
                corp_code,
                normalized_title,
                MIN(CASE WHEN action IS NULL THEN receipt_no END) AS first_original,
                COUNT(*) AS group_size
            FROM normalized
            GROUP BY corp_code, normalized_title
        )
        SELECT
            COUNT(*),
            SUM(g.first_original IS NOT NULL AND g.first_original < n.receipt_no),
            SUM(g.first_original IS NULL OR g.first_original >= n.receipt_no),
            SUM(g.group_size > 2)
        FROM normalized n
        JOIN groups g USING (corp_code, normalized_title)
        WHERE n.action IS NOT NULL
        """,
        prefix_params,
    ).fetchone()

    collisions = conn.execute(
        """
        WITH collision_groups AS (
            SELECT COUNT(*) AS group_size
            FROM disclosures
            GROUP BY corp_code, report_name, date(receipt_datetime)
            HAVING COUNT(*) > 1
        )
        SELECT COUNT(*), COALESCE(SUM(group_size), 0),
               COALESCE(SUM(group_size - 1), 0)
        FROM collision_groups
        """
    ).fetchone()

    cancellation_rows = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM disclosures
            WHERE instr(report_name, '철회') > 0
               OR instr(report_name, '취소') > 0
               OR instr(report_name, '해지') > 0
            """
        ).fetchone()[0]
    )
    remark_flags = conn.execute(
        """
        SELECT
            SUM(instr(COALESCE(json_extract(raw_payload_json, '$.rm'), ''), '정') > 0),
            SUM(instr(COALESCE(json_extract(raw_payload_json, '$.rm'), ''), '철') > 0)
        FROM disclosures
        """
    ).fetchone()

    return {
        "corpus": {
            "disclosures": int(total),
            "first_receipt_datetime": first_date,
            "last_receipt_datetime": last_date,
        },
        "update_prefixes": prefix_counts,
        "update_actions": dict(sorted(action_counts.items())),
        "update_prevalence": {
            "filings": update_filings,
            "fraction_of_corpus": round(update_filings / total, 8) if total else 0.0,
        },
        "title_based_linkage_diagnostic_only": {
            "updates_with_prior_same_issuer_exact_normalized_title": int(
                exact_title_row[1] or 0
            ),
            "updates_unresolved_by_that_rule": int(exact_title_row[2] or 0),
            "updates_in_title_groups_larger_than_two": int(exact_title_row[3] or 0),
            "warning": (
                "Exact-title proximity is not lineage. DART viewer family/att/ref "
                "relationships are required for canonical assignment."
            ),
        },
        "same_issuer_title_date_collisions": {
            "groups": int(collisions[0]),
            "rows": int(collisions[1]),
            "rows_beyond_one_per_group": int(collisions[2]),
            "warning": "Collision counts are a prevalence proxy, not duplicate labels.",
        },
        "status_markers": {
            "title_contains_cancellation_marker": cancellation_rows,
            "dart_rm_contains_later_correction_flag": int(remark_flags[0] or 0),
            "dart_rm_contains_withdrawal_flag": int(remark_flags[1] or 0),
            "warning": (
                "rm flags describe later provider state and are not used as "
                "decision-time event status."
            ),
        },
    }
