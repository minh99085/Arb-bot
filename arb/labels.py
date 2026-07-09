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
    """Outcome labels for learning — evidence-based, not vibes."""

    TRUE_ARB = "true_arb"  # reached FILLED/SETTLED/CLOSED with positive expected edge
    FALSE_POSITIVE = "false_positive"  # gamma/clob flag that evaporated or risk-rejected
    MISSED_THRESHOLD = "missed_threshold"  # near-miss below min edge (optional future)
    WS_EVAPORATED = "ws_evaporated"  # died on WS re-verify
    RISK_REJECTED = "risk_rejected"
    PAPER_WIN = "paper_win"
    PAPER_LOSS = "paper_loss"
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


def _classify(row: dict, fill: dict | None) -> tuple[Label, str]:
    state = row.get("state") or ""
    reason = (row.get("reject_reason") or "") or ""

    if state == OppState.REJECTED.value:
        if reason.startswith("ws_reverify"):
            return Label.WS_EVAPORATED, reason
        if reason in {
            "kill_switch",
            "study_mode",
            "max_open",
            "max_position",
            "daily_loss",
            "daily_trades",
            "duplicate_open",
            "insufficient_depth",
            "category_blocked",
            "below_min_edge",
        } or reason.startswith("risk"):
            return Label.RISK_REJECTED, reason or "risk"
        if reason in {"edge_evaporated", "illiquid", "missing_bid_ask", "no_book"}:
            return Label.FALSE_POSITIVE, reason
        return Label.FALSE_POSITIVE, reason or "rejected"

    if state in {OppState.FILLED.value, OppState.SETTLED.value, OppState.CLOSED.value}:
        if fill is not None:
            realized = fill.get("realized_pnl")
            expected = fill.get("expected_pnl")
            if realized is not None:
                if float(realized) >= 0:
                    return Label.PAPER_WIN, f"realized={realized}"
                return Label.PAPER_LOSS, f"realized={realized}"
            if expected is not None and float(expected) > 0:
                return Label.TRUE_ARB, f"expected={expected}"
        return Label.TRUE_ARB, state

    if state in {OppState.CLOB_VERIFIED.value, OppState.RISK_OK.value, OppState.ORDER_PLACED.value}:
        return Label.TRUE_ARB, f"open:{state}"

    if state == OppState.GAMMA_FLAG.value:
        return Label.FALSE_POSITIVE, "gamma_only_unverified"

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
