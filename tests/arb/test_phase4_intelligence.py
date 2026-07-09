"""Phase 4 intelligence plane tests."""

from pathlib import Path

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.labels import Label, label_counts, label_history
from arb.models import ExecMode, OppState, RejectReason
from arb.postmortem import run_postmortem
from arb.proposals import ProposalStore, new_proposal, render_env_snippet
from arb.state import OpportunityStore


def _opp(condition_id: str = "0xlab") -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id=condition_id,
        slug="lab",
        question="Label me?",
        outcomes=["Yes", "No"],
        token_ids=["a", "b"],
        prices=[0.4, 0.45],
        total=0.85,
        edge=0.1,
        edge_bps=1000.0,
        source="gamma",
    )


def test_label_false_positive_and_ws(tmp_path: Path):
    store = OpportunityStore(tmp_path / "db.sqlite")
    store.save(_opp("0x1"), state=OppState.REJECTED, reject_reason=RejectReason.EDGE_EVAPORATED)
    store.save(
        _opp("0x2"),
        state=OppState.REJECTED,
        reject_reason=None,
    )
    # manually set ws reason via transition path
    oid = store.save(_opp("0x3"), state=OppState.CLOB_VERIFIED, verified=True)
    store.transition(oid, OppState.REJECTED, reason="ws_reverify:edge_evaporated")

    labeled = label_history(store, days=30)
    counts = label_counts(labeled)
    assert counts.get(Label.FALSE_POSITIVE.value, 0) >= 1
    assert counts.get(Label.WS_EVAPORATED.value, 0) >= 1


def test_label_paper_win(tmp_path: Path):
    cfg = ArbConfig(
        state_dir=tmp_path,
        study_mode=False,
        exec_mode=ExecMode.PAPER,
        min_book_depth=1.0,
        max_position_usd=10.0,
    )
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_opp(), state=OppState.CLOB_VERIFIED, verified=True)
    store.transition(oid, OppState.FILLED, reason="paper")
    store.record_fill(
        opportunity_id=oid,
        mode="paper",
        size_usd=10.0,
        fill_total=0.85,
        fees_usd=0.0,
        slippage_usd=0.0,
        expected_pnl=1.0,
        fill_prices=[0.4, 0.45],
        realized_pnl=1.0,
    )
    store.transition(oid, OppState.CLOSED, reason="done")
    labeled = label_history(store, days=30)
    assert any(r.label == Label.PAPER_WIN for r in labeled)


def test_proposal_human_gate(tmp_path: Path):
    store = ProposalStore(tmp_path / "proposals.json")
    p = new_proposal(
        key="ARB_MIN_EDGE_BPS",
        current_value=50,
        proposed_value=75,
        rationale="test",
    )
    store.add(p)
    approved = store.decide(p.id, approve=True, by="tester")
    assert approved.status == "approved"
    snippet = render_env_snippet([approved])
    assert "ARB_MIN_EDGE_BPS=75" in snippet


def test_postmortem_creates_fp_proposal(tmp_path: Path):
    cfg = ArbConfig(state_dir=tmp_path, min_edge_bps=50.0, ws_watch_sec=30.0)
    store = OpportunityStore(cfg.state_db)
    # 10 false positives → should propose raising edge
    for i in range(10):
        store.save(
            _opp(f"0xfp{i}"),
            state=OppState.REJECTED,
            reject_reason=RejectReason.EDGE_EVAPORATED,
        )
    report = run_postmortem(cfg, store, days=30, create_proposals=True)
    assert report.total_labeled == 10
    assert report.false_positive_rate == 1.0
    assert report.proposals_created
    assert report.dataset_path and Path(report.dataset_path).exists()
    assert report.report_path and Path(report.report_path).exists()
