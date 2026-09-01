"""Base-asset alias normalization.

Cross-venue netting only works if `WBTC`, `XBT`, `BTC-PERP` and `BTC_USDT_Perp`
all collapse to `BTC`. Venues disagree on wrappers, suffixes and separators, so
normalization runs in three stages: recognize FX/compound symbols as-is, strip
the quote/product suffix, then apply an explicit alias table.

The alias table here covers wrapped-token and venue-ticker mappings. Cross-venue
collisions (k-prefixed memecoins, index/ETF scale, equity share classes) are in
`hedge_scanner.markets.ASSET_ALIASES` and should be checked as a second pass.
"""

from __future__ import annotations

import re as _re

_FX_PAIR_RE = _re.compile(
    r"^([A-Z]{3})/([A-Z]{3})$"
    r"|^([A-Z]{3})([A-Z]{3})$"  # EURUSD (no separator)
)

_KNOWN_FX_BASES = frozenset({
    "AUD", "CAD", "CHF", "CNH", "EUR", "GBP", "JPY",
    "NZD", "SEK", "SGD", "BRL", "IDR", "INR", "KRW",
    "MXN", "TRY", "TWD", "ZAR",
})

# Explicit aliases applied after suffix/prefix stripping. Keys are uppercase.
ALIASES: dict[str, str] = {
    # Bitcoin
    "XBT": "BTC",
    "WBTC": "BTC",
    "CBBTC": "BTC",
    "TBTC": "BTC",
    "BTCB": "BTC",
    "SBTC": "BTC",
    # Ether
    "WETH": "ETH",
    "STETH": "ETH",
    "WSTETH": "ETH",
    "WEETH": "ETH",
    "RETH": "ETH",
    "CBETH": "ETH",
    # Solana
    "WSOL": "SOL",
    "MSOL": "SOL",
    "JITOSOL": "SOL",
    "BSOL": "SOL",
    # Stables / quote units, normalized so a USD-quoted and a USDT-quoted market
    # are recognized as the same quote leg.
    "USDBC": "USDC",
    "USDCE": "USDC",
    # Venue-specific tickers for the same underlying
    "WAVAX": "AVAX",
    "WBNB": "BNB",
    "WMATIC": "MATIC",
    "WPOL": "POL",
    "WHYPE": "HYPE",
    "WS": "S",
    # 1000x memecoin contracts
    "KPEPE": "PEPE",
    "1000PEPE": "PEPE",
    "KBONK": "BONK",
    "1000BONK": "BONK",
    "KSHIB": "SHIB",
    "1000SHIB": "SHIB",
    "KFLOKI": "FLOKI",
    "1000FLOKI": "FLOKI",
    "KNEIRO": "NEIRO",
    "1000NEIRO": "NEIRO",
    "KLUNC": "LUNC",
    "1000LUNC": "LUNC",
    # Metals
    "GOLD": "XAU",
    "XAUUSD": "XAU",
    "GC": "XAU",
    "SILVER": "XAG",
    "XAGUSD": "XAG",
    "SI": "XAG",
    # Energy
    "CL": "WTI",
    "USOIL": "WTI",
    "WTIV6": "WTI",
    "CRUDE": "WTI",
    "BZ": "BRENT",
    "BRENTV6": "BRENT",
    "BRENTOIL": "BRENT",  # Hyperliquid HIP-3 (xyz sub-DEX)
    "UKOIL": "BRENT",
    "NG": "NATGAS",
    "NATURALGAS": "NATGAS",
    "HG": "COPPER",
    # Equity indices
    "SPX": "US500",
    "SP500": "US500",
    "SPX500": "US500",
    "ES": "US500",
    "NDX": "US100",
    "NAS100": "US100",
    "NASDAQ100": "US100",
    "NQ": "US100",
    # Share-class and ticker spellings
    "GOOG": "GOOGL",
    "SK HYNIX": "SKHYNIX",
    "SKHY": "SKHYNIX",
    "SPACEX": "SPCX",
    # Render
    "RNDR": "RENDER",
    # Polygon rename
    "MATIC": "POL",
    # Upside Perps (same underlying, different cost model)
    "BTC_UPSIDE": "BTC",
    "ETH_UPSIDE": "ETH",
    "SOL_UPSIDE": "SOL",
    "XRP_UPSIDE": "XRP",
    "HYPE_UPSIDE": "HYPE",
}

