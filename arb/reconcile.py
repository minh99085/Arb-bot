"""Reconcile fills vs expected edge — Phase 2 verifier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from arb.config import ArbConfig
from arb.models import OppState
from arb.state import OpportunityStore


@dataclass
class ReconcileReport:
    fills: int
    open_positions: int
    expected_pnl_sum: float
    realized_pnl_sum: float
    pnl_gap: float
    settled: int
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fills": self.fills,
            "open_positions": self.open_positions,
            "expected_pnl_sum": self.expected_pnl_sum,
            "realized_pnl_sum": self.realized_pnl_sum,
            "pnl_gap": self.pnl_gap,
            "settled": self.settled,
            "notes": self.notes,
        }


def reconcile(
    config: ArbConfig,
    store: OpportunityStore,
    *,
    settle_paper: bool = True,
) -> ReconcileReport:
    """Compare expected vs realized PnL and optionally settle paper fills.

    Paper mode: expected_pnl is treated as realized at reconcile time (instant
    resolution assumption for study). Live mode will use actual settlement later.
    """
    fills = store.list_fills(limit=10_000)
    notes: list[str] = []
    expected = 0.0
    realized = 0.0
    settled = 0

    for fill in fills:
        expected += float(fill.get("expected_pnl") or 0.0)
        realized_val = fill.get("realized_pnl")
        if realized_val is None and settle_paper and fill.get("mode") == "paper":
            # Instant-settle paper: mark realized = expected, advance opp to SETTLED/CLOSED
            realized_val = float(fill.get("expected_pnl") or 0.0)
            store.set_fill_realized(int(fill["id"]), float(realized_val))
            opp_id = int(fill["opportunity_id"])
            store.transition(opp_id, OppState.SETTLED, reason="paper_reconcile")
            store.transition(opp_id, OppState.CLOSED, reason="paper_closed")
            settled += 1
        if realized_val is not None:
            realized += float(realized_val)

    open_n = store.count_open()
    gap = round(realized - expected, 6)
    if abs(gap) > 1e-6:
        notes.append(f"PnL gap expected vs realized: {gap}")
    else:
        notes.append("PnL gap within tolerance")

    if config.kill_switch:
        notes.append("KILL SWITCH is ON — no new trades")

    report = ReconcileReport(
        fills=len(fills),
        open_positions=open_n,
        expected_pnl_sum=round(expected, 6),
        realized_pnl_sum=round(realized, 6),
        pnl_gap=gap,
        settled=settled,
        notes=notes,
    )

    # Append to ledger
    _append_reconcile_ledger(config, report)
    return report


def _append_reconcile_ledger(config: ArbConfig, report: ReconcileReport) -> None:
    path = config.ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    block = (
        f"## Reconcile — {now}\n\n"
        f"- Fills: {report.fills}\n"
        f"- Settled this run: {report.settled}\n"
        f"- Open positions: {report.open_positions}\n"
        f"- Expected PnL: ${report.expected_pnl_sum:.4f}\n"
        f"- Realized PnL: ${report.realized_pnl_sum:.4f}\n"
        f"- Gap: ${report.pnl_gap:.4f}\n"
        f"- Notes: {'; '.join(report.notes)}\n\n"
        "---\n\n"
    )
    existing = path.read_text() if path.exists() else "# Polymarket Arb Ledger\n\n"
    path.write_text(existing + block)
