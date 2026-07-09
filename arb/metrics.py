"""Simple counters for Phase 1 study metrics."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ScanMetrics:
    started_at: str = ""
    finished_at: str = ""
    scanned: int = 0
    gamma_hits: int = 0
    verified_hits: int = 0
    rejected: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def bump_reject(self, reason: str) -> None:
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


def start_metrics() -> ScanMetrics:
    return ScanMetrics(started_at=datetime.now(timezone.utc).isoformat())


def finish_metrics(m: ScanMetrics) -> ScanMetrics:
    m.finished_at = datetime.now(timezone.utc).isoformat()
    try:
        start = datetime.fromisoformat(m.started_at)
        end = datetime.fromisoformat(m.finished_at)
        m.duration_seconds = round((end - start).total_seconds(), 3)
    except ValueError:
        m.duration_seconds = 0.0
    return m
