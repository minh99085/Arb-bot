"""Scan Polymarket for Dutch-book opportunities (Phase 1 study mode)."""

from __future__ import annotations

from dataclasses import dataclass, field

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity, detect_from_prices
from arb.ledger import append_ledger
from arb.metrics import finish_metrics, start_metrics
from arb.models import OppState, RejectReason
from arb.plan import (
    BookSnapshot,
    CompleteSetPlan,
    PlanRejectReason,
    build_complete_set_plan,
)
from arb.polymarket_data import (
    fetch_orderbook,
    iter_markets,
    market_tokens,
)
from arb.state import OpportunityStore

# Map a plan rejection to the pipeline's RejectReason vocabulary.
_PLAN_REJECT_MAP: dict[PlanRejectReason, RejectReason] = {
    PlanRejectReason.NO_BOOK: RejectReason.NO_BOOK,
    PlanRejectReason.STALE_BOOK: RejectReason.STALE_BOOK,
    PlanRejectReason.MISSING_OUTCOME: RejectReason.MISSING_BID_ASK,
    PlanRejectReason.INVALID_TICK: RejectReason.INVALID_TICK,
    PlanRejectReason.BELOW_MIN_SIZE: RejectReason.BELOW_MIN_SIZE,
    PlanRejectReason.INVALID_PRICES: RejectReason.INVALID_PRICES,
    PlanRejectReason.NEG_RISK_INELIGIBLE: RejectReason.UNSUPPORTED,
    PlanRejectReason.INELIGIBLE_STRATEGY: RejectReason.UNSUPPORTED,
    PlanRejectReason.UNKNOWN_FEE: RejectReason.UNKNOWN_FEE,
    PlanRejectReason.INSUFFICIENT_DEPTH: RejectReason.ILLIQUID,
    PlanRejectReason.NEGATIVE_NET_PNL: RejectReason.EDGE_EVAPORATED,
    PlanRejectReason.ZERO_BUDGET: RejectReason.OTHER,
    PlanRejectReason.OTHER: RejectReason.OTHER,
}


def reject_reason_for_plan(reason: PlanRejectReason) -> RejectReason:
    return _PLAN_REJECT_MAP.get(reason, RejectReason.OTHER)


def build_buy_plan(
    config: ArbConfig,
    *,
    condition_id: str,
    slug: str,
    question: str,
    outcomes: list[str],
    snapshots: list[BookSnapshot],
) -> CompleteSetPlan:
    """Build a BUY_COMPLETE_SET_MERGE plan from book snapshots using the config."""
    return build_complete_set_plan(
        condition_id=condition_id,
        slug=slug,
        question=question,
        outcomes=list(outcomes),
        snapshots=snapshots,
        fee_model=config.fee_model(),
        cash_budget_usd=config.max_position_usd,
        max_book_age_sec=config.max_book_age_sec,
        conversion_cost_usd=config.conversion_cost_usd,
        depth_levels=config.plan_depth(),
        require_tick=True,
    )


def opportunity_from_plan(plan: CompleteSetPlan, *, source: str = "clob_asks") -> Opportunity:
    """Derive an executable BUY_BUNDLE Opportunity from a valid complete-set plan."""
    prices = [leg.vwap_ask for leg in plan.legs]
    token_ids = [leg.token_id for leg in plan.legs]
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id=plan.condition_id,
        slug=plan.slug,
        question=plan.question,
        outcomes=list(plan.outcomes),
        token_ids=token_ids,
        prices=prices,
        total=round(sum(prices), 6),
        edge=plan.net_edge_per_set,
        edge_bps=round(plan.net_edge_per_set * 10_000, 2),
        source=source,
    )


@dataclass
class VerifyOutcome:
    opportunity: Opportunity | None
    reject_reason: RejectReason | None
    ask_depth: float | None = None
    bid_depth: float | None = None
    plan: CompleteSetPlan | None = None


