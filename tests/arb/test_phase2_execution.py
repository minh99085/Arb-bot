"""Phase 2 risk, paper, execute, reconcile tests."""

from pathlib import Path

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.execute import execute_opportunity
from arb.models import ExecMode, OppState, RiskRejectReason
from arb.paper import simulate_paper_fill
from arb.reconcile import reconcile
from arb.risk import check_risk
from arb.state import OpportunityStore


def _opp(edge_bps: float = 200.0, condition_id: str = "0xabc") -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id=condition_id,
        slug="test",
        question="Test market?",
        outcomes=["Yes", "No"],
        token_ids=["a", "b"],
        prices=[0.40, 0.45],
        total=0.85,
        edge=edge_bps / 10_000,
        edge_bps=edge_bps,
        source="clob_asks",
    )


def _cfg(tmp_path: Path, **kwargs) -> ArbConfig:
    base = dict(
        state_dir=tmp_path,
        study_mode=False,
        dry_run=True,
        exec_mode=ExecMode.PAPER,
        kill_switch=False,
        min_edge_bps=50.0,
        max_position_usd=10.0,
        max_open_positions=3,
        max_daily_trades=10,
        max_daily_loss_usd=50.0,
        min_book_depth=1.0,
    )
    base.update(kwargs)
    return ArbConfig(**base)


def test_risk_kill_switch(tmp_path: Path):
    store = OpportunityStore(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path, kill_switch=True)
    d = check_risk(cfg, store, _opp())
    assert not d.ok
    assert d.reason == RiskRejectReason.KILL_SWITCH


def test_risk_study_mode(tmp_path: Path):
    store = OpportunityStore(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path, study_mode=True)
    d = check_risk(cfg, store, _opp())
    assert not d.ok
    assert d.reason == RiskRejectReason.STUDY_MODE


def test_risk_ok_and_size(tmp_path: Path):
    store = OpportunityStore(tmp_path / "db.sqlite")
    cfg = _cfg(tmp_path)
    d = check_risk(cfg, store, _opp(), ask_depth=100.0, bid_depth=100.0)
    assert d.ok
    assert d.size_usd == 10.0


def test_paper_fill_buy_bundle():
    cfg = ArbConfig(paper_slippage_bps=0.0, taker_fee_bps=0.0, max_position_usd=10.0)
    fill = simulate_paper_fill(cfg, _opp(), size_usd=10.0)
    assert fill.fill_total == 0.85
    assert fill.expected_pnl == round(0.15 * 10.0, 6)


def test_execute_paper_and_reconcile(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = OpportunityStore(cfg.state_db)
    opp_id = store.save(_opp(), state=OppState.CLOB_VERIFIED, verified=True, ask_depth=50, bid_depth=50)
    res = execute_opportunity(
        cfg, store, _opp(), opportunity_id=opp_id, ask_depth=50, bid_depth=50
    )
    assert res.status == "paper_filled"
    assert store.get(opp_id)["state"] == OppState.FILLED.value
    assert store.count_fills_today() == 1

    report = reconcile(cfg, store, settle_paper=True)
    assert report.settled == 1
    assert report.realized_pnl_sum > 0
    assert store.get(opp_id)["state"] == OppState.CLOSED.value
    assert store.count_open() == 0


def test_duplicate_open_blocked(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = OpportunityStore(cfg.state_db)
    opp = _opp()
    id1 = store.save(opp, state=OppState.CLOB_VERIFIED, verified=True)
    r1 = execute_opportunity(cfg, store, opp, opportunity_id=id1, ask_depth=20, bid_depth=20)
    assert r1.status == "paper_filled"
    # second same condition while still FILLED (before reconcile)
    id2 = store.save(_opp(), state=OppState.CLOB_VERIFIED, verified=True)
    r2 = execute_opportunity(cfg, store, _opp(), opportunity_id=id2, ask_depth=20, bid_depth=20)
    assert r2.status == "risk_rejected"
    assert r2.risk and r2.risk.reason == RiskRejectReason.DUPLICATE_OPEN
