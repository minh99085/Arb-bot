"""Autonomous self-tune — learn from paper results and adjust thresholds in-bounds.

Hot path stays deterministic. This module runs on a slow timer (worker / CLI),
writes overrides to state/self_tune.json, and never disables kill switches or
enables live trading.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arb.config import ArbConfig
from arb.labels import Label, label_counts, label_history
from arb.state import OpportunityStore

# Hard safety rails — self-tune cannot leave these ranges
BOUNDS: dict[str, tuple[float, float]] = {
    "ARB_MIN_EDGE_BPS": (-50.0, 150.0),
    "ARB_TAKER_FEE_BPS": (0.0, 50.0),
    "ARB_VERIFY_TOP_N": (50.0, 400.0),
    "ARB_MAX_POSITION_USD": (5.0, 100.0),
    "ARB_MAX_OPEN_POSITIONS": (5.0, 60.0),
    "ARB_MAX_DAILY_TRADES": (20.0, 500.0),
    "ARB_MIN_BOOK_DEPTH": (0.5, 50.0),
    "ARB_WS_WATCH_SEC": (3.0, 120.0),
    "ARB_PAPER_SLIPPAGE_BPS": (0.0, 50.0),
    "ARB_WORKER_TRADE_LIMIT": (5.0, 100.0),
    "ARB_NEAR_MISS_BPS": (5.0, 100.0),
}

# Map env key → ArbConfig field name
KEY_TO_FIELD: dict[str, str] = {
    "ARB_MIN_EDGE_BPS": "min_edge_bps",
    "ARB_TAKER_FEE_BPS": "taker_fee_bps",
    "ARB_VERIFY_TOP_N": "verify_top_n",
    "ARB_MAX_POSITION_USD": "max_position_usd",
    "ARB_MAX_OPEN_POSITIONS": "max_open_positions",
    "ARB_MAX_DAILY_TRADES": "max_daily_trades",
    "ARB_MIN_BOOK_DEPTH": "min_book_depth",
    "ARB_WS_WATCH_SEC": "ws_watch_sec",
    "ARB_PAPER_SLIPPAGE_BPS": "paper_slippage_bps",
    "ARB_NEAR_MISS_BPS": "near_miss_bps",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def self_tune_path(config: ArbConfig) -> Path:
    return config.state_root / "self_tune.json"


def history_path(config: ArbConfig) -> Path:
    return config.state_root / "self_tune_history.jsonl"


@dataclass
class TuneChange:
    key: str
    old_value: float
    new_value: float
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SelfTuneReport:
    enabled: bool
    applied: list[TuneChange] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    overrides: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "applied": [c.to_dict() for c in self.applied],
            "skipped": self.skipped,
            "overrides": self.overrides,
            "metrics": self.metrics,
            "notes": self.notes,
        }


def load_overrides(config: ArbConfig) -> dict[str, float]:
    path = self_tune_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        raw = data.get("overrides") or {}
        out: dict[str, float] = {}
        for k, v in raw.items():
            if k in BOUNDS:
                lo, hi = BOUNDS[k]
                out[k] = float(max(lo, min(hi, float(v))))
        return out
    except Exception:
        return {}


def save_overrides(config: ArbConfig, overrides: dict[str, float], *, note: str = "") -> None:
    path = self_tune_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": _now(),
        "note": note,
        "overrides": overrides,
        "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in BOUNDS.items()},
    }
    path.write_text(json.dumps(payload, indent=2))


def _clip(key: str, value: float) -> float:
    lo, hi = BOUNDS[key]
    return float(max(lo, min(hi, value)))


def _current_value(config: ArbConfig, key: str, overrides: dict[str, float]) -> float:
    if key in overrides:
        return float(overrides[key])
    field = KEY_TO_FIELD.get(key)
    if field and hasattr(config, field):
        return float(getattr(config, field))
    if key == "ARB_WORKER_TRADE_LIMIT":
        return float(os.environ.get("ARB_WORKER_TRADE_LIMIT", "15"))
    return 0.0


def _adjust(
    changes: list[TuneChange],
    overrides: dict[str, float],
    config: ArbConfig,
    *,
    key: str,
    new_value: float,
    rationale: str,
    evidence: dict[str, Any],
) -> None:
    old = _current_value(config, key, overrides)
    clipped = _clip(key, new_value)
    if abs(clipped - old) < 1e-9:
        return
    # Prefer integer-ish keys as ints when appropriate
    if key in {
        "ARB_VERIFY_TOP_N",
        "ARB_MAX_OPEN_POSITIONS",
        "ARB_MAX_DAILY_TRADES",
        "ARB_WORKER_TRADE_LIMIT",
    }:
        clipped = float(int(round(clipped)))
        old_cmp = float(int(round(old)))
        if clipped == old_cmp:
            return
    overrides[key] = clipped
    changes.append(
        TuneChange(
            key=key,
            old_value=old,
            new_value=clipped,
            rationale=rationale,
            evidence=evidence,
        )
    )


def collect_metrics(config: ArbConfig, store: OpportunityStore, *, days: int = 3) -> dict[str, Any]:
    labeled = label_history(store, days=days)
    counts = label_counts(labeled)
    total = max(1, len(labeled))
    fp = counts.get(Label.FALSE_POSITIVE.value, 0)
    ws_evap = counts.get(Label.WS_EVAPORATED.value, 0)
    wins = counts.get(Label.PAPER_WIN.value, 0)
    losses = counts.get(Label.PAPER_LOSS.value, 0)
    true_arb = counts.get(Label.TRUE_ARB.value, 0)
    verified_like = true_arb + wins + losses
    summary = store.trade_summary()
    return {
        "days": days,
        "labeled": len(labeled),
        "false_positive_rate": round(fp / total, 4),
        "ws_evaporation_rate": round(ws_evap / total, 4),
        "paper_wins": wins,
        "paper_losses": losses,
        "verified_like": verified_like,
        "gamma_flags": counts.get(Label.FALSE_POSITIVE.value, 0)
        + counts.get(Label.TRUE_ARB.value, 0)
        + counts.get(Label.WS_EVAPORATED.value, 0),
        "fill_count": summary["fill_count"],
        "realized_pnl_sum": summary["realized_pnl_sum"],
        "realized_pnl_today": summary["realized_pnl_today"],
        "open_positions": summary["open_positions"],
        "label_counts": counts,
    }


def propose_adjustments(
    config: ArbConfig,
    metrics: dict[str, Any],
    overrides: dict[str, float],
) -> list[TuneChange]:
    """Deterministic learning rules — aggressive when quiet, defensive when losing."""
    changes: list[TuneChange] = []
    labeled = int(metrics.get("labeled") or 0)
    fp_rate = float(metrics.get("false_positive_rate") or 0)
    ws_rate = float(metrics.get("ws_evaporation_rate") or 0)
    wins = int(metrics.get("paper_wins") or 0)
    losses = int(metrics.get("paper_losses") or 0)
    verified_like = int(metrics.get("verified_like") or 0)
    fills = int(metrics.get("fill_count") or 0)
    pnl = float(metrics.get("realized_pnl_sum") or 0)

    edge = _current_value(config, "ARB_MIN_EDGE_BPS", overrides)
    verify_n = _current_value(config, "ARB_VERIFY_TOP_N", overrides)
    size = _current_value(config, "ARB_MAX_POSITION_USD", overrides)
    open_n = _current_value(config, "ARB_MAX_OPEN_POSITIONS", overrides)
    daily = _current_value(config, "ARB_MAX_DAILY_TRADES", overrides)
    depth = _current_value(config, "ARB_MIN_BOOK_DEPTH", overrides)
    watch = _current_value(config, "ARB_WS_WATCH_SEC", overrides)
    trade_lim = _current_value(config, "ARB_WORKER_TRADE_LIMIT", overrides)

    # --- Explore when quiet: loosen to find more alpha ---
    if labeled >= 5 and verified_like == 0 and fills < 3:
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MIN_EDGE_BPS",
            new_value=max(-50.0, edge - 10.0),
            rationale="No verified/paper signals — lower edge to explore more markets",
            evidence={"verified_like": verified_like, "fills": fills},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_TAKER_FEE_BPS",
            new_value=max(0.0, _current_value(config, "ARB_TAKER_FEE_BPS", overrides) - 2.0),
            rationale="Quiet book — reduce assumed fees to surface more CLOB hits",
            evidence={"fills": fills},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_VERIFY_TOP_N",
            new_value=min(400.0, verify_n + 40.0),
            rationale="Quiet book — verify more gamma candidates per scan",
            evidence={"verify_top_n": verify_n},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MIN_BOOK_DEPTH",
            new_value=max(0.5, depth - 0.5),
            rationale="Quiet book — allow thinner books for discovery",
            evidence={"min_book_depth": depth},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_WORKER_TRADE_LIMIT",
            new_value=min(100.0, trade_lim + 10.0),
            rationale="Increase per-loop trade attempts while exploring",
            evidence={"trade_limit": trade_lim},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MAX_DAILY_TRADES",
            new_value=min(500.0, daily + 25.0),
            rationale="Quiet book — raise daily trade cap for paper discovery",
            evidence={"daily": daily},
        )

    # --- High false positives: tighten edge ---
    if labeled >= 8 and fp_rate >= 0.55:
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MIN_EDGE_BPS",
            new_value=min(150.0, edge + 5.0),
            rationale=f"FP rate {fp_rate:.0%} — raise min edge to cut noise",
            evidence={"false_positive_rate": fp_rate, "n": labeled},
        )

    # --- WS evaporation: watch longer ---
    if labeled >= 8 and ws_rate >= 0.35:
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_WS_WATCH_SEC",
            new_value=min(120.0, watch + 10.0),
            rationale=f"WS evaporate {ws_rate:.0%} — watch longer before trade",
            evidence={"ws_evaporation_rate": ws_rate},
        )

    # --- Winning streak: scale activity ---
    if (wins + losses) >= 5 and wins > losses and pnl >= 0:
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MAX_OPEN_POSITIONS",
            new_value=min(40.0, open_n + 2.0),
            rationale=f"Winning ({wins}/{wins+losses}) — allow more concurrent positions",
            evidence={"wins": wins, "losses": losses, "pnl": pnl},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MAX_DAILY_TRADES",
            new_value=min(300.0, daily + 10.0),
            rationale="Winning — raise daily trade cap",
            evidence={"wins": wins, "losses": losses},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MAX_POSITION_USD",
            new_value=min(100.0, round(size * 1.1, 2)),
            rationale="Winning — nudge size up 10%",
            evidence={"size": size, "pnl": pnl},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MIN_EDGE_BPS",
            new_value=max(-50.0, edge - 2.0),
            rationale="Winning — slightly ease edge to capture more flow",
            evidence={"edge": edge},
        )

    # --- Losing streak: defend ---
    if (wins + losses) >= 5 and losses > wins:
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MAX_POSITION_USD",
            new_value=max(5.0, round(size * 0.75, 2)),
            rationale=f"Losing ({losses}/{wins+losses}) — cut size 25%",
            evidence={"wins": wins, "losses": losses, "pnl": pnl},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MIN_EDGE_BPS",
            new_value=min(150.0, edge + 5.0),
            rationale="Losing — demand more edge",
            evidence={"edge": edge},
        )
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_MAX_OPEN_POSITIONS",
            new_value=max(3.0, open_n - 2.0),
            rationale="Losing — reduce concurrent risk",
            evidence={"open_n": open_n},
        )

    # --- Many fills but flat PnL: keep exploring lightly ---
    if fills >= 10 and abs(pnl) < 0.05 and verified_like >= 3:
        _adjust(
            changes,
            overrides,
            config,
            key="ARB_VERIFY_TOP_N",
            new_value=min(200.0, verify_n + 10.0),
            rationale="Active but flat PnL — broaden CLOB verify set",
            evidence={"fills": fills, "pnl": pnl},
        )

    return changes


def _count_today_applies(config: ArbConfig) -> int:
    path = history_path(config)
    if not path.exists():
        return 0
    today = _today()
    n = 0
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("at", "")).startswith(today):
            n += int(row.get("n_changes") or 0)
    return n


def _append_history(config: ArbConfig, report: SelfTuneReport) -> None:
    path = history_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": _now(),
        "n_changes": len(report.applied),
        "applied": [c.to_dict() for c in report.applied],
        "metrics": report.metrics,
    }
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _append_ledger(config: ArbConfig, report: SelfTuneReport) -> None:
    if not report.applied:
        return
    path = config.ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"## Self-tune — {now}\n"]
    for c in report.applied:
        lines.append(
            f"- `{c.key}`: {c.old_value} → {c.new_value} — {c.rationale}"
        )
    lines.append("\n---\n\n")
    existing = path.read_text() if path.exists() else "# Polymarket Arb Ledger\n\n"
    path.write_text(existing + "\n".join(lines))


def run_self_tune(
    config: ArbConfig,
    store: OpportunityStore | None = None,
    *,
    days: int = 3,
    dry_run: bool = False,
    force: bool = False,
) -> SelfTuneReport:
    """Observe labels/fills → adjust overrides within bounds → persist."""
    enabled = os.environ.get("ARB_SELF_TUNE", "true").lower() not in {"0", "false", "no"}
    if not enabled and not force:
        return SelfTuneReport(enabled=False, notes=["ARB_SELF_TUNE disabled"])

    store = store or OpportunityStore(config.state_db)
    overrides = load_overrides(config)
    metrics = collect_metrics(config, store, days=days)

    max_daily = int(os.environ.get("ARB_SELF_TUNE_MAX_CHANGES_PER_DAY", "20"))
    already = _count_today_applies(config)
    if already >= max_daily and not force:
        return SelfTuneReport(
            enabled=True,
            overrides=overrides,
            metrics=metrics,
            skipped=[f"daily change budget exhausted ({already}/{max_daily})"],
            notes=["Try again tomorrow or raise ARB_SELF_TUNE_MAX_CHANGES_PER_DAY"],
        )

    # Work on a copy so we can compute deltas
    working = dict(overrides)
    changes = propose_adjustments(config, metrics, working)

    # Cap remaining budget
    remaining = max(0, max_daily - already)
    if len(changes) > remaining and not force:
        changes = changes[:remaining]
        # Rebuild working from original + accepted
        working = dict(overrides)
        for c in changes:
            working[c.key] = c.new_value

    report = SelfTuneReport(
        enabled=True,
        applied=changes,
        overrides=working if (changes and not dry_run) else overrides,
        metrics=metrics,
    )

    if not changes:
        report.notes.append("No rule triggers — thresholds unchanged.")
        return report

    if dry_run:
        report.notes.append("Dry run — not persisted.")
        report.overrides = working
        return report

    save_overrides(config, working, note=f"{len(changes)} auto-adjustments")
    report.overrides = working
    _append_history(config, report)
    _append_ledger(config, report)
    report.notes.append(f"Applied {len(changes)} adjustment(s) to self_tune.json")
    return report


def apply_overrides_to_config(config: ArbConfig) -> ArbConfig:
    """Return config with self_tune.json overrides merged in."""
    from dataclasses import replace

    overrides = load_overrides(config)
    if not overrides:
        return config
    kwargs: dict[str, Any] = {}
    for key, value in overrides.items():
        field = KEY_TO_FIELD.get(key)
        if not field:
            continue
        if field in {
            "verify_top_n",
            "max_open_positions",
            "max_daily_trades",
        }:
            kwargs[field] = int(round(value))
        elif field == "taker_fee_bps":
            kwargs[field] = float(value)
        else:
            kwargs[field] = float(value)
    return replace(config, **kwargs) if kwargs else config


def status_dict(config: ArbConfig) -> dict[str, Any]:
    overrides = load_overrides(config)
    path = self_tune_path(config)
    hist = []
    hp = history_path(config)
    if hp.exists():
        lines = hp.read_text().splitlines()[-10:]
        for line in lines:
            try:
                hist.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {
        "enabled": os.environ.get("ARB_SELF_TUNE", "true").lower() not in {"0", "false", "no"},
        "path": str(path),
        "overrides": overrides,
        "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in BOUNDS.items()},
        "recent_history": hist,
        "effective": {
            KEY_TO_FIELD[k]: _current_value(config, k, overrides)
            for k in KEY_TO_FIELD
            if k in BOUNDS
        },
    }
