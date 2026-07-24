"""Sub-second re-verify from live book cache — Phase 3."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from arb.book_cache import BookCache
from arb.config import ArbConfig
from arb.dutch_book import Opportunity
from arb.models import OppState, RejectReason
from arb.plan import BookSnapshot
from arb.scanner import (
    VerifyOutcome,
    build_buy_plan,
    opportunity_from_plan,
    reject_reason_for_plan,
)
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
    """Re-check executability from the live book cache via the plan builder (no REST)."""
    depth = config.plan_depth()
    snapshots: list[BookSnapshot] = []
    for token_id in opp.token_ids:
        cached = cache.get(token_id)
        if cached is None:
            return VerifyOutcome(None, RejectReason.NO_BOOK)
        snap = BookSnapshot.from_book(
            token_id,
            cached.to_rest_shape(),
            source="ws",
            captured_at=cached.updated_at,
            default_tick_size=config.assumed_tick_size,
            default_min_order_size=config.assumed_min_order_size,
        )
        if not snap.asks or snap.best_bid is None:
            return VerifyOutcome(None, RejectReason.MISSING_BID_ASK)
        snapshots.append(snap)

    ask_depth = min(s.ask_capacity(depth_levels=depth) for s in snapshots)
    bid_depth = min(
        round(sum(lvl.size for lvl in (s.bids if not depth or depth <= 0 else s.bids[:depth])), 8)
        for s in snapshots
    )

    plan = build_buy_plan(
        config,
        condition_id=opp.condition_id,
        slug=opp.slug,
        question=opp.question,
        outcomes=list(opp.outcomes),
        snapshots=snapshots,
    )
    if not plan.executable:
        reason = reject_reason_for_plan(plan.rejection.reason) if plan.rejection else RejectReason.OTHER
        return VerifyOutcome(None, reason, ask_depth, bid_depth, plan=plan)

    verified = replace(opportunity_from_plan(plan), source="ws_asks")
    return VerifyOutcome(verified, None, ask_depth, bid_depth, plan=plan)


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
