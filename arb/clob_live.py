"""Live Polymarket CLOB execution via py-clob-client-v2.

Requires:
  pip install py-clob-client-v2
  POLYMARKET_PRIVATE_KEY in env (or ~/.hermes/.env)
  ARB_ALLOW_LIVE=1, ARB_EXEC_MODE=live, ARB_DRY_RUN=false, ARB_STUDY_MODE=false

Uses CLOB V2 API (https://docs.polymarket.com/trading/overview).
V1 is deprecated — do not use py-clob-client (v1).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from arb.config import ArbConfig
from arb.dutch_book import ArbKind, Opportunity

log = logging.getLogger("arb.clob_live")

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


@dataclass
class LiveLegResult:
    token_id: str
    side: str
    price: float
    size: float
    order_id: Optional[str] = None
    status: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class LiveBundleResult:
    ok: bool
    mode: str = "live"
    legs: list[LiveLegResult] = field(default_factory=list)
    error: Optional[str] = None
    client_ready: bool = False
    size_usd: float = 0.0
    fill_total: float = 0.0
    fill_prices: list[float] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)


def _load_private_key(config: ArbConfig) -> Optional[str]:
    key = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    if key:
        return key
    for name in ("POLY_PRIVATE_KEY", "PK"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    # Fallback: ~/.hermes/.env
    from pathlib import Path

    env_path = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("POLYMARKET_PRIVATE_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def build_clob_client(config: ArbConfig) -> Any:
    """Construct authenticated ClobClient. Raises on missing deps/creds."""
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError as e:
        raise RuntimeError(
            "py-clob-client-v2 not installed. Run: pip install 'hermes-agent[polymarket-arb]' "
            "or: pip install py-clob-client-v2"
        ) from e

    pk = _load_private_key(config)
    if not pk:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY required for live trading "
            "(set in .env or ~/.hermes/.env)"
        )

    kwargs: dict[str, Any] = {
        "host": CLOB_HOST,
        "chain_id": CHAIN_ID,
        "key": pk,
    }
    funder = _env("POLYMARKET_FUNDER") or _env("ARB_POLYMARKET_FUNDER")
    if funder:
        kwargs["funder"] = funder
    sig_raw = _env("POLYMARKET_SIGNATURE_TYPE") or _env("ARB_SIGNATURE_TYPE")
    if sig_raw != "":
        kwargs["signature_type"] = int(sig_raw)

    client = ClobClient(**kwargs)

    api_key = _env("CLOB_API_KEY") or _env("POLYMARKET_API_KEY")
    api_secret = _env("CLOB_SECRET") or _env("POLYMARKET_API_SECRET")
    api_pass = _env("CLOB_PASS_PHRASE") or _env("CLOB_PASSPHRASE") or _env("POLYMARKET_PASSPHRASE")

    if api_key and api_secret and api_pass:
        from py_clob_client_v2 import ApiCreds

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_pass,
        )
        client.set_api_creds(creds)
    else:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

    return client


def _side_buy():
    from py_clob_client_v2 import Side

    return Side.BUY


def _side_sell():
    from py_clob_client_v2 import Side

    return Side.SELL


def _order_type_gtc():
    from py_clob_client_v2 import OrderType

    return OrderType.GTC


def place_leg(
    client: Any,
    *,
    token_id: str,
    price: float,
    size: float,
    side: str,
    tick_size: Optional[str] = None,
) -> LiveLegResult:
    """Place a single limit order (GTC) for one outcome token."""
    from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions

    result = LiveLegResult(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
    )
    try:
        side_enum = _side_buy() if side.upper() == "BUY" else _side_sell()
        args = OrderArgs(
            token_id=str(token_id),
            price=float(price),
            size=float(size),
            side=side_enum,
        )
        options = None
        if tick_size:
            options = PartialCreateOrderOptions(tick_size=str(tick_size))

        signed = client.create_order(args, options)
        resp = client.post_order(signed, _order_type_gtc())
        if isinstance(resp, dict):
            result.raw = resp
            result.order_id = (
                resp.get("orderID")
                or resp.get("orderId")
                or resp.get("id")
                or resp.get("order_id")
            )
            result.status = str(resp.get("status") or resp.get("success") or "posted")
            if resp.get("success") is False or resp.get("error"):
                result.error = str(resp.get("error") or resp.get("errorMsg") or resp)
                result.status = "error"
        else:
            result.raw = {"response": str(resp)}
            result.status = "posted"
    except Exception as e:
        result.error = str(e)
        result.status = "error"
        log.exception("live leg failed token=%s", token_id[:16] if token_id else "?")
    return result


def execute_buy_bundle_live(
    config: ArbConfig,
    opp: Opportunity,
    *,
    size_usd: float,
    dry_run: bool = False,
) -> LiveBundleResult:
    """
    Place orders for every outcome token in the opportunity.

    Buy bundle → BUY each outcome at ask.
    Sell bundle → SELL each outcome at bid (requires inventory; still gated).

    Size per leg (shares) ≈ size_usd / n_outcomes / price.
    """
    if opp.kind == ArbKind.SELL_BUNDLE:
        return LiveBundleResult(
            ok=False,
            error=(
                "UNSUPPORTED_STRATEGY: SELL_BUNDLE execution is disabled — no verified "
                "inventory/collateral-split/common-quantity workflow yet."
            ),
            size_usd=size_usd,
        )

    if not config.live_allowed():
        return LiveBundleResult(
            ok=False,
            error=(
                "Live trading blocked. Set ARB_ALLOW_LIVE=1, ARB_EXEC_MODE=live, "
                "ARB_DRY_RUN=false, ARB_STUDY_MODE=false, ARB_KILL_SWITCH=false, "
                "and POLYMARKET_PRIVATE_KEY."
            ),
            size_usd=size_usd,
        )

    side = "BUY" if opp.kind == ArbKind.BUY_BUNDLE else "SELL"
    n = max(1, len(opp.token_ids))
    per_leg_usd = float(size_usd) / n

    if dry_run:
        legs = []
        prices: list[float] = []
        for token_id, price in zip(opp.token_ids, opp.prices):
            px = float(price)
            sz = round(per_leg_usd / px, 4) if px > 0 else 0.0
            prices.append(px)
            legs.append(
                LiveLegResult(
                    token_id=token_id,
                    side=side,
                    price=px,
                    size=sz,
                    status="dry_run",
                )
            )
        return LiveBundleResult(
            ok=True,
            mode="live_dry_run",
            legs=legs,
            client_ready=False,
            size_usd=size_usd,
        fill_total=round(sum(prices), 6),
        fill_prices=prices,
        )

    try:
        client = build_clob_client(config)
    except Exception as e:
        return LiveBundleResult(
            ok=False, error=str(e), client_ready=False, size_usd=size_usd
        )

    legs_out: list[LiveLegResult] = []
    fill_prices: list[float] = []
    for token_id, price in zip(opp.token_ids, opp.prices):
        if not token_id:
            legs_out.append(
                LiveLegResult(
                    token_id="",
                    side=side,
                    price=float(price),
                    size=0.0,
                    status="error",
                    error="missing token_id",
                )
            )
            continue
        px = round(float(price), 4)
        if px <= 0:
            legs_out.append(
                LiveLegResult(
                    token_id=token_id,
                    side=side,
                    price=px,
                    size=0.0,
                    status="error",
                    error="invalid price",
                )
            )
            continue
        size = round(per_leg_usd / px, 4)
        lr = place_leg(client, token_id=token_id, price=px, size=size, side=side)
        legs_out.append(lr)
        fill_prices.append(px)

    any_error = any(l.error or l.status == "error" for l in legs_out)
    order_ids = [l.order_id for l in legs_out if l.order_id]
    any_ok = len(order_ids) > 0 and not any_error

    return LiveBundleResult(
        ok=any_ok,
        mode="live",
        legs=legs_out,
        error="one or more legs failed" if any_error else None,
        client_ready=True,
        size_usd=size_usd,
        fill_total=round(sum(fill_prices), 6) if fill_prices else 0.0,
        fill_prices=fill_prices,
        order_ids=order_ids,
    )


def cancel_order(config: ArbConfig, order_id: str) -> dict[str, Any]:
    """Cancel a single order by id."""
    client = build_clob_client(config)
    return client.cancel(order_id)


def get_open_orders(config: ArbConfig) -> Any:
    client = build_clob_client(config)
    return client.get_orders()
