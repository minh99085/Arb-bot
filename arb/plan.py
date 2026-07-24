"""Deterministic complete-set execution planning — Phase 2.

The ONLY supported strategy here is ``BUY_COMPLETE_SET_MERGE``: buy one identical
share quantity ``q`` of every outcome of a market (a complete set) at L2/L3 depth,
then merge/redeem the set for $1 each. This module replaces the old
dollar-per-leg and top-of-book arithmetic with a single, auditable plan builder.

Hard rules enforced here:
  * One identical share quantity ``q`` for EVERY outcome (a real complete set).
  * Capacity is limited by the WEAKEST executable leg — depth is never aggregated
    across legs.
  * Dollars and shares are kept strictly separate fields.
  * L2/L3 depth is walked for a real VWAP — not just top of book.
  * Tick size, minimum order size, freshness, outcome count, and rule
    eligibility are validated.
  * Fees come from a venue fee interface; an unavailable fee treatment fails
    closed (the plan is rejected).
  * Simulated fills from captured snapshots are labeled ``shadow``/``simulated``
    and can never be realized PnL.

Not implemented (later phases / out of scope): live orders, sell-complete-set,
naked shorting, negative-risk execution, directional prediction, Kalshi,
Robinhood.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

STRATEGY_BUY_COMPLETE_SET_MERGE = "BUY_COMPLETE_SET_MERGE"

# A complete set of N outcomes redeems for exactly $1 (one dollar per set).
SET_REDEMPTION_USD = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """Best-effort parse of an ISO8601 (or epoch seconds/ms) timestamp to UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Numeric string epoch?
        try:
            ts = float(s)
        except ValueError:
            return None
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Domain models ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BookLevel:
    """A single price level (shares available at a price)."""

    price: float
    size: float


@dataclass(frozen=True)
class BookSnapshot:
    """An order-book snapshot for one outcome token, with its source timestamp.

    ``asks`` are sorted best (lowest) price first; ``bids`` best (highest) first.
    """

    token_id: str
    asks: tuple[BookLevel, ...]
    bids: tuple[BookLevel, ...]
    captured_at: str
    source: str
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def ask_capacity(self, *, depth_levels: int | None = None) -> float:
        levels = self.asks if not depth_levels or depth_levels <= 0 else self.asks[:depth_levels]
        return round(sum(lvl.size for lvl in levels), 8)

    @classmethod
    def from_book(
        cls,
        token_id: str,
        book: dict,
        *,
        source: str,
        captured_at: str | None = None,
        default_tick_size: float | None = None,
        default_min_order_size: float | None = None,
    ) -> "BookSnapshot":
        """Build a snapshot from a REST/cache book dict ({bids,asks,...})."""

        def _levels(raw: Any) -> list[BookLevel]:
            out: list[BookLevel] = []
            for lvl in raw or []:
                try:
                    price = float(lvl.get("price"))
                    size = float(lvl.get("size", 0))
                except (TypeError, ValueError, AttributeError):
                    continue
                if size > 0 and price > 0:
                    out.append(BookLevel(price=price, size=size))
            return out

        asks = sorted(_levels(book.get("asks")), key=lambda x: x.price)
        bids = sorted(_levels(book.get("bids")), key=lambda x: x.price, reverse=True)

        tick = book.get("tick_size", book.get("tickSize"))
        tick_size = float(tick) if tick not in (None, "") else default_tick_size
        mos = book.get("min_order_size", book.get("minOrderSize"))
        min_order_size = float(mos) if mos not in (None, "") else default_min_order_size
        nr = book.get("neg_risk", book.get("negRisk"))
        neg_risk = bool(nr) if nr is not None else None

        cap = captured_at or book.get("updated_at") or book.get("timestamp") or _now_iso()
        return cls(
            token_id=token_id,
            asks=tuple(asks),
            bids=tuple(bids),
            captured_at=str(cap),
            source=source,
            tick_size=tick_size,
            min_order_size=min_order_size,
            neg_risk=neg_risk,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "asks": [{"price": lvl.price, "size": lvl.size} for lvl in self.asks],
            "bids": [{"price": lvl.price, "size": lvl.size} for lvl in self.bids],
            "captured_at": self.captured_at,
            "source": self.source,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "neg_risk": self.neg_risk,
        }


