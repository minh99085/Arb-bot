"""Tests for Phase 1 study store and scanner helpers."""

from pathlib import Path

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.ledger import append_ledger
from arb.models import OppState, RejectReason
from arb.scanner import format_alert, ScanResult
from arb.state import OpportunityStore


def _opp(edge_bps: float = 100.0) -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xabc",
        slug="test",
        question="Test market?",
        outcomes=["Yes", "No"],
        token_ids=["a", "b"],
        prices=[0.45, 0.50],
        total=0.95,
        edge=edge_bps / 10_000,
        edge_bps=edge_bps,
        source="gamma",
    )


def test_scan_run_and_states(tmp_path: Path):
    store = OpportunityStore(tmp_path / "opps.sqlite")
    run_id = store.start_scan_run(study_mode=True)
    assert run_id == 1
    store.save(_opp(), state=OppState.GAMMA_FLAG, scan_run_id=run_id)
    store.save(
        _opp(200),
        state=OppState.CLOB_VERIFIED,
        verified=True,
        hypothetical_pnl=0.02,
        scan_run_id=run_id,
    )
    store.save(
        _opp(80),
        state=OppState.REJECTED,
        reject_reason=RejectReason.EDGE_EVAPORATED,
        scan_run_id=run_id,
    )
    store.finish_scan_run(
        run_id, scanned=10, gamma_hits=3, verified_hits=1, rejected=1, notes="test"
    )
    assert store.count(state=OppState.CLOB_VERIFIED) == 1
    assert store.count(state=OppState.REJECTED) == 1
    summary = store.study_summary(days=30)
    assert summary["verified_hits"] == 1
    assert summary["rejected"] == 1
    assert "edge_evaporated" in summary["reject_breakdown"]


def test_ledger_append(tmp_path: Path):
    path = tmp_path / "LEDGER.md"
    append_ledger(
        path,
        run_id=1,
        scanned=5,
        gamma_hits=1,
        verified=[_opp()],
        rejected=[(_opp(50), RejectReason.ILLIQUID)],
    )
    text = path.read_text()
    assert "Scan run #1" in text
    assert "CLOB_VERIFIED" in text
    assert "illiquid" in text


def test_format_alert_only_when_verified():
    empty = ScanResult(scanned=10, gamma_hits=[], verified_hits=[], rejected=[])
    assert format_alert(empty) is None
    hit = ScanResult(scanned=10, gamma_hits=[_opp()], verified_hits=[_opp()], rejected=[])
    alert = format_alert(hit)
    assert alert is not None
    assert "ARB ALERT" in alert


def test_config_study_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARB_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ARB_STUDY_MODE", raising=False)
    cfg = ArbConfig.from_env()
    assert cfg.study_mode is True
    assert cfg.dry_run is True
    assert cfg.state_db.parent == tmp_path
