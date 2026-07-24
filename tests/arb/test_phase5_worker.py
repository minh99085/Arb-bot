"""Phase 5 worker tests — no network daemon."""

from pathlib import Path

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.models import ExecMode, OppState, SafetyMode
from arb.state import OpportunityStore
from arb.worker import ArbWorker, WorkerConfig


def test_worker_once_scan_and_reconcile(tmp_path: Path, monkeypatch):
    cfg = ArbConfig(
        state_dir=tmp_path,
        study_mode=True,
        exec_mode=ExecMode.PAPER,
        max_markets=0,
    )
    wc = WorkerConfig(scan_limit=0, paper=True, run_postmortem=False, run_self_tune=False)

    # Avoid live Gamma: stub run_scan
    from arb import worker as worker_mod
    from arb.scanner import ScanResult

    def fake_scan(config, gamma_only=False, persist=True):
        store = OpportunityStore(config.state_db)
        store.start_scan_run(study_mode=True)
        return ScanResult(scanned=0, gamma_hits=[], verified_hits=[], rejected=[], run_id=1)

    monkeypatch.setattr(worker_mod, "run_scan", fake_scan)
    w = ArbWorker(cfg, wc)
    out = w.run_once(jobs=["scan", "reconcile"])
    assert "scan" in out["jobs"]
    assert out["jobs"]["scan"]["scanned"] == 0
    assert "reconcile" in out["jobs"]
    assert (tmp_path / "worker_status.json").exists()


def test_worker_once_loop_paper(tmp_path: Path, monkeypatch):
    # Explicit opt-in to paper execution (scanner/shadow-first defaults do not).
    cfg = ArbConfig(
        state_dir=tmp_path,
        safety_mode=SafetyMode.PAPER_EXECUTION,
        paper_execution_enabled=True,
        study_mode=False,
        exec_mode=ExecMode.PAPER,
        min_book_depth=1.0,
        max_position_usd=10.0,
        paper_realistic=False,
    )
    wc = WorkerConfig(scan_limit=0, trade_limit=2, paper=True, use_ws=False, run_self_tune=False)

    from arb import worker as worker_mod
    from arb.scanner import ScanResult

    opp = Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xw5",
        slug="w",
        question="Worker loop?",
        outcomes=["Yes", "No"],
        token_ids=["a", "b"],
        prices=[0.4, 0.45],
        total=0.85,
        edge=0.1,
        edge_bps=1000.0,
        source="clob_asks",
    )

    def fake_scan(config, gamma_only=False, persist=True):
        store = OpportunityStore(config.state_db)
        store.save(opp, state=OppState.CLOB_VERIFIED, verified=True, ask_depth=50, bid_depth=50)
        return ScanResult(scanned=1, gamma_hits=[], verified_hits=[opp], rejected=[], run_id=1)

    monkeypatch.setattr(worker_mod, "run_scan", fake_scan)
    w = ArbWorker(cfg, wc)
    out = w.run_once(jobs=["loop"])
    loop = out["jobs"]["loop"]
    assert loop["traded"] >= 1
    assert loop["executed"] is True
    # No synthetic realization: the paper fill stays UNRESOLVED at 0 realized PnL.
    assert loop["realized_pnl"] == 0
    assert loop["unresolved"] >= 1


def test_worker_gamma_only_is_not_traded(tmp_path: Path, monkeypatch):
    """Even with execution enabled, unverified gamma-only signals are never traded.

    The old ``paper_gamma_fallback`` path (trading GAMMA_FLAG signals) is gone —
    only CLOB_VERIFIED opportunities are eligible.
    """
    cfg = ArbConfig(
        state_dir=tmp_path,
        safety_mode=SafetyMode.PAPER_EXECUTION,
        paper_execution_enabled=True,
        study_mode=False,
        exec_mode=ExecMode.PAPER,
        paper_gamma_fallback=True,  # deliberately on — must be ignored
        paper_realistic=False,
        min_edge_bps=-30.0,
        min_book_depth=1.0,
        max_position_usd=10.0,
    )
    wc = WorkerConfig(scan_limit=0, trade_limit=1, paper=True, use_ws=False, run_self_tune=False)

    from arb import worker as worker_mod
    from arb.scanner import ScanResult

    opp = Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xgamma",
        slug="g",
        question="Gamma only?",
        outcomes=["Yes", "No"],
        token_ids=["a", "b"],
        prices=[0.48, 0.48],
        total=0.96,
        edge=0.02,
        edge_bps=200.0,
        source="gamma",
    )

    def fake_scan(config, gamma_only=False, persist=True):
        store = OpportunityStore(config.state_db)
        store.save(opp, state=OppState.GAMMA_FLAG, verified=False)
        return ScanResult(scanned=1, gamma_hits=[opp], verified_hits=[], rejected=[], run_id=1)

    monkeypatch.setattr(worker_mod, "run_scan", fake_scan)
    w = ArbWorker(cfg, wc)
    out = w.run_once(jobs=["loop"])
    loop = out["jobs"]["loop"]
    assert loop["traded"] == 0
    assert "gamma_fallback" not in loop
    # The gamma flag is still recorded (research signal), just not traded.
    store = OpportunityStore(cfg.state_db)
    assert store.count(state=OppState.GAMMA_FLAG) >= 1
    assert store.count_fills_today() == 0


def test_worker_run_forever_stops(tmp_path: Path, monkeypatch):
    cfg = ArbConfig(state_dir=tmp_path, study_mode=True)
    wc = WorkerConfig(
        scan_interval_sec=1000,
        loop_interval_sec=1000,
        reconcile_interval_sec=1000,
        postmortem_interval_sec=1000,
        heartbeat_sec=1000,
        run_postmortem=False,
        run_self_tune=False,
    )
    sleeps = {"n": 0}

    def fake_sleep(_s):
        sleeps["n"] += 1
        # stop after first sleep
        w.request_stop()

    from arb import worker as worker_mod
    from arb.scanner import ScanResult

    monkeypatch.setattr(
        worker_mod,
        "run_scan",
        lambda *a, **k: ScanResult(0, [], [], [], run_id=None),
    )
    w = ArbWorker(cfg, wc, sleep_fn=fake_sleep)
    w.run_forever()
    assert sleeps["n"] >= 1
    assert w.status.running is False
    assert not (tmp_path / "worker.pid").exists()
