"""Phase 3 book cache and WS re-verify tests (no live network)."""

from pathlib import Path

from arb.book_cache import BookCache
from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.models import OppState, RejectReason
from arb.reverify import reverify_opportunities, reverify_store_verified, verify_from_cache
from arb.state import OpportunityStore


def _opp(prices=None) -> Opportunity:
    prices = prices or [0.40, 0.45]
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xws",
        slug="ws-test",
        question="WS reverify?",
        outcomes=["Yes", "No"],
        token_ids=["tok_yes", "tok_no"],
        prices=prices,
        total=sum(prices),
        edge=max(0.0, 1.0 - sum(prices) - 0.01),
        edge_bps=max(0.0, (1.0 - sum(prices) - 0.01) * 10_000),
        source="gamma",
    )


def test_book_cache_snapshot_and_price_change():
    cache = BookCache()
    cache.apply_event(
        {
            "event_type": "book",
            "asset_id": "tok_yes",
            "market": "0xws",
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.42", "size": "80"}],
        }
    )
    book = cache.get("tok_yes")
    assert book is not None
    assert book.best_bid == 0.40
    assert book.best_ask == 0.42

    cache.apply_event(
        {
            "event_type": "price_change",
            "market": "0xws",
            "price_changes": [
                {
                    "asset_id": "tok_yes",
                    "price": "0.41",
                    "size": "50",
                    "side": "SELL",
                    "best_bid": "0.40",
                    "best_ask": "0.41",
                }
            ],
        }
    )
    book = cache.get("tok_yes")
    assert book.best_ask == 0.41
    assert "0.41" in book.asks.levels


def test_book_cache_remove_level_size_zero():
    cache = BookCache()
    cache.apply_event(
        {
            "event_type": "book",
            "asset_id": "t1",
            "bids": [{"price": "0.5", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    cache.apply_event(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "t1", "price": "0.5", "size": "0", "side": "BUY"}
            ],
        }
    )
    assert cache.get("t1").bids.levels == {}


def test_verify_from_cache_buy_bundle():
    cfg = ArbConfig(min_edge_bps=50.0, min_book_depth=1.0)
    cache = BookCache()
    cache.apply_event(
        {
            "event_type": "book",
            "asset_id": "tok_yes",
            "bids": [{"price": "0.40", "size": "50"}],
            "asks": [{"price": "0.42", "size": "50"}],
        }
    )
    cache.apply_event(
        {
            "event_type": "book",
            "asset_id": "tok_no",
            "bids": [{"price": "0.45", "size": "50"}],
            "asks": [{"price": "0.48", "size": "50"}],
        }
    )
    # asks 0.42+0.48=0.90 → buy bundle edge
    outcome = verify_from_cache(cfg, cache, _opp())
    assert outcome.opportunity is not None
    assert outcome.opportunity.source.startswith("ws_")
    assert abs(outcome.opportunity.total - 0.90) < 1e-9


def test_verify_from_cache_edge_evaporated():
    cfg = ArbConfig(min_edge_bps=50.0, min_book_depth=1.0)
    cache = BookCache()
    # Mid-market books: no buy (asks sum ~1.1) and no sell (bids sum ~0.9)
    for tid, bid, ask in (
        ("tok_yes", "0.45", "0.55"),
        ("tok_no", "0.45", "0.55"),
    ):
        cache.apply_event(
            {
                "event_type": "book",
                "asset_id": tid,
                "bids": [{"price": bid, "size": "10"}],
                "asks": [{"price": ask, "size": "10"}],
            }
        )
    outcome = verify_from_cache(cfg, cache, _opp())
    assert outcome.opportunity is None
    assert outcome.reject_reason == RejectReason.EDGE_EVAPORATED


def test_reverify_store_marks_rejected(tmp_path: Path):
    cfg = ArbConfig(state_dir=tmp_path, min_edge_bps=50.0, min_book_depth=1.0)
    store = OpportunityStore(cfg.state_db)
    opp = _opp()
    oid = store.save(opp, state=OppState.CLOB_VERIFIED, verified=True)
    cache = BookCache()
    for tid, bid, ask in (
        ("tok_yes", "0.40", "0.60"),
        ("tok_no", "0.40", "0.60"),
    ):
        cache.apply_event(
            {
                "event_type": "book",
                "asset_id": tid,
                "bids": [{"price": bid, "size": "10"}],
                "asks": [{"price": ask, "size": "10"}],
            }
        )
    result = reverify_store_verified(cfg, store, cache, limit=10, persist=True)
    assert result.checked == 1
    assert len(result.evaporated) == 1
    assert store.get(oid)["state"] == OppState.REJECTED.value


def test_reverify_opportunities_batch():
    cfg = ArbConfig(min_edge_bps=50.0, min_book_depth=1.0)
    cache = BookCache()
    for tid, ask in (("tok_yes", "0.40"), ("tok_no", "0.45")):
        cache.apply_event(
            {
                "event_type": "book",
                "asset_id": tid,
                "bids": [{"price": "0.35", "size": "20"}],
                "asks": [{"price": ask, "size": "20"}],
            }
        )
    result = reverify_opportunities(cfg, cache, [_opp()])
    assert len(result.still_valid) == 1
