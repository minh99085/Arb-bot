"""Arb bot configuration — secrets via env, thresholds via env or defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from arb.models import ExecMode


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


@dataclass(frozen=True)
class ArbConfig:
    """Tunable scanner, risk, and trading thresholds."""

    min_edge_bps: float = 50.0
    taker_fee_bps: float = 0.0
    page_size: int = 100
    max_markets: int | None = None
    verify_top_n: int = 25
    state_dir: Path | None = None
    dry_run: bool = True
    study_mode: bool = True
    min_book_depth: float = 5.0
    alert_on_verified: bool = True
    # Phase 2 risk / execution
    kill_switch: bool = False
    exec_mode: ExecMode = ExecMode.PAPER
    max_position_usd: float = 25.0
    max_open_positions: int = 5
    max_daily_trades: int = 20
    max_daily_loss_usd: float = 50.0
    paper_slippage_bps: float = 10.0
    allow_live: bool = False
    category_blocklist: tuple[str, ...] = ()

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
    def from_env(cls) -> ArbConfig:
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

        blocklist = tuple(
            x.strip().lower()
            for x in (os.environ.get("ARB_CATEGORY_BLOCKLIST") or "").split(",")
            if x.strip()
        )
        state_dir = os.environ.get("ARB_STATE_DIR")
        return cls(
            min_edge_bps=_float("ARB_MIN_EDGE_BPS", 50.0),
            taker_fee_bps=_float("ARB_TAKER_FEE_BPS", 0.0),
            page_size=int(os.environ.get("ARB_PAGE_SIZE", "100")),
            max_markets=_int("ARB_MAX_MARKETS", None),
            verify_top_n=int(os.environ.get("ARB_VERIFY_TOP_N", "25")),
            state_dir=Path(state_dir) if state_dir else None,
            dry_run=_bool("ARB_DRY_RUN", True),
            study_mode=_bool("ARB_STUDY_MODE", True),
            min_book_depth=_float("ARB_MIN_BOOK_DEPTH", 5.0),
            alert_on_verified=_bool("ARB_ALERT_ON_VERIFIED", True),
            kill_switch=_bool("ARB_KILL_SWITCH", False),
            exec_mode=exec_mode,
            max_position_usd=_float("ARB_MAX_POSITION_USD", 25.0),
            max_open_positions=int(os.environ.get("ARB_MAX_OPEN_POSITIONS", "5")),
            max_daily_trades=int(os.environ.get("ARB_MAX_DAILY_TRADES", "20")),
            max_daily_loss_usd=_float("ARB_MAX_DAILY_LOSS_USD", 50.0),
            paper_slippage_bps=_float("ARB_PAPER_SLIPPAGE_BPS", 10.0),
            allow_live=_bool("ARB_ALLOW_LIVE", False),
            category_blocklist=blocklist,
        )

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

    def live_allowed(self) -> bool:
        return (
            self.allow_live
            and self.exec_mode == ExecMode.LIVE
            and self.trading_enabled()
            and not self.dry_run
            and not self.kill_switch
            and not self.study_mode
        )
