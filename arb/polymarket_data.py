"""Polymarket data access — extends the bundled research skill, no duplicate HTTP."""

from __future__ import annotations

import importlib.util
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

_MODULE: Any | None = None


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


def gamma_get(path: str) -> dict | list:
    pm = load_polymarket_module()
    return pm._get(f"{pm.GAMMA}{path}")


def clob_get(path: str) -> dict | list:
    pm = load_polymarket_module()
    return pm._get(f"{pm.CLOB}{path}")


def parse_json_field(val: Any) -> Any:
    return load_polymarket_module()._parse_json_field(val)


def iter_markets(
    *,
    active: bool = True,
    closed: bool = False,
    page_size: int = 100,
    max_markets: int | None = None,
) -> Iterator[dict]:
    """Paginate all Gamma markets."""
    offset = 0
    seen = 0
    while True:
        params = urllib.parse.urlencode(
            {
                "limit": page_size,
                "offset": offset,
                "active": str(active).lower(),
                "closed": str(closed).lower(),
            }
        )
        batch = gamma_get(f"/markets?{params}")
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


def fetch_orderbook(token_id: str) -> dict:
    return clob_get(f"/book?token_id={urllib.parse.quote(token_id, safe='')}")


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
