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
    """One full money-loop turn: scan → [ws reverify] → paper trade → reconcile."""
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

    if args.ws and config.ws_enabled and result.verified_hits:
        print("=== LOOP: ws reverify ===")
        from arb.reverify import reverify_opportunities
        from arb.ws_feed import run_feed_sync

        asset_ids: list[str] = []
        for opp in result.verified_hits:
            asset_ids.extend(opp.token_ids)
        asset_ids = list(dict.fromkeys(asset_ids))[: config.ws_max_assets]
        cache = run_feed_sync(
            asset_ids,
            duration_sec=min(config.ws_watch_sec, args.ws_sec or config.ws_watch_sec),
            ws_url=config.ws_url,
            seed_rest=config.ws_seed_rest,
        )
        rv = reverify_opportunities(config, cache, result.verified_hits)
        print(
            f"ws checked={rv.checked} still_valid={len(rv.still_valid)} "
            f"evaporated={len(rv.evaporated)} missing={len(rv.missing_book)}"
        )
        # Prefer WS-still-valid for trade selection
        valid_ids = {o.condition_id for o in rv.still_valid}
        rows = [
            r
            for r in store.recent(limit=args.trade_limit * 3, state=OppState.CLOB_VERIFIED)
            if r["condition_id"] in valid_ids
        ][: args.trade_limit]
    else:
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


def cmd_watch(args: argparse.Namespace) -> int:
    """Phase 3: stream CLOB books and re-verify opportunities in real time."""
    from arb.book_cache import BookCache
    from arb.reverify import reverify_opportunities, reverify_store_verified
    from arb.ws_feed import run_feed_sync

    config = ArbConfig.from_env()
    store = OpportunityStore(config.state_db)
    duration = args.seconds if args.seconds is not None else config.ws_watch_sec

    # Collect assets: explicit tokens, or from recent verified / gamma scan
    asset_ids: list[str] = list(args.token or [])
    watch_opps = []

    if args.from_store:
        rows = store.recent(limit=args.limit, state=OppState.CLOB_VERIFIED)
        watch_opps = [store.opportunity_from_row(r) for r in rows]
        for opp in watch_opps:
            asset_ids.extend(opp.token_ids)

    if args.scan_first:
        result = run_scan(
            config.with_overrides(max_markets=args.limit),
            gamma_only=False,
            persist=not args.no_persist,
        )
        watch_opps = result.verified_hits or result.gamma_hits[: args.limit]
        for opp in watch_opps:
            asset_ids.extend(opp.token_ids)

    asset_ids = list(dict.fromkeys(asset_ids))[: config.ws_max_assets]
    if not asset_ids:
        print("No asset IDs to watch. Use --token, --from-store, or --scan-first.")
        return 1

    print(f"Watching {len(asset_ids)} assets for {duration:.0f}s via {config.ws_url}")
    cache = BookCache()
    updates = {"n": 0}

    def on_update(touched, book_cache):
        updates["n"] += 1
        if watch_opps and updates["n"] % max(1, args.every) == 0:
            rv = reverify_opportunities(config, book_cache, watch_opps)
            print(
                f"[update {updates['n']}] touched={len(touched)} "
                f"valid={len(rv.still_valid)} evaporated={len(rv.evaporated)}"
            )
            for opp in rv.still_valid[:3]:
                print(f"  VALID {opp.kind.value} edge={opp.edge_bps:.1f}bps {opp.question[:60]}")

    try:
        cache = run_feed_sync(
            asset_ids,
            duration_sec=duration,
            cache=cache,
            on_update=on_update if watch_opps else None,
            ws_url=config.ws_url,
            seed_rest=not args.no_seed,
        )
    except RuntimeError as exc:
        print(f"Feed error: {exc}")
        return 1

    print(f"Done. cache_size={len(cache)} updates={cache.updates} last={cache.last_event_at}")

    if watch_opps:
        rv = reverify_opportunities(config, cache, watch_opps)
        if args.json:
            print(json.dumps(rv.to_dict(), indent=2))
        else:
            print(
                f"Final reverify: checked={rv.checked} valid={len(rv.still_valid)} "
                f"evaporated={len(rv.evaporated)} missing={len(rv.missing_book)}"
            )
        if args.persist_rejects and args.from_store:
            reverify_store_verified(config, store, cache, limit=args.limit, persist=True)
            print("Persisted evaporated → REJECTED (ws_reverify)")
    return 0


