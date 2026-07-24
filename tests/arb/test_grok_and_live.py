"""Grok intelligence + live CLOB execution tests (mocked; no network)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from arb.clob_live import LiveBundleResult, LiveLegResult, execute_buy_bundle_live
from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.execute import execute_opportunity
from arb.grok import (
    ALLOWED_PROPOSAL_KEYS,
    GrokResult,
    _extract_json_block,
    analyze_postmortem,
    apply_grok_proposals,
)
from arb.models import ExecMode, OppState, SafetyMode
from arb.postmortem import run_postmortem
from arb.state import OpportunityStore


def _opp() -> Opportunity:
    return Opportunity(
        kind=ArbKind.BUY_BUNDLE,
        condition_id="0xlive",
        slug="live-test",
        question="Live?",
        outcomes=["Yes", "No"],
        token_ids=["tok_a", "tok_b"],
        prices=[0.40, 0.45],
        total=0.85,
        edge=0.1,
        edge_bps=1000.0,
        source="clob_asks",
    )


def _cfg(tmp_path: Path, **kwargs) -> ArbConfig:
    base = dict(
        state_dir=tmp_path,
        safety_mode=SafetyMode.LIVE,
        study_mode=False,
        dry_run=False,
        exec_mode=ExecMode.LIVE,
        kill_switch=False,
        allow_live=True,
        min_edge_bps=50.0,
        max_position_usd=10.0,
        max_open_positions=3,
        max_daily_trades=10,
        max_daily_loss_usd=50.0,
        min_book_depth=1.0,
    )
    base.update(kwargs)
    return ArbConfig(**base)


def test_extract_json_block_fenced():
    text = 'Analysis here.\n```json\n{"proposals":[{"key":"ARB_MIN_EDGE_BPS","proposed_value":75}]}\n```\n'
    data = _extract_json_block(text)
    assert data and data["proposals"][0]["key"] == "ARB_MIN_EDGE_BPS"


def test_live_blocked_without_gates(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    # Pass risk gates; fail live_allowed() (allow_live off / dry_run on)
    cfg = _cfg(
        tmp_path,
        allow_live=False,
        dry_run=True,
        study_mode=False,
        exec_mode=ExecMode.LIVE,
    )
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_opp(), state=OppState.CLOB_VERIFIED, verified=True)
    res = execute_opportunity(cfg, store, _opp(), opportunity_id=oid, ask_depth=50, bid_depth=50)
    assert res.status == "live_blocked"


def test_live_dry_run_bundle(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    cfg = _cfg(tmp_path)
    result = execute_buy_bundle_live(cfg, _opp(), size_usd=10.0, dry_run=True)
    assert result.ok
    assert result.mode == "live_dry_run"
    assert len(result.legs) == 2
    assert abs(result.fill_total - 0.85) < 1e-9


def test_execute_live_with_mocked_clob(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "cd" * 32)
    cfg = _cfg(tmp_path)
    store = OpportunityStore(cfg.state_db)
    oid = store.save(_opp(), state=OppState.CLOB_VERIFIED, verified=True)

    # A genuine complete-set fill: every leg matched AND equal share quantities.
    fake = LiveBundleResult(
        ok=True,
        mode="live",
        legs=[
            LiveLegResult(token_id="tok_a", side="BUY", price=0.4, size=12.5, order_id="o1", status="matched"),
            LiveLegResult(token_id="tok_b", side="BUY", price=0.45, size=12.5, order_id="o2", status="matched"),
        ],
        client_ready=True,
        size_usd=10.0,
        fill_total=0.85,
        fill_prices=[0.4, 0.45],
        order_ids=["o1", "o2"],
    )
    with patch("arb.clob_live.execute_buy_bundle_live", return_value=fake):
        res = execute_opportunity(
            cfg, store, _opp(), opportunity_id=oid, ask_depth=50, bid_depth=50
        )
    assert res.status == "live_filled"
    assert store.get(oid)["state"] == OppState.FILLED.value
    assert store.count_fills_today() == 1
    # A fill is not a settlement — realized PnL stays unresolved.
    assert store.list_fills()[0]["realized_pnl"] is None


def test_grok_analyze_mocked(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("ARB_LLM_DAILY_TOKEN_BUDGET", "100000")
    cfg = ArbConfig(state_dir=tmp_path)

    fake_resp = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Edge looks thin.\n"
                        '```json\n{"proposals":[{"key":"ARB_MIN_EDGE_BPS",'
                        '"proposed_value":80,"rationale":"raise edge"}]}\n```'
                    )
                }
            }
        ],
        "usage": {"total_tokens": 120},
    }
    with patch("arb.grok.chat_completion", return_value=fake_resp):
        result = analyze_postmortem(
            cfg,
            report_summary={"total_labeled": 5, "false_positive_rate": 0.8},
            labeled_sample=[{"label": "false_positive"}],
        )
    assert result.ok
    assert result.proposals
    assert result.proposals[0]["key"] in ALLOWED_PROPOSAL_KEYS
    ids = apply_grok_proposals(cfg, result)
    assert len(ids) == 1


def test_postmortem_with_grok_flag(tmp_path: Path, monkeypatch):
    from arb.models import RejectReason

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    cfg = ArbConfig(state_dir=tmp_path, min_edge_bps=50.0)
    store = OpportunityStore(cfg.state_db)
    for i in range(3):
        store.save(
            Opportunity(
                kind=ArbKind.BUY_BUNDLE,
                condition_id=f"0xg{i}",
                slug="g",
                question="?",
                outcomes=["Y", "N"],
                token_ids=["a", "b"],
                prices=[0.4, 0.45],
                total=0.85,
                edge=0.1,
                edge_bps=1000.0,
                source="gamma",
            ),
            state=OppState.REJECTED,
            reject_reason=RejectReason.EDGE_EVAPORATED,
        )

    fake = GrokResult(
        ok=True,
        analysis_md="Looks noisy.",
        proposals=[
            {
                "key": "ARB_WS_WATCH_SEC",
                "proposed_value": 45,
                "rationale": "watch longer",
                "evidence": {},
            }
        ],
        model="grok-3-mini",
        usage={"total_tokens": 10},
    )
    with patch("arb.grok.analyze_postmortem", return_value=fake):
        report = run_postmortem(cfg, store, days=30, create_proposals=True, use_grok=True)
    assert report.grok_ok is True
    assert report.grok_path and Path(report.grok_path).exists()
    assert report.grok_proposals
