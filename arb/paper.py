"""Paper fill simulation at book prices + slippage — Phase 2."""

from __future__ import annotations

from dataclasses import dataclass

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity


@dataclass(frozen=True)
class PaperFill:
    opportunity: Opportunity
    size_usd: float
    fill_prices: list[float]
    fill_total: float
    fees_usd: float
    slippage_usd: float
    expected_pnl: float
    mode: str = "paper"


def simulate_paper_fill(
    config: ArbConfig,
    opp: Opportunity,
    *,
    size_usd: float,
) -> PaperFill:
    """Simulate filling a complete set at quoted prices with fee + slippage haircut.

    Buy bundle: pay sum(asks); receive $1 at resolution → edge ≈ 1 - total - costs
    Sell bundle: receive sum(bids); pay $1 at resolution → edge ≈ total - 1 - costs
    """
    slip = config.paper_slippage
    fee = config.fee_rate

    if opp.kind == ArbKind.BUY_BUNDLE:
        fill_prices = [min(0.99, p * (1.0 + slip)) for p in opp.prices]
        fill_total = sum(fill_prices)
        fees_usd = size_usd * fee * len(fill_prices)
        slippage_usd = size_usd * abs(fill_total - opp.total)
        # Unit edge on $1 complete set, scaled by size
        unit_edge = 1.0 - fill_total - fee * len(fill_prices)
        expected_pnl = round(unit_edge * size_usd, 6)
    else:
        fill_prices = [max(0.01, p * (1.0 - slip)) for p in opp.prices]
        fill_total = sum(fill_prices)
        fees_usd = size_usd * fee * len(fill_prices)
        slippage_usd = size_usd * abs(opp.total - fill_total)
        unit_edge = fill_total - 1.0 - fee * len(fill_prices)
        expected_pnl = round(unit_edge * size_usd, 6)

    if config.paper_realistic:
        # Do not floor negative PnL — honest paper accounting after fees/slippage
        pass
    else:
        expected_pnl = max(0.0, expected_pnl)

    return PaperFill(
        opportunity=opp,
        size_usd=size_usd,
        fill_prices=fill_prices,
        fill_total=round(fill_total, 6),
        fees_usd=round(fees_usd, 6),
        slippage_usd=round(slippage_usd, 6),
        expected_pnl=expected_pnl,
        mode="paper",
    )
