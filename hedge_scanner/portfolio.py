"""Address-namespace detection and concurrent fan-out across venue adapters.

The whole point of this layer is that one dead or gated venue degrades a single
row of output. Every adapter runs concurrently and every failure -- typed or
not -- becomes a `VenueError` rather than an exception that reaches the caller.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from .adapters import EVM_ADAPTERS, SOLANA_ADAPTERS, AdapterError
from .adapters.base import VenueAdapter, make_http_client
from .hedge_venues import avantis
from .models import Position, PortfolioSnapshot, Quote, VenueError

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
# Base58 excludes 0, O, I and l precisely so that these strings stay unambiguous.
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# CONTRACT.md section 7.5.3. Only affects the informational all-in figure Avantis
# attaches to its own quote; the engine recomputes cost for every horizon itself.
DEFAULT_QUOTE_HORIZON_HOURS = Decimal(24)


def detect_namespace(address: str) -> str | None:
    """Return "evm", "solana", or None if the string is neither.

    Never guesses a cross-chain identity: an EVM address and a Solana address
    are separate inputs even if one person controls both.
    """
    candidate = address.strip()
    if EVM_ADDRESS_RE.match(candidate):
        return "evm"
    if BASE58_RE.match(candidate):
        return "solana"
    return None


def adapters_for_namespace(
    namespace: str, only_public: bool = False
) -> list[VenueAdapter]:
    """Instantiate every adapter for a namespace, optionally dropping auth-gated ones.

    ``only_public=True`` excludes venues whose position endpoints require an
    account-bound credential the scanner will never possess (currently GRVT
    and Ondo). Those venues stay usable for quotes -- this flag is scoped to
    position reads. Adapters signal their status via a ``public_positions``
    class attribute; anything without the attribute is treated as public.
    """
    classes = SOLANA_ADAPTERS if namespace == "solana" else EVM_ADAPTERS
    if only_public:
        classes = tuple(c for c in classes if getattr(c, "public_positions", True))
    return [cls() for cls in classes]


async def _positions_or_error(
    adapter: VenueAdapter, address: str
) -> tuple[list[Position], VenueError | None]:
    try:
        return await adapter.get_positions(address), None
    except AdapterError as exc:
        return [], VenueError(
            venue=adapter.venue, message=exc.message, kind=exc.kind, address=address
        )
    except Exception as exc:  # noqa: BLE001 - a venue must never break the request
        return [], VenueError(
            venue=adapter.venue,
            message=f"{type(exc).__name__}: {exc}",
            kind="error",
            address=address,
        )


async def scan(
    addresses: list[str],
    only_public: bool = False,
) -> tuple[list[Position], list[VenueError]]:
    """Fetch every open position across every applicable venue.

    Accepts a mix of EVM and Solana addresses so a user can paste both and get
    one portfolio. Each address is only sent to the venues whose namespace it
    matches.

    Set ``only_public=True`` to skip venues that require an account-bound
    credential for position reads (GRVT, Ondo). Those venues would otherwise
    produce a persistent ``auth_required`` error on every scan the tool can't
    do anything about. Their public quote endpoints are unaffected.
    """
    positions: list[Position] = []
    errors: list[VenueError] = []

    tasks: list[asyncio.Task] = []
    owned: list[VenueAdapter] = []

    for raw_address in addresses:
        address = raw_address.strip()
        namespace = detect_namespace(address)
        if namespace is None:
            errors.append(
                VenueError(
                    venue="-",
                    message=(
                        f"{address!r} is neither an EVM address (0x + 40 hex) nor a "
                        "base58 Solana pubkey (32-44 chars)."
                    ),
                    kind="unsupported_namespace",
                    address=address,
                )
            )
            continue

        for adapter in adapters_for_namespace(namespace, only_public=only_public):
            owned.append(adapter)
            tasks.append(asyncio.create_task(_positions_or_error(adapter, address)))

    try:
        for venue_positions, error in await asyncio.gather(*tasks):
            positions.extend(venue_positions)
            if error is not None:
                errors.append(error)
    finally:
        await asyncio.gather(
            *(adapter.aclose() for adapter in owned), return_exceptions=True
        )

    return positions, errors


async def scan_snapshot(
    addresses: list[str], only_public: bool = False
) -> PortfolioSnapshot:
    """`scan` wrapped in the snapshot shape the CLI and engine consume."""
    positions, errors = await scan(addresses, only_public=only_public)
    return PortfolioSnapshot(addresses=list(addresses), positions=positions, errors=errors)


def _avantis_unlisted_quote(
    base_asset: str, side: str, notional_usd: Decimal
) -> Quote:
    """The Avantis line for an asset Avantis does not list.

    Reported as an unavailable quote rather than an omission: the engine turns
    that into an excluded row with a reason, which is what lets the output keep
    naming Avantis explicitly per CONTRACT.md section 7.5.1.
    """
    zero = Decimal(0)
    return Quote(
        venue=avantis.VENUE,
        market=f"{base_asset}/USD",
        side=side,
        notional_usd=notional_usd,
        taker_fee_bps=zero,
        close_fee_bps=zero,
        price_impact_bps=zero,
        funding_rate_8h_bps=zero,
        borrow_rate_8h_bps=zero,
        est_slippage_bps=zero,
        available=False,
        notes=f"Avantis does not list a {base_asset} pair, so it cannot host this hedge.",
        base_asset=base_asset.strip().upper(),
    )


def _avantis_upside_unlisted_quote(
    base_asset: str, side: str, notional_usd: Decimal
) -> Quote:
    """The Avantis Upside line for an asset that has no Upside pair.

    Upside Perps only exist for a small set of crypto majors (BTC/ETH/SOL/XRP/
    HYPE per §7.6 as of 2026-08). Assets outside that set still get a row, with a
    reason, per §7.5.1's rule that a missing venue is a hard finding.
    """
    zero = Decimal(0)
    return Quote(
        venue=avantis.UPSIDE_VENUE,
        market=f"{base_asset}_UPSIDE/USD",
        side=side,
        notional_usd=notional_usd,
        taker_fee_bps=zero,
        close_fee_bps=zero,
        price_impact_bps=zero,
        funding_rate_8h_bps=zero,
        borrow_rate_8h_bps=zero,
        est_slippage_bps=zero,
        available=False,
        notes=(
            f"Avantis does not list an Upside Perp for {base_asset}. Upside is a "
            "crypto-majors-only product; use the standard Avantis perp for this asset."
        ),
        base_asset=base_asset.strip().upper(),
    )


async def _avantis_quote(
    base_asset: str, side: str, notional_usd: Decimal, horizon_hours: Decimal
) -> tuple[Quote | None, VenueError | None]:
    """Price the primary hedge destination.

    Avantis is never a position source (CONTRACT.md section 1) so it has no entry
    in `adapters` and has to be quoted alongside them. Its own module handles
    client lifetime and response caching.
    """
    try:
        async with make_http_client(timeout=avantis.HTTP_TIMEOUT) as client:
            quote = await avantis.quote_hedge(
                base_asset, side, notional_usd, horizon_hours, client=client
            )
    except Exception as exc:  # noqa: BLE001 - a venue must never break the request
        return None, VenueError(
            venue=avantis.VENUE, message=f"{type(exc).__name__}: {exc}", kind="error"
        )
    if quote is None:
        return _avantis_unlisted_quote(base_asset, side, notional_usd), None
    return quote, None


async def _avantis_upside_quote(
    base_asset: str, side: str, notional_usd: Decimal
) -> tuple[Quote | None, VenueError | None]:
    """Price the Avantis Upside Perps hedge leg as a distinct venue row.

    Upside is a fundamentally different risk shape from the standard perp (§7.6
    final paragraph): no commission or borrow, but a 25 / 20 / 10 / 5 % profit
    share by ROI band on a winning close. It is quoted alongside the standard
    perp so a hedger can see both rows and evaluate the tradeoff -- Upside is
    cheaper when the hedge turns out unnecessary and more expensive when the
    hedge works.

    A failing Upside call becomes one ``VenueError`` row per §7 and §12.1, same
    contract as any adapter. Upside-unlisted assets return ``available=False``
    with a reason rather than an omission so the row keeps showing up.
    """
    try:
        async with make_http_client(timeout=avantis.HTTP_TIMEOUT) as client:
            quote = await avantis.quote_upside_hedge(
                base_asset, side, notional_usd, client=client
            )
    except Exception as exc:  # noqa: BLE001 - a venue must never break the request
        return None, VenueError(
            venue=avantis.UPSIDE_VENUE,
            message=f"{type(exc).__name__}: {exc}",
            kind="error",
        )
    if quote is None:
        return _avantis_upside_unlisted_quote(base_asset, side, notional_usd), None
    return quote, None


async def quotes_for(
    base_asset: str,
    side: str,
    notional_usd: Decimal,
    horizon_hours: Decimal = DEFAULT_QUOTE_HORIZON_HOURS,
) -> tuple[list[Quote], list[VenueError]]:
    """Quote a hedge leg on every venue, including ones we cannot read positions from.

    GRVT and Ondo publish market data without auth, so the engine can still
    price a hedge there even though it can never see the user's positions.
    Avantis is quoted too, as the hedge destination the comparison is built
    around; it is priced live because its cost is not a constant (section 7.6).

    Avantis Upside Perps are quoted alongside the standard Avantis perp under a
    distinct ``avantis_upside`` venue string (§12.4). Both rows coexist so a
    hedger can see the two instruments side by side: the standard perp is
    commission + spread + carry, and Upside surrenders a share of gross profit
    only on a winning close, so the honest comparison is between an
    unconditional cost and a contingent one.
    """
    adapters = [cls() for cls in (*SOLANA_ADAPTERS, *EVM_ADAPTERS)]
    quotes: list[Quote] = []
    errors: list[VenueError] = []

    async def one(adapter: VenueAdapter) -> tuple[Quote | None, VenueError | None]:
        try:
            return await adapter.get_quote(base_asset, side, notional_usd), None
        except AdapterError as exc:
            return None, VenueError(
                venue=adapter.venue, message=exc.message, kind=exc.kind
            )
        except Exception as exc:  # noqa: BLE001
            return None, VenueError(
                venue=adapter.venue, message=f"{type(exc).__name__}: {exc}", kind="error"
            )

    try:
        results = await asyncio.gather(
            *(one(a) for a in adapters),
            _avantis_quote(base_asset, side, notional_usd, horizon_hours),
            _avantis_upside_quote(base_asset, side, notional_usd),
        )
        for quote, error in results:
            if quote is not None:
                quotes.append(quote)
            if error is not None:
                errors.append(error)
    finally:
        await asyncio.gather(
            *(adapter.aclose() for adapter in adapters), return_exceptions=True
        )

    return quotes, errors
