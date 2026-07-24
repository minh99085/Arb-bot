---
name: polymarket-arb
description: "Scan Polymarket for Dutch-book arbitrage; verify with CLOB books."
version: 0.1.0
author: Arb-bot / Hermes Agent
tags: [polymarket, arbitrage, dutch-book, trading, prediction-markets]
platforms: [linux, macos, windows]
metadata:
  hermes:
    related_skills: [polymarket]
---

# Polymarket Dutch-Book Arbitrage

Detect **Dutch-book violations** on Polymarket binary markets: when the sum of
outcome prices deviates from \$1 beyond fees, a risk-bounded bundle trade may exist.

This skill **extends** the bundled read-only `polymarket` research skill. It does
not duplicate market data HTTP — it imports `skills/research/polymarket/scripts/polymarket.py`.

## When to Use

- User wants to scan **all** Polymarket markets for arbitrage
- User asks about Dutch-book, no-arbitrage, or bundle mispricing
- User wants scheduled watchdog scans without LLM cost per tick
- User wants to verify gamma prices against live CLOB orderbooks

## Prerequisites

- Bundled `polymarket` research skill (market data)
- Stdlib only for scanning
- Optional live trading: `POLYMARKET_PRIVATE_KEY` + `py-clob-client-v2` (`pip install 'hermes-agent[polymarket-arb]'`)
- Optional Grok postmortems: `XAI_API_KEY` in `~/.hermes/.env`

Config (optional, in `.env`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `ARB_SAFETY_MODE` | `scan_only` | `scan_only` \| `shadow` \| `paper_execution` \| `live` |
| `ARB_PAPER_EXECUTION_ENABLED` | `false` | Separate gate; must be `true` for simulated fills |
| `ARB_STUDY_MODE` | `true` | Extra guard that blocks any execution |
| `ARB_SELF_TUNE` | `false` | Autonomous tuning off; stale overrides ignored |
| `ARB_MIN_EDGE_BPS` | `25` | Minimum edge after fees (basis points) |
| `ARB_TAKER_FEE_BPS` | `10` | Assumed taker fee per leg |
| `ARB_VERIFY_TOP_N` | `50` | CLOB book checks for top gamma hits |
| `ARB_DRY_RUN` | `true` | Skip live order placement |
| `ARB_STATE_DIR` | `~/.hermes/profiles/polymarket-arb/state` | SQLite opportunity log |

## Safety Modes (scanner/shadow-first)

Defaults are **safe**: the bot scans, verifies, and logs — nothing else.

- `SCAN_ONLY` (default) — scan / verify / log. No orders, no fills.
- `SHADOW` — also record observations, still no orders/fills.
- `PAPER_EXECUTION` — simulated fills, **only** with `ARB_PAPER_EXECUTION_ENABLED=true`
  and `ARB_STUDY_MODE=false`. `--paper` / `paper=true` alone never enables execution.
- `LIVE` — real orders, behind every live gate (`ARB_ALLOW_LIVE`, `ARB_DRY_RUN=false`,
  kill switch off, private key, study off).

Truthfulness rules: expected PnL is **never** auto-realized (fills stay
`UNRESOLVED`); a candidate / CLOB-verified / order-posted record is not a win;
`SELL_BUNDLE` is a research signal whose execution is `UNSUPPORTED_STRATEGY` for
now; a live order acknowledgement does not become `FILLED`.

## Phase 1 — Study Mode

```bash
python -m arb scan --study
python -m arb alpha --liquid 400    # pre-deploy: direct CLOB alpha dashboard
python -m arb status
python -m arb study --days 30
```

## Phase 2 — Execution Plane (paper money loop, opt-in)

Paper execution is **off by default**. Enable it explicitly first:

```bash
export ARB_SAFETY_MODE=paper_execution
export ARB_PAPER_EXECUTION_ENABLED=true
export ARB_STUDY_MODE=false
python -m arb loop --limit 50 --trade-limit 5
python -m arb trade --limit 5
python -m arb reconcile     # reports UNRESOLVED fills; never auto-realizes PnL
python -m arb status
```

## Phase 3 — WebSocket feed

```bash
python -m arb watch --scan-first --limit 10 --seconds 20
python -m arb loop --paper --ws --limit 50 --ws-sec 15
```

## Phase 4 — Intelligence plane (human-gated)

```bash
python -m arb postmortem --days 7
python -m arb proposals
python -m arb approve <proposal_id>
python -m arb proposals --env-snippet   # copy into .env yourself
```

## Phase 5 — 24/7 standalone worker

```bash
python -m arb worker once --jobs scan,reconcile
python -m arb worker run                 # daemon
python -m arb worker status
```

Deploy templates: `deploy/systemd/`, `deploy/cron/`, `deploy/github-actions/`.
Hermes is optional for alert delivery only — not on the hot path.

Hot path is deterministic: scan → verify → WS re-verify → risk → paper fill → reconcile.
Learning path proposes only; humans approve. Live CLOB still gated.

## Commands

```bash
python -m arb scan
python -m arb scan --gamma-only --limit 200 --json
python -m arb status --state CLOB_VERIFIED
python -m arb study --days 30
python -m arb loop --limit 50           # scan/shadow-only unless execution is enabled
python -m arb trade                      # needs ARB_SAFETY_MODE=paper_execution + gate
python -m arb reconcile
python -m arb watch --scan-first --seconds 20
python -m arb postmortem --days 7
python -m arb proposals
python -m arb worker once --jobs scan
python -m arb worker status
```

Cron script-only (no LLM):

```bash
python3 optional-skills/trading/polymarket-arb/scripts/scan_dutch_book.py
```

Schedule via Hermes:

```text
cronjob(action="create", schedule="every 5m", script="scan_dutch_book.py",
        no_agent=True, deliver="telegram")
```

## Dutch-Book Logic

For a complete set of mutually exclusive outcomes that pay \$1 total:

- **Buy bundle**: \(\sum \text{ask}_i < 1 - \text{fees} - \text{min\_edge}\)
- **Sell bundle**: \(\sum \text{bid}_i > 1 + \text{fees} + \text{min\_edge}\)

The scanner:

1. Paginates **all active** Gamma markets
2. Flags gamma `outcomePrices` violations
3. Re-checks top hits against CLOB best bid/ask per token
4. Persists hits to SQLite for verifier audit

See `references/dutch-book.md` for formulas and risk notes.

## Limitations

- Execution is off by default (scanner/shadow-first); enable it explicitly.
- `SELL_BUNDLE` execution is unsupported (research signal only) until a later
  phase implements collateral split / inventory / common quantity / reconciliation.
- Expected PnL is never converted to realized PnL; paper/live fills stay UNRESOLVED.
- Gamma prices are indicative; only CLOB-verified hits are trade-ready
- Live execution uses `py-clob-client-v2` behind hard gates (`ARB_ALLOW_LIVE`, etc.) — see `arb/PHASE6.md`
- Grok postmortems: `python -m arb postmortem --grok` (human-gated proposals only)
- **Windows Docker paper trading:** `deploy/WINDOWS-DOCKER.md`
- Multi-outcome (>2) markets are scanned but most arb is on binary Yes/No
- Geographic trading restrictions still apply for live orders
