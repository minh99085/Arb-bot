"""Phase 2 acceptance tests — deterministic complete-set execution planning.

Proves:
  * Equal-dollar leg allocation cannot produce a valid complete set.
  * Every valid plan uses the same q for every leg.
  * The smallest leg limits capacity (never aggregate depth across legs).
  * L2 depth changes VWAP and can invalidate a top-of-book candidate.
  * Shares and dollars cannot be interchanged.
  * Unknown fee, stale book, invalid tick/min size, missing outcome, and
    negative net PnL reject the plan.
  * Shadow PnL cannot be realized PnL.
  * Scan reports distinguish a candidate from an executable plan.
"""

from __future__ import annotations

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.plan import (
    STRATEGY_BUY_COMPLETE_SET_MERGE,
    BookLevel,
    BookSnapshot,
    PlanRejectReason,
    PolymarketFeeModel,
    UnknownFeeModel,
    build_complete_set_plan,
    simulate_shadow_fill,
)

NOW = "2026-07-24T00:00:00+00:00"


def _snap(token_id, asks, *, bids=(), captured_at=NOW, tick=0.01, mos=5.0, neg_risk=False):
    return BookSnapshot(
        token_id=token_id,
        asks=tuple(BookLevel(price=p, size=s) for p, s in asks),
        bids=tuple(BookLevel(price=p, size=s) for p, s in bids),
        captured_at=captured_at,
        source="test",
        tick_size=tick,
        min_order_size=mos,
        neg_risk=neg_risk,
    )


def _build(snaps, *, outcomes=("Y", "N"), fee=None, budget=1000.0, now=NOW, **kw):
    return build_complete_set_plan(
        condition_id="0xcid",
        slug="slug",
        question="Q?",
        outcomes=list(outcomes),
        snapshots=list(snaps),
        fee_model=fee if fee is not None else PolymarketFeeModel(0.0),
        cash_budget_usd=budget,
        now_iso=now,
        max_book_age_sec=30.0,
        **kw,
    )


# ── Equal-dollar allocation is not a complete set ────────────────────────────
def test_equal_dollar_allocation_is_not_a_complete_set():
    snaps = [_snap("a", [(0.40, 1000)]), _snap("b", [(0.45, 1000)])]
    budget = 10.0
    # Naive equal-DOLLAR allocation ($5/leg) buys UNEQUAL shares → cannot merge.
    naive_shares = [(budget / 2) / 0.40, (budget / 2) / 0.45]
    assert round(naive_shares[0], 4) != round(naive_shares[1], 4)

    plan = _build(snaps, budget=budget)
    assert plan.executable
    qs = [leg.q_shares for leg in plan.legs]
    assert qs[0] == qs[1] == plan.q_complete_sets       # ONE common q
    assert round(plan.q_complete_sets, 4) not in {
        round(naive_shares[0], 4),
        round(naive_shares[1], 4),
    }


# ── Same q for every leg ─────────────────────────────────────────────────────
def test_every_valid_plan_uses_one_common_q():
    snaps = [_snap("a", [(0.30, 40)]), _snap("b", [(0.30, 40)]), _snap("c", [(0.30, 40)])]
    plan = _build(snaps, outcomes=("A", "B", "C"), budget=1000.0)
    assert plan.executable
    assert plan.strategy == STRATEGY_BUY_COMPLETE_SET_MERGE
    assert all(leg.q_shares == plan.q_complete_sets for leg in plan.legs)
    assert len({leg.q_shares for leg in plan.legs}) == 1


# ── Weakest leg limits capacity ──────────────────────────────────────────────
def test_smallest_leg_limits_capacity():
    snaps = [_snap("a", [(0.40, 100)]), _snap("b", [(0.45, 10)])]
    plan = _build(snaps, budget=10_000.0)  # budget not binding → depth binds
    assert plan.executable
    assert plan.q_complete_sets == 10           # weakest leg (10), NOT 100 or 110
    assert [leg.capacity_shares for leg in plan.legs] == [100, 10]
    assert all(leg.q_shares == 10 for leg in plan.legs)


