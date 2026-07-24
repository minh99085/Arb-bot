"""Arb bot configuration — secrets via env, thresholds via env or defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from arb.models import ExecMode, SafetyMode


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


@dataclass(frozen=True)
class ArbConfig:
    """Tunable scanner, risk, and trading thresholds.

    Defaults are intentionally aggressive for paper discovery; self_tune.json
    and env vars can tighten or loosen further within safety rails.
    """

    # Execution safety — scanner/shadow-first. SCAN_ONLY is the safe default;
    # any order/fill creation requires an explicit opt-in (see SafetyMode).
    safety_mode: SafetyMode = SafetyMode.SCAN_ONLY
    paper_execution_enabled: bool = False
    min_edge_bps: float = 25.0
    taker_fee_bps: float = 10.0
    page_size: int = 100
    max_markets: int | None = None
    verify_top_n: int = 50
    state_dir: Path | None = None
    dry_run: bool = True
    study_mode: bool = True
    min_book_depth: float = 2.0
    alert_on_verified: bool = True
    # Scanner / alpha
    scan_source: str = "events"
    gamma_max_offset: int = 2000
    liquid_scan_limit: int = 800
    near_miss_bps: float = 30.0
    paper_gamma_fallback: bool = False
    paper_realistic: bool = True
    paper_min_edge_bps: float = 15.0
    alpha_workers: int = 12
    # Phase 2 risk / execution — high activity paper defaults
    kill_switch: bool = False
    exec_mode: ExecMode = ExecMode.PAPER
    max_position_usd: float = 15.0
    max_open_positions: int = 10
    max_daily_trades: int = 50
    max_daily_loss_usd: float = 75.0
    paper_slippage_bps: float = 15.0
    allow_live: bool = False
    category_blocklist: tuple[str, ...] = ()
    # Phase 3 feed
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_enabled: bool = True
    ws_watch_sec: float = 15.0
    ws_max_assets: int = 80
    ws_seed_rest: bool = True
    # Self-tune — OFF by default. When disabled, historical self_tune.json
    # overrides are neither loaded nor applied (old files are kept for audit).
    self_tune: bool = False

    @property
    def min_edge(self) -> float:
        return self.min_edge_bps / 10_000.0

    @property
    def fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000.0

    @property
    def paper_slippage(self) -> float:
        return self.paper_slippage_bps / 10_000.0

    @property
    def state_root(self) -> Path:
        base = self.state_dir or (_hermes_home() / "profiles" / "polymarket-arb" / "state")
        base.mkdir(parents=True, exist_ok=True)
        return base

    @property
    def state_db(self) -> Path:
        return self.state_root / "opportunities.sqlite"

    @property
    def ledger_path(self) -> Path:
        return self.state_root / "LEDGER.md"

    @property
    def metrics_path(self) -> Path:
        return self.state_root / "metrics.json"

    @classmethod
    def from_env(cls, *, apply_self_tune: bool = True) -> ArbConfig:
        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            return float(raw)

        def _int(name: str, default: int | None) -> int | None:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            return int(raw)

        def _bool(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            return raw.lower() not in {"0", "false", "no"}

        mode_raw = (os.environ.get("ARB_EXEC_MODE") or "paper").lower().strip()
        try:
            exec_mode = ExecMode(mode_raw)
        except ValueError:
            exec_mode = ExecMode.PAPER

        safety_raw = (os.environ.get("ARB_SAFETY_MODE") or "scan_only").lower().strip()
        try:
            safety_mode = SafetyMode(safety_raw)
        except ValueError:
            safety_mode = SafetyMode.SCAN_ONLY

        blocklist = tuple(
            x.strip().lower()
            for x in (os.environ.get("ARB_CATEGORY_BLOCKLIST") or "").split(",")
            if x.strip()
        )
        state_dir = os.environ.get("ARB_STATE_DIR")
        cfg = cls(
            safety_mode=safety_mode,
            paper_execution_enabled=_bool("ARB_PAPER_EXECUTION_ENABLED", False),
            min_edge_bps=_float("ARB_MIN_EDGE_BPS", 25.0),
            taker_fee_bps=_float("ARB_TAKER_FEE_BPS", 10.0),
            page_size=int(os.environ.get("ARB_PAGE_SIZE", "100")),
            max_markets=_int("ARB_MAX_MARKETS", None),
            verify_top_n=int(os.environ.get("ARB_VERIFY_TOP_N", "50")),
            state_dir=Path(state_dir) if state_dir else None,
            dry_run=_bool("ARB_DRY_RUN", True),
            study_mode=_bool("ARB_STUDY_MODE", True),
            min_book_depth=_float("ARB_MIN_BOOK_DEPTH", 2.0),
            alert_on_verified=_bool("ARB_ALERT_ON_VERIFIED", True),
            kill_switch=_bool("ARB_KILL_SWITCH", False),
            exec_mode=exec_mode,
            max_position_usd=_float("ARB_MAX_POSITION_USD", 15.0),
            max_open_positions=int(os.environ.get("ARB_MAX_OPEN_POSITIONS", "10")),
            max_daily_trades=int(os.environ.get("ARB_MAX_DAILY_TRADES", "50")),
            max_daily_loss_usd=_float("ARB_MAX_DAILY_LOSS_USD", 75.0),
            paper_slippage_bps=_float("ARB_PAPER_SLIPPAGE_BPS", 15.0),
            allow_live=_bool("ARB_ALLOW_LIVE", False),
            category_blocklist=blocklist,
            ws_url=os.environ.get(
                "ARB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
            ),
            ws_enabled=_bool("ARB_WS_ENABLED", True),
            ws_watch_sec=_float("ARB_WS_WATCH_SEC", 15.0),
            ws_max_assets=int(os.environ.get("ARB_WS_MAX_ASSETS", "80")),
            ws_seed_rest=_bool("ARB_WS_SEED_REST", True),
            scan_source=(os.environ.get("ARB_SCAN_SOURCE") or "events").lower().strip(),
            gamma_max_offset=int(os.environ.get("ARB_GAMMA_MAX_OFFSET", "2000")),
            liquid_scan_limit=int(os.environ.get("ARB_LIQUID_SCAN_LIMIT", "800")),
            near_miss_bps=_float("ARB_NEAR_MISS_BPS", 30.0),
            paper_gamma_fallback=_bool("ARB_PAPER_GAMMA_FALLBACK", False),
            paper_realistic=_bool("ARB_PAPER_REALISTIC", True),
            paper_min_edge_bps=_float("ARB_PAPER_MIN_EDGE_BPS", 15.0),
            alpha_workers=int(os.environ.get("ARB_ALPHA_WORKERS", "12")),
            self_tune=_bool("ARB_SELF_TUNE", False),
        )
        if apply_self_tune and cfg.self_tune:
            try:
                from arb.self_tune import apply_overrides_to_config

                cfg = apply_overrides_to_config(cfg)
            except Exception:
                pass
        if cfg.paper_realistic:
            cfg = cfg._apply_realistic_paper_clamp()
        return cfg

    def _apply_realistic_paper_clamp(self) -> ArbConfig:
        """Prevent fantasy paper settings — CLOB-only, positive edge floor."""
        kwargs: dict[str, Any] = {"paper_gamma_fallback": False}
        floor = max(0.0, self.paper_min_edge_bps)
        if self.min_edge_bps < floor:
            kwargs["min_edge_bps"] = max(floor, 25.0)
        if self.taker_fee_bps < 5.0:
            kwargs["taker_fee_bps"] = 10.0
        if self.paper_slippage_bps < 10.0:
            kwargs["paper_slippage_bps"] = 15.0
        return replace(self, **kwargs)

    def effective_min_edge_bps(self) -> float:
        """Edge threshold used by risk + execution (stricter in realistic paper)."""
        if self.paper_realistic:
            return max(self.min_edge_bps, self.paper_min_edge_bps)
        return self.min_edge_bps

    def with_overrides(
        self,
        *,
        min_edge_bps: float | None = None,
        max_markets: int | None = None,
        verify_top_n: int | None = None,
        study_mode: bool | None = None,
        exec_mode: ExecMode | None = None,
        kill_switch: bool | None = None,
    ) -> ArbConfig:
        return replace(
            self,
            min_edge_bps=self.min_edge_bps if min_edge_bps is None else min_edge_bps,
            max_markets=self.max_markets if max_markets is None else max_markets,
            verify_top_n=self.verify_top_n if verify_top_n is None else verify_top_n,
            study_mode=self.study_mode if study_mode is None else study_mode,
            exec_mode=self.exec_mode if exec_mode is None else exec_mode,
            kill_switch=self.kill_switch if kill_switch is None else kill_switch,
        )

    def trading_enabled(self) -> bool:
        return bool(os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip())

    def paper_execution_allowed(self) -> bool:
        """Simulated order/fill creation is only allowed under an explicit opt-in.

        Requires SafetyMode.PAPER_EXECUTION *and* the separate
        ARB_PAPER_EXECUTION_ENABLED gate, with the kill switch off and study
        mode off. Shadow observation collection never needs this.
        """
        return (
            self.safety_mode == SafetyMode.PAPER_EXECUTION
            and self.paper_execution_enabled
            and not self.kill_switch
            and not self.study_mode
        )

    def live_allowed(self) -> bool:
        return (
            self.safety_mode == SafetyMode.LIVE
            and self.allow_live
            and self.exec_mode == ExecMode.LIVE
            and self.trading_enabled()
            and not self.dry_run
            and not self.kill_switch
            and not self.study_mode
        )

    def execution_allowed(self) -> bool:
        """Any order/fill creation (paper or live) is permitted right now."""
        return self.paper_execution_allowed() or self.live_allowed()
