"""Label historical opportunities for the intelligence plane — Phase 4."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from arb.models import OppState
from arb.state import OpportunityStore


class Label(str, Enum):
    """Honest outcome labels — no label may claim a win or a realized arb.

    A candidate, CLOB-verified, risk-approved, order-posted, or merely
    expected-positive record is NOT a win and NOT a realized arbitrage. Those
    map to CANDIDATE / SHADOW / UNRESOLVED. Money-claiming labels do not exist
    in this phase because there is no verified settlement model yet.
    """

    CANDIDATE = "candidate"  # detected but unverified (gamma flag)
    SHADOW = "shadow"  # CLOB-verified observation, not executed
    UNRESOLVED = "unresolved"  # entered execution pipeline; outcome not settled
    PARTIAL = "partial"  # partial fill — not a complete, settled bundle
    REJECTED = "rejected"  # risk/verify/unsupported rejection
    FALSE_POSITIVE = "false_positive"  # gamma/clob flag that evaporated
    WS_EVAPORATED = "ws_evaporated"  # died on WS re-verify
    LEGACY_SYNTHETIC = "legacy_synthetic"  # old auto-settled synthetic record
    UNKNOWN = "unknown"


@dataclass
class LabeledRow:
    opportunity_id: int
    detected_at: str
    kind: str
    condition_id: str
    slug: str
    question: str
    state: str
    edge_bps: float
    source: str
    reject_reason: str | None
    label: Label
    label_detail: str
    expected_pnl: float | None = None
    realized_pnl: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label.value
        return data


def is_legacy_synthetic(fill: dict | None) -> bool:
    """A fill carrying a realized_pnl is a legacy synthetic record.

    The removed instant-settle reconcile path is the only thing that ever wrote
    realized_pnl onto a (paper) fill. Going forward paper fills stay unresolved,
    so any realized_pnl present marks a pre-safety-phase synthetic record.
    """
    return bool(fill is not None and fill.get("realized_pnl") is not None)


def _classify(row: dict, fill: dict | None) -> tuple[Label, str]:
    state = row.get("state") or ""
    reason = (row.get("reject_reason") or "") or ""

    # Legacy synthetic records (old auto-settled paper fills) are never a win.
    if is_legacy_synthetic(fill):
        return Label.LEGACY_SYNTHETIC, f"legacy_realized={fill.get('realized_pnl')}"

    if state == OppState.REJECTED.value:
        if reason.startswith("ws_reverify"):
            return Label.WS_EVAPORATED, reason
        if reason in {"edge_evaporated", "illiquid", "missing_bid_ask", "no_book"}:
            return Label.FALSE_POSITIVE, reason
        # risk / unsupported-strategy / scan-only / other rejections
        return Label.REJECTED, reason or "rejected"

    # Reached the execution pipeline but there is no settlement model: an open
    # or "filled" paper/live position is UNRESOLVED, never a win/true arb.
    if state in {
        OppState.RISK_OK.value,
        OppState.ORDER_PLACED.value,
        OppState.FILLED.value,
        OppState.SETTLED.value,
        OppState.CLOSED.value,
    }:
        return Label.UNRESOLVED, f"open:{state}"

    # CLOB-verified is a shadow observation — verified, but not executed.
    if state == OppState.CLOB_VERIFIED.value:
        return Label.SHADOW, "clob_verified"

    # Gamma flag is an unverified candidate.
    if state == OppState.GAMMA_FLAG.value:
        return Label.CANDIDATE, "gamma_only_unverified"

    return Label.UNKNOWN, state or "unknown"


def label_history(
    store: OpportunityStore,
    *,
    days: int = 30,
    limit: int = 5_000,
) -> list[LabeledRow]:
    """Label recent opportunities using state + fills."""
    rows = store.list_since(days=days, limit=limit)
    fills_by_opp: dict[int, dict] = {}
    for fill in store.list_fills(limit=10_000):
        oid = int(fill["opportunity_id"])
        # keep latest fill per opp
        if oid not in fills_by_opp:
            fills_by_opp[oid] = fill

    labeled: list[LabeledRow] = []
    for row in rows:
        oid = int(row["id"])
        fill = fills_by_opp.get(oid)
        label, detail = _classify(row, fill)
        labeled.append(
            LabeledRow(
                opportunity_id=oid,
                detected_at=str(row.get("detected_at") or ""),
                kind=str(row.get("kind") or ""),
                condition_id=str(row.get("condition_id") or ""),
                slug=str(row.get("slug") or ""),
                question=str(row.get("question") or ""),
                state=str(row.get("state") or ""),
                edge_bps=float(row.get("edge_bps") or 0.0),
                source=str(row.get("source") or ""),
                reject_reason=row.get("reject_reason"),
                label=label,
                label_detail=detail,
                expected_pnl=float(fill["expected_pnl"]) if fill and fill.get("expected_pnl") is not None else None,
                realized_pnl=float(fill["realized_pnl"]) if fill and fill.get("realized_pnl") is not None else None,
            )
        )
    return labeled


def export_dataset(labeled: list[LabeledRow], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in labeled:
            f.write(json.dumps(row.to_dict()) + "\n")
    return path


def label_counts(labeled: list[LabeledRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in labeled:
        counts[row.label.value] = counts.get(row.label.value, 0) + 1
    return counts
