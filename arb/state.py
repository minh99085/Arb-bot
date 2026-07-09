"""Persistent opportunity store, scan runs, and study metrics."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from arb.dutch_book import Opportunity
from arb.models import OppState, RejectReason


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    updated_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    slug TEXT,
                    question TEXT,
                    edge_bps REAL NOT NULL,
                    total REAL NOT NULL,
                    source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    reject_reason TEXT,
                    ask_depth REAL,
                    bid_depth REAL,
                    hypothetical_pnl REAL,
                    scan_run_id INTEGER,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    gamma_hits INTEGER NOT NULL DEFAULT 0,
                    verified_hits INTEGER NOT NULL DEFAULT 0,
                    rejected INTEGER NOT NULL DEFAULT 0,
                    study_mode INTEGER NOT NULL DEFAULT 1,
                    notes TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    reason TEXT,
                    FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_opps_detected ON opportunities(detected_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_opps_condition ON opportunities(condition_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_opps_state ON opportunities(state)"
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        additions = {
            "updated_at": "TEXT",
            "state": "TEXT",
            "reject_reason": "TEXT",
            "ask_depth": "REAL",
            "bid_depth": "REAL",
            "hypothetical_pnl": "REAL",
            "scan_run_id": "INTEGER",
        }
        for name, typ in additions.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE opportunities ADD COLUMN {name} {typ}")
        # Backfill legacy rows
        conn.execute(
            """
            UPDATE opportunities
            SET state = CASE WHEN verified = 1 THEN ? ELSE ? END
            WHERE state IS NULL OR state = ''
            """,
            (OppState.CLOB_VERIFIED.value, OppState.GAMMA_FLAG.value),
        )
        conn.execute(
            """
            UPDATE opportunities
            SET updated_at = detected_at
            WHERE updated_at IS NULL OR updated_at = ''
            """
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

    def start_scan_run(self, *, study_mode: bool = True) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO scan_runs (started_at, study_mode)
                VALUES (?, ?)
                """,
                (_now(), 1 if study_mode else 0),
            )
            return int(cur.lastrowid)

    def finish_scan_run(
        self,
        run_id: int,
        *,
        scanned: int,
        gamma_hits: int,
        verified_hits: int,
        rejected: int,
        notes: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scan_runs
                SET finished_at = ?, scanned = ?, gamma_hits = ?,
                    verified_hits = ?, rejected = ?, notes = ?
                WHERE id = ?
                """,
                (_now(), scanned, gamma_hits, verified_hits, rejected, notes, run_id),
            )

    def save(
        self,
        opp: Opportunity,
        *,
        state: OppState,
        verified: bool = False,
        reject_reason: RejectReason | None = None,
        ask_depth: float | None = None,
        bid_depth: float | None = None,
        hypothetical_pnl: float | None = None,
        scan_run_id: int | None = None,
    ) -> int:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunities (
                    detected_at, updated_at, kind, condition_id, slug, question,
                    edge_bps, total, source, state, verified, reject_reason,
                    ask_depth, bid_depth, hypothetical_pnl, scan_run_id, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    opp.kind.value,
                    opp.condition_id,
                    opp.slug,
                    opp.question,
                    opp.edge_bps,
                    opp.total,
                    opp.source,
                    state.value,
                    1 if verified else 0,
                    reject_reason.value if reject_reason else None,
                    ask_depth,
                    bid_depth,
                    hypothetical_pnl,
                    scan_run_id,
                    json.dumps(opp.to_dict()),
                ),
            )
            opp_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO transitions (opportunity_id, at, from_state, to_state, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    opp_id,
                    now,
                    OppState.DISCOVERED.value,
                    state.value,
                    reject_reason.value if reject_reason else None,
                ),
            )
            return opp_id

    def recent(self, limit: int = 20, *, state: OppState | None = None) -> list[dict]:
        with self._connect() as conn:
            if state is None:
                rows = conn.execute(
                    """
                    SELECT * FROM opportunities
                    ORDER BY detected_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM opportunities
                    WHERE state = ?
                    ORDER BY detected_at DESC
                    LIMIT ?
                    """,
                    (state.value, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def count(self, *, state: OppState | None = None) -> int:
        with self._connect() as conn:
            if state is None:
                row = conn.execute("SELECT COUNT(*) AS c FROM opportunities").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM opportunities WHERE state = ?",
                    (state.value,),
                ).fetchone()
        return int(row["c"]) if row else 0

    def study_summary(self, *, days: int = 30) -> dict[str, Any]:
        """Aggregate study-mode stats for go/no-go gate."""
        with self._connect() as conn:
            runs = conn.execute(
                """
                SELECT COUNT(*) AS c,
                       COALESCE(SUM(scanned), 0) AS scanned,
                       COALESCE(SUM(gamma_hits), 0) AS gamma_hits,
                       COALESCE(SUM(verified_hits), 0) AS verified_hits,
                       COALESCE(SUM(rejected), 0) AS rejected
                FROM scan_runs
                WHERE started_at >= datetime('now', ?)
                """,
                (f"-{days} days",),
            ).fetchone()
            by_reject = conn.execute(
                """
                SELECT reject_reason, COUNT(*) AS c
                FROM opportunities
                WHERE state = ? AND detected_at >= datetime('now', ?)
                GROUP BY reject_reason
                ORDER BY c DESC
                """,
                (OppState.REJECTED.value, f"-{days} days"),
            ).fetchall()
            hyp = conn.execute(
                """
                SELECT COALESCE(SUM(hypothetical_pnl), 0) AS pnl,
                       COUNT(*) AS n
                FROM opportunities
                WHERE state = ? AND detected_at >= datetime('now', ?)
                """,
                (OppState.CLOB_VERIFIED.value, f"-{days} days"),
            ).fetchone()
        return {
            "days": days,
            "scan_runs": int(runs["c"] or 0),
            "markets_scanned": int(runs["scanned"] or 0),
            "gamma_hits": int(runs["gamma_hits"] or 0),
            "verified_hits": int(runs["verified_hits"] or 0),
            "rejected": int(runs["rejected"] or 0),
            "reject_breakdown": {
                (row["reject_reason"] or "unknown"): int(row["c"]) for row in by_reject
            },
            "hypothetical_pnl_sum": float(hyp["pnl"] or 0.0),
            "verified_count": int(hyp["n"] or 0),
            "go_no_go": {
                "phase2_gate": ">=10 CLOB-verified signals/week",
                "verified_hits_in_window": int(runs["verified_hits"] or 0),
                "ready_for_phase2": int(runs["verified_hits"] or 0) >= 10 and days >= 7,
            },
        }

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scan_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
