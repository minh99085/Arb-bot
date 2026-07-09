"""Daily postmortem — observe → label → propose (human approves). Phase 4."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arb.config import ArbConfig
from arb.labels import Label, export_dataset, label_counts, label_history
from arb.proposals import ProposalStore, new_proposal, render_env_snippet
from arb.state import OpportunityStore


@dataclass
class PostmortemReport:
    days: int
    generated_at: str
    total_labeled: int
    label_counts: dict[str, int]
    reject_breakdown: dict[str, int]
    false_positive_rate: float
    ws_evaporation_rate: float
    paper_pnl: float
    verified_hits: int
    proposals_created: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dataset_path: str | None = None
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "generated_at": self.generated_at,
            "total_labeled": self.total_labeled,
            "label_counts": self.label_counts,
            "reject_breakdown": self.reject_breakdown,
            "false_positive_rate": self.false_positive_rate,
            "ws_evaporation_rate": self.ws_evaporation_rate,
            "paper_pnl": self.paper_pnl,
            "verified_hits": self.verified_hits,
            "proposals_created": self.proposals_created,
            "notes": self.notes,
            "dataset_path": self.dataset_path,
            "report_path": self.report_path,
        }


def _reject_breakdown(labeled) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in labeled:
        if row.label in {Label.FALSE_POSITIVE, Label.WS_EVAPORATED, Label.RISK_REJECTED}:
            key = row.reject_reason or row.label_detail or row.label.value
            out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def _build_proposals(config: ArbConfig, labeled, counts: dict[str, int]) -> list:
    """Deterministic proposal rules — no LLM. Conservative."""
    proposals = []
    total = max(1, len(labeled))
    fp = counts.get(Label.FALSE_POSITIVE.value, 0)
    ws_evap = counts.get(Label.WS_EVAPORATED.value, 0)
    wins = counts.get(Label.PAPER_WIN.value, 0)
    losses = counts.get(Label.PAPER_LOSS.value, 0)
    true_arb = counts.get(Label.TRUE_ARB.value, 0) + wins

    fp_rate = fp / total
    if fp_rate >= 0.5 and total >= 10:
        new_edge = min(500.0, config.min_edge_bps + 25.0)
        if new_edge > config.min_edge_bps:
            proposals.append(
                new_proposal(
                    key="ARB_MIN_EDGE_BPS",
                    current_value=config.min_edge_bps,
                    proposed_value=new_edge,
                    rationale=(
                        f"False-positive rate {fp_rate:.0%} over {total} labels; "
                        f"raise min edge to filter weak gamma flags."
                    ),
                    evidence={"false_positive_rate": fp_rate, "n": total, "false_positives": fp},
                )
            )

    if ws_evap >= 5 and ws_evap >= true_arb:
        new_watch = max(config.ws_watch_sec, 45.0)
        if new_watch > config.ws_watch_sec:
            proposals.append(
                new_proposal(
                    key="ARB_WS_WATCH_SEC",
                    current_value=config.ws_watch_sec,
                    proposed_value=new_watch,
                    rationale=(
                        f"{ws_evap} WS evaporations vs {true_arb} lasting signals; "
                        f"watch longer before trading."
                    ),
                    evidence={"ws_evaporated": ws_evap, "true_arb_like": true_arb},
                )
            )

    if losses > wins and (wins + losses) >= 5:
        new_size = max(5.0, round(config.max_position_usd * 0.5, 2))
        if new_size < config.max_position_usd:
            proposals.append(
                new_proposal(
                    key="ARB_MAX_POSITION_USD",
                    current_value=config.max_position_usd,
                    proposed_value=new_size,
                    rationale=(
                        f"Paper losses ({losses}) exceed wins ({wins}); "
                        f"halve position size until edge quality improves."
                    ),
                    evidence={"paper_wins": wins, "paper_losses": losses},
                )
            )

    # If almost no verified hits, suggest lowering edge slightly — still human-gated
    verified_like = true_arb + counts.get(Label.TRUE_ARB.value, 0)
    if total >= 20 and verified_like == 0 and config.min_edge_bps > 25:
        proposals.append(
            new_proposal(
                key="ARB_MIN_EDGE_BPS",
                current_value=config.min_edge_bps,
                proposed_value=max(25.0, config.min_edge_bps - 10.0),
                rationale=(
                    "No lasting verified/paper signals in window; "
                    "optional slight threshold ease for study — review carefully."
                ),
                evidence={"total": total, "verified_like": verified_like},
            )
        )

    return proposals


def run_postmortem(
    config: ArbConfig,
    store: OpportunityStore,
    *,
    days: int = 7,
    create_proposals: bool = True,
) -> PostmortemReport:
    labeled = label_history(store, days=days)
    counts = label_counts(labeled)
    total = len(labeled)
    fp = counts.get(Label.FALSE_POSITIVE.value, 0)
    ws_evap = counts.get(Label.WS_EVAPORATED.value, 0)
    paper_pnl = sum(r.realized_pnl or 0.0 for r in labeled if r.realized_pnl is not None)
    verified = sum(
        1
        for r in labeled
        if r.label in {Label.TRUE_ARB, Label.PAPER_WIN, Label.PAPER_LOSS}
    )

    dataset_path = config.state_root / "datasets" / f"labels_{days}d.jsonl"
    export_dataset(labeled, dataset_path)

    proposal_ids: list[str] = []
    notes: list[str] = []
    if create_proposals:
        prop_store = ProposalStore(config.state_root / "proposals.json")
        for prop in _build_proposals(config, labeled, counts):
            saved = prop_store.add(prop)
            proposal_ids.append(saved.id)
        if not proposal_ids:
            notes.append("No threshold proposals — evidence below rule triggers.")
        else:
            notes.append(
                f"Created {len(proposal_ids)} proposal(s). "
                "Review with `python -m arb proposals` then `approve` / `reject`."
            )
            notes.append("Never auto-applied. Copy env snippet only after approve.")

    if total == 0:
        notes.append("No labeled rows in window — keep Phase 1/2/3 loops collecting data.")

    report = PostmortemReport(
        days=days,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_labeled=total,
        label_counts=counts,
        reject_breakdown=_reject_breakdown(labeled),
        false_positive_rate=round(fp / total, 4) if total else 0.0,
        ws_evaporation_rate=round(ws_evap / total, 4) if total else 0.0,
        paper_pnl=round(paper_pnl, 6),
        verified_hits=verified,
        proposals_created=proposal_ids,
        notes=notes,
        dataset_path=str(dataset_path),
    )

    report_path = _write_report(config, report)
    report.report_path = str(report_path)
    _append_ledger(config, report)
    return report


def _write_report(config: ArbConfig, report: PostmortemReport) -> Path:
    path = config.state_root / "postmortems" / f"postmortem_{report.days}d.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Postmortem — last {report.days} days",
        "",
        f"Generated: {report.generated_at}",
        "",
        "## Summary",
        "",
        f"- Labeled rows: **{report.total_labeled}**",
        f"- Label counts: `{report.label_counts}`",
        f"- False-positive rate: **{report.false_positive_rate:.1%}**",
        f"- WS evaporation rate: **{report.ws_evaporation_rate:.1%}**",
        f"- Paper realized PnL: **${report.paper_pnl:.4f}**",
        f"- Verified-like hits: **{report.verified_hits}**",
        "",
        "## Reject breakdown",
        "",
    ]
    if report.reject_breakdown:
        for reason, n in report.reject_breakdown.items():
            lines.append(f"- `{reason}`: {n}")
    else:
        lines.append("- (none)")
    lines += ["", "## Proposals (human gate)", ""]
    if report.proposals_created:
        for pid in report.proposals_created:
            lines.append(f"- `{pid}` — pending review")
        lines.append("")
        lines.append("```bash")
        lines.append("python -m arb proposals")
        lines.append("python -m arb approve <proposal_id>")
        lines.append("python -m arb proposals --env-snippet")
        lines.append("```")
    else:
        lines.append("- None")
    lines += ["", "## Notes", ""]
    for note in report.notes:
        lines.append(f"- {note}")
    lines += ["", f"Dataset: `{report.dataset_path}`", ""]
    path.write_text("\n".join(lines))
    return path


def _append_ledger(config: ArbConfig, report: PostmortemReport) -> None:
    path = config.ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    block = (
        f"## Postmortem — {now}\n\n"
        f"- Window: {report.days}d\n"
        f"- Labeled: {report.total_labeled}\n"
        f"- FP rate: {report.false_positive_rate:.1%}\n"
        f"- WS evaporate: {report.ws_evaporation_rate:.1%}\n"
        f"- Paper PnL: ${report.paper_pnl:.4f}\n"
        f"- Proposals: {', '.join(report.proposals_created) or 'none'}\n"
        f"- Report: {report.report_path}\n\n"
        "---\n\n"
    )
    existing = path.read_text() if path.exists() else "# Polymarket Arb Ledger\n\n"
    path.write_text(existing + block)
