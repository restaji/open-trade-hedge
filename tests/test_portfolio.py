"""Namespace routing and the guarantee that one dead venue never kills a request."""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedge_scanner import portfolio
from hedge_scanner.adapters.base import VenueRequiresAuthError, VenueUnavailableError
from hedge_scanner.models import Position, Quote

SOLANA = "2JVs9RekjARxu9tRYq8Dbq2eGNRegzRSGJMrCBXKj8ti"
EVM = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (EVM, "evm"),
        ("0x" + "0" * 40, "evm"),
        (SOLANA, "solana"),
        ("So11111111111111111111111111111111111111112", "solana"),
        ("0x123", None),
        ("0x" + "0" * 41, None),
        ("not an address", None),
        ("", None),
        # Base58 excludes 0, O, I and l, so these are not Solana pubkeys.
        ("0OIl" + "1" * 30, None),
    ],
)
def test_detect_namespace(address, expected):
    assert portfolio.detect_namespace(address) == expected


def test_solana_and_evm_route_to_disjoint_venue_sets():
    solana = {a.venue for a in portfolio.adapters_for_namespace("solana")}
    evm = {a.venue for a in portfolio.adapters_for_namespace("evm")}
    assert solana == {"jupiter", "pacifica"}
    assert evm == {"grvt", "ondo", "hyperliquid", "ostium"}
    assert not solana & evm


def test_only_public_drops_auth_gated_venues():
    """`only_public=True` must exclude venues whose position endpoints are
    auth-gated (GRVT, Ondo), and only those; the public-position adapters on
    each namespace must still be present.
    """
    evm_public = {
        a.venue for a in portfolio.adapters_for_namespace("evm", only_public=True)
    }
    solana_public = {
        a.venue for a in portfolio.adapters_for_namespace("solana", only_public=True)
    }
    assert evm_public == {"hyperliquid", "ostium"}
    assert solana_public == {"jupiter", "pacifica"}


class _StubAdapter:
    def __init__(self, venue, namespace, result=None, error=None):
        self.venue = venue
        self.namespace = namespace
        self._result = result or []
        self._error = error
        self.closed = False

    async def get_positions(self, address):
        if self._error is not None:
            raise self._error
        return self._result

    async def get_quote(self, base_asset, side, notional_usd):
        raise NotImplementedError

    async def health(self):
        return True

    async def aclose(self):
        self.closed = True


def _position(venue: str, address: str) -> Position:
    return Position(
        venue=venue,
        address=address,
        market="BTC-PERP",
        base_asset="BTC",
        quote_asset="USDC",
        side="long",
        size_base=Decimal(1),
        notional_usd=Decimal(65_000),
        entry_price=Decimal(64_000),
        mark_price=Decimal(65_000),
    )


async def test_one_failing_venue_never_hides_the_others(monkeypatch):
    working = _StubAdapter("jupiter", "solana", result=[_position("jupiter", SOLANA)])
    gated = _StubAdapter(
        "pacifica",
        "solana",
        error=VenueUnavailableError("pacifica", "503 from upstream"),
    )
    monkeypatch.setattr(
        portfolio,
        "adapters_for_namespace",
        lambda ns, only_public=False: [working, gated],
    )

    positions, errors = await portfolio.scan([SOLANA])

    assert [p.venue for p in positions] == ["jupiter"]
    assert [(e.venue, e.kind) for e in errors] == [("pacifica", "unavailable")]
    assert working.closed and gated.closed


async def test_auth_gated_venue_is_reported_not_swallowed(monkeypatch):
    gated = _StubAdapter(
        "grvt", "evm", error=VenueRequiresAuthError("grvt", "needs your API key")
    )
    monkeypatch.setattr(
        portfolio,
        "adapters_for_namespace",
        lambda ns, only_public=False: [gated],
    )

    positions, errors = await portfolio.scan([EVM])

    assert positions == []
    assert len(errors) == 1
    assert errors[0].kind == "auth_required"
    assert errors[0].address == EVM


async def test_an_unexpected_exception_is_still_contained(monkeypatch):
    class _Boom(_StubAdapter):
        async def get_positions(self, address):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(
        portfolio,
        "adapters_for_namespace",
        lambda ns, only_public=False: [_Boom("jupiter", "solana")],
    )
    positions, errors = await portfolio.scan([SOLANA])

    assert positions == []
    assert errors[0].kind == "error"
    assert "kaboom" in errors[0].message


async def test_unparseable_address_is_an_error_row_not_a_crash():
    positions, errors = await portfolio.scan(["definitely not an address"])
    assert positions == []
    assert errors[0].kind == "unsupported_namespace"