@dataclass
class ScanResult:
    scanned: int
    gamma_hits: list[Opportunity]
    verified_hits: list[Opportunity]
    rejected: list[tuple[Opportunity, RejectReason]] = field(default_factory=list)
    run_id: int | None = None
    metrics_path: str | None = None

    @property
    def all_hits(self) -> list[Opportunity]:
        seen: set[str] = set()
        merged: list[Opportunity] = []
        for opp in self.verified_hits + self.gamma_hits:
            key = f"{opp.condition_id}:{opp.kind.value}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(opp)
        merged.sort(key=lambda o: o.edge_bps, reverse=True)
        return merged


def _market_meta(market: dict) -> tuple[str, str, str]:
    return (
        market.get("question") or "?",
        market.get("slug") or "",
        market.get("conditionId") or market.get("condition_id") or "",
    )


def scan_gamma(config: ArbConfig) -> tuple[int, list[Opportunity]]:
    hits: list[Opportunity] = []
    scanned = 0
    for market in iter_markets(
        active=True,
        closed=False,
        page_size=config.page_size,
        max_markets=config.max_markets,
        max_offset=config.gamma_max_offset,
        source=config.scan_source,
    ):
        if market.get("closed"):
            continue
        outcomes, tokens, prices = market_tokens(market)
        if len(tokens) < 2 or len(prices) < 2:
            continue
        question, slug, condition_id = _market_meta(market)
        if not condition_id:
            continue
        scanned += 1
        opp = detect_from_prices(
            question=question,
            slug=slug,
            condition_id=condition_id,
            outcomes=outcomes,
            token_ids=tokens,
            prices=prices,
            min_edge=config.min_edge,
            fee_rate=config.fee_rate,
            source="gamma",
        )
        if opp:
            hits.append(opp)
    hits.sort(key=lambda o: o.edge_bps, reverse=True)
    return scanned, hits


def _leg_bid_depth(snap: BookSnapshot, depth_levels: int | None) -> float:
    levels = snap.bids if not depth_levels or depth_levels <= 0 else snap.bids[:depth_levels]
    return round(sum(lvl.size for lvl in levels), 8)


def verify_one(config: ArbConfig, opp: Opportunity) -> VerifyOutcome:
    """Verify a gamma candidate by building an executable complete-set plan.

    A candidate is only CLOB_VERIFIED when a valid BUY_COMPLETE_SET_MERGE plan
    exists (real L2/L3 VWAP, common q, fresh books, known fees, positive net).
    Sell-side violations are not executable in this phase and are rejected here.
    """
    depth = config.plan_depth()
    snapshots: list[BookSnapshot] = []
    for token_id in opp.token_ids:
        book = fetch_orderbook(token_id)
        if not book:
            return VerifyOutcome(None, RejectReason.NO_BOOK)
        snap = BookSnapshot.from_book(
            token_id,
            book,
            source="rest",
            default_tick_size=config.assumed_tick_size,
            default_min_order_size=config.assumed_min_order_size,
        )
        if not snap.asks or snap.best_bid is None:
            return VerifyOutcome(None, RejectReason.MISSING_BID_ASK)
        snapshots.append(snap)

    # Capacity is the weakest leg — never aggregated across legs.
    ask_depth = min(s.ask_capacity(depth_levels=depth) for s in snapshots)
    bid_depth = min(_leg_bid_depth(s, depth) for s in snapshots)

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

    return VerifyOutcome(opportunity_from_plan(plan), None, ask_depth, bid_depth, plan=plan)


def verify_with_books(
    config: ArbConfig, candidates: list[Opportunity]
) -> tuple[list[Opportunity], list[tuple[Opportunity, RejectReason]], list[VerifyOutcome]]:
    verified: list[Opportunity] = []
    rejected: list[tuple[Opportunity, RejectReason]] = []
    outcomes: list[VerifyOutcome] = []
    for opp in candidates[: config.verify_top_n]:
        outcome = verify_one(config, opp)
        outcomes.append(outcome)
        if outcome.opportunity is not None:
            verified.append(outcome.opportunity)
        else:
            rejected.append((opp, outcome.reject_reason or RejectReason.OTHER))
    verified.sort(key=lambda o: o.edge_bps, reverse=True)
    return verified, rejected, outcomes


