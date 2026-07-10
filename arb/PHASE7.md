# Phase 7 — Alpha dashboard + VPS-tuned scanner

## What changed

| Area | Fix |
|------|-----|
| Gamma pagination | Stop at offset 2000 (422 crash fixed); volume-ordered `events` source |
| `python -m arb alpha` | Direct CLOB scan on top liquid markets + near-miss report |
| Defaults | 30bps edge, 10bps fees, verify_top_n=40 |
| Trade selection | `top_by_edge` not recency |
| Alerts | Include hypothetical $ PnL at position size |

## Pre-deploy (run on VPS or locally)

```bash
python -m arb alpha --liquid 400
python -m arb scan --study --top 15
python -m arb study --days 7
```

## VPS profile

```bash
cp deploy/vps.env.example ~/.hermes/profiles/polymarket-arb/worker.env
# systemd: EnvironmentFile= that path + ~/.hermes/.env
python -m arb worker run --paper --ws
```

## Interpreting output

- **No verified alpha** on liquid markets is normal — Polymarket is efficient (~-10bps spread).
- **Near-misses** show markets closest to threshold (watch these).
- **Scanner healthy** = `clob_checked` ≥ 50, low `books_missing`.
- Deploy worker anyway to catch **fleeting** arbs between scans.
