"""Phase 2 risk, paper, execute, reconcile tests."""

from pathlib import Path

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.execute import execute_opportunity
from arb.models import ExecMode, OppState, RiskRejectReason, SafetyMode
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
    # Opt in to paper execution explicitly — scanner/shadow-first defaults do not.
    base = dict(
        state_dir=tmp_path,
        safety_mode=SafetyMode.PAPER_EXECUTION,
        paper_execution_enabled=True,
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
        paper_realistic=False,
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
    # size_usd is a CASH BUDGET; it buys q complete sets (shares), not a notional.
    cfg = ArbConfig(taker_fee_bps=0.0, max_position_usd=10.0)
    fill = simulate_paper_fill(cfg, _opp(), size_usd=10.0)
    assert fill.fill_total == 0.85                       # sum of per-set ask prices
    # q = budget / total = 10 / 0.85; net = q * $1 (redeem) - gross_notional
    q = 10.0 / 0.85
    assert abs(fill.q_complete_sets - q) < 1e-6
    assert abs(fill.expected_pnl - (q - 10.0)) < 1e-6   # ≈ 1.7647, not 1.5
    assert fill.label == "shadow"                        # never realized


def test_execute_paper_and_reconcile_leaves_unresolved(tmp_path: Path):
    """Paper fills are recorded but NEVER synthetically realized/closed."""
    cfg = _cfg(tmp_path)
    store = OpportunityStore(cfg.state_db)
    opp_id = store.save(_opp(), state=OppState.CLOB_VERIFIED, verified=True, ask_depth=50, bid_depth=50)
    res = execute_opportunity(
        cfg, store, _opp(), opportunity_id=opp_id, ask_depth=50, bid_depth=50
    )
    assert res.status == "paper_filled"
    assert store.get(opp_id)["state"] == OppState.FILLED.value
    assert store.count_fills_today() == 1

    # The recorded fill must carry expected PnL but NO realized PnL.
    fill = store.list_fills()[0]
    assert fill["expected_pnl"] is not None
    assert fill["realized_pnl"] is None

    # reconcile must not settle, not realize, and not close the position.
    report = reconcile(cfg, store, settle_paper=True)
    assert report.settled == 0
    assert report.unresolved == 1
    assert report.realized_pnl_sum == 0.0
    assert report.expected_pnl_sum > 0
    assert store.get(opp_id)["state"] == OppState.FILLED.value
    assert store.count_open() == 1


def test_realistic_paper_rejects_gamma_source(tmp_path: Path):
    cfg = _cfg(tmp_path, paper_realistic=True, min_edge_bps=25.0)
    store = OpportunityStore(cfg.state_db)
    base = _opp()
    gamma_opp = Opportunity(
        kind=base.kind,
        condition_id=base.condition_id,
        slug=base.slug,
        question=base.question,
        outcomes=base.outcomes,
        token_ids=base.token_ids,
        prices=base.prices,
        total=base.total,
        edge=base.edge,
        edge_bps=base.edge_bps,
        source="gamma",
    )
    oid = store.save(gamma_opp, state=OppState.GAMMA_FLAG, verified=False)
    res = execute_opportunity(cfg, store, gamma_opp, opportunity_id=oid)
    assert res.status == "gamma_rejected"


def test_realistic_paper_refreshes_clob(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path, paper_realistic=True, min_edge_bps=25.0)
    store = OpportunityStore(cfg.state_db)
    opp = _opp(edge_bps=200.0)
    oid = store.save(opp, state=OppState.CLOB_VERIFIED, verified=True, ask_depth=50, bid_depth=50)

    from arb import execute as execute_mod
    from arb.scanner import VerifyOutcome

    monkeypatch.setattr(
        execute_mod,
        "verify_one",
        lambda config, o: VerifyOutcome(opp, None, 50.0, 50.0),
    )
    res = execute_opportunity(cfg, store, opp, opportunity_id=oid, ask_depth=50, bid_depth=50)
    assert res.status == "paper_filled"
    assert "clob refresh" in res.detail


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