# ── L2 depth changes VWAP and can invalidate a top-of-book candidate ─────────
def test_l2_depth_changes_vwap_and_can_invalidate():
    # Top-of-book sums to 0.85 (< $1 → looks like an arb), but level 2 is dear.
    snaps = [
        _snap("a", [(0.40, 5), (0.90, 1000)], mos=1.0),
        _snap("b", [(0.45, 5), (0.90, 1000)], mos=1.0),
    ]
    top_of_book_total = 0.40 + 0.45
    assert top_of_book_total < 1.0  # naive top-of-book check would say "arb"

    # Tiny budget stays on level 1 → executable.
    small = _build(snaps, budget=4.25)
    assert small.executable
    assert small.q_complete_sets == 5
    assert small.net_cash_pnl_usd > 0

    # Large budget walks into level 2 → blended VWAP > $1 → not executable.
    big = _build(snaps, budget=100.0)
    assert not big.executable
    assert big.rejection is not None
    assert big.rejection.reason == PlanRejectReason.NEGATIVE_NET_PNL
    # VWAP genuinely moved above top-of-book.
    assert big.gross_notional_usd > big.q_complete_sets * top_of_book_total


# ── Shares and dollars cannot be interchanged ────────────────────────────────
def test_shares_and_dollars_are_separate_units():
    snaps = [_snap("a", [(0.40, 100)]), _snap("b", [(0.45, 10)])]
    plan = _build(snaps, budget=10_000.0)
    assert plan.q_complete_sets == 10          # shares
    assert plan.gross_notional_usd == 8.5      # dollars
    assert plan.q_complete_sets != plan.gross_notional_usd
    # dollars = shares * price (dimensional identity)
    vwap_sum = sum(leg.vwap_ask for leg in plan.legs)
    assert abs(plan.gross_notional_usd - plan.q_complete_sets * vwap_sum) < 1e-6
    # redemption converts shares → dollars at $1/set
    assert plan.redemption_value_usd == plan.q_complete_sets * 1.0
    assert abs(
        plan.net_cash_pnl_usd
        - (
            plan.redemption_value_usd
            - plan.gross_notional_usd
            - plan.fees_usd
            - plan.conversion_costs_usd
        )
    ) < 1e-9


# ── Rejections ───────────────────────────────────────────────────────────────
def test_unknown_fee_rejects_fail_closed():
    snaps = [_snap("a", [(0.40, 100)]), _snap("b", [(0.45, 100)])]
    plan = _build(snaps, fee=UnknownFeeModel("mystery"), budget=100.0)
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.UNKNOWN_FEE


def test_stale_book_rejects():
    stale = _snap("a", [(0.40, 100)], captured_at="2026-07-23T00:00:00+00:00")  # 24h old
    fresh = _snap("b", [(0.45, 100)])
    plan = _build([stale, fresh], budget=100.0)
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.STALE_BOOK


def test_invalid_tick_rejects():
    snaps = [_snap("a", [(0.40, 100)], tick=None), _snap("b", [(0.45, 100)])]
    plan = _build(snaps, budget=100.0)
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.INVALID_TICK


def test_below_min_order_size_rejects():
    # Only 5 shares of depth per leg but a 1000-share minimum → q < min.
    snaps = [_snap("a", [(0.40, 5)], mos=1000.0), _snap("b", [(0.45, 5)], mos=1000.0)]
    plan = _build(snaps, budget=1000.0)
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.BELOW_MIN_SIZE


def test_missing_outcome_rejects():
    snaps = [_snap("a", [(0.40, 100)])]  # one snapshot for two outcomes
    plan = _build(snaps, outcomes=("Y", "N"), budget=100.0)
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.MISSING_OUTCOME


def test_negative_net_pnl_rejects():
    snaps = [_snap("a", [(0.60, 100)]), _snap("b", [(0.55, 100)])]  # asks sum 1.15 > 1
    plan = _build(snaps, budget=100.0)
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.NEGATIVE_NET_PNL


