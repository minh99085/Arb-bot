"""Native low-latency Polymarket CLOB L2 scanner — algorithmic hot path only.

Decouples live order-book tracking from any LLM / agent reasoning loop.
Connects to the production Polymarket CLOB market WebSocket, maintains
in-memory Level-2 books for YES/NO tokens, and evaluates Dutch-book
buy-bundle arb: best YES ask + best NO ask < 1.0 - target_edge.

No LLM completions, no Hermes agent imports on the hot path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Production CLOB V2 market channel (user typo wss://://polymarket.com corrected)
DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_SEC = 10.0


@dataclass
class L2Book:
    """Minimal Level-2 book: price(str) -> size(float). Native types only."""

    asset_id: str
    bids: dict[str, float] = field(default_factory=dict)
    asks: dict[str, float] = field(default_factory=dict)
    best_bid: float | None = None
    best_ask: float | None = None
    best_ask_size: float = 0.0
    updates: int = 0

    def apply_snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids.clear()
        self.asks.clear()
        for level in bids:
            try:
                price = str(level["price"])
                size = float(level.get("size", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                self.bids[price] = size
        for level in asks:
            try:
                price = str(level["price"])
                size = float(level.get("size", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                self.asks[price] = size
        self._refresh_top()

    def set_level(self, side: str, price: str, size: float) -> None:
        book = self.bids if side == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self._refresh_top()

    def _refresh_top(self) -> None:
        self.updates += 1
        if self.bids:
            self.best_bid = max(float(p) for p in self.bids)
        else:
            self.best_bid = None
        if self.asks:
            best_p = min(self.asks, key=lambda p: float(p))
            self.best_ask = float(best_p)
            self.best_ask_size = float(self.asks[best_p])
        else:
            self.best_ask = None
            self.best_ask_size = 0.0


@dataclass(frozen=True)
class ArbSignal:
    """Execution trigger emitted when YES ask + NO ask clears the edge floor."""

    yes_ask: float
    no_ask: float
    ask_sum: float
    edge: float
    edge_percent: float
    max_shares: float
    yes_size: float
    no_size: float
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "yes_ask": self.yes_ask,
            "no_ask": self.no_ask,
            "ask_sum": self.ask_sum,
            "edge": self.edge,
            "edge_percent": self.edge_percent,
            "max_shares": self.max_shares,
            "yes_size": self.yes_size,
            "no_size": self.no_size,
            "ts": self.ts,
        }


class PolymarketL2Scanner:
    """Async WebSocket L2 tracker for a YES/NO token pair.

    Hot path is purely algorithmic — no LLM, no agent framework.
    """

    def __init__(
        self,
        yes_token_id: str,
        no_token_id: str,
        *,
        min_edge_percent: float = 0.5,
        ws_url: str = DEFAULT_WS_URL,
        ping_interval: float = PING_INTERVAL_SEC,
        scanner_id: str | None = None,
    ):
        yes = str(yes_token_id).strip()
        no = str(no_token_id).strip()
        if not yes or not no:
            raise ValueError("yes_token_id and no_token_id are required")
        if yes == no:
            raise ValueError("yes_token_id and no_token_id must differ")

        self.yes_token_id = yes
        self.no_token_id = no
        # min_edge_percent is percent points (0.5 => 0.5% => 0.005 absolute)
        self.min_edge = max(0.0, float(min_edge_percent) / 100.0)
        self.min_edge_percent = float(min_edge_percent)
        self.ws_url = ws_url
        self.ping_interval = ping_interval
        self.scanner_id = scanner_id or f"{yes[:8]}_{no[:8]}"

        self.books: dict[str, L2Book] = {
            yes: L2Book(asset_id=yes),
            no: L2Book(asset_id=no),
        }
        self._stop = asyncio.Event()
        self.connected = False
        self.messages = 0
        self.reconnects = 0
        self.signals = 0
        self.last_signal: ArbSignal | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None
        self._task: asyncio.Task | None = None

    @property
    def asset_ids(self) -> list[str]:
        return [self.yes_token_id, self.no_token_id]

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        yes = self.books[self.yes_token_id]
        no = self.books[self.no_token_id]
        return {
            "scanner_id": self.scanner_id,
            "connected": self.connected,
            "messages": self.messages,
            "reconnects": self.reconnects,
            "signals": self.signals,
            "min_edge_percent": self.min_edge_percent,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "yes_best_ask": yes.best_ask,
            "no_best_ask": no.best_ask,
            "ask_sum": (
                round(yes.best_ask + no.best_ask, 6)
                if yes.best_ask is not None and no.best_ask is not None
                else None
            ),
            "last_signal": self.last_signal.to_dict() if self.last_signal else None,
            "last_error": self.last_error,
            "running": self._task is not None and not self._task.done(),
            "ws_url": self.ws_url,
        }

    def evaluate(self) -> ArbSignal | None:
        """Sub-millisecond Dutch-book check on current top-of-book asks."""
        yes = self.books[self.yes_token_id]
        no = self.books[self.no_token_id]
        yes_ask = yes.best_ask
        no_ask = no.best_ask
        if yes_ask is None or no_ask is None:
            return None
        if yes_ask <= 0 or no_ask <= 0 or yes_ask >= 1 or no_ask >= 1:
            return None

        ask_sum = yes_ask + no_ask
        threshold = 1.0 - self.min_edge
        if ask_sum >= threshold:
            return None

        edge = threshold - ask_sum
        max_shares = min(yes.best_ask_size, no.best_ask_size)
        if max_shares <= 0:
            return None

        return ArbSignal(
            yes_ask=yes_ask,
            no_ask=no_ask,
            ask_sum=round(ask_sum, 6),
            edge=round(edge, 6),
            edge_percent=round(edge * 100.0, 4),
            max_shares=round(max_shares, 4),
            yes_size=yes.best_ask_size,
            no_size=no.best_ask_size,
            ts=time.time(),
        )

    def _emit_if_arb(self) -> None:
        signal = self.evaluate()
        if signal is None:
            return
        self.signals += 1
        self.last_signal = signal
        # Clear stdout trigger for operators / log shippers — no LLM
        print(
            f"[ARB TRIGGER] scanner={self.scanner_id} "
            f"yes_ask={signal.yes_ask:.4f} no_ask={signal.no_ask:.4f} "
            f"sum={signal.ask_sum:.4f} edge={signal.edge_percent:.3f}% "
            f"max_shares={signal.max_shares:.2f}",
            flush=True,
        )

    def _apply_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type") or event.get("type")

        if event_type == "book":
            asset_id = str(event.get("asset_id") or "")
            book = self.books.get(asset_id)
            if book is None:
                return
            book.apply_snapshot(event.get("bids") or [], event.get("asks") or [])
            self._emit_if_arb()
            return

        if event_type == "price_change":
            touched = False
            for change in event.get("price_changes") or []:
                asset_id = str(change.get("asset_id") or "")
                book = self.books.get(asset_id)
                if book is None:
                    continue
                side = str(change.get("side") or "").upper()
                price = str(change.get("price") or "")
                if side not in {"BUY", "SELL"} or not price:
                    continue
                try:
                    size = float(change.get("size", 0))
                except (TypeError, ValueError):
                    continue
                book.set_level(side, price, size)
                # Prefer explicit top-of-book when present
                if change.get("best_ask") not in (None, ""):
                    try:
                        val = float(change["best_ask"])
                        if val > 0:
                            book.best_ask = val
                    except (TypeError, ValueError):
                        pass
                if change.get("best_bid") not in (None, ""):
                    try:
                        book.best_bid = float(change["best_bid"]) or book.best_bid
                    except (TypeError, ValueError):
                        pass
                touched = True
            if touched:
                self._emit_if_arb()
            return

        if event_type == "best_bid_ask":
            asset_id = str(event.get("asset_id") or "")
            book = self.books.get(asset_id)
            if book is None:
                return
            try:
                if event.get("best_ask") not in (None, ""):
                    book.best_ask = float(event["best_ask"])
                if event.get("best_bid") not in (None, ""):
                    book.best_bid = float(event["best_bid"])
            except (TypeError, ValueError):
                return
            self._emit_if_arb()

    def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw.strip()
        if not text or text in {"PONG", "PING"}:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("scanner %s: invalid JSON frame: %s", self.scanner_id, text[:80])
            return

        self.messages += 1
        events: list[dict[str, Any]]
        if isinstance(payload, list):
            events = [e for e in payload if isinstance(e, dict)]
        elif isinstance(payload, dict):
            events = [payload]
        else:
            return

        for event in events:
            try:
                self._apply_event(event)
            except Exception as exc:
                self.last_error = f"event_apply: {exc}"
                logger.debug("scanner %s event error: %s", self.scanner_id, exc)

    async def run(self) -> None:
        """Connect, subscribe, and track until stop()."""
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "websockets package required for PolymarketL2Scanner "
                "(declared in hermes-agent dependencies)"
            ) from exc

        self.started_at = time.time()
        self._stop.clear()
        backoff = 1.0

        while not self._stop.is_set():
            try:
                await self._session(websockets)
                backoff = 1.0
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:
                self.connected = False
                self.reconnects += 1
                self.last_error = str(exc)
                logger.warning(
                    "scanner %s WS drop (%s); reconnect in %.1fs",
                    self.scanner_id,
                    exc,
                    backoff,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, 30.0)

        self.connected = False

    async def _session(self, websockets: Any) -> None:
        async with websockets.connect(self.ws_url, ping_interval=None) as ws:
            self.connected = True
            # Market channel subscription — book updates for both legs
            sub = {
                "assets_ids": self.asset_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(sub))
            logger.info(
                "scanner %s hooked into %s assets=%s…/%s…",
                self.scanner_id,
                self.ws_url,
                self.yes_token_id[:12],
                self.no_token_id[:12],
            )

            async def _pinger() -> None:
                while not self._stop.is_set():
                    try:
                        await ws.send("PING")
                    except Exception:
                        return
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.ping_interval)
                        return
                    except asyncio.TimeoutError:
                        continue

            ping_task = asyncio.create_task(_pinger())
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    self._handle_raw(raw)
            finally:
                ping_task.cancel()
                self.connected = False
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass


# ---------------------------------------------------------------------------
# Process-local registry for MCP background tasks
# ---------------------------------------------------------------------------

_ACTIVE: dict[str, PolymarketL2Scanner] = {}
_REGISTRY_LOCK = threading.Lock()


def _make_key(yes_token_id: str, no_token_id: str) -> str:
    return f"{yes_token_id.strip()}:{no_token_id.strip()}"


def get_active_scanners() -> dict[str, dict[str, Any]]:
    with _REGISTRY_LOCK:
        return {k: s.status() for k, s in _ACTIVE.items()}


def stop_scanner(scanner_key: str) -> bool:
    with _REGISTRY_LOCK:
        scanner = _ACTIVE.pop(scanner_key, None)
    if scanner is None:
        return False
    scanner.stop()
    task = scanner._task
    if task is not None and not task.done():
        task.cancel()
    return True


def start_scanner_background(
    yes_token_id: str,
    no_token_id: str,
    min_edge_percent: float = 0.5,
    *,
    ws_url: str = DEFAULT_WS_URL,
) -> tuple[PolymarketL2Scanner, str]:
    """Instantiate scanner and schedule run() via asyncio.create_task.

    Must be called from a running event loop (e.g. FastMCP async tool).
    Returns (scanner, status_message).
    """
    key = _make_key(yes_token_id, no_token_id)
    with _REGISTRY_LOCK:
        existing = _ACTIVE.get(key)
        if existing is not None and existing._task is not None and not existing._task.done():
            return existing, (
                f"High-speed scanner already tracking YES={yes_token_id[:16]}… "
                f"NO={no_token_id[:16]}… "
                f"(scanner_id={existing.scanner_id}, connected={existing.connected}, "
                f"messages={existing.messages}, signals={existing.signals}). "
                f"Hooked into websocket gateway: {existing.ws_url}"
            )

        scanner = PolymarketL2Scanner(
            yes_token_id,
            no_token_id,
            min_edge_percent=min_edge_percent,
            ws_url=ws_url,
            scanner_id=key[:32],
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "start_scanner_background requires a running asyncio event loop"
            ) from exc

        task = loop.create_task(scanner.run(), name=f"polymarket-l2-{scanner.scanner_id}")
        scanner._task = task

        def _cleanup(t: asyncio.Task) -> None:
            with _REGISTRY_LOCK:
                cur = _ACTIVE.get(key)
                if cur is scanner:
                    _ACTIVE.pop(key, None)
            if t.cancelled():
                return
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.warning("scanner %s task ended with error: %s", scanner.scanner_id, exc)

        task.add_done_callback(_cleanup)
        _ACTIVE[key] = scanner

    msg = (
        f"High-speed L2 scanner started (non-blocking). "
        f"Tracking YES={yes_token_id[:20]}… NO={no_token_id[:20]}… "
        f"min_edge={min_edge_percent}% on production CLOB feed. "
        f"Engine successfully hooked into websocket gateway: {ws_url}. "
        f"scanner_id={scanner.scanner_id}. "
        f"Arb triggers will print to stdout when YES ask + NO ask < "
        f"1.0 - {min_edge_percent}%."
    )
    return scanner, msg