# Suffixes stripped from a venue-native market symbol, longest first.
# e.g. "BTC_USDT_Perp" -> "BTC", "BTC-USD.P" -> "BTC", "BTC-PERP" -> "BTC".
_QUOTE_UNITS = ("USDT", "USDC", "USD", "PERP", "P", "SWAP", "PERPETUAL")
_SEPARATORS = ("_", "-", "/", ".", ":")

# Wrapper prefixes are NOT stripped generically: `W` would destroy `WIF`, `WLD`
# and `WBT`. Wrappers live in ALIASES instead, which keeps the rule explicit.


def _strip_suffixes(symbol: str) -> str:
    """Peel trailing quote/product tokens off a venue-native market symbol."""
    parts = [symbol]
    for sep in _SEPARATORS:
        parts = [p for chunk in parts for p in chunk.split(sep)]
    parts = [p for p in parts if p]
    if not parts:
        return symbol

    while len(parts) > 1 and parts[-1].upper() in _QUOTE_UNITS:
        parts.pop()
    return parts[0]


def _is_fx_pair(symbol: str) -> str | None:
    """Return the canonical compound key (e.g. ``EURUSD``) if *symbol* is a forex pair.

    Avantis uses ``EUR/USD``; others use ``EURUSD``. Both must resolve to the same
    canonical key, and must NOT be split on ``/`` by ``_strip_suffixes``.

    >>> _is_fx_pair("EUR/USD")
    'EURUSD'
    >>> _is_fx_pair("USDJPY")
    'USDJPY'
    >>> _is_fx_pair("BTC")
    """
    up = symbol.upper()
    m = _FX_PAIR_RE.match(up)
    if not m:
        return None
    a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    if a in _KNOWN_FX_BASES or b in _KNOWN_FX_BASES:
        return f"{a}{b}"
    return None


def _strip_hip3_prefix(symbol: str) -> str:
    """Strip Hyperliquid HIP-3 ``<dex>:<market>`` namespace, keep the market.

    HIP-3 sub-DEX markets are always ``<short_lowercase_dex>:<market_symbol>``,
    e.g. ``xyz:BRENTOIL``, ``flx:SILVER``, ``hyna:BTC``. Without this step
    ``_strip_suffixes`` splits on the colon and returns the DEX prefix as the
    "base", which collapses every HIP-3 market on a DEX into one nonsense
    asset. See CONTRACT.md §12.5 for the read-side context.
    """
    if ":" not in symbol:
        return symbol
    head, _, tail = symbol.partition(":")
    if not tail:
        return symbol
    # Only strip when the prefix looks like a HIP-3 DEX name: short, alnum,
    # entirely lowercase. Anything else (e.g. ``BTC:PERP``) keeps the colon
    # so existing suffix-strip behaviour continues to apply.
    if 1 <= len(head) <= 8 and head.islower() and head.isalnum():
        return tail
    return symbol


def normalize_base_asset(symbol: str) -> str:
    """Normalize a venue-native symbol or ticker to a canonical base asset.

    >>> normalize_base_asset("BTC_USDT_Perp")
    'BTC'
    >>> normalize_base_asset("BTC-USD.P")
    'BTC'
    >>> normalize_base_asset("wBTC")
    'BTC'
    >>> normalize_base_asset("WIF")
    'WIF'
    >>> normalize_base_asset("EUR/USD")
    'EURUSD'
    >>> normalize_base_asset("USDJPY")
    'USDJPY'
    >>> normalize_base_asset("USD/JPY")
    'USDJPY'
    >>> normalize_base_asset("xyz:BRENTOIL")
    'BRENT'
    >>> normalize_base_asset("xyz:AAPL")
    'AAPL'
    """
    if not symbol:
        return ""
    stripped = _strip_hip3_prefix(symbol.strip())
    fx = _is_fx_pair(stripped)
    if fx is not None:
        return ALIASES.get(fx, fx)
    base = _strip_suffixes(stripped).upper()
    return ALIASES.get(base, base)


def normalize_quote_asset(symbol: str) -> str:
    """Normalize a quote/settlement ticker (`USD`, `USDT`, `USDC`, ...)."""
    if not symbol:
        return ""
    up = symbol.strip().upper()
    return ALIASES.get(up, up)
