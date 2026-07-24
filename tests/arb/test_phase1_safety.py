"""Phase 1 safety acceptance tests — scanner/shadow-first, truthful accounting.

Proves the seven required guarantees:
  1. Default worker mode cannot create paper or live execution.
  2. Explicit shadow mode records observations but no order/fill.
  3. Self-tune is disabled by default and does not apply stale overrides.
  4. Expected PnL cannot become realized PnL.
  5. Open/candidate records cannot be labeled a win or true arb.
  6. Sell-bundle execution is rejected.
  7. A live order acknowledgement cannot create a FILLED state.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from arb.clob_live import LiveBundleResult, LiveLegResult, execute_buy_bundle_live
from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.execute import execute_opportunity
from arb.labels import Label, label_history
from arb.models import ExecMode, OppState, SafetyMode
from arb.reconcile import reconcile
from arb.scanner import ScanResult
from arb.state import OpportunityStore
from arb.worker import ArbWorker, WorkerConfig


def _buy(cid: str = "0xacc", edge_bps: float = 1000.0) -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id=cid,
        slug="s",
        question="Q?",
        outcomes=["Yes", "No"],
        token_ids=["tok_a", "tok_b"],
        prices=[0.40, 0.45],
        total=0.85,
        edge=edge_bps / 10_000,
        edge_bps=edge_bps,
        source="clob_asks",
    )


def _sell(cid: str = "0xsell", edge_bps: float = 1000.0) -> Opportunity:
    return Opportunity(
        kind=ArbKind.SELL_BUNDLE,
        condition_id=cid,
        slug="s",
        question="Q?",
        outcomes=["Yes", "No"],
        token_ids=["tok_a", "tok_b"],
        prices=[0.60, 0.55],
        total=1.15,
        edge=edge_bps / 10_000,
        edge_bps=edge_bps,
        source="clob_bids",
    )


def _stub_scan_with_verified(opp: Opportunity):
    def fake_scan(config, gamma_only=False, persist=True):
        store = OpportunityStore(config.state_db)
        store.save(opp, state=OppState.CLOB_VERIFIED, verified=True, ask_depth=50, bid_depth=50)
        return ScanResult(scanned=1, gamma_hits=[], verified_hits=[opp], rejected=[], run_id=1)

    return fake_scan


# 1. Default worker mode cannot create paper or live execution -----------------
def test_default_worker_creates_no_execution(tmp_path: Path, monkeypatch):
    cfg = ArbConfig(state_dir=tmp_path)  # all defaults → SCAN_ONLY
    assert cfg.safety_mode == SafetyMode.SCAN_ONLY
    assert cfg.execution_allowed() is False

    from arb import worker as worker_mod

    monkeypatch.setattr(worker_mod, "run_scan", _stub_scan_with_verified(_buy("0xd1")))
    wc = WorkerConfig(scan_limit=0, trade_limit=5, use_ws=False, run_self_tune=False)
    out = ArbWorker(cfg, wc).run_once(jobs=["loop"])
    loop = out["jobs"]["loop"]
    assert loop["executed"] is False
    assert loop["traded"] == 0

    store = OpportunityStore(cfg.state_db)
    assert store.count_fills_today() == 0
    assert store.count_open() == 0  # nothing entered the execution pipeline


def test_default_execute_call_is_scan_only(tmp_path: Path):
    cfg = ArbConfig(state_dir=tmp_path)
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_buy("0xd2"), state=OppState.CLOB_VERIFIED, verified=True)
    res = execute_opportunity(cfg, store, _buy("0xd2"), opportunity_id=oid, ask_depth=50, bid_depth=50)
    assert res.status == "scan_only"
    assert res.fill is None
    # The observation is preserved (not rejected) and no fill is created.
    assert store.get(oid)["state"] == OppState.CLOB_VERIFIED.value
    assert store.count_fills_today() == 0


# 2. Explicit shadow mode records observations but no order/fill ---------------
def test_shadow_mode_records_but_does_not_execute(tmp_path: Path, monkeypatch):
    cfg = ArbConfig(state_dir=tmp_path, safety_mode=SafetyMode.SHADOW)
    assert cfg.execution_allowed() is False

    from arb import worker as worker_mod

    monkeypatch.setattr(worker_mod, "run_scan", _stub_scan_with_verified(_buy("0xs1")))
    wc = WorkerConfig(scan_limit=0, trade_limit=5, use_ws=False, run_self_tune=False)
    out = ArbWorker(cfg, wc).run_once(jobs=["loop"])
    loop = out["jobs"]["loop"]
    assert loop["executed"] is False
    assert loop["traded"] == 0

    store = OpportunityStore(cfg.state_db)
    # Observation recorded ...
    assert store.count(state=OppState.CLOB_VERIFIED) >= 1
    # ... but no order/fill created.
    assert store.count_fills_today() == 0
    assert store.count_open() == 0

    res = execute_opportunity(cfg, store, _buy("0xs1"), ask_depth=50, bid_depth=50)
    assert res.status == "shadow"
    assert res.fill is None


# 3. Self-tune disabled by default and does not apply stale overrides ----------
def test_self_tune_off_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ARB_SELF_TUNE", raising=False)
    monkeypatch.setenv("ARB_STATE_DIR", str(tmp_path))
    cfg = ArbConfig.from_env()
    assert cfg.self_tune is False


def test_stale_overrides_not_applied_when_disabled(monkeypatch, tmp_path: Path):
    from arb.self_tune import apply_overrides_to_config, save_overrides

    monkeypatch.setenv("ARB_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ARB_MIN_EDGE_BPS", "25")
    monkeypatch.delenv("ARB_SELF_TUNE", raising=False)

    # A historical override file exists on disk (kept for audit) ...
    seed = ArbConfig(state_dir=tmp_path, self_tune=True)
    save_overrides(seed, {"ARB_MIN_EDGE_BPS": 18.0})
    assert (tmp_path / "self_tune.json").exists()

    # ... but with self-tune disabled it is neither loaded nor applied.
    cfg = ArbConfig.from_env()
    assert cfg.self_tune is False
    assert cfg.min_edge_bps == 25.0
    assert apply_overrides_to_config(cfg).min_edge_bps == 25.0
    # File preserved for audit.
    assert (tmp_path / "self_tune.json").exists()


# 4. Expected PnL cannot become realized PnL ----------------------------------
def test_expected_pnl_never_becomes_realized(tmp_path: Path):
    cfg = ArbConfig(
        state_dir=tmp_path,
        safety_mode=SafetyMode.PAPER_EXECUTION,
        paper_execution_enabled=True,
        study_mode=False,
        exec_mode=ExecMode.PAPER,
        paper_realistic=False,
        min_edge_bps=50.0,
        min_book_depth=1.0,
        max_position_usd=10.0,
    )
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_buy("0xp1"), state=OppState.CLOB_VERIFIED, verified=True)
    res = execute_opportunity(cfg, store, _buy("0xp1"), opportunity_id=oid, ask_depth=50, bid_depth=50)
    assert res.status == "paper_filled"

    report = reconcile(cfg, store, settle_paper=True)  # even asking to settle ...
    assert report.settled == 0                          # ... nothing is settled
    assert report.realized_pnl_sum == 0.0
    assert report.unresolved == 1
    assert report.expected_pnl_sum > 0
    assert store.list_fills()[0]["realized_pnl"] is None
    assert store.get(oid)["state"] == OppState.FILLED.value  # not auto-closed


# 5. Open/candidate records cannot be labeled a win or true arb ----------------
def test_candidate_and_verified_never_win(tmp_path: Path):
    store = OpportunityStore(tmp_path / "db.sqlite")
    store.save(_buy("0xc1"), state=OppState.GAMMA_FLAG, verified=False)
    store.save(_buy("0xc2"), state=OppState.CLOB_VERIFIED, verified=True)
    labeled = {r.condition_id: r.label for r in label_history(store, days=30)}
    assert labeled["0xc1"] == Label.CANDIDATE
    assert labeled["0xc2"] == Label.SHADOW
    assert not hasattr(Label, "TRUE_ARB")
    assert not hasattr(Label, "PAPER_WIN")


# 6. Sell-bundle execution is rejected ----------------------------------------
def test_sell_bundle_execution_rejected(tmp_path: Path):
    cfg = ArbConfig(
        state_dir=tmp_path,
        safety_mode=SafetyMode.PAPER_EXECUTION,
        paper_execution_enabled=True,
        study_mode=False,
        exec_mode=ExecMode.PAPER,
        min_edge_bps=50.0,
        min_book_depth=1.0,
    )
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_sell("0xsb"), state=OppState.CLOB_VERIFIED, verified=True)
    res = execute_opportunity(cfg, store, _sell("0xsb"), opportunity_id=oid, ask_depth=50, bid_depth=50)
    assert res.status == "unsupported_strategy"
    assert res.fill is None
    assert store.count_fills_today() == 0


def test_sell_bundle_live_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    cfg = ArbConfig(
        state_dir=tmp_path,
        safety_mode=SafetyMode.LIVE,
        allow_live=True,
        dry_run=False,
        study_mode=False,
        exec_mode=ExecMode.LIVE,
    )
    res = execute_buy_bundle_live(cfg, _sell("0xsb2"), size_usd=10.0)
    assert res.ok is False
    assert "UNSUPPORTED_STRATEGY" in (res.error or "")


# 7. A live order acknowledgement cannot create a FILLED state -----------------
def _live_cfg(tmp_path: Path) -> ArbConfig:
    return ArbConfig(
        state_dir=tmp_path,
        safety_mode=SafetyMode.LIVE,
        study_mode=False,
        dry_run=False,
        exec_mode=ExecMode.LIVE,
        allow_live=True,
        min_edge_bps=50.0,
        max_position_usd=10.0,
        min_book_depth=1.0,
    )


def test_live_ack_does_not_fill(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "cd" * 32)
    cfg = _live_cfg(tmp_path)
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_buy("0xack"), state=OppState.CLOB_VERIFIED, verified=True)

    # Orders merely acknowledged/resting (status "live"), not matched.
    ack = LiveBundleResult(
        ok=True,
        mode="live",
        legs=[
            LiveLegResult(token_id="tok_a", side="BUY", price=0.4, size=12.5, order_id="o1", status="live"),
            LiveLegResult(token_id="tok_b", side="BUY", price=0.45, size=12.5, order_id="o2", status="live"),
        ],
        client_ready=True,
        size_usd=10.0,
        fill_total=0.85,
        fill_prices=[0.4, 0.45],
        order_ids=["o1", "o2"],
    )
    with patch("arb.clob_live.execute_buy_bundle_live", return_value=ack):
        res = execute_opportunity(cfg, store, _buy("0xack"), opportunity_id=oid, ask_depth=50, bid_depth=50)
    assert res.status == "order_posted"
    assert store.get(oid)["state"] == OppState.ORDER_PLACED.value
    assert store.get(oid)["state"] != OppState.FILLED.value
    assert store.count_fills_today() == 0


def test_live_unequal_matched_legs_not_filled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ef" * 32)
    cfg = _live_cfg(tmp_path)
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_buy("0xuneq"), state=OppState.CLOB_VERIFIED, verified=True)

    # Both legs matched, but with UNEQUAL quantities → not a complete-set fill.
    uneven = LiveBundleResult(
        ok=True,
        mode="live",
        legs=[
            LiveLegResult(token_id="tok_a", side="BUY", price=0.4, size=12.5, order_id="o1", status="matched"),
            LiveLegResult(token_id="tok_b", side="BUY", price=0.45, size=11.1, order_id="o2", status="matched"),
        ],
        client_ready=True,
        size_usd=10.0,
        fill_total=0.85,
        fill_prices=[0.4, 0.45],
        order_ids=["o1", "o2"],
    )
    with patch("arb.clob_live.execute_buy_bundle_live", return_value=uneven):
        res = execute_opportunity(cfg, store, _buy("0xuneq"), opportunity_id=oid, ask_depth=50, bid_depth=50)
    assert res.status == "live_partial"
    assert store.get(oid)["state"] != OppState.FILLED.value
    assert store.count_fills_today() == 0
