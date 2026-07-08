"""Persistent opportunity store for verifier audits."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from arb.dutch_book import Opportunity


class OpportunityStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detected_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    slug TEXT,
                    question TEXT,
                    edge_bps REAL NOT NULL,
                    total REAL NOT NULL,
                    source TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_opps_detected ON opportunities(detected_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_opps_condition ON opportunities(condition_id)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save(self, opp: Opportunity, *, verified: bool = False) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunities (
                    detected_at, kind, condition_id, slug, question,
                    edge_bps, total, source, verified, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    opp.kind.value,
                    opp.condition_id,
                    opp.slug,
                    opp.question,
                    opp.edge_bps,
                    opp.total,
                    opp.source,
                    1 if verified else 0,
                    json.dumps(opp.to_dict()),
                ),
            )
            return int(cur.lastrowid)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM opportunities
                ORDER BY detected_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM opportunities").fetchone()
        return int(row["c"]) if row else 0
