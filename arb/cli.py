"""CLI for the Polymarket Dutch-book arb bot (Phase 1 study mode)."""

from __future__ import annotations

import argparse
import json
import sys

from arb.config import ArbConfig
from arb.models import OppState
from arb.scanner import format_alert, run_scan
from arb.state import OpportunityStore


def _print_opportunity(opp, prefix: str = "") -> None:
    kind = opp.kind.value.replace("_", " ")
    print(f"{prefix}{kind:12} edge={opp.edge_bps:6.1f}bps total={opp.total:.4f} [{opp.source}]")
    print(f"{prefix}  {opp.question[:100]}")
    print(f"{prefix}  slug={opp.slug} condition={opp.condition_id[:18]}...")


def cmd_scan(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env().with_overrides(
        min_edge_bps=args.min_edge_bps,
        max_markets=args.limit,
        study_mode=True if args.study else None,
    )
    result = run_scan(config, gamma_only=args.gamma_only, persist=not args.no_persist)

    if args.json:
        payload = {
            "phase": 1,
            "study_mode": config.study_mode,
            "run_id": result.run_id,
            "scanned": result.scanned,
            "gamma_hits": [o.to_dict() for o in result.gamma_hits],
            "verified_hits": [o.to_dict() for o in result.verified_hits],
            "rejected": [
                {"opportunity": o.to_dict(), "reason": r.value} for o, r in result.rejected
            ],
            "hits": [o.to_dict() for o in result.all_hits],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Phase 1 study scan — run_id={result.run_id}")
    print(f"Scanned {result.scanned} active markets")
    print(f"Gamma flags: {len(result.gamma_hits)}")
    if not args.gamma_only:
        print(f"CLOB-verified: {len(result.verified_hits)}")
        print(f"Rejected after book: {len(result.rejected)}")
        if result.rejected:
            reasons: dict[str, int] = {}
            for _, reason in result.rejected:
                reasons[reason.value] = reasons.get(reason.value, 0) + 1
            print(f"Reject reasons: {reasons}")
    print()

    if result.verified_hits:
        print(f"Verified opportunities ({len(result.verified_hits)}):")
        for opp in result.verified_hits[: args.top]:
            _print_opportunity(opp)
            print()
    elif result.gamma_hits and args.gamma_only:
        print(f"Gamma candidates ({len(result.gamma_hits)}):")
        for opp in result.gamma_hits[: args.top]:
            _print_opportunity(opp)
            print()
    else:
        print("No CLOB-verified Dutch-book opportunities above threshold.")

    if config.alert_on_verified and not args.quiet:
        alert = format_alert(result)
        if alert:
            print()
            print(alert)

    if result.metrics_path:
        print(f"Metrics: {result.metrics_path}")
        print(f"Ledger:  {config.ledger_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env()
    store = OpportunityStore(config.state_db)
    state = OppState(args.state) if args.state else None
    rows = store.recent(limit=args.limit, state=state)
    if args.json:
        print(
            json.dumps(
                {
                    "count": store.count(state=state),
                    "by_state": {
                        s.value: store.count(state=s)
                        for s in (OppState.GAMMA_FLAG, OppState.CLOB_VERIFIED, OppState.REJECTED)
                    },
                    "recent": rows,
                },
                indent=2,
                default=str,
            )
        )
        return 0

    print(f"Phase 1 study mode: {config.study_mode}")
    print(f"Trading enabled: {config.trading_enabled()} dry_run={config.dry_run}")
    print(f"State DB: {config.state_db}")
    print(f"Ledger:   {config.ledger_path}")
    print()
    print(
        f"Counts — gamma={store.count(state=OppState.GAMMA_FLAG)} "
        f"verified={store.count(state=OppState.CLOB_VERIFIED)} "
        f"rejected={store.count(state=OppState.REJECTED)} "
        f"total={store.count()}"
    )
    print()
    for row in rows:
        state_s = row.get("state") or ("verified" if row.get("verified") else "gamma")
        print(
            f"- {row['detected_at']} [{state_s:14}] {row['kind']:12} "
            f"edge={row['edge_bps']:.1f}bps {row['question'][:70]}"
        )
    return 0


def cmd_study(args: argparse.Namespace) -> int:
    """30-day study summary — go/no-go gate for Phase 2."""
    config = ArbConfig.from_env()
    store = OpportunityStore(config.state_db)
    summary = store.study_summary(days=args.days)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"Study summary — last {summary['days']} days")
    print(f"  Scan runs:          {summary['scan_runs']}")
    print(f"  Markets scanned:    {summary['markets_scanned']}")
    print(f"  Gamma hits:         {summary['gamma_hits']}")
    print(f"  CLOB verified:      {summary['verified_hits']}")
    print(f"  Rejected:           {summary['rejected']}")
    print(f"  Reject breakdown:   {summary['reject_breakdown']}")
    print(f"  Hypothetical PnL:   {summary['hypothetical_pnl_sum']:.4f} (unit size)")
    print()
    gate = summary["go_no_go"]
    ready = gate["ready_for_phase2"]
    print(f"Phase 2 gate: {gate['phase2_gate']}")
    print(f"Verified in window: {gate['verified_hits_in_window']}")
    print(f"Ready for Phase 2:  {'YES' if ready else 'NO — keep collecting study data'}")
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    """Phase 1: trading is blocked. Phase 2 will unlock paper/live."""
    print(
        "Phase 1 study mode — trading is disabled.\n"
        "Complete the study gate (`python -m arb study`) before Phase 2 execution."
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-arb",
        description="Polymarket Dutch-book arb bot — Phase 1 study scanner.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan all active markets (study mode)")
    scan.add_argument("--gamma-only", action="store_true", help="Skip CLOB book verification")
    scan.add_argument("--limit", type=int, default=None, help="Max markets to scan")
    scan.add_argument("--min-edge-bps", type=float, default=None, help="Minimum edge in bps")
    scan.add_argument("--top", type=int, default=10, help="Show top N hits")
    scan.add_argument("--json", action="store_true", help="JSON output")
    scan.add_argument("--no-persist", action="store_true", help="Do not write to state DB")
    scan.add_argument("--study", action="store_true", help="Force study_mode=true")
    scan.add_argument("--quiet", action="store_true", help="Suppress alert block")
    scan.set_defaults(func=cmd_scan)

    status = sub.add_parser("status", help="Show stored opportunities")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--state", choices=[s.value for s in OppState], default=None)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    study = sub.add_parser("study", help="Study-mode go/no-go summary")
    study.add_argument("--days", type=int, default=30)
    study.add_argument("--json", action="store_true")
    study.set_defaults(func=cmd_study)

    trade = sub.add_parser("trade", help="Blocked in Phase 1")
    trade.add_argument("--limit", type=int, default=5)
    trade.add_argument("--verified-only", action="store_true")
    trade.set_defaults(func=cmd_trade)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
