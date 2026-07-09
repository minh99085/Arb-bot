"""Deterministic risk gates — Phase 2. No LLM on this path."""

from __future__ import annotations

from dataclasses import dataclass

from arb.config import ArbConfig
from arb.dutch_book import Opportunity
from arb.models import RiskRejectReason
from arb.state import OpportunityStore


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: RiskRejectReason | None = None
    detail: str = ""
    size_usd: float = 0.0


def _size_for(config: ArbConfig, opp: Opportunity, ask_depth: float | None, bid_depth: float | None) -> float:
    """Cap size by config and available top-of-book depth."""
    depth = None
    if opp.kind.value == "buy_bundle" and ask_depth is not None:
        depth = ask_depth
    elif opp.kind.value == "sell_bundle" and bid_depth is not None:
        depth = bid_depth
    elif ask_depth is not None or bid_depth is not None:
        depth = min(x for x in (ask_depth, bid_depth) if x is not None)

    size = config.max_position_usd
    if depth is not None and depth > 0:
        # Rough: depth is share count; treat $1/share notional ceiling as min(depth, max_position)
        size = min(size, float(depth))
    return max(0.0, round(size, 4))


def check_risk(
    config: ArbConfig,
    store: OpportunityStore,
    opp: Opportunity,
    *,
    ask_depth: float | None = None,
    bid_depth: float | None = None,
    category: str | None = None,
) -> RiskDecision:
    """Independent risk gate. Fail-closed."""
    if config.kill_switch:
        return RiskDecision(False, RiskRejectReason.KILL_SWITCH, "ARB_KILL_SWITCH is on")

    if config.study_mode:
        return RiskDecision(
            False,
            RiskRejectReason.STUDY_MODE,
            "ARB_STUDY_MODE=true — set false to allow paper/live execution",
        )

    if opp.edge_bps < config.min_edge_bps:
        return RiskDecision(
            False,
            RiskRejectReason.BELOW_MIN_EDGE,
            f"edge {opp.edge_bps:.1f}bps < min {config.min_edge_bps:.1f}bps",
        )

    if category and category.lower() in config.category_blocklist:
        return RiskDecision(
            False,
            RiskRejectReason.CATEGORY_BLOCKED,
            f"category '{category}' is blocklisted",
        )

    size = _size_for(config, opp, ask_depth, bid_depth)
    if size <= 0:
        return RiskDecision(False, RiskRejectReason.INSUFFICIENT_DEPTH, "sized to zero")

    if ask_depth is not None and bid_depth is not None:
        if ask_depth < config.min_book_depth and bid_depth < config.min_book_depth:
            return RiskDecision(
                False,
                RiskRejectReason.INSUFFICIENT_DEPTH,
                f"depth ask={ask_depth:.2f} bid={bid_depth:.2f} < {config.min_book_depth}",
            )

    open_n = store.count_open()
    if open_n >= config.max_open_positions:
        return RiskDecision(
            False,
            RiskRejectReason.MAX_OPEN,
            f"open positions {open_n} >= max {config.max_open_positions}",
        )

    if store.has_open_condition(opp.condition_id):
        return RiskDecision(
            False,
            RiskRejectReason.DUPLICATE_OPEN,
            f"already open on {opp.condition_id[:16]}…",
        )

    daily_trades = store.count_fills_today()
    if daily_trades >= config.max_daily_trades:
        return RiskDecision(
            False,
            RiskRejectReason.DAILY_TRADES,
            f"daily trades {daily_trades} >= max {config.max_daily_trades}",
        )

    daily_pnl = store.realized_pnl_today()
    if daily_pnl <= -abs(config.max_daily_loss_usd):
        return RiskDecision(
            False,
            RiskRejectReason.DAILY_LOSS,
            f"daily pnl {daily_pnl:.2f} hit loss limit -{config.max_daily_loss_usd}",
        )

    return RiskDecision(True, None, "risk ok", size_usd=size)