@dataclass(frozen=True)
class LegEstimate:
    """Per-leg execution estimate for buying ``q_shares`` of one outcome."""

    token_id: str
    outcome: str
    q_shares: float
    vwap_ask: float
    gross_cost_usd: float
    top_of_book_ask: float
    capacity_shares: float
    levels_consumed: tuple[BookLevel, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "outcome": self.outcome,
            "q_shares": self.q_shares,
            "vwap_ask": self.vwap_ask,
            "gross_cost_usd": self.gross_cost_usd,
            "top_of_book_ask": self.top_of_book_ask,
            "capacity_shares": self.capacity_shares,
            "levels_consumed": [
                {"price": lvl.price, "size": lvl.size} for lvl in self.levels_consumed
            ],
        }


@dataclass(frozen=True)
class FeeQuote:
    """A venue fee quote. ``known=False`` means the plan must fail closed."""

    venue: str
    fee_usd: float
    known: bool
    detail: str = ""
    fee_bps: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "fee_usd": self.fee_usd,
            "known": self.known,
            "detail": self.detail,
            "fee_bps": self.fee_bps,
        }


class PlanRejectReason(str, Enum):
    """Explicit reason a complete-set plan is not executable."""

    INELIGIBLE_STRATEGY = "ineligible_strategy"
    MISSING_OUTCOME = "missing_outcome"
    NO_BOOK = "no_book"
    STALE_BOOK = "stale_book"
    INVALID_TICK = "invalid_tick"
    BELOW_MIN_SIZE = "below_min_size"
    INVALID_PRICES = "invalid_prices"
    NEG_RISK_INELIGIBLE = "neg_risk_ineligible"
    UNKNOWN_FEE = "unknown_fee"
    ZERO_BUDGET = "zero_budget"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    NEGATIVE_NET_PNL = "negative_net_pnl"
    OTHER = "other"


@dataclass(frozen=True)
class PlanRejection:
    reason: PlanRejectReason
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason.value, "detail": self.detail}


@dataclass(frozen=True)
class CompleteSetPlan:
    """Immutable, auditable complete-set execution plan record.

    Contains the source snapshots, the price levels consumed, timestamps, the
    fee inputs, the single common quantity ``q_complete_sets``, and — when not
    executable — the explicit rejection reason. Dollars and shares are separate.
    """

    strategy: str
    condition_id: str
    slug: str
    question: str
    outcomes: tuple[str, ...]
    legs: tuple[LegEstimate, ...]
    q_complete_sets: float           # shares (one identical q for every outcome)
    cash_budget_usd: float           # dollars
    gross_notional_usd: float        # dollars
    fees_usd: float                  # dollars
    slippage_usd: float              # dollars (VWAP vs top-of-book)
    conversion_costs_usd: float      # dollars (merge/redeem)
    net_cash_pnl_usd: float          # dollars (shadow — never realized)
    net_edge_per_set: float          # dollars per set
    fee_quote: FeeQuote | None
    snapshots: tuple[BookSnapshot, ...]
    created_at: str
    rejection: PlanRejection | None = None

    @property
    def executable(self) -> bool:
        return (
            self.rejection is None
            and self.q_complete_sets > 0
            and self.net_cash_pnl_usd > 0
        )

    @property
    def redemption_value_usd(self) -> float:
        """Dollars the complete sets redeem for ($1 per set) — shares → dollars."""
        return round(self.q_complete_sets * SET_REDEMPTION_USD, 8)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "condition_id": self.condition_id,
            "slug": self.slug,
            "question": self.question,
            "outcomes": list(self.outcomes),
            "legs": [leg.to_dict() for leg in self.legs],
            "q_complete_sets": self.q_complete_sets,
            "cash_budget_usd": self.cash_budget_usd,
            "gross_notional_usd": self.gross_notional_usd,
            "fees_usd": self.fees_usd,
            "slippage_usd": self.slippage_usd,
            "conversion_costs_usd": self.conversion_costs_usd,
            "net_cash_pnl_usd": self.net_cash_pnl_usd,
            "net_edge_per_set": self.net_edge_per_set,
            "redemption_value_usd": self.redemption_value_usd,
            "fee_quote": self.fee_quote.to_dict() if self.fee_quote else None,
            "snapshots": [s.to_dict() for s in self.snapshots],
            "created_at": self.created_at,
            "executable": self.executable,
            "rejection": self.rejection.to_dict() if self.rejection else None,
        }


