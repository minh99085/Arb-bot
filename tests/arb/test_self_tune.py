"""Self-tune autonomous learning tests."""

from __future__ import annotations

from pathlib import Path

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.models import OppState, RejectReason
from arb.self_tune import (
    apply_overrides_to_config,
    load_overrides,
    run_self_tune,
    status_dict,
)
from arb.state import OpportunityStore


def _opp(cid: str = "0x1") -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id=cid,
        slug="s",
        question="Q?",
        outcomes=["Y", "N"],
        token_ids=["a", "b"],
        prices=[0.4, 0.45],
        total=0.85,
        edge=0.05,
        edge_bps=500.0,
        source="gamma",
    )


def test_self_tune_lowers_edge_when_quiet(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARB_SELF_TUNE", "true")
    cfg = ArbConfig(state_dir=tmp_path, min_edge_bps=20.0, verify_top_n=40, self_tune=True)
    store = OpportunityStore(cfg.state_db)
    # Many false positives, no verified/paper wins → explore
    for i in range(8):
        store.save(
            _opp(f"0xq{i}"),
            state=OppState.REJECTED,
            reject_reason=RejectReason.EDGE_EVAPORATED,
        )
    report = run_self_tune(cfg, store, days=30, force=True)
    assert report.enabled
    assert any(c.key == "ARB_MIN_EDGE_BPS" for c in report.applied)
    edge_change = next(c for c in report.applied if c.key == "ARB_MIN_EDGE_BPS")
    assert edge_change.new_value < edge_change.old_value
    overrides = load_overrides(cfg)
    assert "ARB_MIN_EDGE_BPS" in overrides


def test_self_tune_ignores_legacy_synthetic(tmp_path: Path, monkeypatch):
    """Synthetic (auto-settled) realized results never drive tuning.

    Old-style paper fills carrying realized_pnl are labeled LEGACY_SYNTHETIC and
    must not cause position-size / activity changes (those rules are removed).
    """
    from arb.labels import Label, label_history

    monkeypatch.setenv("ARB_SELF_TUNE", "true")
    cfg = ArbConfig(
        state_dir=tmp_path,
        min_edge_bps=25.0,
        max_position_usd=20.0,
        max_open_positions=10,
        max_daily_trades=20,
        self_tune=True,
    )
    store = OpportunityStore(cfg.state_db)
    for i in range(6):
        oid = store.save(_opp(f"0xl{i}"), state=OppState.FILLED, verified=True)
        store.record_fill(
            opportunity_id=oid,
            mode="paper",
            size_usd=10.0,
            fill_total=0.9,
            fees_usd=0.0,
            slippage_usd=0.0,
            expected_pnl=-0.5,
            fill_prices=[0.45, 0.45],
            realized_pnl=-0.5,
        )
        store.transition(oid, OppState.CLOSED, reason="done")

    # Those fills are legacy synthetic, never "paper losses".
    labeled = label_history(store, days=30)
    assert all(r.label == Label.LEGACY_SYNTHETIC for r in labeled)

    report = run_self_tune(cfg, store, days=30, force=True)
    # No size / open / daily-trade / activity tuning from synthetic outcomes.
    tuned_keys = {c.key for c in report.applied}
    assert "ARB_MAX_POSITION_USD" not in tuned_keys
    assert "ARB_MAX_OPEN_POSITIONS" not in tuned_keys
    assert "ARB_MAX_DAILY_TRADES" not in tuned_keys
    assert "ARB_WORKER_TRADE_LIMIT" not in tuned_keys


def test_apply_overrides_to_config(tmp_path: Path):
    cfg = ArbConfig(state_dir=tmp_path, min_edge_bps=25.0, self_tune=True)
    from arb.self_tune import save_overrides

    save_overrides(cfg, {"ARB_MIN_EDGE_BPS": 18.0, "ARB_VERIFY_TOP_N": 120})
    merged = apply_overrides_to_config(cfg)
    assert merged.min_edge_bps == 18.0
    assert merged.verify_top_n == 120


def test_from_env_merges_self_tune(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARB_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ARB_SELF_TUNE", "true")
    monkeypatch.setenv("ARB_MIN_EDGE_BPS", "25")
    cfg0 = ArbConfig.from_env(apply_self_tune=False)
    from arb.self_tune import save_overrides

    save_overrides(cfg0, {"ARB_MIN_EDGE_BPS": 20.0})
    cfg = ArbConfig.from_env(apply_self_tune=True)
    assert cfg.min_edge_bps == 20.0
    st = status_dict(cfg)
    assert st["enabled"] is True
