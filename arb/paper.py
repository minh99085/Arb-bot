"""Paper fills as shadow simulations built from the complete-set plan — Phase 2.

Paper fills are SHADOW simulations from captured book snapshots. They are never
realized PnL. The dollar-per-leg / top-of-book arithmetic is gone: every paper
fill is a real ``BUY_COMPLETE_SET_MERGE`` plan with one common share quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.plan import (
    STRATEGY_BUY_COMPLETE_SET_MERGE,
    BookLevel,
    BookSnapshot,
    CompleteSetPlan,
    build_complete_set_plan,
    simulate_shadow_fill,
)

# Big sentinel depth for a top-of-book-only snapshot: the cash budget, not the
# (unknown) depth, is what bounds a paper fill from a bare Opportunity.
_TOB_DEPTH = 1e12


@dataclass(frozen=True)
class PaperFill:
    opportunity: Opportunity
    size_usd: float
    fill_prices: list[float]
    fill_total: float
    fees_usd: float
    slippage_usd: float
    expected_pnl: float
    q_complete_sets: float = 0.0
    label: str = "shadow"          # never realized
    mode: str = "paper"


def _plan_from_opportunity(config: ArbConfig, opp: Opportunity, *, size_usd: float) -> CompleteSetPlan:
    """Build a top-of-book complete-set plan from a bare Opportunity (shadow)."""
    now = datetime.now(timezone.utc).isoformat()
    snapshots = [
        BookSnapshot(
            token_id=str(tid),
            asks=(BookLevel(price=float(price), size=_TOB_DEPTH),),
            bids=(),
            captured_at=now,
            source="paper_top_of_book",
            tick_size=config.assumed_tick_size,
            min_order_size=0.0,  # paper simulation does not enforce min order size
            neg_risk=False,
        )
        for tid, price in zip(opp.token_ids, opp.prices)
    ]
    return build_complete_set_plan(
        condition_id=opp.condition_id,
        slug=opp.slug,
        question=opp.question,
        outcomes=list(opp.outcomes),
        snapshots=snapshots,
        fee_model=config.fee_model(),
        cash_budget_usd=size_usd,
        now_iso=now,
        max_book_age_sec=config.max_book_age_sec,
        conversion_cost_usd=config.conversion_cost_usd,
        depth_levels=None,
        require_tick=True,
        # Paper accounting reports the honest (possibly negative) net; it does
        # not reject on non-positive PnL — the risk gate already screened edge.
        min_net_pnl_usd=float("-inf"),
    )


def paper_fill_from_plan(opp: Opportunity, plan: CompleteSetPlan) -> PaperFill:
    """Produce a shadow PaperFill from an already-built complete-set plan."""
    shadow = simulate_shadow_fill(plan, label="shadow")
    fill_prices = [leg.vwap_ask for leg in plan.legs]
    return PaperFill(
        opportunity=opp,
        size_usd=plan.gross_notional_usd,
        fill_prices=fill_prices,
        fill_total=round(sum(fill_prices), 6),
        fees_usd=plan.fees_usd,
        slippage_usd=plan.slippage_usd,
        expected_pnl=shadow.net_cash_pnl_usd,  # shadow — never realized
        q_complete_sets=plan.q_complete_sets,
        label=shadow.label,
        mode="paper",
    )


def simulate_paper_fill(
    config: ArbConfig,
    opp: Opportunity,
    *,
    size_usd: float,
    plan: CompleteSetPlan | None = None,
) -> PaperFill:
    """Simulate a shadow complete-set fill for ``size_usd`` of cash budget.

    Only BUY_COMPLETE_SET_MERGE is simulated. If a pre-built L2/L3 plan is
    supplied (e.g. from a live CLOB refresh) it is used directly; otherwise a
    top-of-book plan is built from the opportunity's quoted prices.
    """
    if opp.kind != ArbKind.BUY_BUNDLE:
        raise ValueError(
            f"paper fills only support {STRATEGY_BUY_COMPLETE_SET_MERGE}; got {opp.kind.value}"
        )
    if plan is None:
        plan = _plan_from_opportunity(config, opp, size_usd=size_usd)
    return paper_fill_from_plan(opp, plan)
