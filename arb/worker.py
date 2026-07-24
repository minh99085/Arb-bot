"""Standalone 24/7 arb worker — Phase 5. No Hermes on the hot path."""

from __future__ import annotations

import json
import os
import signal
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from arb.config import ArbConfig
from arb.models import OppState
from arb.postmortem import run_postmortem
from arb.reconcile import reconcile
from arb.scanner import format_alert, run_scan
from arb.state import OpportunityStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerConfig:
    """Schedule for the standalone worker (seconds)."""

    scan_interval_sec: float = 120.0
    loop_interval_sec: float = 120.0
    postmortem_interval_sec: float = 21600.0
    reconcile_interval_sec: float = 900.0
    self_tune_interval_sec: float = 1800.0
    scan_limit: int | None = None
    trade_limit: int = 10
    use_ws: bool = True
    ws_sec: float = 12.0
    paper: bool = True
    run_postmortem: bool = True
    run_self_tune: bool = True
    heartbeat_sec: float = 60.0

    @classmethod
    def from_env(cls) -> WorkerConfig:
        import os

        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return float(raw) if raw not in (None, "") else default

        def _i(name: str, default: int | None) -> int | None:
            raw = os.environ.get(name)
            if raw in (None, ""):
                return default
            return int(raw)

        def _b(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw in (None, ""):
                return default
            return raw.lower() not in {"0", "false", "no"}

        return cls(
            scan_interval_sec=_f("ARB_WORKER_SCAN_SEC", 120.0),
            loop_interval_sec=_f("ARB_WORKER_LOOP_SEC", 120.0),
            postmortem_interval_sec=_f("ARB_WORKER_POSTMORTEM_SEC", 21600.0),
            reconcile_interval_sec=_f("ARB_WORKER_RECONCILE_SEC", 900.0),
            self_tune_interval_sec=_f("ARB_WORKER_SELF_TUNE_SEC", 1800.0),
            scan_limit=_i("ARB_WORKER_SCAN_LIMIT", None),
            trade_limit=int(os.environ.get("ARB_WORKER_TRADE_LIMIT", "10")),
            use_ws=_b("ARB_WORKER_USE_WS", True),
            ws_sec=_f("ARB_WORKER_WS_SEC", 12.0),
            paper=_b("ARB_WORKER_PAPER", True),
            run_postmortem=_b("ARB_WORKER_POSTMORTEM", True),
            run_self_tune=_b("ARB_SELF_TUNE", False),
            heartbeat_sec=_f("ARB_WORKER_HEARTBEAT_SEC", 60.0),
        )


@dataclass
class WorkerStatus:
    running: bool = False
    started_at: str | None = None
    last_heartbeat: str | None = None
    last_scan_at: str | None = None
    last_loop_at: str | None = None
    last_reconcile_at: str | None = None
    last_postmortem_at: str | None = None
    last_self_tune_at: str | None = None
    last_error: str | None = None
    scans: int = 0
    loops: int = 0
    alerts: int = 0
    self_tunes: int = 0
    stop_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArbWorker:
    """Long-running process: scan / paper-loop / reconcile / postmortem on timers."""

    def __init__(
        self,
        arb_config: ArbConfig | None = None,
        worker_config: WorkerConfig | None = None,
        *,
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
    ):
        self.arb = arb_config or ArbConfig.from_env()
        self.wc = worker_config or WorkerConfig.from_env()
        self.status = WorkerStatus()
        self._sleep = sleep_fn or time.sleep
        self._time = time_fn or time.time
        self._next_scan = 0.0
        self._next_loop = 0.0
        self._next_reconcile = 0.0
        self._next_postmortem = 0.0
        self._next_self_tune = 0.0
        self._next_heartbeat = 0.0

    def _reload_config(self) -> ArbConfig:
        """Merge self_tune.json overrides onto the worker's ArbConfig in place.

        When self-tune is disabled, historical overrides are neither loaded nor
        applied (old files are preserved on disk for audit).
        """
        if not self.arb.self_tune:
            return self.arb
        from arb.self_tune import apply_overrides_to_config, load_overrides

        self.arb = apply_overrides_to_config(self.arb)
        ov = load_overrides(self.arb)
        if "ARB_WORKER_TRADE_LIMIT" in ov:
            self.wc.trade_limit = int(round(ov["ARB_WORKER_TRADE_LIMIT"]))
        return self.arb

    @property
    def status_path(self) -> Path:
        return self.arb.state_root / "worker_status.json"

    @property
    def pid_path(self) -> Path:
        return self.arb.state_root / "worker.pid"

    @property
    def alert_path(self) -> Path:
        return self.arb.state_root / "last_alert.txt"

    def write_status(self) -> None:
        self.status.last_heartbeat = _now()
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(self.status.to_dict(), indent=2))

    def request_stop(self, *_args) -> None:
        self.status.stop_requested = True

    def _install_signals(self) -> None:
        try:
            signal.signal(signal.SIGTERM, self.request_stop)
            signal.signal(signal.SIGINT, self.request_stop)
        except Exception:
            pass

    def _write_pid(self) -> None:
        self.pid_path.write_text(str(__import__("os").getpid()))

    def _clear_pid(self) -> None:
        if self.pid_path.exists():
            self.pid_path.unlink()

    def tick_scan(self) -> dict[str, Any]:
        cfg = self._reload_config().with_overrides(max_markets=self.wc.scan_limit)
        result = run_scan(cfg, gamma_only=False, persist=True)
        self.status.scans += 1
        self.status.last_scan_at = _now()
        alert = format_alert(result, position_usd=cfg.max_position_usd) if cfg.alert_on_verified else None
        if alert:
            self.status.alerts += 1
            self.alert_path.write_text(alert + "\n")
            print(alert)
        return {
            "scanned": result.scanned,
            "verified": len(result.verified_hits),
            "alert": bool(alert),
        }

    def tick_loop(self) -> dict[str, Any]:
        """One loop turn: scan (shadow observations) → optional WS → [execute] → reconcile.

        The scanner mode is NEVER overridden here. Order/fill creation happens
        only when the config's safety mode explicitly permits it
        (PAPER_EXECUTION + ARB_PAPER_EXECUTION_ENABLED, or a fully-gated LIVE).
        Otherwise the loop is scan/shadow-only.
        """
        from arb.execute import execute_batch

        cfg = self._reload_config().with_overrides(max_markets=self.wc.scan_limit)
        result = run_scan(cfg, gamma_only=False, persist=True)  # shadow observations
        store = OpportunityStore(cfg.state_db)

        execute_enabled = cfg.execution_allowed()
        trade_results = []
        if execute_enabled:
            rows = store.top_by_edge(limit=self.wc.trade_limit, state=OppState.CLOB_VERIFIED)
            if self.wc.use_ws and cfg.ws_enabled and result.verified_hits:
                from arb.reverify import reverify_opportunities
                from arb.ws_feed import run_feed_sync

                asset_ids: list[str] = []
                for opp in result.verified_hits:
                    asset_ids.extend(opp.token_ids)
                asset_ids = list(dict.fromkeys(asset_ids))[: cfg.ws_max_assets]
                cache = run_feed_sync(
                    asset_ids,
                    duration_sec=min(cfg.ws_watch_sec, self.wc.ws_sec),
                    ws_url=cfg.ws_url,
                    seed_rest=cfg.ws_seed_rest,
                )
                rv = reverify_opportunities(cfg, cache, result.verified_hits)
                valid_ids = {o.condition_id for o in rv.still_valid}
                rows = [r for r in rows if r["condition_id"] in valid_ids][: self.wc.trade_limit]

            pairs = [(store.opportunity_from_row(r), int(r["id"])) for r in rows]
            trade_results = execute_batch(cfg, store, pairs) if pairs else []

        report = reconcile(cfg, store, settle_paper=False)
        self.status.loops += 1
        self.status.last_loop_at = _now()
        filled = sum(1 for t in trade_results if t.status in {"paper_filled", "live_filled"})
        return {
            "scanned": result.scanned,
            "verified": len(result.verified_hits),
            "traded": filled,
            "executed": execute_enabled,
            "safety_mode": cfg.safety_mode.value,
            "realized_pnl": report.realized_pnl_sum,
            "unresolved": report.unresolved,
        }

    def tick_reconcile(self) -> dict[str, Any]:
        cfg = self._reload_config()
        store = OpportunityStore(cfg.state_db)
        report = reconcile(cfg, store, settle_paper=False)
        self.status.last_reconcile_at = _now()
        return report.to_dict()

    def tick_self_tune(self) -> dict[str, Any]:
        from arb.self_tune import run_self_tune

        cfg = self._reload_config()
        store = OpportunityStore(cfg.state_db)
        report = run_self_tune(cfg, store, days=3)
        self.status.last_self_tune_at = _now()
        self.status.self_tunes += 1
        # Reload again so next ticks see new overrides
        self._reload_config()
        return report.to_dict()

    def tick_postmortem(self) -> dict[str, Any]:
        store = OpportunityStore(self._reload_config().state_db)
        use_grok = os.environ.get("ARB_WORKER_GROK", "").lower() not in {
            "",
            "0",
            "false",
            "no",
        }
        report = run_postmortem(
            self.arb,
            store,
            days=7,
            create_proposals=True,
            use_grok=use_grok,
        )
        self.status.last_postmortem_at = _now()
        return {
            "labeled": report.total_labeled,
            "proposals": report.proposals_created,
            "report": report.report_path,
            "grok_ok": report.grok_ok,
            "grok_path": report.grok_path,
        }

    def run_once(self, *, jobs: list[str] | None = None) -> dict[str, Any]:
        """Run selected jobs once (for cron / GH Actions)."""
        jobs = jobs or ["scan", "reconcile"]
        out: dict[str, Any] = {"at": _now(), "jobs": {}}
        for job in jobs:
            try:
                if job == "scan":
                    out["jobs"]["scan"] = self.tick_scan()
                elif job == "loop":
                    out["jobs"]["loop"] = self.tick_loop()
                elif job == "reconcile":
                    out["jobs"]["reconcile"] = self.tick_reconcile()
                elif job == "postmortem":
                    out["jobs"]["postmortem"] = self.tick_postmortem()
                elif job in {"self-tune", "self_tune", "tune"}:
                    out["jobs"]["self_tune"] = self.tick_self_tune()
                else:
                    out["jobs"][job] = {"error": f"unknown job {job}"}
            except Exception as exc:
                self.status.last_error = f"{job}: {exc}"
                out["jobs"][job] = {"error": str(exc), "trace": traceback.format_exc()}
        self.write_status()
        return out

    def run_forever(self) -> None:
        """Daemon loop until SIGINT/SIGTERM or stop_requested."""
        self._install_signals()
        self._write_pid()
        now = self._time()
        self.status.running = True
        self.status.started_at = _now()
        self.status.stop_requested = False
        # Stagger first runs slightly
        self._next_scan = now
        self._next_loop = now + min(60.0, self.wc.loop_interval_sec)
        self._next_reconcile = now + min(120.0, self.wc.reconcile_interval_sec)
        self._next_postmortem = now + min(300.0, self.wc.postmortem_interval_sec)
        self._next_self_tune = now + min(90.0, self.wc.self_tune_interval_sec)
        self._next_heartbeat = now
        self.write_status()
        print(f"ArbWorker started pid={self.pid_path.read_text().strip()} state={self.arb.state_root}")
        print(
            f"  cadence: scan={self.wc.scan_interval_sec}s loop={self.wc.loop_interval_sec}s "
            f"self_tune={self.wc.self_tune_interval_sec}s trade_limit={self.wc.trade_limit}"
        )

        try:
            while not self.status.stop_requested:
                now = self._time()
                try:
                    if now >= self._next_scan:
                        info = self.tick_scan()
                        print(f"[scan] {info}")
                        self._next_scan = now + self.wc.scan_interval_sec
                    if now >= self._next_loop:
                        info = self.tick_loop()
                        print(f"[loop] {info}")
                        self._next_loop = now + self.wc.loop_interval_sec
                    if now >= self._next_reconcile:
                        info = self.tick_reconcile()
                        print(f"[reconcile] fills={info.get('fills')} pnl={info.get('realized_pnl_sum')}")
                        self._next_reconcile = now + self.wc.reconcile_interval_sec
                    if self.wc.run_self_tune and now >= self._next_self_tune:
                        info = self.tick_self_tune()
                        applied = info.get("applied") or []
                        print(f"[self-tune] applied={len(applied)} overrides={info.get('overrides')}")
                        self._next_self_tune = now + self.wc.self_tune_interval_sec
                    if self.wc.run_postmortem and now >= self._next_postmortem:
                        info = self.tick_postmortem()
                        print(f"[postmortem] {info}")
                        self._next_postmortem = now + self.wc.postmortem_interval_sec
                    if now >= self._next_heartbeat:
                        self.write_status()
                        self._next_heartbeat = now + self.wc.heartbeat_sec
                except Exception as exc:
                    self.status.last_error = str(exc)
                    print(f"[error] {exc}")
                    traceback.print_exc()
                    self.write_status()
                    self._sleep(5.0)
                    continue

                # Sleep until next due work
                next_due = min(
                    self._next_scan,
                    self._next_loop,
                    self._next_reconcile,
                    self._next_self_tune if self.wc.run_self_tune else now + 3600,
                    self._next_postmortem if self.wc.run_postmortem else now + 3600,
                    self._next_heartbeat,
                )
                delay = max(0.5, min(5.0, next_due - self._time()))
                self._sleep(delay)
        finally:
            self.status.running = False
            self.write_status()
            self._clear_pid()
            print("ArbWorker stopped")


def load_status(state_root: Path) -> dict[str, Any] | None:
    path = state_root / "worker_status.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
