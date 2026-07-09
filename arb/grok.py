"""Grok (xAI) client for intelligence-plane postmortems — never on the hot path."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arb.config import ArbConfig
from arb.proposals import ProposalStore, new_proposal

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-3-mini"


def _resolve_api_key() -> str:
    key = os.environ.get("XAI_API_KEY", "").strip()
    if key:
        return key
    try:
        from tools.xai_http import get_env_value

        val = get_env_value("XAI_API_KEY")
        if val:
            return str(val).strip()
    except Exception:
        pass
    # Fallback: ~/.hermes/.env
    env_path = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("XAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


@dataclass
class GrokResult:
    ok: bool
    analysis_md: str = ""
    proposals: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "analysis_md": self.analysis_md,
            "proposals": self.proposals,
            "model": self.model,
            "error": self.error,
            "usage": self.usage,
        }


def _budget_path(config: ArbConfig) -> Path:
    return config.state_root / "grok_budget.json"


def _load_budget(config: ArbConfig) -> dict[str, Any]:
    path = _budget_path(config)
    if not path.exists():
        return {"day": "", "tokens": 0, "calls": 0}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"day": "", "tokens": 0, "calls": 0}


def _save_budget(config: ArbConfig, data: dict[str, Any]) -> None:
    path = _budget_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _check_and_bump_budget(config: ArbConfig, used_tokens: int) -> tuple[bool, str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _load_budget(config)
    if data.get("day") != today:
        data = {"day": today, "tokens": 0, "calls": 0}
    limit = int(os.environ.get("ARB_LLM_DAILY_TOKEN_BUDGET", "100000"))
    if int(data.get("tokens", 0)) + used_tokens > limit:
        return False, f"daily Grok token budget exceeded ({data.get('tokens')}/{limit})"
    data["tokens"] = int(data.get("tokens", 0)) + used_tokens
    data["calls"] = int(data.get("calls", 0)) + 1
    _save_budget(config, data)
    return True, "ok"


def chat_completion(
    *,
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    api_key: str | None = None,
) -> dict[str, Any]:
    key = (api_key or _resolve_api_key()).strip()
    if not key:
        raise RuntimeError("XAI_API_KEY not set (env or ~/.hermes/.env)")

    payload = {
        "model": model or os.environ.get("ARB_GROK_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        XAI_CHAT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "polymarket-arb-bot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"xAI HTTP {e.code}: {body}") from e


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Pull a JSON object from model output (fenced or raw)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fence.group(1) if fence else None
    if raw is None:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            raw = text[start : end + 1]
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


ALLOWED_PROPOSAL_KEYS = {
    "ARB_MIN_EDGE_BPS",
    "ARB_WS_WATCH_SEC",
    "ARB_MAX_POSITION_USD",
    "ARB_MAX_OPEN_POSITIONS",
    "ARB_MAX_DAILY_TRADES",
    "ARB_MAX_DAILY_LOSS_USD",
    "ARB_MIN_BOOK_DEPTH",
    "ARB_VERIFY_TOP_N",
    "ARB_PAPER_SLIPPAGE_BPS",
}


def analyze_postmortem(
    config: ArbConfig,
    *,
    report_summary: dict[str, Any],
    labeled_sample: list[dict[str, Any]],
    use_grok: bool = True,
) -> GrokResult:
    """Ask Grok to explain results and suggest human-gated proposals."""
    if not use_grok:
        return GrokResult(ok=False, error="Grok disabled")

    key = _resolve_api_key()
    if not key:
        return GrokResult(ok=False, error="XAI_API_KEY missing")

    # Pre-check budget with estimated max tokens
    ok_budget, msg = _check_and_bump_budget(config, 0)
    if not ok_budget:
        return GrokResult(ok=False, error=msg)

    model = os.environ.get("ARB_GROK_MODEL", DEFAULT_MODEL)
    system = (
        "You are the Verifier+Researcher for a Polymarket Dutch-book arb bot. "
        "Hot-path trading is deterministic; you only analyze offline results. "
        "Be skeptical. Prefer fewer, evidence-backed proposals. "
        "Never suggest enabling live trading or disabling kill switches. "
        "Respond with markdown analysis, then a JSON block:\n"
        '{"proposals":[{"key":"ARB_MIN_EDGE_BPS","proposed_value":75,'
        '"rationale":"...","evidence":{}}]}\n'
        f"Only these keys are allowed: {sorted(ALLOWED_PROPOSAL_KEYS)}."
    )
    user = (
        "Postmortem summary:\n"
        + json.dumps(report_summary, indent=2)
        + "\n\nSample labeled rows (up to 30):\n"
        + json.dumps(labeled_sample[:30], indent=2)
        + "\n\nCurrent config:\n"
        + json.dumps(
            {
                "ARB_MIN_EDGE_BPS": config.min_edge_bps,
                "ARB_WS_WATCH_SEC": config.ws_watch_sec,
                "ARB_MAX_POSITION_USD": config.max_position_usd,
                "ARB_MAX_OPEN_POSITIONS": config.max_open_positions,
                "ARB_MIN_BOOK_DEPTH": config.min_book_depth,
            },
            indent=2,
        )
    )

    try:
        resp = chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=model,
            api_key=key,
        )
    except Exception as exc:
        return GrokResult(ok=False, error=str(exc), model=model)

    content = ""
    try:
        content = resp["choices"][0]["message"]["content"] or ""
    except Exception:
        content = json.dumps(resp)[:2000]

    usage = resp.get("usage") or {}
    total_tokens = int(usage.get("total_tokens") or usage.get("completion_tokens") or 500)
    _check_and_bump_budget(config, total_tokens)

    parsed = _extract_json_block(content) or {"proposals": []}
    proposals = []
    for item in parsed.get("proposals") or []:
        if not isinstance(item, dict):
            continue
        key_name = str(item.get("key") or "")
        if key_name not in ALLOWED_PROPOSAL_KEYS:
            continue
        if "proposed_value" not in item:
            continue
        proposals.append(
            {
                "key": key_name,
                "proposed_value": item["proposed_value"],
                "rationale": str(item.get("rationale") or "Grok proposal"),
                "evidence": item.get("evidence") if isinstance(item.get("evidence"), dict) else {},
            }
        )

    # Strip JSON fence from analysis for readability
    analysis = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", content, flags=re.DOTALL).strip()
    return GrokResult(
        ok=True,
        analysis_md=analysis or content,
        proposals=proposals,
        model=model,
        usage=usage if isinstance(usage, dict) else {},
    )


def apply_grok_proposals(
    config: ArbConfig,
    grok: GrokResult,
) -> list[str]:
    """Add Grok proposals to pending store (human must still approve)."""
    if not grok.ok or not grok.proposals:
        return []
    store = ProposalStore(config.state_root / "proposals.json")
    current = {
        "ARB_MIN_EDGE_BPS": config.min_edge_bps,
        "ARB_WS_WATCH_SEC": config.ws_watch_sec,
        "ARB_MAX_POSITION_USD": config.max_position_usd,
        "ARB_MAX_OPEN_POSITIONS": config.max_open_positions,
        "ARB_MAX_DAILY_TRADES": config.max_daily_trades,
        "ARB_MAX_DAILY_LOSS_USD": config.max_daily_loss_usd,
        "ARB_MIN_BOOK_DEPTH": config.min_book_depth,
        "ARB_VERIFY_TOP_N": config.verify_top_n,
        "ARB_PAPER_SLIPPAGE_BPS": config.paper_slippage_bps,
    }
    ids: list[str] = []
    for item in grok.proposals:
        key = item["key"]
        prop = new_proposal(
            key=key,
            current_value=current.get(key),
            proposed_value=item["proposed_value"],
            rationale=f"[Grok] {item['rationale']}",
            evidence={**(item.get("evidence") or {}), "source": "grok", "model": grok.model},
        )
        saved = store.add(prop)
        ids.append(saved.id)
    return ids


def write_grok_analysis(config: ArbConfig, grok: GrokResult, *, days: int) -> Path:
    path = config.state_root / "postmortems" / f"grok_analysis_{days}d.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Grok Postmortem Analysis\n\n"
        f"Model: `{grok.model}`  \n"
        f"Generated: {datetime.now(timezone.utc).isoformat()}  \n"
        f"OK: {grok.ok}\n\n"
    )
    if grok.error:
        body = f"Error: {grok.error}\n"
    else:
        body = grok.analysis_md + "\n"
        if grok.proposals:
            body += "\n## Proposed changes (pending human approval)\n\n"
            for p in grok.proposals:
                body += f"- `{p['key']}` → `{p['proposed_value']}` — {p['rationale']}\n"
    path.write_text(header + body)
    return path
