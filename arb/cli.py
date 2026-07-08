"""CLI for the Polymarket Dutch-book arb bot."""

from __future__ import annotations

import argparse
import json
import sys

from arb.config import ArbConfig
from arb.execute import execute_batch
from arb.scanner import run_scan
from arb.state import OpportunityStore


def _print_opportunity(opp, prefix: str = "") -> None:
    kind = opp.kind.value.replace("_", " ")
    print(f"{prefix}{kind:12} edge={opp.edge_bps:6.1f}bps total={opp.total:.4f} [{opp.source}]")
    print(f"{prefix}  {opp.question[:100]}")
    print(f"{prefix}  slug={opp.slug} condition={opp.condition_id[:18]}...")


def cmd_scan(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env()
    if args.min_edge_bps is not None:
        config = ArbConfig(
            min_edge_bps=args.min_edge_bps,
            taker_fee_bps=config.taker_fee_bps,
            page_size=config.page_size,
            max_markets=args.limit,
            verify_top_n=config.verify_top_n,
            state_dir=config.state_dir,
            dry_run=config.dry_run,
        )
    elif args.limit is not None:
        config = ArbConfig(
            min_edge_bps=config.min_edge_bps,
            taker_fee_bps=config.taker_fee_bps,
            page_size=config.page_size,
            max_markets=args.limit,
            verify_top_n=config.verify_top_n,
            state_dir=config.state_dir,
            dry_run=config.dry_run,
        )

    result = run_scan(config, gamma_only=args.gamma_only, persist=not args.no_persist)
    hits = result.all_hits

    if args.json:
        payload = {
            "scanned": result.scanned,
            "gamma_hits": [o.to_dict() for o in result.gamma_hits],
            "verified_hits": [o.to_dict() for o in result.verified_hits],
            "hits": [o.to_dict() for o in hits],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Scanned {result.scanned} active binary markets")
    print(f"Gamma candidates: {len(result.gamma_hits)}")
    if not args.gamma_only:
        print(f"CLOB-verified: {len(result.verified_hits)}")
    print()

    if not hits:
        print("No Dutch-book opportunities above threshold.")
        return 0

    print(f"Top opportunities ({len(hits)}):")
    for opp in hits[: args.top]:
        _print_opportunity(opp)
        print()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env()
    store = OpportunityStore(config.state_db)
    rows = store.recent(limit=args.limit)
    if args.json:
        print(json.dumps({"count": store.count(), "recent": rows}, indent=2, default=str))
        return 0

    print(f"Stored opportunities: {store.count()}")
    print(f"Trading enabled: {config.trading_enabled()} dry_run={config.dry_run}")
    print(f"State DB: {config.state_db}")
    print()
    for row in rows:
        verified = "verified" if row["verified"] else "gamma"
        print(
            f"- {row['detected_at']} {row['kind']:12} "
            f"edge={row['edge_bps']:.1f}bps [{verified}] {row['question'][:80]}"
        )
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env()
    store = OpportunityStore(config.state_db)
    rows = store.recent(limit=args.limit)
    if not rows:
        print("No stored opportunities to trade.")
        return 1

    from arb.dutch_book import ArbKind, Opportunity

    opps: list[Opportunity] = []
    for row in rows:
        if row["verified"] == 0 and args.verified_only:
            continue
        payload = json.loads(row["payload"])
        opps.append(
            Opportunity(
                kind=ArbKind(payload["kind"]),
                condition_id=payload["condition_id"],
                slug=payload["slug"],
                question=payload["question"],
                outcomes=payload["outcomes"],
                token_ids=payload["token_ids"],
                prices=payload["prices"],
                total=payload["total"],
                edge=payload["edge"],
                edge_bps=payload["edge_bps"],
                source=payload["source"],
            )
        )

    results = execute_batch(config, opps[: args.limit])
    for res in results:
        print(f"{res.status}: {res.opportunity.question[:60]} — {res.detail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-arb",
        description="Scan Polymarket for Dutch-book arbitrage opportunities.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan all active markets")
    scan.add_argument("--gamma-only", action="store_true", help="Skip CLOB book verification")
    scan.add_argument("--limit", type=int, default=None, help="Max markets to scan")
    scan.add_argument("--min-edge-bps", type=float, default=None, help="Minimum edge in bps")
    scan.add_argument("--top", type=int, default=10, help="Show top N hits")
    scan.add_argument("--json", action="store_true", help="JSON output")
    scan.add_argument("--no-persist", action="store_true", help="Do not write to state DB")
    scan.set_defaults(func=cmd_scan)

    status = sub.add_parser("status", help="Show stored opportunities")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    trade = sub.add_parser("trade", help="Execute or dry-run stored opportunities")
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
