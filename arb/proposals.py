"""Threshold / config proposals — human must approve. Never auto-apply."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Proposal:
    id: str
    created_at: str
    key: str
    current_value: Any
    proposed_value: Any
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | approved | rejected | applied
    decided_at: str | None = None
    decided_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Proposal:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


class ProposalStore:
    """JSON file of proposals under state dir. Apply is explicit and separate."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[Proposal]:
        raw = json.loads(self.path.read_text())
        return [Proposal.from_dict(x) for x in raw]

    def _write(self, items: list[Proposal]) -> None:
        self.path.write_text(json.dumps([p.to_dict() for p in items], indent=2))

    def list(self, *, status: str | None = None) -> list[Proposal]:
        items = self._read()
        if status is None:
            return items
        return [p for p in items if p.status == status]

    def add(self, proposal: Proposal) -> Proposal:
        items = self._read()
        # de-dupe pending same key+proposed_value
        for existing in items:
            if (
                existing.status == "pending"
                and existing.key == proposal.key
                and existing.proposed_value == proposal.proposed_value
            ):
                return existing
        items.append(proposal)
        self._write(items)
        return proposal

    def decide(self, proposal_id: str, *, approve: bool, by: str = "human") -> Proposal:
        items = self._read()
        for p in items:
            if p.id == proposal_id:
                if p.status not in {"pending"}:
                    raise ValueError(f"proposal {proposal_id} is {p.status}, not pending")
                p.status = "approved" if approve else "rejected"
                p.decided_at = _now()
                p.decided_by = by
                self._write(items)
                return p
        raise KeyError(f"proposal {proposal_id} not found")

    def mark_applied(self, proposal_id: str) -> Proposal:
        items = self._read()
        for p in items:
            if p.id == proposal_id:
                if p.status != "approved":
                    raise ValueError("only approved proposals can be applied")
                p.status = "applied"
                p.decided_at = p.decided_at or _now()
                self._write(items)
                return p
        raise KeyError(f"proposal {proposal_id} not found")


def new_proposal(
    *,
    key: str,
    current_value: Any,
    proposed_value: Any,
    rationale: str,
    evidence: dict[str, Any] | None = None,
) -> Proposal:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return Proposal(
        id=f"prop_{ts}_{key}",
        created_at=_now(),
        key=key,
        current_value=current_value,
        proposed_value=proposed_value,
        rationale=rationale,
        evidence=evidence or {},
    )


def render_env_snippet(proposals: list[Proposal]) -> str:
    """Human-copyable env lines for approved proposals — never written automatically."""
    lines = ["# Approved proposals — copy into ~/.hermes/.env manually", ""]
    for p in proposals:
        if p.status != "approved":
            continue
        lines.append(f"# {p.rationale}")
        lines.append(f"{p.key}={p.proposed_value}")
        lines.append("")
    return "\n".join(lines)
