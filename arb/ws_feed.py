"""Polymarket CLOB market-channel WebSocket feed — Phase 3."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from arb.book_cache import BookCache
from arb.polymarket_data import fetch_orderbook

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_SEC = 10.0

OnUpdate = Callable[[list[str], BookCache], Awaitable[None] | None]


class MarketFeed:
    """Async market-channel client with REST seed + reconnect."""

    def __init__(
        self,
        asset_ids: list[str],
        *,
        cache: BookCache | None = None,
        ws_url: str = DEFAULT_WS_URL,
        ping_interval: float = PING_INTERVAL_SEC,
        on_update: OnUpdate | None = None,
        seed_rest: bool = True,
    ):
        self.asset_ids = list(dict.fromkeys(asset_ids))  # unique, stable order
        self.cache = cache or BookCache()
        self.ws_url = ws_url
        self.ping_interval = ping_interval
        self.on_update = on_update
        self.seed_rest = seed_rest
        self._stop = asyncio.Event()
        self.connected = False
        self.messages = 0
        self.reconnects = 0

    def stop(self) -> None:
        self._stop.set()

    def seed_books(self) -> None:
        """Blocking REST seed — call before or during watch start."""
        for asset_id in self.asset_ids:
            try:
                rest = fetch_orderbook(asset_id)
                self.cache.seed_from_rest(asset_id, rest)
            except SystemExit:
                logger.warning("REST seed failed for %s", asset_id[:16])
            except Exception as exc:
                logger.warning("REST seed error for %s: %s", asset_id[:16], exc)

    async def _emit(self, touched: list[str]) -> None:
        if not touched or self.on_update is None:
            return
        result = self.on_update(touched, self.cache)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            await result

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw.strip()
        if not text or text == "PONG":
            return
        if text == "PING":
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("non-json ws message: %s", text[:80])
            return

        events: list[dict[str, Any]]
        if isinstance(payload, list):
            events = [e for e in payload if isinstance(e, dict)]
        elif isinstance(payload, dict):
            events = [payload]
        else:
            return

        touched_all: list[str] = []
        for event in events:
            touched = self.cache.apply_event(event)
            touched_all.extend(touched)
            self.messages += 1
        if touched_all:
            await self._emit(list(dict.fromkeys(touched_all)))

    async def run(self, *, duration_sec: float | None = None) -> None:
        """Run until stop() or duration elapses. Reconnects with backoff."""
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "websockets package required for Phase 3 feed "
                "(already in hermes-agent dependencies)"
            ) from exc

        if self.seed_rest:
            await asyncio.to_thread(self.seed_books)

        deadline = None
        if duration_sec is not None:
            deadline = asyncio.get_event_loop().time() + duration_sec

        backoff = 1.0
        while not self._stop.is_set():
            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                break
            try:
                await self._session(websockets, deadline)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.reconnects += 1
                logger.warning("WS disconnected (%s); reconnect in %.1fs", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    async def _session(self, websockets: Any, deadline: float | None) -> None:
        async with websockets.connect(self.ws_url, ping_interval=None) as ws:
            self.connected = True
            sub = {
                "assets_ids": self.asset_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(sub))

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
                    timeout = None
                    if deadline is not None:
                        remaining = deadline - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            break
                        timeout = remaining
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    await self._handle_message(raw)
            finally:
                ping_task.cancel()
                self.connected = False
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass


def run_feed_sync(
    asset_ids: list[str],
    *,
    duration_sec: float = 30.0,
    cache: BookCache | None = None,
    on_update: OnUpdate | None = None,
    ws_url: str = DEFAULT_WS_URL,
    seed_rest: bool = True,
) -> BookCache:
    """Blocking helper for CLI / cron."""
    feed = MarketFeed(
        asset_ids,
        cache=cache,
        ws_url=ws_url,
        on_update=on_update,
        seed_rest=seed_rest,
    )

    async def _main() -> BookCache:
        await feed.run(duration_sec=duration_sec)
        return feed.cache

    return asyncio.run(_main())