def test_neg_risk_market_ineligible():
    snaps = [_snap("a", [(0.40, 100)], neg_risk=True), _snap("b", [(0.45, 100)])]
    plan = _build(snaps, budget=100.0)
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.NEG_RISK_INELIGIBLE


def test_sell_complete_set_strategy_ineligible():
    snaps = [_snap("a", [(0.40, 100)]), _snap("b", [(0.45, 100)])]
    plan = _build(snaps, budget=100.0, strategy="SELL_COMPLETE_SET")
    assert not plan.executable
    assert plan.rejection.reason == PlanRejectReason.INELIGIBLE_STRATEGY


# ── Shadow PnL is never realized PnL ─────────────────────────────────────────
def test_shadow_fill_is_never_realized():
    snaps = [_snap("a", [(0.40, 100)]), _snap("b", [(0.45, 100)])]
    plan = _build(snaps, budget=100.0)
    assert plan.executable
    fill = simulate_shadow_fill(plan, latency_sec=0.5)
    assert fill.label in {"shadow", "simulated"}
    assert fill.realized is False
    assert fill.realized_pnl_usd is None
    assert fill.to_dict()["realized_pnl_usd"] is None
    assert fill.net_cash_pnl_usd == plan.net_cash_pnl_usd  # shadow PnL, not realized


# ── Immutable record ─────────────────────────────────────────────────────────
def test_plan_record_is_immutable_and_complete():
    snaps = [_snap("a", [(0.40, 100)]), _snap("b", [(0.45, 100)])]
    plan = _build(snaps, budget=100.0)
    import dataclasses

    # frozen dataclass — cannot mutate the record
    try:
        plan.q_complete_sets = 999  # type: ignore[misc]
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised
    d = plan.to_dict()
    for key in (
        "snapshots",
        "legs",
        "q_complete_sets",
        "fee_quote",
        "created_at",
        "net_cash_pnl_usd",
        "rejection",
    ):
        assert key in d
    assert d["snapshots"] and d["snapshots"][0]["captured_at"] == NOW


# ── Scan reports distinguish candidate from executable plan ──────────────────
def _gamma_candidate() -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xcand",
        slug="cand",
        question="Candidate?",
        outcomes=["Yes", "No"],
        token_ids=["tok_a", "tok_b"],
        prices=[0.40, 0.45],
        total=0.85,
        edge=0.15,
        edge_bps=1500.0,
        source="gamma",
    )


def test_scan_distinguishes_candidate_from_executable(monkeypatch):
    from arb import scanner as scanner_mod

    cfg = ArbConfig(max_position_usd=15.0)

    # Case A — L2 makes the top-of-book candidate non-executable.
    def books_not_executable(token_id):
        ask = 0.40 if token_id == "tok_a" else 0.45
        return {
            "bids": [{"price": ask - 0.02, "size": "50"}],
            "asks": [{"price": ask, "size": "5"}, {"price": 0.90, "size": "1000"}],
        }

    monkeypatch.setattr(scanner_mod, "fetch_orderbook", books_not_executable)
    outcome = scanner_mod.verify_one(cfg, _gamma_candidate())
    assert outcome.opportunity is None          # candidate, but NOT executable
    assert outcome.plan is not None             # an execution-plan record exists
    assert not outcome.plan.executable
    assert outcome.plan.rejection is not None

    # Case B — genuinely executable at depth.
    def books_executable(token_id):
        ask = 0.40 if token_id == "tok_a" else 0.45
        return {
            "bids": [{"price": ask - 0.02, "size": "50"}],
            "asks": [{"price": ask, "size": "50"}],
        }

    monkeypatch.setattr(scanner_mod, "fetch_orderbook", books_executable)
    outcome2 = scanner_mod.verify_one(cfg, _gamma_candidate())
    assert outcome2.opportunity is not None     # executable plan
    assert outcome2.plan is not None and outcome2.plan.executable
