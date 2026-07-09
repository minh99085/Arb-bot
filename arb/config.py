"""Arb bot configuration — secrets via env, thresholds via env or defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


@dataclass(frozen=True)
class ArbConfig:
    """Tunable scanner and trading thresholds."""

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

    @property
    def min_edge(self) -> float:
        return self.min_edge_bps / 10_000.0

    @property
    def fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000.0

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
        )

    def with_overrides(
        self,
        *,
        min_edge_bps: float | None = None,
        max_markets: int | None = None,
        verify_top_n: int | None = None,
        study_mode: bool | None = None,
    ) -> ArbConfig:
        return replace(
            self,
            min_edge_bps=self.min_edge_bps if min_edge_bps is None else min_edge_bps,
            max_markets=self.max_markets if max_markets is None else max_markets,
            verify_top_n=self.verify_top_n if verify_top_n is None else verify_top_n,
            study_mode=self.study_mode if study_mode is None else study_mode,
        )

    def trading_enabled(self) -> bool:
        return bool(os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip())
