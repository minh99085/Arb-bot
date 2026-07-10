"""Dashboard API and trade history tests."""

from __future__ import annotations

import json
import urllib.request

from arb.config import ArbConfig
from arb.dashboard import build_dashboard_payload, run_dashboard_background
from arb.dutch_book import ArbKind, Opportunity
from arb.models import OppState
from arb.state import OpportunityStore


def _opp() -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xdash",
        slug="dash-test",
        question="Dashboard test market?",
        outcomes=["Yes", "No"],
        token_ids=["a", "b"],
        prices=[0.4, 0.45],
        total=0.85,
        edge=0.1,
        edge_bps=1000.0,
        source="clob_asks",
    )


def test_list_trades_and_summary(tmp_path):
    cfg = ArbConfig(state_dir=tmp_path)
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_opp(), state=OppState.CLOB_VERIFIED, verified=True)
    store.transition(oid, OppState.FILLED, reason="paper")
    store.record_fill(
        opportunity_id=oid,
        mode="paper",
        size_usd=10.0,
        fill_total=0.85,
        fees_usd=0.1,
        slippage_usd=0.05,
        expected_pnl=1.0,
        fill_prices=[0.4, 0.45],
        realized_pnl=1.0,
    )
    trades = store.list_trades(limit=50)
    assert len(trades) == 1
    assert trades[0]["question"] == "Dashboard test market?"
    assert trades[0]["fill_prices_list"] == [0.4, 0.45]

    summary = store.trade_summary()
    assert summary["fill_count"] == 1
    assert summary["realized_pnl_sum"] == 1.0


def test_dashboard_api(tmp_path):
    cfg = ArbConfig(state_dir=tmp_path)
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_opp(), state=OppState.FILLED, verified=True)
    store.record_fill(
        opportunity_id=oid,
        mode="paper",
        size_usd=5.0,
        fill_total=0.85,
        fees_usd=0.0,
        slippage_usd=0.0,
        expected_pnl=0.5,
        fill_prices=[0.4, 0.45],
        realized_pnl=0.5,
    )

    run_dashboard_background(cfg, host="127.0.0.1", port=0)
    # ThreadingHTTPServer with port 0 - we need actual port. Use run_dashboard with fixed port in test.

    import socket
    from http.server import ThreadingHTTPServer
    from arb.dashboard import DashboardHandler

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    handler = type("T", (DashboardHandler,), {"config": cfg, "trade_limit": 50})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=5) as resp:
            data = json.loads(resp.read().decode())
        assert data["summary"]["fill_count"] == 1
        assert len(data["trades"]) == 1
        assert "Dashboard test" in data["trades"][0]["question"]
    finally:
        server.shutdown()


def test_build_dashboard_payload(tmp_path):
    cfg = ArbConfig(state_dir=tmp_path)
    payload = build_dashboard_payload(cfg, trade_limit=50)
    assert "summary" in payload
    assert "trades" in payload
    assert payload["trades"] == []