@dataclass(frozen=True)
class ShadowFill:
    """A simulated fill from captured snapshots — labeled shadow, never realized."""

    condition_id: str
    label: str                       # "shadow" | "simulated"
    simulated_at: str
    latency_sec: float
    q_complete_sets: float
    gross_notional_usd: float
    fees_usd: float
    slippage_usd: float
    conversion_costs_usd: float
    net_cash_pnl_usd: float          # shadow PnL — NOT realized
    realized: bool = False

    @property
    def realized_pnl_usd(self) -> None:
        """Shadow fills never have realized PnL."""
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "label": self.label,
            "simulated_at": self.simulated_at,
            "latency_sec": self.latency_sec,
            "q_complete_sets": self.q_complete_sets,
            "gross_notional_usd": self.gross_notional_usd,
            "fees_usd": self.fees_usd,
            "slippage_usd": self.slippage_usd,
            "conversion_costs_usd": self.conversion_costs_usd,
            "net_cash_pnl_usd": self.net_cash_pnl_usd,
            "realized": self.realized,
            "realized_pnl_usd": None,
        }


# ── Venue fee interface ──────────────────────────────────────────────────────


class VenueFeeModel(ABC):
    """A venue's fee treatment. Implementations must return a FeeQuote.

    If a venue's exact fee treatment is unavailable, return ``known=False`` so
    the plan builder fails closed instead of guessing.
    """

    venue: str = "abstract"

    @abstractmethod
    def quote(
        self,
        *,
        legs: tuple[LegEstimate, ...],
        q: float,
        gross_notional_usd: float,
    ) -> FeeQuote: ...


class PolymarketFeeModel(VenueFeeModel):
    """Polymarket CLOB fee treatment, driven by an explicit taker-fee input.

    The fee is a known function of gross notional. (Polymarket's on-chain
    trading fee is currently 0; the configured bps lets research model a
    conservative taker cost.)
    """

    venue = "polymarket"

    def __init__(self, taker_fee_bps: float) -> None:
        self.taker_fee_bps = float(taker_fee_bps)

    def quote(
        self,
        *,
        legs: tuple[LegEstimate, ...],
        q: float,
        gross_notional_usd: float,
    ) -> FeeQuote:
        fee = gross_notional_usd * (self.taker_fee_bps / 10_000.0)
        return FeeQuote(
            venue=self.venue,
            fee_usd=round(fee, 8),
            known=True,
            detail=f"taker {self.taker_fee_bps:.1f}bps on gross notional",
            fee_bps=self.taker_fee_bps,
        )


class UnknownFeeModel(VenueFeeModel):
    """Sentinel model for venues whose fee treatment is unavailable — fail closed."""

    def __init__(self, venue: str = "unknown") -> None:
        self.venue = venue

    def quote(self, **_kw: Any) -> FeeQuote:
        return FeeQuote(
            venue=self.venue,
            fee_usd=0.0,
            known=False,
            detail="venue fee treatment unavailable — cannot price fees",
        )


# ── Plan builder ─────────────────────────────────────────────────────────────


def _walk_asks(
    levels: tuple[BookLevel, ...], q: float, *, depth_levels: int | None
) -> tuple[float, float, tuple[BookLevel, ...]]:
    """VWAP-walk ``q`` shares up the ask book. Returns (filled, cost, consumed)."""
    usable = levels if not depth_levels or depth_levels <= 0 else levels[:depth_levels]
    remaining = q
    cost = 0.0
    consumed: list[BookLevel] = []
    for lvl in usable:
        if remaining <= 1e-12:
            break
        take = min(remaining, lvl.size)
        cost += take * lvl.price
        consumed.append(BookLevel(price=lvl.price, size=take))
        remaining -= take
    filled = q - max(0.0, remaining)
    return filled, cost, tuple(consumed)