async def test_mixed_namespaces_produce_one_portfolio(monkeypatch):
    def fake(namespace, only_public=False):
        if namespace == "solana":
            return [_StubAdapter("jupiter", "solana", result=[_position("jupiter", SOLANA)])]
        return [_StubAdapter("grvt", "evm", error=VenueRequiresAuthError("grvt", "key"))]

    monkeypatch.setattr(portfolio, "adapters_for_namespace", fake)
    snapshot = await portfolio.scan_snapshot([SOLANA, EVM])

    assert snapshot.addresses == [SOLANA, EVM]
    assert [p.venue for p in snapshot.positions] == ["jupiter"]
    assert [e.venue for e in snapshot.errors] == ["grvt"]


# --------------------------------------------------------------------------
# Hedge quotes. Avantis is the destination the whole comparison is built
# around (CONTRACT.md 7.5.1), but it is not a position source, so it is easy
# to leave out of this fan-out and hard to notice: the engine degrades to
# "Avantis: not ranked" rather than failing. These pin it in place.
# --------------------------------------------------------------------------


def _quote(venue: str, notional_usd: Decimal) -> Quote:
    zero = Decimal(0)
    return Quote(
        venue=venue,
        market="BTC-PERP",
        side="short",
        notional_usd=notional_usd,
        taker_fee_bps=Decimal(4),
        close_fee_bps=Decimal(4),
        price_impact_bps=zero,
        funding_rate_8h_bps=zero,
        borrow_rate_8h_bps=zero,
        est_slippage_bps=zero,
        available=True,
        base_asset="BTC",
    )


def _quoting_adapter(venue: str):
    class _Adapter(_StubAdapter):
        def __init__(self) -> None:
            super().__init__(venue, "solana")

        async def get_quote(self, base_asset, side, notional_usd):
            return _quote(venue, notional_usd)

    return _Adapter


def _only_adapter(monkeypatch, venue: str = "pacifica") -> None:
    monkeypatch.setattr(portfolio, "SOLANA_ADAPTERS", (_quoting_adapter(venue),))
    monkeypatch.setattr(portfolio, "EVM_ADAPTERS", ())


async def test_quotes_for_includes_the_avantis_hedge_destination(monkeypatch):
    _only_adapter(monkeypatch)
    captured: dict = {}

    async def fake_quote_hedge(base_asset, side, notional_usd, horizon_hours, client=None):
        captured.update(
            base_asset=base_asset,
            side=side,
            notional_usd=notional_usd,
            horizon_hours=horizon_hours,
        )
        return _quote("avantis", notional_usd)

    async def fake_upside(base_asset, side, notional_usd, client=None):
        return _quote("avantis_upside", notional_usd)

    monkeypatch.setattr(portfolio.avantis, "quote_hedge", fake_quote_hedge)
    monkeypatch.setattr(portfolio.avantis, "quote_upside_hedge", fake_upside)

    quotes, errors = await portfolio.quotes_for(
        "BTC", "short", Decimal(50_000), Decimal(72)
    )

    assert errors == []
    assert "avantis" in {q.venue for q in quotes}
    # The horizon has to reach Avantis: it is the only venue that prices its own
    # all-in figure rather than leaving it to the engine.
    assert captured == {
        "base_asset": "BTC",
        "side": "short",
        "notional_usd": Decimal(50_000),
        "horizon_hours": Decimal(72),
    }


async def test_avantis_not_listing_an_asset_is_an_unavailable_quote_not_an_omission(
    monkeypatch,
):
    _only_adapter(monkeypatch)

    async def unlisted(base_asset, side, notional_usd, horizon_hours, client=None):
        return None

    async def upside_unlisted(base_asset, side, notional_usd, client=None):
        return None

    monkeypatch.setattr(portfolio.avantis, "quote_hedge", unlisted)
    monkeypatch.setattr(portfolio.avantis, "quote_upside_hedge", upside_unlisted)

    quotes, errors = await portfolio.quotes_for("DOGE", "long", Decimal(1_000))

    assert errors == []
    avantis_quotes = [q for q in quotes if q.venue == "avantis"]
    assert len(avantis_quotes) == 1
    assert avantis_quotes[0].available is False
    assert "does not list" in avantis_quotes[0].notes
    assert avantis_quotes[0].base_asset == "DOGE"


