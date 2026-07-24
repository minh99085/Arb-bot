# Phase 2 — Executable Complete-Set Plan (Shadow/Paper)

Status: **COMPLETE** (deterministic plan builder). Live CLOB still disabled.

## Goal

Replace the old dollar-per-leg / top-of-book arithmetic with a single,
deterministic, auditable **complete-set execution plan** for research and
shadow/paper. Only one strategy is implemented:

```
BUY_COMPLETE_SET_MERGE
```

Buy one identical share quantity `q` of every outcome (a complete set) at real
L2/L3 depth, then merge/redeem the set for $1 each.

```
gross_cost_usd   = q * sum(all-leg L2/L3 VWAP asks)
net_cash_pnl_usd = q - gross_cost_usd - fees_usd - conversion_costs_usd
```

No LLM on this path. Nothing here places live orders or realizes PnL.

## Delivered

| Module | Role |
|--------|------|
| `arb/plan.py` | Typed models + `build_complete_set_plan` + venue fee interface + shadow fill |
| `arb/scanner.py` | `verify_one` → CLOB_VERIFIED **only** when an executable plan exists |
| `arb/reverify.py` | WS-cache re-verify via the same plan builder |
| `arb/alpha.py` | Pre-deploy spread report; "verified" = executable plan |
| `arb/risk.py` | Sizes from the plan (shares vs dollars kept separate) |
| `arb/paper.py` | Paper fills are **shadow** simulations from the plan |
| `arb/state.py` | Persists the immutable plan record in the opportunity payload |

## Domain models (`arb/plan.py`)

- `BookLevel`, `BookSnapshot` (with source timestamp, tick size, min order size)
- `LegEstimate` (per-leg q, VWAP, gross cost, top-of-book, capacity, levels consumed)
- `FeeQuote` + `VenueFeeModel` interface (`PolymarketFeeModel`, `UnknownFeeModel`)
- `CompleteSetPlan` (immutable record) with the common `q_complete_sets` and the
  strictly-separate dollar fields: `cash_budget_usd`, `gross_notional_usd`,
  `fees_usd`, `slippage_usd`, `conversion_costs_usd`, `net_cash_pnl_usd`
- `PlanRejection` + `PlanRejectReason`
- `ShadowFill` (labeled `shadow`/`simulated`; `realized_pnl_usd` is always `None`)

## Guarantees

- One identical `q` for **every** outcome; capacity is the **weakest** leg
  (never aggregated across legs).
- L2/L3 depth is walked for a real VWAP — a top-of-book candidate can be
  invalidated at depth.
- Dollars and shares are separate fields everywhere.
- Tick size, minimum order size, freshness, outcome count, and rule eligibility
  are validated; fees come from a venue interface and an **unknown fee fails
  closed** (plan rejected).
- Paper/shadow fills are never realized PnL (reconcile leaves them UNRESOLVED,
  per Phase 1).
- Scan reports distinguish a **candidate** (gamma flag) from an **executable
  plan**.

## Env (Phase 2 additions)

| Variable | Default | Meaning |
|----------|---------|---------|
| `ARB_FEE_VENUE` | `polymarket` | Venue fee model; unknown venue → plan fails closed |
| `ARB_MAX_BOOK_AGE_SEC` | `30` | Book freshness window for a valid plan |
| `ARB_CONVERSION_COST_USD` | `0` | Merge/redeem cost per plan |
| `ARB_ASSUMED_TICK_SIZE` | `0.01` | Used when a book omits tick size |
| `ARB_ASSUMED_MIN_ORDER_SIZE` | `5` | Used when a book omits min order size |
| `ARB_PLAN_DEPTH_LEVELS` | `10` | L2/L3 depth to walk (0 = all levels) |

## Explicitly out of scope (this phase)

- Live orders
- Sell-complete-set, naked shorting, negative-risk execution
- Directional prediction
- Kalshi, Robinhood adapters
