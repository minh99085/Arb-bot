"""Alpha scanner — direct CLOB spread analysis for pre-deploy checks."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from arb.config import ArbConfig
from arb.dutch_book import Opportunity, detect_from_asks, detect_from_bids, detect_from_prices
from arb.polymarket_data import (
    _market_volume,
    best_bid_ask,
    fetch_orderbook,
    iter_event_markets,
    market_tokens,
)


@dataclass
class SpreadRow:
    """Per-market CLOB spread snapshot."""

    question: str
    slug: str
    condition_id: str
    volume: float
    ask_total: float
    bid_total: float
    buy_edge_bps: float
    sell_edge_bps: float
    best_edge_bps: float
    ask_depth: float
    bid_depth: float
    opportunity: Opportunity | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "slug": self.slug,
            "condition_id": self.condition_id,
            "volume": self.volume,
            "ask_total": self.ask_total,
            "bid_total": self.bid_total,
            "buy_edge_bps": self.buy_edge_bps,
            "sell_edge_bps": self.sell_edge_bps,
            "best_edge_bps": self.best_edge_bps,
            "ask_depth": self.ask_depth,
            "bid_depth": self.bid_depth,
            "opportunity": self.opportunity.to_dict() if self.opportunity else None,
        }


@dataclass
class AlphaResult:
    liquid_limit: int
    markets_seen: int
    clob_checked: int
    books_missing: int
    verified: list[SpreadRow] = field(default_factory=list)
    near_misses: list[SpreadRow] = field(default_factory=list)
    spread_leaders: list[SpreadRow] = field(default_factory=list)
    gamma_hits: int = 0
    duration_seconds: float = 0.0
    config_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def has_alpha(self) -> bool:
        return len(self.verified) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_alpha": self.has_alpha,
            "liquid_limit": self.liquid_limit,
            "markets_seen": self.markets_seen,
            "clob_checked": self.clob_checked,
            "books_missing": self.books_missing,
            "verified_count": len(self.verified),
            "near_miss_count": len(self.near_misses),
            "gamma_hits": self.gamma_hits,
            "duration_seconds": self.duration_seconds,
            "config": self.config_snapshot,
            "verified": [r.to_dict() for r in self.verified],
            "near_misses": [r.to_dict() for r in self.near_misses[:20]],
            "spread_leaders": [r.to_dict() for r in self.spread_leaders[:20]],
        }


def _book_depth(book: dict, *, side: str) -> float:
    total = 0.0
    for level in (book.get(side) or [])[:5]:
        try:
            total += float(level.get("size", 0))
        except (TypeError, ValueError):
            continue
    return total


def _edge_bps_from_totals(
    *,
    ask_total: float,
    bid_total: float,
    min_edge: float,
    fee_rate: float,
) -> tuple[float, float]:
    buy_edge = (1.0 - fee_rate - min_edge - ask_total) * 10_000
    sell_edge = (bid_total - 1.0 - fee_rate - min_edge) * 10_000
    return round(buy_edge, 2), round(sell_edge, 2)


def _fetch_books(token_ids: list[str]) -> tuple[list[float], list[float], float, float] | None:
    asks: list[float] = []
    bids: list[float] = []
    ask_depth = 0.0
    bid_depth = 0.0
    for token_id in token_ids:
        book = fetch_orderbook(token_id)
        if not book:
            return None
        best_bid, best_ask = best_bid_ask(book)
        if best_bid is None or best_ask is None:
            return None
        asks.append(best_ask)
        bids.append(best_bid)
        ask_depth += _book_depth(book, side="asks")
        bid_depth += _book_depth(book, side="bids")
    return asks, bids, ask_depth, bid_depth


def _analyze_market(config: ArbConfig, market: dict) -> SpreadRow | None:
    outcomes, tokens, gamma_prices = market_tokens(market)
    if len(tokens) < 2 or len(gamma_prices) < 2:
        return None
    question = market.get("question") or "?"
    slug = market.get("slug") or ""
    condition_id = market.get("conditionId") or market.get("condition_id") or ""
    if not condition_id:
        return None

    books = _fetch_books(tokens)
    if books is None:
        return None
    asks, bids, ask_depth, bid_depth = books
    ask_total = round(sum(asks), 6)
    bid_total = round(sum(bids), 6)
    buy_edge_bps, sell_edge_bps = _edge_bps_from_totals(
        ask_total=ask_total,
        bid_total=bid_total,
        min_edge=config.min_edge,
        fee_rate=config.fee_rate,
    )
    best_edge_bps = max(buy_edge_bps, sell_edge_bps)

    if ask_depth < config.min_book_depth and bid_depth < config.min_book_depth:
        return SpreadRow(
            question=question,
            slug=slug,
            condition_id=condition_id,
            volume=_market_volume(market),
            ask_total=ask_total,
            bid_total=bid_total,
            buy_edge_bps=buy_edge_bps,
            sell_edge_bps=sell_edge_bps,
            best_edge_bps=best_edge_bps,
            ask_depth=ask_depth,
            bid_depth=bid_depth,
            opportunity=None,
        )

    buy_opp = detect_from_asks(
        question=question,
        slug=slug,
        condition_id=condition_id,
        outcomes=outcomes,
        token_ids=tokens,
        asks=asks,
        min_edge=config.min_edge,
        fee_rate=config.fee_rate,
    )
    sell_opp = detect_from_bids(
        question=question,
        slug=slug,
        condition_id=condition_id,
        outcomes=outcomes,
        token_ids=tokens,
        bids=bids,
        min_edge=config.min_edge,
        fee_rate=config.fee_rate,
    )
    opportunity: Opportunity | None = None
    if buy_opp and sell_opp:
        opportunity = buy_opp if buy_opp.edge_bps >= sell_opp.edge_bps else sell_opp
    elif buy_opp:
        opportunity = buy_opp
    elif sell_opp:
        opportunity = sell_opp

    return SpreadRow(
        question=question,
        slug=slug,
        condition_id=condition_id,
        volume=_market_volume(market),
        ask_total=ask_total,
        bid_total=bid_total,
        buy_edge_bps=buy_edge_bps,
        sell_edge_bps=sell_edge_bps,
        best_edge_bps=best_edge_bps,
        ask_depth=ask_depth,
        bid_depth=bid_depth,
        opportunity=opportunity,
    )


def run_alpha_scan(
    config: ArbConfig,
    *,
    liquid_limit: int = 400,
    near_miss_bps: float = 15.0,
    workers: int = 8,
    count_gamma: bool = True,
) -> AlphaResult:
    """Scan top liquid markets with direct CLOB books (not gamma-dependent)."""
    started = time.monotonic()
    candidates: list[dict] = []
    gamma_hits = 0

    for market in iter_event_markets(max_markets=None):
        outcomes, tokens, prices = market_tokens(market)
        if len(tokens) < 2:
            continue
        vol = _market_volume(market)
        candidates.append(market)
        if count_gamma:
            condition_id = market.get("conditionId") or market.get("condition_id") or ""
            opp = detect_from_prices(
                question=market.get("question") or "?",
                slug=market.get("slug") or "",
                condition_id=condition_id,
                outcomes=outcomes,
                token_ids=tokens,
                prices=prices,
                min_edge=config.min_edge,
                fee_rate=config.fee_rate,
                source="gamma",
            )
            if opp:
                gamma_hits += 1

    candidates.sort(key=_market_volume, reverse=True)

    rows: list[SpreadRow] = []
    books_missing = 0
    workers_n = max(1, min(workers, int(os.environ.get("ARB_ALPHA_WORKERS", str(workers)))))

    # Many high-volume markets have stale/404 token books — scan until liquid_limit
    # successful CLOB checks or we exhaust the candidate pool.
    batch_size = max(workers_n * 4, 32)
    checked_ids: set[str] = set()
    idx = 0
    while len(rows) < liquid_limit and idx < len(candidates):
        chunk = []
        while idx < len(candidates) and len(chunk) < batch_size:
            m = candidates[idx]
            idx += 1
            cid = m.get("conditionId") or m.get("condition_id") or ""
            if cid and cid not in checked_ids:
                checked_ids.add(cid)
                chunk.append(m)
        if not chunk:
            break
        with ThreadPoolExecutor(max_workers=workers_n) as pool:
            futures = {pool.submit(_analyze_market, config, m): m for m in chunk}
            for fut in as_completed(futures):
                try:
                    row = fut.result()
                except Exception:
                    books_missing += 1
                    continue
                if row is None:
                    books_missing += 1
                else:
                    rows.append(row)
                    if len(rows) >= liquid_limit:
                        break

    verified = [r for r in rows if r.opportunity is not None]
    verified.sort(key=lambda r: r.opportunity.edge_bps if r.opportunity else 0, reverse=True)

    near_misses = [
        r
        for r in rows
        if r.opportunity is None and r.best_edge_bps >= -near_miss_bps
    ]
    near_misses.sort(key=lambda r: r.best_edge_bps, reverse=True)

    spread_leaders = sorted(rows, key=lambda r: r.best_edge_bps, reverse=True)

    return AlphaResult(
        liquid_limit=liquid_limit,
        markets_seen=len(candidates),
        clob_checked=len(rows),
        books_missing=books_missing,
        verified=verified,
        near_misses=near_misses,
        spread_leaders=spread_leaders[:30],
        gamma_hits=gamma_hits,
        duration_seconds=round(time.monotonic() - started, 2),
        config_snapshot={
            "min_edge_bps": config.min_edge_bps,
            "taker_fee_bps": config.taker_fee_bps,
            "min_book_depth": config.min_book_depth,
            "max_position_usd": config.max_position_usd,
            "near_miss_bps": near_miss_bps,
        },
    )


def hypothetical_pnl_usd(opp: Opportunity, size_usd: float) -> float:
    return round(opp.edge * size_usd, 4)


def format_alpha_report(result: AlphaResult, config: ArbConfig) -> str:
    """Human-readable alpha dashboard for VPS pre-deploy."""
    lines: list[str] = []
    sep = "=" * 72
    lines.append(sep)
    lines.append("POLYMARKET ARB — ALPHA REPORT")
    lines.append(sep)
    lines.append(
        f"Universe: {result.markets_seen:,} markets | "
        f"CLOB checked: {result.clob_checked} (top {result.liquid_limit} liquid) | "
        f"{result.duration_seconds:.1f}s"
    )
    lines.append(
        f"Threshold: {config.min_edge_bps:.0f}bps edge + {config.taker_fee_bps:.0f}bps fees | "
        f"min depth {config.min_book_depth:.0f} shares | "
        f"size cap ${config.max_position_usd:.0f}"
    )
    lines.append(
        f"Gamma flags (mid prices): {result.gamma_hits} | "
        f"Missing books: {result.books_missing}"
    )
    lines.append("")

    if result.verified:
        lines.append(f"✓ CLOB-VERIFIED ALPHA ({len(result.verified)})")
        lines.append("-" * 72)
        for i, row in enumerate(result.verified[:15], 1):
            opp = row.opportunity
            assert opp is not None
            pnl = hypothetical_pnl_usd(opp, config.max_position_usd)
            lines.append(
                f"{i:2}. {opp.kind.value:11} {opp.edge_bps:6.1f}bps  "
                f"~${pnl:6.3f} @ ${config.max_position_usd:.0f}  "
                f"ask={row.ask_total:.4f} bid={row.bid_total:.4f}  "
                f"vol=${row.volume/1e6:.2f}M"
            )
            lines.append(f"    {row.question[:68]}")
            lines.append(f"    slug={row.slug}")
        lines.append("")
    else:
        lines.append("✗ NO CLOB-VERIFIED ALPHA above threshold right now.")
        lines.append("  (Markets may still be efficient — see near-misses below.)")
        lines.append("")

    if result.near_misses:
        lines.append(
            f"NEAR-MISSES (within {result.config_snapshot.get('near_miss_bps', 15):.0f}bps of threshold)"
        )
        lines.append("-" * 72)
        for i, row in enumerate(result.near_misses[:10], 1):
            lines.append(
                f"{i:2}. best={row.best_edge_bps:6.1f}bps  "
                f"buy={row.buy_edge_bps:6.1f}  sell={row.sell_edge_bps:6.1f}  "
                f"ask={row.ask_total:.4f} bid={row.bid_total:.4f}  "
                f"vol=${row.volume/1e6:.2f}M"
            )
            lines.append(f"    {row.question[:68]}")
        lines.append("")

    if result.spread_leaders:
        lines.append("TOP SPREAD INEFFICIENCY (raw CLOB, even if below threshold)")
        lines.append("-" * 72)
        for i, row in enumerate(result.spread_leaders[:8], 1):
            lines.append(
                f"{i:2}. best={row.best_edge_bps:6.1f}bps  "
                f"ask={row.ask_total:.4f} bid={row.bid_total:.4f}  "
                f"vol=${row.volume/1e6:.2f}M | {row.question[:48]}"
            )
        lines.append("")

    lines.append("DEPLOY READINESS")
    lines.append("-" * 72)
    if result.has_alpha:
        lines.append("  Scanner found tradeable alpha — safe to deploy worker in paper mode.")
    elif result.clob_checked >= 50:
        lines.append("  Scanner healthy (books OK). No alpha now = normal on efficient markets.")
        lines.append("  Deploy VPS worker to catch fleeting arbs; keep WS re-verify on.")
    elif result.clob_checked >= 20:
        lines.append("  Partial CLOB coverage — many top-volume markets lack live books (404).")
        lines.append("  Increase --liquid or ARB_LIQUID_SCAN_LIMIT; worker will still catch arbs.")
    else:
        lines.append("  WARNING: Low CLOB coverage — check network/API before VPS deploy.")
    lines.append("")
    lines.append("VPS quick start:")
    lines.append("  cp deploy/vps.env.example ~/.hermes/profiles/polymarket-arb/worker.env")
    lines.append("  python -m arb alpha --liquid 400    # pre-flight")
    lines.append("  python -m arb worker run --paper --ws")
    lines.append(sep)
    return "\n".join(lines)