async def test_a_failing_avantis_never_costs_the_other_quotes(monkeypatch):
    _only_adapter(monkeypatch)

    async def boom(base_asset, side, notional_usd, horizon_hours, client=None):
        raise RuntimeError("spread engine down")

    async def upside_boom(base_asset, side, notional_usd, client=None):
        raise RuntimeError("upside spread engine down")

    monkeypatch.setattr(portfolio.avantis, "quote_hedge", boom)
    monkeypatch.setattr(portfolio.avantis, "quote_upside_hedge", upside_boom)

    quotes, errors = await portfolio.quotes_for("BTC", "short", Decimal(50_000))

    assert [q.venue for q in quotes] == ["pacifica"]
    venue_error_kinds = {(e.venue, e.kind) for e in errors}
    assert ("avantis", "error") in venue_error_kinds
    assert ("avantis_upside", "error") in venue_error_kinds
    assert any("spread engine down" in e.message for e in errors)


# --------------------------------------------------------------------------
# Upside Perps: quoted as a distinct venue alongside the standard Avantis perp
# (CONTRACT.md §7.6 final paragraph, §12.4). The two instruments cannot share
# one venue name or the ranking would treat them as duplicate quotes and the
# comparison engine would never find the direct Upside quote.
# --------------------------------------------------------------------------


async def test_quotes_for_returns_both_avantis_and_avantis_upside(monkeypatch):
    """A listable crypto asset must produce two Avantis rows, standard and Upside.

    Both quotes coexist under distinct venue names, so the ranking table can
    show ``Avantis`` and ``Avantis (Upside)`` as separate rows for the same
    base asset and a hedger can compare them directly.
    """
    _only_adapter(monkeypatch)

    async def fake_standard(base_asset, side, notional_usd, horizon_hours, client=None):
        return _quote("avantis", notional_usd)

    async def fake_upside(base_asset, side, notional_usd, client=None):
        upside = _quote("avantis_upside", notional_usd)
        # Upside always carries no borrow (§7.6), so a fake fixture must too --
        # otherwise the assertion below would pin an accidental drift.
        upside.borrow_rate_8h_bps = Decimal(0)
        return upside

    monkeypatch.setattr(portfolio.avantis, "quote_hedge", fake_standard)
    monkeypatch.setattr(portfolio.avantis, "quote_upside_hedge", fake_upside)

    quotes, errors = await portfolio.quotes_for(
        "BTC", "short", Decimal(50_000), Decimal(24)
    )

    assert errors == []
    venues = {q.venue for q in quotes}
    assert "avantis" in venues
    assert "avantis_upside" in venues
    # Exactly one row per venue: no accidental duplication.
    assert sum(1 for q in quotes if q.venue == "avantis") == 1
    assert sum(1 for q in quotes if q.venue == "avantis_upside") == 1


async def test_upside_unlisted_asset_returns_an_unavailable_row_not_an_omission(
    monkeypatch,
):
    """An asset without an Upside pair still gets a row so the venue stays visible.

    Silent omission would fail §7.5.1 -- the output must always name Avantis
    (and, once wired, Avantis (Upside)) whether it can host the hedge or not.
    """
    _only_adapter(monkeypatch)

    async def standard(base_asset, side, notional_usd, horizon_hours, client=None):
        return _quote("avantis", notional_usd)

    async def upside_none(base_asset, side, notional_usd, client=None):
        return None

    monkeypatch.setattr(portfolio.avantis, "quote_hedge", standard)
    monkeypatch.setattr(portfolio.avantis, "quote_upside_hedge", upside_none)

    quotes, errors = await portfolio.quotes_for("DOGE", "long", Decimal(1_000))

    assert errors == []
    upside_rows = [q for q in quotes if q.venue == "avantis_upside"]
    assert len(upside_rows) == 1
    assert upside_rows[0].available is False
    assert "Upside" in upside_rows[0].notes
    assert upside_rows[0].base_asset == "DOGE"


async def test_failing_upside_never_hides_the_standard_avantis_quote(monkeypatch):
    """An Upside failure is one ``VenueError`` row, not a cascade. §12.1."""
    _only_adapter(monkeypatch)

    async def standard(base_asset, side, notional_usd, horizon_hours, client=None):
        return _quote("avantis", notional_usd)

    async def upside_boom(base_asset, side, notional_usd, client=None):
        raise RuntimeError("upside spread engine timed out")

    monkeypatch.setattr(portfolio.avantis, "quote_hedge", standard)
    monkeypatch.setattr(portfolio.avantis, "quote_upside_hedge", upside_boom)

    quotes, errors = await portfolio.quotes_for("BTC", "short", Decimal(50_000))

    assert "avantis" in {q.venue for q in quotes}
    assert "avantis_upside" not in {q.venue for q in quotes}
    assert [(e.venue, e.kind) for e in errors] == [("avantis_upside", "error")]
    assert "upside spread engine timed out" in errors[0].message
