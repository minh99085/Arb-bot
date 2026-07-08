"""Scan Polymarket for Dutch-book opportunities."""

from __future__ import annotations

from dataclasses import dataclass

from arb.config import ArbConfig
from arb.dutch_book import Opportunity, detect_from_asks, detect_from_bids, detect_from_prices
from arb.polymarket_data import (
    best_bid_ask,
    fetch_orderbook,
    iter_markets,
    market_tokens,
)
from arb.state import OpportunityStore


@dataclass
class ScanResult:
    scanned: int
    gamma_hits: list[Opportunity]
    verified_hits: list[Opportunity]

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


def verify_with_books(config: ArbConfig, candidates: list[Opportunity]) -> list[Opportunity]:
    verified: list[Opportunity] = []
    for opp in candidates[: config.verify_top_n]:
        asks: list[float] = []
        bids: list[float] = []
        for token_id in opp.token_ids:
            book = fetch_orderbook(token_id)
            best_bid, best_ask = best_bid_ask(book)
            if best_ask is None or best_bid is None:
                asks = []
                bids = []
                break
            asks.append(best_ask)
            bids.append(best_bid)
        if len(asks) == len(opp.token_ids):
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
            if buy_opp:
                verified.append(buy_opp)
        if len(bids) == len(opp.token_ids):
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
            if sell_opp:
                verified.append(sell_opp)
    verified.sort(key=lambda o: o.edge_bps, reverse=True)
    return verified


def run_scan(
    config: ArbConfig,
    *,
    gamma_only: bool = False,
    persist: bool = True,
) -> ScanResult:
    scanned, gamma_hits = scan_gamma(config)
    verified_hits: list[Opportunity] = []
    if not gamma_only and gamma_hits:
        verified_hits = verify_with_books(config, gamma_hits)

    if persist:
        store = OpportunityStore(config.state_db)
        for opp in gamma_hits:
            store.save(opp, verified=False)
        for opp in verified_hits:
            store.save(opp, verified=True)

    return ScanResult(
        scanned=scanned,
        gamma_hits=gamma_hits,
        verified_hits=verified_hits,
    )
