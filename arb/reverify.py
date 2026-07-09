"""Sub-second re-verify from live book cache — Phase 3."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from arb.book_cache import BookCache
from arb.config import ArbConfig
from arb.dutch_book import Opportunity, detect_from_asks, detect_from_bids
from arb.models import OppState, RejectReason
from arb.scanner import VerifyOutcome
from arb.state import OpportunityStore


@dataclass
class ReverifyResult:
    checked: int = 0
    still_valid: list[Opportunity] = field(default_factory=list)
    evaporated: list[tuple[Opportunity, RejectReason]] = field(default_factory=list)
    missing_book: list[Opportunity] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "still_valid": [o.to_dict() for o in self.still_valid],
            "evaporated": [
                {"opportunity": o.to_dict(), "reason": r.value} for o, r in self.evaporated
            ],
            "missing_book": [o.to_dict() for o in self.missing_book],
        }


def verify_from_cache(config: ArbConfig, cache: BookCache, opp: Opportunity) -> VerifyOutcome:
    """Re-check Dutch-book using cached top-of-book (no REST)."""
    asks: list[float] = []
    bids: list[float] = []
    ask_depth = 0.0
    bid_depth = 0.0

    for token_id in opp.token_ids:
        book = cache.get(token_id)
        if book is None:
            return VerifyOutcome(None, RejectReason.NO_BOOK)
        best_bid = book.best_bid if book.best_bid is not None else book.bids.best(side="bid")
        best_ask = book.best_ask if book.best_ask is not None else book.asks.best(side="ask")
        if best_ask is None or best_bid is None:
            return VerifyOutcome(None, RejectReason.MISSING_BID_ASK)
        asks.append(best_ask)
        bids.append(best_bid)
        ask_depth += book.asks.depth(side="ask")
        bid_depth += book.bids.depth(side="bid")

    if ask_depth < config.min_book_depth and bid_depth < config.min_book_depth:
        return VerifyOutcome(None, RejectReason.ILLIQUID, ask_depth, bid_depth)

    buy_opp = detect_from_asks(
        question=opp.question,
        slug=opp.slug,
        condition_id=opp.condition_id,
        outcomes=opp.outcomes,
        token_ids=opp.token_ids,
        asks=asks,
        min_edge=config.min_edge,
        fee_rate=config.fee_rate,
    )
    sell_opp = detect_from_bids(
        question=opp.question,
        slug=opp.slug,
        condition_id=opp.condition_id,
        outcomes=opp.outcomes,
        token_ids=opp.token_ids,
        bids=bids,
        min_edge=config.min_edge,
        fee_rate=config.fee_rate,
    )

    verified: Opportunity | None = None
    if buy_opp and sell_opp:
        verified = buy_opp if buy_opp.edge_bps >= sell_opp.edge_bps else sell_opp
    elif buy_opp:
        verified = buy_opp
    elif sell_opp:
        verified = sell_opp

    if verified is None:
        return VerifyOutcome(None, RejectReason.EDGE_EVAPORATED, ask_depth, bid_depth)

    source = "ws_asks" if verified.source.endswith("asks") else "ws_bids"
    verified = replace(verified, source=source)
    return VerifyOutcome(verified, None, ask_depth, bid_depth)


def reverify_opportunities(
    config: ArbConfig,
    cache: BookCache,
    opportunities: list[Opportunity],
) -> ReverifyResult:
    result = ReverifyResult()
    for opp in opportunities:
        result.checked += 1
        outcome = verify_from_cache(config, cache, opp)
        if outcome.opportunity is not None:
            result.still_valid.append(outcome.opportunity)
        elif outcome.reject_reason == RejectReason.NO_BOOK:
            result.missing_book.append(opp)
        else:
            result.evaporated.append((opp, outcome.reject_reason or RejectReason.OTHER))
    result.still_valid.sort(key=lambda o: o.edge_bps, reverse=True)
    return result


def _row_kind(row: dict) -> str:
    try:
        return json.loads(row["payload"]).get("kind", row.get("kind"))
    except Exception:
        return str(row.get("kind") or "")


def reverify_store_verified(
    config: ArbConfig,
    store: OpportunityStore,
    cache: BookCache,
    *,
    limit: int = 25,
    persist: bool = True,
) -> ReverifyResult:
    """Re-verify recent CLOB_VERIFIED rows against the live cache."""
    rows = store.recent(limit=limit, state=OppState.CLOB_VERIFIED)
    opps = [store.opportunity_from_row(r) for r in rows]
    result = reverify_opportunities(config, cache, opps)
    if not persist:
        return result

    row_by_key = {f"{r['condition_id']}:{_row_kind(r)}": int(r["id"]) for r in rows}
    for opp, reason in result.evaporated:
        key = f"{opp.condition_id}:{opp.kind.value}"
        oid = row_by_key.get(key)
        if oid is not None:
            store.transition(oid, OppState.REJECTED, reason=f"ws_reverify:{reason.value}")
    return result
