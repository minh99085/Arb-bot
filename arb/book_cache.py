"""In-memory CLOB orderbook cache — Phase 3 feed layer."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BookSide:
    """price -> size map for one side."""

    levels: dict[str, float] = field(default_factory=dict)

    def set_level(self, price: str, size: float) -> None:
        key = str(price)
        if size <= 0:
            self.levels.pop(key, None)
        else:
            self.levels[key] = float(size)

    def apply_levels(self, levels: list[dict[str, Any]]) -> None:
        self.levels.clear()
        for level in levels:
            try:
                price = str(level.get("price"))
                size = float(level.get("size", 0))
            except (TypeError, ValueError):
                continue
            if size > 0:
                self.levels[price] = size

    def best(self, *, side: str) -> float | None:
        if not self.levels:
            return None
        prices = [float(p) for p in self.levels]
        return max(prices) if side == "bid" else min(prices)

    def depth(self, *, side: str, levels: int = 5) -> float:
        if not self.levels:
            return 0.0
        items = sorted(
            ((float(p), s) for p, s in self.levels.items()),
            key=lambda x: x[0],
            reverse=(side == "bid"),
        )
        return sum(size for _, size in items[:levels])

    def as_list(self, *, side: str) -> list[dict[str, str]]:
        items = sorted(
            ((float(p), s) for p, s in self.levels.items()),
            key=lambda x: x[0],
            reverse=(side == "bid"),
        )
        return [{"price": f"{p:.6f}".rstrip("0").rstrip("."), "size": str(s)} for p, s in items]


@dataclass
class CachedBook:
    asset_id: str
    market: str | None = None
    bids: BookSide = field(default_factory=BookSide)
    asks: BookSide = field(default_factory=BookSide)
    best_bid: float | None = None
    best_ask: float | None = None
    updated_at: str = field(default_factory=_now_iso)
    source: str = "empty"

    def refresh_top(self) -> None:
        self.best_bid = self.bids.best(side="bid")
        self.best_ask = self.asks.best(side="ask")
        self.updated_at = _now_iso()

    def to_rest_shape(self) -> dict[str, Any]:
        """Shape compatible with arb.polymarket_data.best_bid_ask / scanner depth."""
        return {
            "asset_id": self.asset_id,
            "market": self.market,
            "bids": self.bids.as_list(side="bid"),
            "asks": self.asks.as_list(side="ask"),
            "updated_at": self.updated_at,
            "source": self.source,
        }


class BookCache:
    """Thread-unsafe in-process cache; one watch loop owns it."""

    def __init__(self) -> None:
        self._books: dict[str, CachedBook] = {}
        self.updates: int = 0
        self.last_event_at: str | None = None

    def get(self, asset_id: str) -> CachedBook | None:
        return self._books.get(asset_id)

    def ensure(self, asset_id: str) -> CachedBook:
        book = self._books.get(asset_id)
        if book is None:
            book = CachedBook(asset_id=asset_id)
            self._books[asset_id] = book
        return book

    def seed_from_rest(self, asset_id: str, rest_book: dict[str, Any]) -> CachedBook:
        book = self.ensure(asset_id)
        book.market = rest_book.get("market") or book.market
        book.bids.apply_levels(rest_book.get("bids") or [])
        book.asks.apply_levels(rest_book.get("asks") or [])
        book.source = "rest_seed"
        book.refresh_top()
        self.updates += 1
        self.last_event_at = book.updated_at
        return book

    def apply_event(self, event: dict[str, Any]) -> list[str]:
        """Apply a WS market-channel event. Returns touched asset_ids."""
        event_type = event.get("event_type") or event.get("type")
        touched: list[str] = []

        if event_type == "book":
            asset_id = str(event.get("asset_id") or "")
            if not asset_id:
                return []
            book = self.ensure(asset_id)
            book.market = event.get("market") or book.market
            book.bids.apply_levels(event.get("bids") or [])
            book.asks.apply_levels(event.get("asks") or [])
            book.source = "ws_book"
            book.refresh_top()
            touched.append(asset_id)

        elif event_type == "price_change":
            for change in event.get("price_changes") or []:
                asset_id = str(change.get("asset_id") or "")
                if not asset_id:
                    continue
                book = self.ensure(asset_id)
                book.market = event.get("market") or book.market
                side = str(change.get("side") or "").upper()
                price = str(change.get("price") or "")
                try:
                    size = float(change.get("size", 0))
                except (TypeError, ValueError):
                    continue
                if side == "BUY":
                    book.bids.set_level(price, size)
                elif side == "SELL":
                    book.asks.set_level(price, size)
                # Prefer explicit top-of-book if present
                if change.get("best_bid") not in (None, ""):
                    try:
                        book.best_bid = float(change["best_bid"]) or book.best_bid
                    except (TypeError, ValueError):
                        pass
                if change.get("best_ask") not in (None, ""):
                    try:
                        val = float(change["best_ask"])
                        book.best_ask = val if val > 0 else book.best_ask
                    except (TypeError, ValueError):
                        pass
                book.source = "ws_price_change"
                book.refresh_top()
                touched.append(asset_id)

        elif event_type == "best_bid_ask":
            asset_id = str(event.get("asset_id") or "")
            if not asset_id:
                return []
            book = self.ensure(asset_id)
            book.market = event.get("market") or book.market
            try:
                if event.get("best_bid") not in (None, ""):
                    book.best_bid = float(event["best_bid"])
                if event.get("best_ask") not in (None, ""):
                    book.best_ask = float(event["best_ask"])
            except (TypeError, ValueError):
                pass
            book.source = "ws_best_bid_ask"
            book.updated_at = _now_iso()
            touched.append(asset_id)

        if touched:
            self.updates += 1
            self.last_event_at = _now_iso()
        return touched

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {aid: deepcopy(book.to_rest_shape()) for aid, book in self._books.items()}

    def __len__(self) -> int:
        return len(self._books)
