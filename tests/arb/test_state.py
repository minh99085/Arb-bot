"""Tests for opportunity persistence."""

import json
from pathlib import Path

from arb.dutch_book import ArbKind, Opportunity
from arb.state import OpportunityStore


def _sample_opportunity() -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xabc",
        slug="test-market",
        question="Test?",
        outcomes=["Yes", "No"],
        token_ids=["a", "b"],
        prices=[0.45, 0.50],
        total=0.95,
        edge=0.04,
        edge_bps=400.0,
        source="gamma",
    )


def test_store_roundtrip(tmp_path: Path):
    db = tmp_path / "opps.sqlite"
    store = OpportunityStore(db)
    row_id = store.save(_sample_opportunity(), verified=True)
    assert row_id == 1
    assert store.count() == 1
    rows = store.recent()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["kind"] == "buy_bundle"
    assert rows[0]["verified"] == 1
