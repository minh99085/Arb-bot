"""Alpha scanner and API resilience tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from arb.alpha import (
    SpreadRow,
    _edge_bps_from_totals,
    format_alpha_report,
    hypothetical_pnl_usd,
    run_alpha_scan,
)
from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.polymarket_data import PolymarketAPIError, iter_event_markets


def test_edge_bps_from_totals():
    buy, sell = _edge_bps_from_totals(
        ask_total=0.99, bid_total=0.98, min_edge=0.003, fee_rate=0.001
    )
    assert buy > 0
    assert sell < 0


def test_hypothetical_pnl():
    opp = Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0x1",
        slug="s",
        question="Q?",
        outcomes=["Y", "N"],
        token_ids=["a", "b"],
        prices=[0.4, 0.45],
        total=0.85,
        edge=0.1,
        edge_bps=1000.0,
        source="clob_asks",
    )
    assert hypothetical_pnl_usd(opp, 25.0) == 2.5


def test_format_alpha_report_no_hits():
    cfg = ArbConfig(state_dir=None, min_edge_bps=30.0)
    from arb.alpha import AlphaResult

    result = AlphaResult(
        liquid_limit=100,
        markets_seen=1000,
        clob_checked=80,
        books_missing=5,
        spread_leaders=[
            SpreadRow(
                question="Test?",
                slug="t",
                condition_id="0x",
                volume=1e6,
                ask_total=1.001,
                bid_total=0.999,
                buy_edge_bps=-10,
                sell_edge_bps=-10,
                best_edge_bps=-10,
                ask_depth=100,
                bid_depth=100,
            )
        ],
    )
    text = format_alpha_report(result, cfg)
    assert "NO CLOB-VERIFIED ALPHA" in text
    assert "DEPLOY READINESS" in text


def test_iter_event_markets_stops_on_422():
    calls = {"n": 0}

    def fake_gamma(path):
        calls["n"] += 1
        if "offset=200" in path or calls["n"] > 3:
            raise PolymarketAPIError(422, "Unprocessable Entity")
        return [
            {
                "markets": [
                    {
                        "conditionId": f"0x{calls['n']}",
                        "outcomes": "[]",
                        "clobTokenIds": "[]",
                    }
                ]
            }
        ]

    with patch("arb.polymarket_data.gamma_get", side_effect=fake_gamma):
        markets = list(iter_event_markets(page_size=100, max_offset=500))
    assert len(markets) >= 1


def test_run_alpha_scan_mocked():
    cfg = ArbConfig(min_edge_bps=30.0, taker_fee_bps=10.0, min_book_depth=1.0)
    fake_market = {
        "question": "Will X?",
        "slug": "will-x",
        "conditionId": "0xabc",
        "volume": 500000,
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["t1","t2"]',
        "outcomePrices": '["0.45","0.50"]',
    }

    def fake_books(token_ids):
        return [0.45, 0.50], [0.44, 0.49], 50.0, 50.0

    with patch("arb.alpha.iter_event_markets", return_value=[fake_market]):
        with patch("arb.alpha._fetch_books", side_effect=fake_books):
            result = run_alpha_scan(cfg, liquid_limit=1, workers=1, count_gamma=True)
    assert result.markets_seen == 1
    assert result.clob_checked == 1
