"""CLI for the Polymarket Dutch-book arb bot — Phase 1 scan + Phase 2 execution."""

from __future__ import annotations

import argparse
import json
import sys

from arb.config import ArbConfig
from arb.execute import execute_batch
from arb.models import ExecMode, OppState
from arb.reconcile import reconcile
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
            "phase": 2 if not config.study_mode else 1,
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

    mode = "study" if config.study_mode else "execution"
    print(f"Scan ({mode}) — run_id={result.run_id}")
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
    states = [
        OppState.GAMMA_FLAG,
        OppState.CLOB_VERIFIED,
        OppState.RISK_OK,
        OppState.ORDER_PLACED,
        OppState.FILLED,
        OppState.SETTLED,
        OppState.CLOSED,
        OppState.REJECTED,
    ]
    if args.json:
        print(
            json.dumps(
                {
                    "count": store.count(state=state),
                    "by_state": {s.value: store.count(state=s) for s in states},
                    "open_positions": store.count_open(),
                    "fills_today": store.count_fills_today(),
                    "realized_pnl_today": store.realized_pnl_today(),
                    "recent": rows,
                },
                indent=2,
                default=str,
            )
        )
        return 0

    print(f"Study mode: {config.study_mode}  exec_mode={config.exec_mode.value}")
    print(f"Kill switch: {config.kill_switch}  dry_run={config.dry_run}")
    print(f"Live allowed: {config.live_allowed()}")
    print(f"State DB: {config.state_db}")
    print(f"Ledger:   {config.ledger_path}")
    print()
    print(
        f"Counts — verified={store.count(state=OppState.CLOB_VERIFIED)} "
        f"risk_ok={store.count(state=OppState.RISK_OK)} "
        f"filled={store.count(state=OppState.FILLED)} "
        f"closed={store.count(state=OppState.CLOSED)} "
        f"rejected={store.count(state=OppState.REJECTED)}"
    )
    print(
        f"Open={store.count_open()} fills_today={store.count_fills_today()} "
        f"realized_today=${store.realized_pnl_today():.4f}"
    )
    print()
    for row in rows:
        state_s = row.get("state") or "?"
        print(
            f"- {row['detected_at']} [{state_s:14}] {row['kind']:12} "
            f"edge={row['edge_bps']:.1f}bps {row['question'][:70]}"
        )
    return 0


def cmd_study(args: argparse.Namespace) -> int:
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
    print()
    print("Override: set ARB_STUDY_MODE=false to enable paper execution anyway.")
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    """Paper (default) or live-gated execution of CLOB-verified opportunities."""
    config = ArbConfig.from_env()
    if args.paper:
        config = config.with_overrides(exec_mode=ExecMode.PAPER, study_mode=False)
    if args.force_study_off:
        config = config.with_overrides(study_mode=False)

    if config.study_mode and not args.force_study_off and not args.paper:
        print(
            "Study mode is on — trading blocked.\n"
            "Use: python -m arb trade --paper   (sets study_mode off for paper)\n"
            "Or:  ARB_STUDY_MODE=false python -m arb trade"
        )
        return 1

    store = OpportunityStore(config.state_db)
    rows = store.recent(limit=args.limit * 5, state=OppState.CLOB_VERIFIED)
    if args.verified_only is False:
        # also allow RISK_OK retries? keep verified-only default path
        pass
    if not rows:
        print("No CLOB_VERIFIED opportunities to trade.")
        return 1

    pairs = []
    for row in rows[: args.limit]:
        opp = store.opportunity_from_row(row)
        pairs.append((opp, int(row["id"])))

    results = execute_batch(config, store, pairs)
    for res in results:
        print(f"{res.status}: {res.opportunity.question[:60]} — {res.detail}")
    return 0 if any(r.status == "paper_filled" for r in results) else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env()
    store = OpportunityStore(config.state_db)
    report = reconcile(config, store, settle_paper=not args.no_settle)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    print("Reconcile report")
    print(f"  Fills:          {report.fills}")
    print(f"  Settled now:    {report.settled}")
    print(f"  Open positions: {report.open_positions}")
    print(f"  Expected PnL:   ${report.expected_pnl_sum:.4f}")
    print(f"  Realized PnL:   ${report.realized_pnl_sum:.4f}")
    print(f"  Gap:            ${report.pnl_gap:.4f}")
    for note in report.notes:
        print(f"  Note: {note}")
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    """One full money-loop turn: scan → paper trade verified → reconcile."""
    config = ArbConfig.from_env().with_overrides(
        max_markets=args.limit,
        study_mode=False if args.paper else None,
        exec_mode=ExecMode.PAPER if args.paper else None,
    )
    if config.study_mode and not args.paper:
        print("Loop requires paper mode or ARB_STUDY_MODE=false")
        print("Use: python -m arb loop --paper --limit 50")
        return 1

    print("=== LOOP: scan ===")
    result = run_scan(config, gamma_only=False, persist=True)
    print(f"scanned={result.scanned} verified={len(result.verified_hits)}")

    store = OpportunityStore(config.state_db)
    rows = store.recent(limit=args.trade_limit, state=OppState.CLOB_VERIFIED)
    pairs = [(store.opportunity_from_row(r), int(r["id"])) for r in rows]

    print("=== LOOP: trade ===")
    if not pairs:
        print("No verified opportunities to trade.")
    else:
        for res in execute_batch(config, store, pairs):
            print(f"{res.status}: {res.detail}")

    print("=== LOOP: reconcile ===")
    report = reconcile(config, store, settle_paper=True)
    print(
        f"fills={report.fills} settled={report.settled} "
        f"realized=${report.realized_pnl_sum:.4f} gap=${report.pnl_gap:.4f}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-arb",
        description="Polymarket Dutch-book arb bot — Phase 1+2 money loop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan all active markets")
    scan.add_argument("--gamma-only", action="store_true")
    scan.add_argument("--limit", type=int, default=None)
    scan.add_argument("--min-edge-bps", type=float, default=None)
    scan.add_argument("--top", type=int, default=10)
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--no-persist", action="store_true")
    scan.add_argument("--study", action="store_true")
    scan.add_argument("--quiet", action="store_true")
    scan.set_defaults(func=cmd_scan)

    status = sub.add_parser("status", help="Show stored opportunities / positions")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--state", choices=[s.value for s in OppState], default=None)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    study = sub.add_parser("study", help="Study-mode go/no-go summary")
    study.add_argument("--days", type=int, default=30)
    study.add_argument("--json", action="store_true")
    study.set_defaults(func=cmd_study)

    trade = sub.add_parser("trade", help="Paper/live execute CLOB-verified opps")
    trade.add_argument("--limit", type=int, default=5)
    trade.add_argument("--verified-only", action="store_true", default=True)
    trade.add_argument("--paper", action="store_true", help="Force paper mode, exit study")
    trade.add_argument("--force-study-off", action="store_true")
    trade.set_defaults(func=cmd_trade)

    rec = sub.add_parser("reconcile", help="Reconcile fills vs expected PnL")
    rec.add_argument("--no-settle", action="store_true", help="Do not auto-settle paper")
    rec.add_argument("--json", action="store_true")
    rec.set_defaults(func=cmd_reconcile)

    loop = sub.add_parser("loop", help="One turn: scan → trade → reconcile")
    loop.add_argument("--paper", action="store_true", help="Paper execution loop")
    loop.add_argument("--limit", type=int, default=50, help="Max markets to scan")
    loop.add_argument("--trade-limit", type=int, default=5)
    loop.set_defaults(func=cmd_loop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
