"""Adapter protocol and the typed failures adapters are allowed to raise.

`portfolio.scan` converts every exception below into a `models.VenueError` so a
single dead venue degrades one row of the output instead of the whole request.
The distinction between the subclasses is product-visible: `auth_required` means
"paste-an-address can never work here", `unavailable` means "retry later".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import httpx

from ..models import Position, Quote


def make_http_client(**kwargs: Any) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient hardened for venue traffic.

    Every venue we hit (Hyperliquid, Ostium, Avantis, Pacifica, Jupiter, GRVT,
    Ondo) is a public endpoint reached directly over the internet -- none of
    them are supposed to sit behind an HTTP proxy. But `httpx.AsyncClient`
    defaults to `trust_env=True`, so it silently picks up `HTTP_PROXY`,
    `HTTPS_PROXY`, `ALL_PROXY` etc. from the process environment.

    Cursor's sandboxed terminals inject `HTTP_PROXY=http://127.0.0.1:61076`
    into every shell as part of their network policy. That proxy rejects
    unlisted domains (i.e. all of the venues above) with HTTP 403 Forbidden,
    which `httpx` re-raises as `httpx.ProxyError` and adapters translate into
    `VenueUnavailableError("subgraph query failed: 403 Forbidden")`. From the
    UI it is indistinguishable from a real venue outage, and it happens on
    every request instead of intermittently, so it looks like the scanner is
    "sometimes" broken depending on which terminal launched the server.

    Forcing `trust_env=False` here means the scanner ignores ambient proxy env
    entirely. If someone genuinely needs to route through a proxy in the
    future, they should pass `proxies=` explicitly to this helper.
    """
    kwargs.setdefault("trust_env", False)
    return httpx.AsyncClient(**kwargs)


class AdapterError(Exception):
    """Base class for adapter failures. `kind` is surfaced in the UI."""

    kind = "error"

    def __init__(self, venue: str, message: str) -> None:
        self.venue = venue
        self.message = message
        super().__init__(f"{venue}: {message}")


class VenueRequiresAuthError(AdapterError):
    """The venue will not serve position data for an address we do not control.

    Raised when reading positions requires an API key or a signed session bound
    to that account, which a third-party scanner cannot obtain.
    """

    kind = "auth_required"


class VenueUnavailableError(AdapterError):
    """The venue is reachable in principle but this request failed.

    Network errors, non-2xx responses, malformed payloads, or a product that
    does not exist yet.
    """

    kind = "unavailable"


BPS = Decimal(10) ** 4


def walk_book(
    levels: list[tuple[Decimal, Decimal]], notional_usd: Decimal
) -> tuple[Decimal | None, str]:
    """Average fill slippage in bps against the touch, or None if too thin.

    `levels` are (price, base_size) on the side being taken, best price first.

    Returning None rather than 0 when the visible book cannot absorb the size
    matters: a zero would make the venue look free and could win a hedge
    ranking it should not. The caller marks such a quote unavailable.
    """
    usable = [(p, s) for p, s in levels if p > 0 and s > 0]
    if not usable:
        return None, "Orderbook side is empty."
    if notional_usd <= 0:
        return Decimal(0), "Zero notional."

    touch = usable[0][0]
    remaining = notional_usd
    filled_notional = Decimal(0)
    filled_base = Decimal(0)
    for price, size in usable:
        take = min(price * size, remaining)
        filled_notional += take
        filled_base += take / price
        remaining -= take
        if remaining <= 0:
            break

    if remaining > 0:
        return None, (
            f"Visible book absorbs only {filled_notional:,.0f} USD of the requested "
            f"{notional_usd:,.0f} USD."
        )
    average = filled_notional / filled_base
    return abs(average - touch) / touch * BPS, "Walked against the live orderbook."


@runtime_checkable
class VenueAdapter(Protocol):
    venue: str
    namespace: str  # "evm" | "solana"

    async def get_positions(self, address: str) -> list[Position]: ...

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote: ...

    async def health(self) -> bool: ...
