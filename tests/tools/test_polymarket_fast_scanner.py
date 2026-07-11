"""Unit tests for tools.polymarket_fast_scanner — algorithmic L2 path."""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.polymarket_fast_scanner import (
    DEFAULT_WS_URL,
    PolymarketL2Scanner,
    _ACTIVE,
    get_active_scanners,
    start_scanner_background,
    stop_scanner,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Isolate scanner registry across tests."""
    keys = list(_ACTIVE.keys())
    for k in keys:
        stop_scanner(k)
    yield
    keys = list(_ACTIVE.keys())
    for k in keys:
        stop_scanner(k)


def test_evaluate_detects_buy_bundle_arb():
    scanner = PolymarketL2Scanner("yes1", "no1", min_edge_percent=0.5)
    yes = scanner.books["yes1"]
    no = scanner.books["no1"]
    yes.apply_snapshot([], [{"price": "0.40", "size": "100"}])
    no.apply_snapshot([], [{"price": "0.50", "size": "80"}])
    # 0.40 + 0.50 = 0.90 < 1.0 - 0.005 = 0.995
    signal = scanner.evaluate()
    assert signal is not None
    assert signal.ask_sum == 0.9
    assert signal.max_shares == 80.0
    assert signal.edge_percent > 0


def test_evaluate_rejects_when_above_threshold():
    scanner = PolymarketL2Scanner("yes1", "no1", min_edge_percent=1.0)
    yes = scanner.books["yes1"]
    no = scanner.books["no1"]
    yes.apply_snapshot([], [{"price": "0.49", "size": "10"}])
    no.apply_snapshot([], [{"price": "0.50", "size": "10"}])
    # 0.99 >= 1.0 - 0.01 = 0.99 → no arb (strict <)
    assert scanner.evaluate() is None


def test_book_event_triggers_stdout(capsys):
    scanner = PolymarketL2Scanner("YTOK", "NTOK", min_edge_percent=0.1)
    scanner._handle_raw(
        json.dumps(
            {
                "event_type": "book",
                "asset_id": "YTOK",
                "bids": [],
                "asks": [{"price": "0.42", "size": "50"}],
            }
        )
    )
    scanner._handle_raw(
        json.dumps(
            {
                "event_type": "book",
                "asset_id": "NTOK",
                "bids": [],
                "asks": [{"price": "0.45", "size": "25"}],
            }
        )
    )
    out = capsys.readouterr().out
    assert "[ARB TRIGGER]" in out
    assert "max_shares=25.00" in out
    assert scanner.signals == 1


def test_invalid_json_does_not_raise():
    scanner = PolymarketL2Scanner("a", "b")
    scanner._handle_raw("not-json{{{")
    scanner._handle_raw("PING")
    scanner._handle_raw("PONG")
    assert scanner.messages == 0


def test_price_change_updates_asks():
    scanner = PolymarketL2Scanner("yesX", "noX", min_edge_percent=0.5)
    scanner._handle_raw(
        json.dumps(
            {
                "event_type": "price_change",
                "price_changes": [
                    {"asset_id": "yesX", "side": "SELL", "price": "0.30", "size": "40"},
                    {"asset_id": "noX", "side": "SELL", "price": "0.40", "size": "15"},
                ],
            }
        )
    )
    signal = scanner.evaluate()
    assert signal is not None
    assert signal.max_shares == 15.0


def test_start_scanner_background_creates_task(monkeypatch):
    # Avoid real network: short-circuit run()
    async def fake_run(self):
        self.connected = True
        await asyncio.sleep(0.05)
        self.connected = False

    monkeypatch.setattr(PolymarketL2Scanner, "run", fake_run)

    async def _body():
        scanner, msg = start_scanner_background("tokenYES", "tokenNO", min_edge_percent=0.5)
        assert "hooked into websocket gateway" in msg
        assert DEFAULT_WS_URL in msg
        assert scanner._task is not None
        assert not scanner._task.done()
        active = get_active_scanners()
        assert len(active) == 1

        scanner2, msg2 = start_scanner_background("tokenYES", "tokenNO", min_edge_percent=0.5)
        assert scanner2 is scanner
        assert "already tracking" in msg2

        stop_scanner("tokenYES:tokenNO")
        await asyncio.sleep(0.01)
        assert scanner._task.done() or scanner._task.cancelled()

    asyncio.run(_body())


def test_constructor_rejects_same_tokens():
    with pytest.raises(ValueError):
        PolymarketL2Scanner("same", "same")
