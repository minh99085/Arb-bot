"""Execution plane — paper fills + live CLOB (py-clob-client-v2), hard-gated."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity
from arb.models import ExecMode, OppState, RiskRejectReason, SafetyMode
from arb.paper import PaperFill, simulate_paper_fill
from arb.risk import RiskDecision, check_risk
from arb.scanner import VerifyOutcome, verify_one
from arb.state import OpportunityStore

# Live-leg statuses that represent a confirmed match/fill (not a mere ack).
_MATCHED_STATUSES = {"matched", "filled"}


@dataclass
class TradeResult:
    opportunity: Opportunity
    status: str
    detail: str
    opportunity_id: int | None = None
    fill: PaperFill | None = None
    risk: RiskDecision | None = None
    live: Any | None = None


def refresh_opportunity_from_clob(config: ArbConfig, opp: Opportunity) -> VerifyOutcome:
    """Re-fetch live CLOB books immediately before trade (live-parity paper path)."""
    return verify_one(config, opp)


def _bundle_fill_state(live: Any) -> str:
    """Classify a live bundle result: 'filled' | 'partial' | 'posted'.

    A bundle is only 'filled' when every leg is a confirmed match AND all legs
    share the same (positive) share quantity — i.e. a genuine complete set. A
    mere order acknowledgement (resting/posted), a failed/errored leg, or
    unequal leg quantities is NOT a filled bundle.
    """
    legs = [leg for leg in (live.legs or []) if getattr(leg, "token_id", "")]
    if not legs:
        return "posted"
    matched = [
        leg
        for leg in legs
        if (getattr(leg, "status", "") or "").lower() in _MATCHED_STATUSES
        and not getattr(leg, "error", None)
    ]
    if len(matched) != len(legs):
        return "partial" if matched else "posted"
    sizes = [round(float(getattr(leg, "size", 0.0) or 0.0), 4) for leg in legs]
    if sizes[0] <= 0 or len(set(sizes)) != 1:
        # Unequal (or zero) leg quantities are not a complete, settleable set.
        return "partial"
    return "filled"


def execute_opportunity(
    config: ArbConfig,
    store: OpportunityStore,
    opp: Opportunity,
    *,
    opportunity_id: int | None = None,
    ask_depth: float | None = None,
    bid_depth: float | None = None,
    category: str | None = None,
) -> TradeResult:
    """Run risk → order → fill for one opportunity.

    Default is paper. Live requires ARB_ALLOW_LIVE + key + exec_mode=live + gates.
    Realistic paper re-verifies on live CLOB books at execution time.
    """
    # --- Sell bundles are not executable yet ---------------------------------
    # No verified collateral-split / inventory / common-quantity / reconciliation
    # workflow exists. It stays a detected research signal, but execution is a
    # clear UNSUPPORTED_STRATEGY rejection (no state mutation, no order).
    if opp.kind == ArbKind.SELL_BUNDLE:
        return TradeResult(
            opportunity=opp,
            status="unsupported_strategy",
            detail=(
                "SELL_BUNDLE execution disabled (UNSUPPORTED_STRATEGY): no verified "
                "inventory/collateral-split/common-quantity workflow yet — research "
                "signal only until a later phase implements it."
            ),
            opportunity_id=opportunity_id,
        )

    # --- Execution safety gate ------------------------------------------------
    # SCAN_ONLY / SHADOW never create orders or fills, and PAPER_EXECUTION needs
    # the explicit ARB_PAPER_EXECUTION_ENABLED gate. Blocked opportunities keep
    # their current state (they remain shadow/candidate records — not rejected)
    # and no network calls are made.
    if config.safety_mode == SafetyMode.SCAN_ONLY:
        return TradeResult(
            opportunity=opp,
            status="scan_only",
            detail="SCAN_ONLY safety mode — scan/verify/log only, no order or fill.",
            opportunity_id=opportunity_id,
        )
    if config.safety_mode == SafetyMode.SHADOW:
        return TradeResult(
            opportunity=opp,
            status="shadow",
            detail="SHADOW safety mode — observation recorded, no order or fill.",
            opportunity_id=opportunity_id,
        )
    if config.safety_mode == SafetyMode.PAPER_EXECUTION and not config.paper_execution_enabled:
        return TradeResult(
            opportunity=opp,
            status="paper_execution_disabled",
            detail=(
                "PAPER_EXECUTION requires the explicit ARB_PAPER_EXECUTION_ENABLED=true "
                "gate — simulated fills are disabled."
            ),
            opportunity_id=opportunity_id,
        )

    row = store.get(opportunity_id) if opportunity_id is not None else None
    exec_plan = None  # L2/L3 complete-set plan from a live CLOB refresh, if any

    if config.paper_realistic and config.exec_mode == ExecMode.PAPER:
        if row and row.get("state") == OppState.GAMMA_FLAG.value:
            if opportunity_id is not None:
                store.transition(
                    opportunity_id,
                    OppState.REJECTED,
                    reason=RiskRejectReason.GAMMA_ONLY.value,
                )
            return TradeResult(
                opportunity=opp,
                status="gamma_rejected",
                detail="Realistic paper: gamma-only signals cannot be traded",
                opportunity_id=opportunity_id,
            )
        if opp.source == "gamma":
            if opportunity_id is not None:
                store.transition(
                    opportunity_id,
                    OppState.REJECTED,
                    reason=RiskRejectReason.GAMMA_ONLY.value,
                )
            return TradeResult(
                opportunity=opp,
                status="gamma_rejected",
                detail="Realistic paper: opportunity source is gamma mid-price, not CLOB",
                opportunity_id=opportunity_id,
            )

        refresh = refresh_opportunity_from_clob(config, opp)
        if refresh.opportunity is None:
            reason = refresh.reject_reason.value if refresh.reject_reason else "unknown"
            if opportunity_id is not None:
                store.transition(
                    opportunity_id,
                    OppState.REJECTED,
                    reason=f"exec_verify:{reason}",
                )
            return TradeResult(
                opportunity=opp,
                status="exec_verify_failed",
                detail=f"Live CLOB re-verify failed at execution: {reason}",
                opportunity_id=opportunity_id,
            )
        opp = refresh.opportunity
        ask_depth = refresh.ask_depth
        bid_depth = refresh.bid_depth
        exec_plan = refresh.plan

    risk = check_risk(
        config,
        store,
        opp,
        ask_depth=ask_depth,
        bid_depth=bid_depth,
        category=category,
        plan=exec_plan,
    )
    if not risk.ok:
        if opportunity_id is not None:
            store.transition(
                opportunity_id,
                OppState.REJECTED,
                reason=(risk.reason or RiskRejectReason.OTHER).value,
            )
        return TradeResult(
            opportunity=opp,
            status="risk_rejected",
            detail=risk.detail,
            opportunity_id=opportunity_id,
            risk=risk,
        )

    opp_id = opportunity_id
    if opp_id is None:
        opp_id = store.save(
            opp,
            state=OppState.RISK_OK,
            verified=True,
            ask_depth=ask_depth,
            bid_depth=bid_depth,
            hypothetical_pnl=opp.edge * risk.size_usd,
        )
    else:
        store.transition(opp_id, OppState.RISK_OK, reason="risk_ok")

    if config.exec_mode == ExecMode.DISABLED:
        return TradeResult(
            opportunity=opp,
            status="disabled",
            detail="ARB_EXEC_MODE=disabled",
            opportunity_id=opp_id,
            risk=risk,
        )

    if config.exec_mode == ExecMode.LIVE:
        if not config.live_allowed():
            return TradeResult(
                opportunity=opp,
                status="live_blocked",
                detail=(
                    "Live blocked. Need ARB_ALLOW_LIVE=true, ARB_EXEC_MODE=live, "
                    "ARB_DRY_RUN=false, ARB_STUDY_MODE=false, POLYMARKET_PRIVATE_KEY, "
                    "and ARB_KILL_SWITCH=false."
                ),
                opportunity_id=opp_id,
                risk=risk,
            )
        from arb.clob_live import execute_buy_bundle_live

        store.transition(opp_id, OppState.ORDER_PLACED, reason="live_order")
        live = execute_buy_bundle_live(config, opp, size_usd=risk.size_usd)
        if not live.ok:
            store.transition(
                opp_id,
                OppState.REJECTED,
                reason=f"live_failed:{live.error or 'unknown'}",
            )
            return TradeResult(
                opportunity=opp,
                status="live_failed",
                detail=live.error or "live order failed",
                opportunity_id=opp_id,
                risk=risk,
                live=live,
            )

        # An order acknowledgement is NOT a fill. Only a confirmed complete set
        # (every leg matched, equal share quantities) becomes FILLED. Anything
        # else stays ORDER_PLACED with no fill row and no realized PnL.
        fill_state = _bundle_fill_state(live)
        if fill_state != "filled":
            return TradeResult(
                opportunity=opp,
                status="order_posted" if fill_state == "posted" else "live_partial",
                detail=(
                    f"live orders {fill_state}: {len(live.order_ids)} leg(s) acknowledged "
                    "but not a confirmed complete-set fill — no FILLED state, no PnL"
                ),
                opportunity_id=opp_id,
                risk=risk,
                live=live,
            )

        expected_pnl = round(opp.edge * risk.size_usd, 6)
        # realized_pnl stays None: a fill is not a settlement.
        fill_id = store.record_fill(
            opportunity_id=opp_id,
            mode="live",
            size_usd=live.size_usd or risk.size_usd,
            fill_total=live.fill_total,
            fees_usd=0.0,
            slippage_usd=0.0,
            expected_pnl=expected_pnl,
            fill_prices=live.fill_prices or list(opp.prices),
        )
        oid_note = ",".join(live.order_ids[:4]) if live.order_ids else str(fill_id)
        store.transition(opp_id, OppState.FILLED, reason=f"live_fill:{oid_note}")
        return TradeResult(
            opportunity=opp,
            status="live_filled",
            detail=(
                f"live size=${risk.size_usd:.2f} orders={len(live.order_ids)} "
                f"fill_total={live.fill_total:.4f} expected_pnl=${expected_pnl:.4f} (UNRESOLVED)"
            ),
            opportunity_id=opp_id,
            risk=risk,
            live=live,
        )

    # Paper path — shadow fill via the complete-set plan (L2 plan when available)
    fill = simulate_paper_fill(config, opp, size_usd=risk.size_usd, plan=exec_plan)
    store.transition(opp_id, OppState.ORDER_PLACED, reason="paper_order")
    fill_id = store.record_fill(
        opportunity_id=opp_id,
        mode="paper",
        size_usd=fill.size_usd,
        fill_total=fill.fill_total,
        fees_usd=fill.fees_usd,
        slippage_usd=fill.slippage_usd,
        expected_pnl=fill.expected_pnl,
        fill_prices=fill.fill_prices,
    )
    store.transition(opp_id, OppState.FILLED, reason=f"paper_fill:{fill_id}")

    return TradeResult(
        opportunity=opp,
        status="paper_filled",
        detail=(
            f"paper size=${fill.size_usd:.2f} fill_total={fill.fill_total:.4f} "
            f"expected_pnl=${fill.expected_pnl:.4f}"
            + (" (clob refresh)" if config.paper_realistic else "")
        ),
        opportunity_id=opp_id,
        fill=fill,
        risk=risk,
    )


def execute_batch(
    config: ArbConfig,
    store: OpportunityStore,
    opportunities: list[tuple[Opportunity, int | None]],
) -> list[TradeResult]:
    """Execute a list of (opportunity, optional_row_id) pairs."""
    results: list[TradeResult] = []
    for opp, opp_id in opportunities:
        row = store.get(opp_id) if opp_id is not None else None
        ask_depth = float(row["ask_depth"]) if row and row.get("ask_depth") is not None else None
        bid_depth = float(row["bid_depth"]) if row and row.get("bid_depth") is not None else None
        results.append(
            execute_opportunity(
                config,
                store,
                opp,
                opportunity_id=opp_id,
                ask_depth=ask_depth,
                bid_depth=bid_depth,
            )
        )
    return results
