from __future__ import annotations

import json
import logging
import sqlite3

from kdtb.schemas.extraction import Extraction

logger = logging.getLogger(__name__)


class ExtractionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, ext: Extraction) -> int:
        """Insert (or replace by (disclosure_id, model_name)). Returns row id."""
        self.conn.execute(
            """
            DELETE FROM extractions
            WHERE disclosure_id = ? AND model_name = ?
            """,
            (ext.disclosure_id, ext.model_name),
        )
        cur = self.conn.execute(
            """
            INSERT INTO extractions (
                disclosure_id, model_name, prompt_version,
                event_type, direction, confidence,
                contract_value_krw, prior_year_revenue_krw, contract_to_revenue_ratio,
                is_new_contract, is_revision, is_cancellation,
                counterparty_name, counterparty_type,
                red_flags_json, summary, raw_llm_output,
                validation_status, validation_errors_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ext.disclosure_id, ext.model_name, ext.prompt_version,
                ext.event_type, ext.direction, ext.confidence,
                ext.contract_value_krw, ext.prior_year_revenue_krw, ext.contract_to_revenue_ratio,
                int(ext.is_new_contract) if ext.is_new_contract is not None else None,
                int(ext.is_revision) if ext.is_revision is not None else None,
                int(ext.is_cancellation) if ext.is_cancellation is not None else None,
                ext.counterparty_name,
                ext.counterparty_type,
                json.dumps(ext.red_flags, ensure_ascii=False),
                ext.summary,
                ext.raw_llm_output,
                ext.validation_status,
                json.dumps(ext.validation_errors, ensure_ascii=False),
            ),
        )
        return cur.lastrowid or 0

    def count_by_model(self, model_name: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM extractions WHERE model_name = ?", (model_name,)
        ).fetchone()[0]
