"""Human-readable study ledger — agent forgets, the repo doesn't."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from arb.dutch_book import Opportunity
from arb.models import OppState, RejectReason


def append_ledger(
    path: Path,
    *,
    run_id: int,
    scanned: int,
    gamma_hits: int,
    verified: list[Opportunity],
    rejected: list[tuple[Opportunity, RejectReason]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"## Scan run #{run_id} — {now}",
        "",
        f"- Markets scanned: {scanned}",
        f"- Gamma flags: {gamma_hits}",
        f"- CLOB verified: {len(verified)}",
        f"- Rejected after book check: {len(rejected)}",
        "",
    ]
    if verified:
        lines.append("### Verified")
        for opp in verified:
            lines.append(
                f"- `{OppState.CLOB_VERIFIED.value}` {opp.kind.value} "
                f"edge={opp.edge_bps:.1f}bps total={opp.total:.4f} — {opp.question[:80]}"
            )
        lines.append("")
    if rejected:
        lines.append("### Rejected")
        for opp, reason in rejected[:20]:
            lines.append(
                f"- `{OppState.REJECTED.value}` {reason.value} "
                f"gamma_edge={opp.edge_bps:.1f}bps — {opp.question[:80]}"
            )
        if len(rejected) > 20:
            lines.append(f"- … and {len(rejected) - 20} more")
        lines.append("")
    lines.append("---")
    lines.append("")

    existing = path.read_text() if path.exists() else (
        "# Polymarket Arb Study Ledger\n\n"
        "Phase 1 study mode. No live trading. "
        "Read this weekly to avoid comprehension rot.\n\n"
    )
    path.write_text(existing + "\n".join(lines))
