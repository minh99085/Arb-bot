"""Dutch-book / no-arbitrage detection for binary Polymarket markets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class ArbKind(str, Enum):
    BUY_BUNDLE = "buy_bundle"
    SELL_BUNDLE = "sell_bundle"


@dataclass(frozen=True)
class Opportunity:
    """A detected Dutch-book violation."""

    kind: ArbKind
    condition_id: str
    slug: str
    question: str
    outcomes: list[str]
    token_ids: list[str]
    prices: list[float]
    total: float
    edge: float
    edge_bps: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data


def _edge_bps(edge: float) -> float:
    return round(edge * 10_000, 2)


def detect_from_prices(
    *,
    question: str,
    slug: str,
    condition_id: str,
    outcomes: list[str],
    token_ids: list[str],
    prices: list[float],
    min_edge: float,
    fee_rate: float,
    source: str,
) -> Opportunity | None:
    """Detect buy/sell bundle arbs from outcome price vector.

    Buy bundle: purchase all outcomes for < $1 (after fees).
    Sell bundle: sell all outcomes for > $1 (after fees).
  """
    if len(prices) < 2 or len(token_ids) < 2:
        return None
    if any(p <= 0 or p >= 1 for p in prices):
        return None

    total = sum(prices)
    buy_threshold = 1.0 - fee_rate - min_edge
    sell_threshold = 1.0 + fee_rate + min_edge

    if total < buy_threshold:
        edge = buy_threshold - total
        return Opportunity(
            kind=ArbKind.BUY_BUNDLE,
            condition_id=condition_id,
            slug=slug,
            question=question,
            outcomes=outcomes,
            token_ids=token_ids,
            prices=prices,
            total=total,
            edge=edge,
            edge_bps=_edge_bps(edge),
            source=source,
        )

    if total > sell_threshold:
        edge = total - sell_threshold
        return Opportunity(
            kind=ArbKind.SELL_BUNDLE,
            condition_id=condition_id,
            slug=slug,
            question=question,
            outcomes=outcomes,
            token_ids=token_ids,
            prices=prices,
            total=total,
            edge=edge,
            edge_bps=_edge_bps(edge),
            source=source,
        )

    return None


def detect_from_asks(
    *,
    question: str,
    slug: str,
    condition_id: str,
    outcomes: list[str],
    token_ids: list[str],
    asks: list[float],
    min_edge: float,
    fee_rate: float,
) -> Opportunity | None:
    """Buy-bundle only: sum of asks must be below 1 - fees - min_edge."""
    if len(asks) < 2 or len(token_ids) < 2:
        return None
    if any(p <= 0 or p >= 1 for p in asks):
        return None
    total = sum(asks)
    buy_threshold = 1.0 - fee_rate - min_edge
    if total >= buy_threshold:
        return None
    edge = buy_threshold - total
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id=condition_id,
        slug=slug,
        question=question,
        outcomes=outcomes,
        token_ids=token_ids,
        prices=asks,
        total=total,
        edge=edge,
        edge_bps=_edge_bps(edge),
        source="clob_asks",
    )


def detect_from_bids(
    *,
    question: str,
    slug: str,
    condition_id: str,
    outcomes: list[str],
    token_ids: list[str],
    bids: list[float],
    min_edge: float,
    fee_rate: float,
) -> Opportunity | None:
    if len(bids) < 2:
        return None
    total = sum(bids)
    sell_threshold = 1.0 + fee_rate + min_edge
    if total <= sell_threshold:
        return None
    edge = total - sell_threshold
    return Opportunity(
        kind=ArbKind.SELL_BUNDLE,
        condition_id=condition_id,
        slug=slug,
        question=question,
        outcomes=outcomes,
        token_ids=token_ids,
        prices=bids,
        total=total,
        edge=edge,
        edge_bps=_edge_bps(edge),
        source="clob_bids",
    )
