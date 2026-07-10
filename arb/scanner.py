"""Scan Polymarket for Dutch-book opportunities (Phase 1 study mode)."""

from __future__ import annotations

from dataclasses import dataclass, field

from arb.config import ArbConfig
from arb.dutch_book import Opportunity, detect_from_asks, detect_from_bids, detect_from_prices
from arb.ledger import append_ledger
from arb.metrics import finish_metrics, start_metrics
from arb.models import OppState, RejectReason
from arb.polymarket_data import (
    best_bid_ask,
    fetch_orderbook,
    iter_markets,
    market_tokens,
)
from arb.state import OpportunityStore


@dataclass
class VerifyOutcome:
    opportunity: Opportunity | None
    reject_reason: RejectReason | None
    ask_depth: float | None = None
    bid_depth: float | None = None


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


def _book_depth(book: dict, *, side: str) -> float:
    levels = book.get(side) or []
    total = 0.0
    for level in levels[:5]:
        try:
            total += float(level.get("size", 0))
        except (TypeError, ValueError):
            continue
    return total


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


def verify_one(config: ArbConfig, opp: Opportunity) -> VerifyOutcome:
    asks: list[float] = []
    bids: list[float] = []
    ask_depth = 0.0
    bid_depth = 0.0
    for token_id in opp.token_ids:
        book = fetch_orderbook(token_id)
        if not book:
            return VerifyOutcome(None, RejectReason.NO_BOOK)
        best_bid, best_ask = best_bid_ask(book)
        if best_ask is None or best_bid is None:
            return VerifyOutcome(None, RejectReason.MISSING_BID_ASK)
        asks.append(best_ask)
        bids.append(best_bid)
        ask_depth += _book_depth(book, side="asks")
        bid_depth += _book_depth(book, side="bids")

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
    return VerifyOutcome(verified, None, ask_depth, bid_depth)


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
            store.save(
                opp,
                state=OppState.CLOB_VERIFIED,
                verified=True,
                ask_depth=meta.ask_depth if meta else None,
                bid_depth=meta.bid_depth if meta else None,
                hypothetical_pnl=_hypothetical_pnl(opp),
                scan_run_id=run_id,
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
