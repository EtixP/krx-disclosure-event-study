from __future__ import annotations

import json
import sqlite3
from typing import Optional

from kdtb.schemas.signal import Signal


class SignalStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, sig: Signal) -> int:
        """Replace any prior row with the same signal_id, then insert. Returns row id."""
        self.conn.execute("DELETE FROM signals WHERE signal_id = ?", (sig.signal_id,))
        cur = self.conn.execute(
            """
            INSERT INTO signals (
                signal_id, disclosure_id, extraction_id, stock_code, strategy_name,
                direction, strength, reason_codes_json, entry_type, max_entry_price,
                stop_loss_pct, take_profit_pct, time_exit, notional_krw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sig.signal_id, sig.disclosure_id, sig.extraction_id, sig.stock_code,
                sig.strategy_name, sig.direction, sig.strength,
                json.dumps(sig.reason_codes, ensure_ascii=False),
                sig.entry_type, sig.max_entry_price,
                sig.stop_loss_pct, sig.take_profit_pct, sig.time_exit, sig.notional_krw,
            ),
        )
        return cur.lastrowid or 0

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    def count_by_strategy(self, name: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy_name = ?", (name,)
        ).fetchone()[0]

    def clear_strategy(self, name: str) -> int:
        cur = self.conn.execute("DELETE FROM signals WHERE strategy_name = ?", (name,))
        return cur.rowcount