def _gross_cost(snapshots: list[BookSnapshot], q: float, *, depth_levels: int | None) -> float:
    total = 0.0
    for snap in snapshots:
        _, cost, _ = _walk_asks(snap.asks, q, depth_levels=depth_levels)
        total += cost
    return total


def _max_q_within_budget(
    snapshots: list[BookSnapshot],
    budget: float,
    q_cap: float,
    *,
    depth_levels: int | None,
) -> float:
    """Largest q in [0, q_cap] whose gross cost is within budget (binary search)."""
    if q_cap <= 0:
        return 0.0
    if _gross_cost(snapshots, q_cap, depth_levels=depth_levels) <= budget:
        return q_cap
    lo, hi = 0.0, q_cap
    for _ in range(64):
        mid = (lo + hi) / 2.0
        if _gross_cost(snapshots, mid, depth_levels=depth_levels) <= budget:
            lo = mid
        else:
            hi = mid
    return lo


def _reject(
    reason: PlanRejectReason,
    detail: str,
    *,
    strategy: str,
    condition_id: str,
    slug: str,
    question: str,
    outcomes: tuple[str, ...],
    snapshots: tuple[BookSnapshot, ...],
    cash_budget_usd: float,
    created_at: str,
    fee_quote: FeeQuote | None = None,
) -> CompleteSetPlan:
    return CompleteSetPlan(
        strategy=strategy,
        condition_id=condition_id,
        slug=slug,
        question=question,
        outcomes=outcomes,
        legs=(),
        q_complete_sets=0.0,
        cash_budget_usd=cash_budget_usd,
        gross_notional_usd=0.0,
        fees_usd=0.0,
        slippage_usd=0.0,
        conversion_costs_usd=0.0,
        net_cash_pnl_usd=0.0,
        net_edge_per_set=0.0,
        fee_quote=fee_quote,
        snapshots=snapshots,
        created_at=created_at,
        rejection=PlanRejection(reason=reason, detail=detail),
    )


