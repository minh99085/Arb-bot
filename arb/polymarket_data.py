"""Polymarket data access — extends the bundled research skill, no duplicate HTTP."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

_MODULE: Any | None = None

# Gamma caps offset around 2000 (limit=100). Paginate via volume-ordered events.
GAMMA_MAX_OFFSET = 2000
DEFAULT_USER_AGENT = "polymarket-arb-bot/1.0"


class PolymarketAPIError(Exception):
    """Non-fatal API error from Gamma/CLOB."""

    def __init__(self, status: int, reason: str, url: str = ""):
        self.status = status
        self.reason = reason
        self.url = url
        super().__init__(f"HTTP {status}: {reason}")


def _repo_polymarket_script() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "research"
        / "polymarket"
        / "scripts"
        / "polymarket.py"
    )


def _installed_polymarket_script() -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "skills" / "research" / "polymarket" / "scripts" / "polymarket.py"


def load_polymarket_module():
    """Import polymarket.py from repo or installed Hermes skills."""
    global _MODULE
    if _MODULE is not None:
        return _MODULE

    for path in (_repo_polymarket_script(), _installed_polymarket_script()):
        if path.is_file():
            spec = importlib.util.spec_from_file_location("hermes_polymarket", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules["hermes_polymarket"] = module
            spec.loader.exec_module(module)
            _MODULE = module
            return module

    raise FileNotFoundError(
        "polymarket.py not found. Install the bundled polymarket research skill "
        "or run from the Arb-bot repository root."
    )


def _gamma_base() -> str:
    return load_polymarket_module().GAMMA


def _clob_base() -> str:
    return load_polymarket_module().CLOB


def api_get(url: str, *, timeout: float = 20.0) -> dict | list:
    """GET JSON without sys.exit — raises PolymarketAPIError on HTTP errors."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": os.environ.get("ARB_USER_AGENT", DEFAULT_USER_AGENT)},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise PolymarketAPIError(e.code, e.reason, url) from e
    except urllib.error.URLError as e:
        raise PolymarketAPIError(0, str(e.reason), url) from e


def gamma_get(path: str) -> dict | list:
    return api_get(f"{_gamma_base()}{path}")


def clob_get(path: str) -> dict | list:
    return api_get(f"{_clob_base()}{path}")


def parse_json_field(val: Any) -> Any:
    return load_polymarket_module()._parse_json_field(val)


def _market_volume(market: dict) -> float:
    try:
        return float(market.get("volume") or 0)
    except (TypeError, ValueError):
        return 0.0


def iter_event_markets(
    *,
    active: bool = True,
    closed: bool = False,
    page_size: int = 100,
    max_markets: int | None = None,
    max_offset: int | None = None,
    order: str = "volume",
) -> Iterator[dict]:
    """Yield markets from volume-ordered Gamma events (best for liquid alpha).

    Dedupes by condition_id. Stops cleanly when Gamma returns 422 (offset cap).
    """
    offset_cap = max_offset if max_offset is not None else int(
        os.environ.get("ARB_GAMMA_MAX_OFFSET", str(GAMMA_MAX_OFFSET))
    )
    seen: set[str] = set()
    yielded = 0
    offset = 0

    while offset <= offset_cap:
        params = urllib.parse.urlencode(
            {
                "limit": page_size,
                "offset": offset,
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "order": order,
                "ascending": "false",
            }
        )
        try:
            batch = gamma_get(f"/events?{params}")
        except PolymarketAPIError as e:
            if e.status == 422:
                break
            raise
        if not isinstance(batch, list) or not batch:
            break
        for event in batch:
            for market in event.get("markets") or []:
                condition_id = market.get("conditionId") or market.get("condition_id") or ""
                if not condition_id or condition_id in seen:
                    continue
                seen.add(condition_id)
                yield market
                yielded += 1
                if max_markets is not None and yielded >= max_markets:
                    return
        if len(batch) < page_size:
            break
        offset += page_size


def iter_markets(
    *,
    active: bool = True,
    closed: bool = False,
    page_size: int = 100,
    max_markets: int | None = None,
    max_offset: int | None = None,
    order: str = "volume",
    source: str | None = None,
) -> Iterator[dict]:
    """Paginate Gamma markets.

    Default source is ``events`` (volume-ordered, deduped). Set ARB_SCAN_SOURCE=markets
    for legacy /markets pagination (also capped at max offset).
    """
    scan_source = (source or os.environ.get("ARB_SCAN_SOURCE", "events")).lower().strip()
    if scan_source == "events":
        yield from iter_event_markets(
            active=active,
            closed=closed,
            page_size=page_size,
            max_markets=max_markets,
            max_offset=max_offset,
            order=order,
        )
        return

    offset_cap = max_offset if max_offset is not None else int(
        os.environ.get("ARB_GAMMA_MAX_OFFSET", str(GAMMA_MAX_OFFSET))
    )
    offset = 0
    seen = 0
    while offset <= offset_cap:
        params = urllib.parse.urlencode(
            {
                "limit": page_size,
                "offset": offset,
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "order": order,
                "ascending": "false",
            }
        )
        try:
            batch = gamma_get(f"/markets?{params}")
        except PolymarketAPIError as e:
            if e.status == 422:
                break
            raise
        if not isinstance(batch, list) or not batch:
            break
        for market in batch:
            yield market
            seen += 1
            if max_markets is not None and seen >= max_markets:
                return
        if len(batch) < page_size:
            break
        offset += page_size


def market_tokens(market: dict) -> tuple[list[str], list[str], list[float]]:
    """Return (outcomes, token_ids, prices) for a binary or multi-outcome market."""
    outcomes = parse_json_field(market.get("outcomes", "[]"))
    tokens = parse_json_field(market.get("clobTokenIds", "[]"))
    prices = parse_json_field(market.get("outcomePrices", "[]"))

    if not isinstance(outcomes, list):
        outcomes = []
    if not isinstance(tokens, list):
        tokens = []
    if not isinstance(prices, list):
        prices = []

    floats: list[float] = []
    for p in prices:
        try:
            floats.append(float(p))
        except (TypeError, ValueError):
            floats.append(0.0)

    return outcomes, tokens, floats


def fetch_orderbook(token_id: str) -> dict | None:
    """Fetch CLOB book. Returns None on 404 (closed/invalid token)."""
    try:
        return clob_get(f"/book?token_id={urllib.parse.quote(token_id, safe='')}")
    except PolymarketAPIError as e:
        if e.status == 404:
            return None
        raise


def best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    """Return (best_bid, best_ask) from a CLOB orderbook."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = None
    best_ask = None
    if bids:
        best_bid = max(float(b.get("price", 0)) for b in bids)
    if asks:
        best_ask = min(float(a.get("price", 0)) for a in asks)
    return best_bid, best_ask