def cmd_postmortem(args: argparse.Namespace) -> int:
    from arb.postmortem import run_postmortem

    config = ArbConfig.from_env()
    store = OpportunityStore(config.state_db)
    use_grok = bool(getattr(args, "grok", False))
    report = run_postmortem(
        config,
        store,
        days=args.days,
        create_proposals=not args.no_proposals,
        use_grok=use_grok,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    print(f"Postmortem — last {report.days} days")
    print(f"  Labeled:        {report.total_labeled}")
    print(f"  Labels:         {report.label_counts}")
    print(f"  FP rate:        {report.false_positive_rate:.1%}")
    print(f"  WS evaporate:   {report.ws_evaporation_rate:.1%}")
    print(f"  Paper PnL:      ${report.paper_pnl:.4f}")
    print(f"  Verified-like:  {report.verified_hits}")
    print(f"  Dataset:        {report.dataset_path}")
    print(f"  Report:         {report.report_path}")
    if report.grok_ok is not None:
        print(f"  Grok:           {'ok' if report.grok_ok else 'failed'} ({report.grok_path})")
        if report.grok_error:
            print(f"  Grok error:     {report.grok_error}")
        if report.grok_proposals:
            print(f"  Grok proposals: {', '.join(report.grok_proposals)}")
    if report.proposals_created:
        print(f"  Proposals:      {', '.join(report.proposals_created)}")
        print("  Next: python -m arb proposals && python -m arb approve <id>")
    for note in report.notes:
        print(f"  Note: {note}")
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    from arb.proposals import ProposalStore, render_env_snippet

    config = ArbConfig.from_env()
    store = ProposalStore(config.state_root / "proposals.json")
    items = store.list(status=args.status)
    if args.env_snippet:
        approved = store.list(status="approved")
        print(render_env_snippet(approved))
        return 0
    if args.json:
        print(json.dumps([p.to_dict() for p in items], indent=2))
        return 0
    if not items:
        print("No proposals." + (f" (status={args.status})" if args.status else ""))
        return 0
    for p in items:
        print(
            f"- [{p.status:8}] {p.id}\n"
            f"    {p.key}: {p.current_value} → {p.proposed_value}\n"
            f"    {p.rationale}"
        )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from arb.proposals import ProposalStore, render_env_snippet

    config = ArbConfig.from_env()
    store = ProposalStore(config.state_root / "proposals.json")
    try:
        p = store.decide(args.proposal_id, approve=True, by=args.by)
    except (KeyError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Approved {p.id}: {p.key}={p.proposed_value}")
    print("Copy into .env manually (not auto-applied):")
    print(render_env_snippet([p]))
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    from arb.proposals import ProposalStore

    config = ArbConfig.from_env()
    store = ProposalStore(config.state_root / "proposals.json")
    try:
        p = store.decide(args.proposal_id, approve=False, by=args.by)
    except (KeyError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Rejected {p.id}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Phase 5 standalone worker — daemon or one-shot jobs."""
    from dataclasses import replace as dc_replace

    from arb.worker import ArbWorker, WorkerConfig, load_status

    config = ArbConfig.from_env()
    wc = WorkerConfig.from_env()
    overrides = {}
    if getattr(args, "scan_limit", None) is not None:
        overrides["scan_limit"] = args.scan_limit
    if getattr(args, "paper", False):
        overrides["paper"] = True
    if getattr(args, "ws", False):
        overrides["use_ws"] = True
    if overrides:
        wc = dc_replace(wc, **overrides)

    worker = ArbWorker(config, wc)

    if args.worker_command == "status":
        st = load_status(config.state_root)
        if st is None:
            print(f"No worker status at {config.state_root / 'worker_status.json'}")
            return 1
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(f"running={st.get('running')} started={st.get('started_at')}")
            print(f"heartbeat={st.get('last_heartbeat')}")
            print(f"scans={st.get('scans')} loops={st.get('loops')} alerts={st.get('alerts')}")
            print(f"last_scan={st.get('last_scan_at')} last_loop={st.get('last_loop_at')}")
            print(f"last_error={st.get('last_error')}")
            pid_path = config.state_root / "worker.pid"
            if pid_path.exists():
                print(f"pid_file={pid_path.read_text().strip()}")
        return 0

    if args.worker_command == "once":
        jobs = [j.strip() for j in (args.jobs or "scan").split(",") if j.strip()]
        out = worker.run_once(jobs=jobs)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"worker once @ {out['at']}")
            for name, result in out["jobs"].items():
                print(f"  {name}: {result}")
        return 0 if not any(isinstance(v, dict) and "error" in v for v in out["jobs"].values()) else 1

    if args.worker_command == "run":
        print("Starting ArbWorker (Ctrl+C / SIGTERM to stop)…")
        worker.run_forever()
        return 0

    print(f"Unknown worker command: {args.worker_command}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-arb",
        description="Polymarket Dutch-book arb bot — Phases 1–5 (money loop + intelligence + worker).",
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

    loop = sub.add_parser("loop", help="One turn: scan → [ws] → trade → reconcile")
    loop.add_argument("--paper", action="store_true", help="Paper execution loop")
    loop.add_argument("--limit", type=int, default=50, help="Max markets to scan")
    loop.add_argument("--trade-limit", type=int, default=5)
    loop.add_argument("--ws", action="store_true", help="WS re-verify before trade")
    loop.add_argument("--ws-sec", type=float, default=None, help="WS watch seconds")
    loop.set_defaults(func=cmd_loop)

    watch = sub.add_parser("watch", help="Stream CLOB books and re-verify (Phase 3)")
    watch.add_argument("--seconds", type=float, default=None, help="Watch duration")
    watch.add_argument("--token", action="append", default=[], help="Token ID to subscribe")
    watch.add_argument("--from-store", action="store_true", help="Watch CLOB_VERIFIED from DB")
    watch.add_argument("--scan-first", action="store_true", help="Scan then watch those tokens")
    watch.add_argument("--limit", type=int, default=20)
    watch.add_argument("--every", type=int, default=5, help="Reverify every N WS updates")
    watch.add_argument("--no-seed", action="store_true", help="Skip REST book seed")
    watch.add_argument("--no-persist", action="store_true")
    watch.add_argument("--persist-rejects", action="store_true", help="Mark evaporated REJECTED")
    watch.add_argument("--json", action="store_true")
    watch.set_defaults(func=cmd_watch)

    post = sub.add_parser("postmortem", help="Label history + propose thresholds (Phase 4)")
    post.add_argument("--days", type=int, default=7)
    post.add_argument("--no-proposals", action="store_true")
    post.add_argument(
        "--grok",
        action="store_true",
        help="Ask Grok (xAI) for offline analysis + human-gated proposals (needs XAI_API_KEY)",
    )
    post.add_argument("--json", action="store_true")
    post.set_defaults(func=cmd_postmortem)

    props = sub.add_parser("proposals", help="List threshold proposals")
    props.add_argument("--status", default=None, help="pending|approved|rejected|applied")
    props.add_argument("--env-snippet", action="store_true", help="Print approved env lines")
    props.add_argument("--json", action="store_true")
    props.set_defaults(func=cmd_proposals)

    appr = sub.add_parser("approve", help="Human-approve a proposal (does not auto-apply)")
    appr.add_argument("proposal_id")
    appr.add_argument("--by", default="human")
    appr.set_defaults(func=cmd_approve)

    rej = sub.add_parser("reject", help="Human-reject a proposal")
    rej.add_argument("proposal_id")
    rej.add_argument("--by", default="human")
    rej.set_defaults(func=cmd_reject)

    worker = sub.add_parser("worker", help="Phase 5 standalone 24/7 worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)

    w_run = worker_sub.add_parser("run", help="Run forever (daemon)")
    w_run.add_argument("--paper", action="store_true", help="Force paper loop")
    w_run.add_argument("--ws", action="store_true", help="WS re-verify in loop ticks")
    w_run.add_argument("--scan-limit", type=int, default=None)
    w_run.set_defaults(func=cmd_worker)

    w_once = worker_sub.add_parser("once", help="Run selected jobs once (cron/cloud)")
    w_once.add_argument(
        "--jobs",
        default="scan",
        help="Comma-separated: scan,loop,reconcile,postmortem",
    )
    w_once.add_argument("--paper", action="store_true")
    w_once.add_argument("--ws", action="store_true")
    w_once.add_argument("--scan-limit", type=int, default=None)
    w_once.add_argument("--json", action="store_true")
    w_once.set_defaults(func=cmd_worker)

    w_st = worker_sub.add_parser("status", help="Show worker heartbeat/status")
    w_st.add_argument("--json", action="store_true")
    w_st.add_argument("--paper", action="store_true")
    w_st.add_argument("--ws", action="store_true")
    w_st.add_argument("--scan-limit", type=int, default=None)
    w_st.set_defaults(func=cmd_worker)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
