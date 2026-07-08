"""Trade execution hook — defers to py-clob-client when credentials are present."""

from __future__ import annotations

from dataclasses import dataclass

from arb.config import ArbConfig
from arb.dutch_book import Opportunity


@dataclass
class TradeResult:
    opportunity: Opportunity
    status: str
    detail: str


def execute_opportunity(config: ArbConfig, opp: Opportunity) -> TradeResult:
    """Execute or simulate a Dutch-book trade.

    Live execution requires POLYMARKET_PRIVATE_KEY and a future py-clob-client
    integration. Until then, dry-run mode records intent without placing orders.
    """
    if config.dry_run or not config.trading_enabled():
        return TradeResult(
            opportunity=opp,
            status="dry_run",
            detail=(
                "Trading skipped. Set POLYMARKET_PRIVATE_KEY and ARB_DRY_RUN=false "
                "to enable live execution once the CLOB client is wired."
            ),
        )

    return TradeResult(
        opportunity=opp,
        status="not_implemented",
        detail=(
            "Credentials detected but live CLOB execution is not wired yet. "
            "Install py-clob-client and implement signed order placement."
        ),
    )


def execute_batch(config: ArbConfig, opportunities: list[Opportunity]) -> list[TradeResult]:
    return [execute_opportunity(config, opp) for opp in opportunities]
