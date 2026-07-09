# Phase 3 — Feed Upgrade (WebSocket)

Status: **COMPLETE**

## Goal

Replace poll-only CLOB verification with a live market-channel feed and
sub-second re-verify before trading. Still **deterministic** — no LLM.

## Delivered

| Module | Role |
|--------|------|
| `arb/book_cache.py` | In-memory L2 book + best bid/ask |
| `arb/ws_feed.py` | Market-channel WS client, PING, reconnect, REST seed |
| `arb/reverify.py` | Re-check Dutch-book from cache; mark evaporated REJECTED |
| CLI `watch` | Stream books / re-verify on updates |
| CLI `loop --ws` | Scan → WS reverify → paper trade → reconcile |

## Endpoint

```
wss://ws-subscriptions-clob.polymarket.com/ws/market
```

Subscribe:

```json
{"assets_ids": ["..."], "type": "market", "custom_feature_enabled": true}
```

Handles: `book`, `price_change`, `best_bid_ask`. Client sends `PING` every 10s.

## Commands

```bash
# Watch tokens from a quick scan
python -m arb watch --scan-first --limit 10 --seconds 20

# Watch stored CLOB_VERIFIED and persist evaporations
python -m arb watch --from-store --seconds 30 --persist-rejects

# Full loop with WS gate before paper trade
python -m arb loop --paper --ws --limit 50 --ws-sec 15 --trade-limit 5
```

## Env

| Variable | Default | Meaning |
|----------|---------|---------|
| `ARB_WS_ENABLED` | `true` | Allow WS path |
| `ARB_WS_URL` | Polymarket market channel | Override endpoint |
| `ARB_WS_WATCH_SEC` | `30` | Default watch duration |
| `ARB_WS_MAX_ASSETS` | `40` | Cap subscriptions per connection |
| `ARB_WS_SEED_REST` | `true` | REST snapshot before WS deltas |

## Loop (updated)

```
scan → CLOB verify → WS re-verify → risk → paper execute → reconcile
```

## Out of scope (later)

- Multi-connection fanout for >40 assets
- User-channel fill stream (needs auth)
- Phase 4 postmortem / Phase 5 cloud worker
