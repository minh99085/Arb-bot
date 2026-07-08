"""Tests for Dutch-book detection."""

from arb.dutch_book import ArbKind, detect_from_bids, detect_from_prices


def _base_kwargs():
    return {
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "condition_id": "0xabc",
        "outcomes": ["Yes", "No"],
        "token_ids": ["t_yes", "t_no"],
        "min_edge": 0.01,
        "fee_rate": 0.0,
    }


def test_buy_bundle_detected_when_sum_below_one():
    opp = detect_from_prices(prices=[0.45, 0.50], source="test", **_base_kwargs())
    assert opp is not None
    assert opp.kind == ArbKind.BUY_BUNDLE
    assert opp.total == 0.95
    assert opp.edge_bps == 400.0


def test_sell_bundle_detected_when_sum_above_one():
    opp = detect_from_prices(prices=[0.55, 0.50], source="test", **_base_kwargs())
    assert opp is not None
    assert opp.kind == ArbKind.SELL_BUNDLE
    assert opp.total == 1.05


def test_no_signal_inside_band():
    opp = detect_from_prices(prices=[0.50, 0.49], source="test", **_base_kwargs())
    assert opp is None


def test_invalid_prices_ignored():
    opp = detect_from_prices(prices=[0.0, 0.5], source="test", **_base_kwargs())
    assert opp is None


def test_detect_from_bids_sell_bundle():
    opp = detect_from_bids(
        bids=[0.55, 0.50],
        **_base_kwargs(),
    )
    assert opp is not None
    assert opp.kind == ArbKind.SELL_BUNDLE
    assert opp.source == "clob_bids"
