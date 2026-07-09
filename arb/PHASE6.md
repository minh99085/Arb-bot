"""Phase 6 — Grok intelligence + live CLOB execution.

## Delivered

| Module | Role |
|--------|------|
| `arb/grok.py` | xAI chat client, token budget, proposal parse |
| `arb/clob_live.py` | `py-clob-client-v2` buy/sell bundle orders |
| `arb/execute.py` | Live path wired (still hard-gated) |
| `arb/postmortem.py` | Optional `--grok` analysis |

## Grok (intelligence plane only)

```bash
# XAI_API_KEY in ~/.hermes/.env (never commit)
python -m arb postmortem --days 7 --grok
```

- Never on the hot path (scan / trade / worker loop).
- Proposals still require `approve` then manual `.env` edit.
- Daily token budget: `ARB_LLM_DAILY_TOKEN_BUDGET` (default 100000).
- Worker opt-in: `ARB_WORKER_GROK=true`.

## Live CLOB

```bash
pip install 'hermes-agent[polymarket-arb]'   # py-clob-client-v2
# All of these required:
export ARB_ALLOW_LIVE=true
export ARB_EXEC_MODE=live
export ARB_DRY_RUN=false
export ARB_STUDY_MODE=false
export ARB_KILL_SWITCH=false
export POLYMARKET_PRIVATE_KEY=0x...
python -m arb trade --limit 1
```

Fail-closed: missing any gate → `live_blocked`. Order failures → `REJECTED`.

## Security

- Do **not** commit API keys. Use `~/.hermes/.env` (chmod 600).
- If a key was ever pushed to git, **rotate it** at https://console.x.ai/
- `.gitignore` blocks `*api*key*.txt` and `xAI*.txt`.

## Still out of scope

- Capital allocator / inventory for sell-bundle
- Auto-apply of Grok proposals
- Evidence gate that scales live $ from study PnL
"""