def build_complete_set_plan(
    *,
    condition_id: str,
    slug: str,
    question: str,
    outcomes: list[str],
    snapshots: list[BookSnapshot],
    fee_model: VenueFeeModel,
    cash_budget_usd: float,
    strategy: str = STRATEGY_BUY_COMPLETE_SET_MERGE,
    now_iso: str | None = None,
    max_book_age_sec: float = 30.0,
    conversion_cost_usd: float = 0.0,
    min_net_pnl_usd: float = 0.0,
    require_tick: bool = True,
    depth_levels: int | None = None,
    target_q: float | None = None,
) -> CompleteSetPlan:
    """Build a deterministic BUY_COMPLETE_SET_MERGE plan (or a rejection record).

    Buys one identical share quantity ``q`` of every outcome from L2/L3 depth::

        gross_cost_usd    = q * sum(all-leg VWAP asks)
        net_cash_pnl_usd  = q - gross_cost_usd - fees_usd - conversion_costs_usd

    ``q`` is bounded by the weakest leg's depth and by ``cash_budget_usd`` (and
    ``target_q`` if given). Fees come from ``fee_model`` and an unknown fee fails
    closed. The return is ALWAYS an immutable CompleteSetPlan; on failure it
    carries ``rejection`` and is not ``executable``.
    """
    created_at = now_iso or _now_iso()
    outcomes_t = tuple(outcomes)
    snaps_t = tuple(snapshots)

    def rej(reason: PlanRejectReason, detail: str, fee_quote: FeeQuote | None = None) -> CompleteSetPlan:
        return _reject(
            reason,
            detail,
            strategy=strategy,
            condition_id=condition_id,
            slug=slug,
            question=question,
            outcomes=outcomes_t,
            snapshots=snaps_t,
            cash_budget_usd=cash_budget_usd,
            created_at=created_at,
            fee_quote=fee_quote,
        )

    # 1. Strategy eligibility — only BUY_COMPLETE_SET_MERGE is implemented.
    if strategy != STRATEGY_BUY_COMPLETE_SET_MERGE:
        return rej(PlanRejectReason.INELIGIBLE_STRATEGY, f"unsupported strategy {strategy!r}")

    # 2. Outcome count + one snapshot per outcome.
    if len(outcomes_t) < 2:
        return rej(PlanRejectReason.MISSING_OUTCOME, "a complete set needs >= 2 outcomes")
    if len(snaps_t) != len(outcomes_t):
        return rej(
            PlanRejectReason.MISSING_OUTCOME,
            f"{len(snaps_t)} snapshots for {len(outcomes_t)} outcomes",
        )

    ref_now = _parse_ts(created_at)

    # 3. Per-leg validation: book present, fresh, tick/min known, eligible, priced.
    for snap in snaps_t:
        if not snap.asks:
            return rej(PlanRejectReason.NO_BOOK, f"no ask book for {snap.token_id[:16]}")
        cap_ts = _parse_ts(snap.captured_at)
        if ref_now is None or cap_ts is None:
            return rej(PlanRejectReason.STALE_BOOK, "unparseable book timestamp")
        age = (ref_now - cap_ts).total_seconds()
        if age > max_book_age_sec:
            return rej(
                PlanRejectReason.STALE_BOOK,
                f"book age {age:.1f}s > max {max_book_age_sec:.1f}s ({snap.token_id[:16]})",
            )
        if require_tick and (snap.tick_size is None or snap.tick_size <= 0):
            return rej(PlanRejectReason.INVALID_TICK, f"tick size unknown for {snap.token_id[:16]}")
        if require_tick and (snap.min_order_size is None or snap.min_order_size < 0):
            return rej(PlanRejectReason.BELOW_MIN_SIZE, f"min order size unknown for {snap.token_id[:16]}")
        if snap.neg_risk is True:
            return rej(
                PlanRejectReason.NEG_RISK_INELIGIBLE,
                "negative-risk market — execution not supported in this phase",
            )
        for lvl in snap.asks:
            if not (0.0 < lvl.price < 1.0):
                return rej(PlanRejectReason.INVALID_PRICES, f"ask price {lvl.price} out of (0,1)")

    # 4. Fee availability — fail closed if the venue fee treatment is unknown.
    fee_probe = fee_model.quote(legs=(), q=0.0, gross_notional_usd=0.0)
    if not fee_probe.known:
        return rej(PlanRejectReason.UNKNOWN_FEE, fee_probe.detail or "fee treatment unavailable", fee_probe)

    # 5. Budget must be positive.
    if cash_budget_usd <= 0:
        return rej(PlanRejectReason.ZERO_BUDGET, "cash budget must be > 0")

    # 6. Capacity from the WEAKEST leg (never aggregate depth across legs).
    leg_capacities = [snap.ask_capacity(depth_levels=depth_levels) for snap in snaps_t]
    q_capacity = min(leg_capacities)
    if q_capacity <= 0:
        return rej(PlanRejectReason.INSUFFICIENT_DEPTH, "a leg has zero ask depth")

    # 7. Common quantity q: bounded by depth, budget, and optional target.
    q_budget = _max_q_within_budget(list(snaps_t), cash_budget_usd, q_capacity, depth_levels=depth_levels)
    q = min(q_capacity, q_budget)
    if target_q is not None:
        if target_q > q_capacity + 1e-9:
            return rej(
                PlanRejectReason.INSUFFICIENT_DEPTH,
                f"target q {target_q} exceeds weakest-leg capacity {q_capacity}",
            )
        q = min(q, target_q)
    q = round(q, 8)
    if q <= 0:
        return rej(PlanRejectReason.INSUFFICIENT_DEPTH, "sized to zero shares (budget/depth)")

    # 8. Minimum order size — the SAME q must clear every leg's minimum.
    for snap in snaps_t:
        mos = snap.min_order_size or 0.0
        if q < mos:
            return rej(
                PlanRejectReason.BELOW_MIN_SIZE,
                f"q {q} < min order size {mos} for {snap.token_id[:16]}",
            )

    # 9. Build per-leg estimates at the common q.
    legs: list[LegEstimate] = []
    gross_notional = 0.0
    top_of_book_total = 0.0
    for outcome, snap in zip(outcomes_t, snaps_t):
        filled, cost, consumed = _walk_asks(snap.asks, q, depth_levels=depth_levels)
        vwap = cost / filled if filled > 0 else 0.0
        top = snap.best_ask or 0.0
        gross_notional += cost
        top_of_book_total += top
        legs.append(
            LegEstimate(
                token_id=snap.token_id,
                outcome=outcome,
                q_shares=q,
                vwap_ask=round(vwap, 8),
                gross_cost_usd=round(cost, 8),
                top_of_book_ask=top,
                capacity_shares=snap.ask_capacity(depth_levels=depth_levels),
                levels_consumed=consumed,
            )
        )

    gross_notional = round(gross_notional, 8)
    fee_quote = fee_model.quote(legs=tuple(legs), q=q, gross_notional_usd=gross_notional)
    if not fee_quote.known:
        return rej(PlanRejectReason.UNKNOWN_FEE, fee_quote.detail or "fee treatment unavailable", fee_quote)
    fees_usd = round(fee_quote.fee_usd, 8)
    conversion = round(conversion_cost_usd, 8)
    slippage_usd = round(gross_notional - q * top_of_book_total, 8)
    redemption = q * SET_REDEMPTION_USD
    net_cash_pnl = round(redemption - gross_notional - fees_usd - conversion, 8)
    net_edge_per_set = round(net_cash_pnl / q, 8) if q > 0 else 0.0

    plan = CompleteSetPlan(
        strategy=strategy,
        condition_id=condition_id,
        slug=slug,
        question=question,
        outcomes=outcomes_t,
        legs=tuple(legs),
        q_complete_sets=q,
        cash_budget_usd=round(cash_budget_usd, 8),
        gross_notional_usd=gross_notional,
        fees_usd=fees_usd,
        slippage_usd=slippage_usd,
        conversion_costs_usd=conversion,
        net_cash_pnl_usd=net_cash_pnl,
        net_edge_per_set=net_edge_per_set,
        fee_quote=fee_quote,
        snapshots=snaps_t,
        created_at=created_at,
    )

    # 10. Reject non-profitable plans (after real fees/slippage/conversion).
    if net_cash_pnl <= min_net_pnl_usd:
        from dataclasses import replace as _replace

        return _replace(
            plan,
            rejection=PlanRejection(
                reason=PlanRejectReason.NEGATIVE_NET_PNL,
                detail=f"net ${net_cash_pnl:.6f} <= floor ${min_net_pnl_usd:.6f}",
            ),
        )

    return plan


def simulate_shadow_fill(
    plan: CompleteSetPlan,
    *,
    latency_sec: float = 0.0,
    label: str = "shadow",
    simulated_at: str | None = None,
) -> ShadowFill:
    """Simulate filling ``plan`` from its captured snapshots (never realized).

    The fill reproduces the plan's computed quantities from the captured book;
    ``latency_sec`` is recorded metadata (real settlement is out of scope). The
    result is labeled ``shadow``/``simulated`` and exposes no realized PnL.
    """
    if label not in {"shadow", "simulated"}:
        raise ValueError("shadow fills must be labeled 'shadow' or 'simulated'")
    return ShadowFill(
        condition_id=plan.condition_id,
        label=label,
        simulated_at=simulated_at or _now_iso(),
        latency_sec=float(latency_sec),
        q_complete_sets=plan.q_complete_sets,
        gross_notional_usd=plan.gross_notional_usd,
        fees_usd=plan.fees_usd,
        slippage_usd=plan.slippage_usd,
        conversion_costs_usd=plan.conversion_costs_usd,
        net_cash_pnl_usd=plan.net_cash_pnl_usd,
        realized=False,
    )
