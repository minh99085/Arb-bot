"""Tests for top_by_edge trade selection."""

from pathlib import Path

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.models import OppState
from arb.state import OpportunityStore


def _opp(edge_bps: float, cid: str) -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id=cid,
        slug="s",
        question=f"Edge {edge_bps}?",
        outcomes=["Y", "N"],
        token_ids=["a", "b"],
        prices=[0.4, 0.45],
        total=0.85,
        edge=edge_bps / 10_000,
        edge_bps=edge_bps,
        source="clob_asks",
    )


def test_top_by_edge_orders_by_bps(tmp_path: Path):
    store = OpportunityStore(tmp_path / "db.sqlite")
    store.save(_opp(50, "0x1"), state=OppState.CLOB_VERIFIED, verified=True)
    store.save(_opp(200, "0x2"), state=OppState.CLOB_VERIFIED, verified=True)
    store.save(_opp(100, "0x3"), state=OppState.CLOB_VERIFIED, verified=True)
    rows = store.top_by_edge(limit=2)
    assert len(rows) == 2
    assert rows[0]["edge_bps"] == 200.0
    assert rows[1]["edge_bps"] == 100.0
