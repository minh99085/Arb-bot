# Dutch-Book Arbitrage on Polymarket

## Binary market identity

A Polymarket binary market has outcomes `Yes` and `No` with prices in \([0,1]\).
If the market is efficient, `price(Yes) + price(No) ≈ 1` because exactly one
outcome pays \$1 and the other pays \$0 at resolution.

## Violations

| Signal | Condition | Action |
|--------|-----------|--------|
| Buy bundle | `ask_yes + ask_no < 1 - fees - edge` | Buy both legs, hold to resolution |
| Sell bundle | `bid_yes + bid_no > 1 + fees + edge` | Mint/split and sell both legs |

`edge` is your minimum required profit margin. `fees` includes taker fees on each leg.

## Scanner pipeline

1. **Gamma pass** — fast universe scan via `outcomePrices`
2. **CLOB pass** — top-N candidates verified with `/book` best bid/ask
3. **Persist** — SQLite log at `ARB_STATE_DIR/opportunities.sqlite`
4. **Trade** — `arb trade` (dry-run until CLOB client is connected)

## Risk controls

- Require CLOB verification before trading
- Cap size by top-of-book depth (not yet enforced in v0.1)
- Skip closed or illiquid books (missing bid/ask)
- Log every detection for post-trade verifier audit

## Related Hermes surfaces

- Market data: `skills/research/polymarket/`
- Scheduling: `cron` with `no_agent=True`
- Alerts: cron delivery to Telegram/Discord via gateway