def _hypothetical_pnl(opp: Opportunity) -> float:
    """Unit-size hypothetical PnL assuming $1 notional per complete set."""
    return round(opp.edge, 6)


def run_scan(
    config: ArbConfig,
    *,
    gamma_only: bool = False,
    persist: bool = True,
) -> ScanResult:
    metrics = start_metrics()
    store = OpportunityStore(config.state_db) if persist else None
    run_id = store.start_scan_run(study_mode=config.study_mode) if store else None

    scanned, gamma_hits = scan_gamma(config)
    metrics.scanned = scanned
    metrics.gamma_hits = len(gamma_hits)

    verified_hits: list[Opportunity] = []
    rejected: list[tuple[Opportunity, RejectReason]] = []
    outcomes: list[VerifyOutcome] = []

    if not gamma_only and gamma_hits:
        verified_hits, rejected, outcomes = verify_with_books(config, gamma_hits)

    metrics.verified_hits = len(verified_hits)
    metrics.rejected = len(rejected)
    for _, reason in rejected:
        metrics.bump_reject(reason.value)

    if store and run_id is not None:
        for opp in gamma_hits:
            store.save(
                opp,
                state=OppState.GAMMA_FLAG,
                verified=False,
                scan_run_id=run_id,
            )
        outcome_by_key = {
            f"{o.opportunity.condition_id}:{o.opportunity.kind.value}": o
            for o in outcomes
            if o.opportunity is not None
        }
        for opp in verified_hits:
            key = f"{opp.condition_id}:{opp.kind.value}"
            meta = outcome_by_key.get(key)
            plan = meta.plan if meta else None
            store.save(
                opp,
                state=OppState.CLOB_VERIFIED,
                verified=True,
                ask_depth=meta.ask_depth if meta else None,
                bid_depth=meta.bid_depth if meta else None,
                hypothetical_pnl=plan.net_cash_pnl_usd if plan else _hypothetical_pnl(opp),
                scan_run_id=run_id,
                plan_record=plan.to_dict() if plan else None,
            )
        for opp, reason in rejected:
            store.save(
                opp,
                state=OppState.REJECTED,
                verified=False,
                reject_reason=reason,
                scan_run_id=run_id,
            )
        store.finish_scan_run(
            run_id,
            scanned=scanned,
            gamma_hits=len(gamma_hits),
            verified_hits=len(verified_hits),
            rejected=len(rejected),
            notes="phase1_study" if config.study_mode else "scan",
        )
        append_ledger(
            config.ledger_path,
            run_id=run_id,
            scanned=scanned,
            gamma_hits=len(gamma_hits),
            verified=verified_hits,
            rejected=rejected,
        )

    finish_metrics(metrics)
    if persist:
        metrics.write(config.metrics_path)

    return ScanResult(
        scanned=scanned,
        gamma_hits=gamma_hits,
        verified_hits=verified_hits,
        rejected=rejected,
        run_id=run_id,
        metrics_path=str(config.metrics_path) if persist else None,
    )


def format_alert(result: ScanResult, *, position_usd: float = 25.0) -> str | None:
    """Stdout alert for cron delivery — only when verified hits exist."""
    if not result.verified_hits:
        return None
    lines = [
        f"ARB ALERT: {len(result.verified_hits)} CLOB-verified Dutch-book hit(s)",
        f"scanned={result.scanned} gamma={len(result.gamma_hits)} rejected={len(result.rejected)}",
        "",
    ]
    for opp in result.verified_hits[:5]:
        pnl = round(opp.edge * position_usd, 4)
        lines.append(
            f"- {opp.kind.value} edge={opp.edge_bps:.1f}bps "
            f"~${pnl:.3f}@${position_usd:.0f} total={opp.total:.4f} | {opp.question[:80]}"
        )
    return "\n".join(lines)
