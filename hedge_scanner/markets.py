"""Cross-venue perp market registry: which instruments exist where.

Hard feasibility layer for the hedge engine: you cannot hedge exposure to asset X
on venue Y unless venue Y lists a market for X. `can_hedge()` is the gate that the
router must pass before it bothers pricing anything.

Captured 2026-08-19 from live venue APIs:

  avantis   GET  https://prod-api.avantisfi.com/data/v2/trading
  grvt      POST https://market-data.grvt.io/full/v1/all_instruments  (+ /margin_rules)
  pacifica  GET  https://api.pacifica.fi/api/v1/info
  jupiter   GET  https://perps-api.jup.ag/v1/market-stats?mint=...   (mint enum)
  ondo      GET  https://api.ondoperps.xyz/v1/markets

Narrative version, including the Avantis hedgeability verdict, the symbol traps and
the index-multiplier mismatch: ../../market-overlap.md

TODO(refresh): listings change weekly -- Pacifica and GRVT add markets continuously and
  Avantis pauses pairs without removing the slot. Re-run the captures above and regenerate
  this module on a schedule (monthly at minimum). A stale registry fails CLOSED: it makes
  the engine skip a hedge that now exists, which is safe. The dangerous direction is a
  market that got paused after capture -- see AVANTIS_PAUSED for pairs already in that state.

Caveats carried over from capture (see market-overlap.md for detail):
  - GRVT and Pacifica asset classes are INFERRED from tickers; neither API exposes a class.
  - GRVT does not distinguish active from delisted; all 185 instruments are marked active.
  - Jupiter max leverage is from docs, not an API field. Its market list IS API-verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hedge_scanner.assets import normalize_base_asset

__all__ = [
    "MarketSpec",
    "VENUE_MARKETS",
    "ASSET_ALIASES",
    "AVANTIS_PAUSED",
    "SCALE_MISMATCH",
    "VENUES",
    "venues_listing",
    "can_hedge",
    "resolve_asset",
    "canonical_base",
    "same_asset",
    "market_for",
]

DATA_CAPTURED = "2026-08-19"

VENUES: tuple[str, ...] = ("avantis", "grvt", "pacifica", "jupiter", "ondo", "hyperliquid", "ostium")
"""Position sources are grvt/pacifica/jupiter/ondo/hyperliquid/ostium; avantis is the hedge destination."""


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """One tradeable perp market on one venue."""

    native_symbol: str
    """Venue-native identifier, exactly as the venue's API returns it."""

    asset_class: str
    """One of: crypto, equity, index, commodity, forex."""

    max_leverage: Decimal
    """Highest leverage the venue allows. For tiered-margin venues (GRVT, Ondo) this is
    the tier-1 (smallest-notional) figure; large positions get less."""

    contract_multiplier: Decimal = Decimal(1)
    """Base units per contract. 1000 for GRVT/Pacifica k-prefixed memecoin contracts.
    Does NOT capture index-vs-ETF scale differences -- those are in SCALE_MISMATCH,
    because the underlying differs rather than the unit size."""

    active: bool = True

    class_inferred: bool = False
    """True when asset_class was inferred from the ticker rather than read from the API."""

    upside_perp: str | None = None
    """Avantis only. Native symbol of the parallel Upside Perp, if one exists. Upside Perps
    charge no open, close or borrow fee and instead take a profit share, so they are a
    distinct hedge instrument on the same underlying -- not just a leverage tier."""


VENUE_MARKETS: dict[str, dict[str, MarketSpec]] = {
    "avantis": {
        "AAVE": MarketSpec("AAVE", "crypto", Decimal("20")),
        "AERO": MarketSpec("AERO", "crypto", Decimal("10")),
        "ARB": MarketSpec("ARB", "crypto", Decimal("20")),
        "ASTER": MarketSpec("ASTER", "crypto", Decimal("10")),
        "AVAX": MarketSpec("AVAX", "crypto", Decimal("20")),
        "AVNT": MarketSpec("AVNT", "crypto", Decimal("10")),
        "BERA": MarketSpec("BERA", "crypto", Decimal("10")),
        "BNB": MarketSpec("BNB", "crypto", Decimal("40")),
        "BTC": MarketSpec("BTC", "crypto", Decimal("50"), upside_perp="BTC_UPSIDE"),
        "DOGE": MarketSpec("DOGE", "crypto", Decimal("20")),
        "EIGEN": MarketSpec("EIGEN", "crypto", Decimal("20")),
        "ENA": MarketSpec("ENA", "crypto", Decimal("20")),
        "ETH": MarketSpec("ETH", "crypto", Decimal("50"), upside_perp="ETH_UPSIDE"),
        "ETHFI": MarketSpec("ETHFI", "crypto", Decimal("10")),
        "FARTCOIN": MarketSpec("FARTCOIN", "crypto", Decimal("20")),
        "HYPE": MarketSpec("HYPE", "crypto", Decimal("20"), upside_perp="HYPE_UPSIDE"),
        "INJ": MarketSpec("INJ", "crypto", Decimal("20")),
        "JUP": MarketSpec("JUP", "crypto", Decimal("10")),
        "KAITO": MarketSpec("KAITO", "crypto", Decimal("10")),
        "LDO": MarketSpec("LDO", "crypto", Decimal("20")),
        "LINK": MarketSpec("LINK", "crypto", Decimal("20")),
        "LIT": MarketSpec("LIT", "crypto", Decimal("10")),
        "MON": MarketSpec("MON", "crypto", Decimal("10")),
        "NEAR": MarketSpec("NEAR", "crypto", Decimal("20")),
        "ONDO": MarketSpec("ONDO", "crypto", Decimal("10")),
        "OP": MarketSpec("OP", "crypto", Decimal("5")),
        "PENDLE": MarketSpec("PENDLE", "crypto", Decimal("10")),
        "PENGU": MarketSpec("PENGU", "crypto", Decimal("20")),
        "POL": MarketSpec("POL", "crypto", Decimal("20")),
        "PUMP": MarketSpec("PUMP", "crypto", Decimal("5")),
        "RENDER": MarketSpec("RENDER", "crypto", Decimal("10")),
        "REZ": MarketSpec("REZ", "crypto", Decimal("10")),
        "SEI": MarketSpec("SEI", "crypto", Decimal("20")),
        "SOL": MarketSpec("SOL", "crypto", Decimal("25"), upside_perp="SOL_UPSIDE"),
        "SUI": MarketSpec("SUI", "crypto", Decimal("20")),
        "TAO": MarketSpec("TAO", "crypto", Decimal("10")),
        "TIA": MarketSpec("TIA", "crypto", Decimal("20")),
        "TRUMP": MarketSpec("TRUMP", "crypto", Decimal("20")),
        "VIRTUAL": MarketSpec("VIRTUAL", "crypto", Decimal("10")),
        "WIF": MarketSpec("WIF", "crypto", Decimal("20")),
        "WLD": MarketSpec("WLD", "crypto", Decimal("10")),
        "XMR": MarketSpec("XMR", "crypto", Decimal("10")),
        "XPL": MarketSpec("XPL", "crypto", Decimal("10")),
        "XRP": MarketSpec("XRP", "crypto", Decimal("20"), upside_perp="XRP_UPSIDE"),
        "ZEC": MarketSpec("ZEC", "crypto", Decimal("10")),
        "ZRO": MarketSpec("ZRO", "crypto", Decimal("10")),
        "AAPL": MarketSpec("AAPL", "equity", Decimal("5")),
        "AMD": MarketSpec("AMD", "equity", Decimal("3")),
        "AMZN": MarketSpec("AMZN", "equity", Decimal("5")),
        "AVGO": MarketSpec("AVGO", "equity", Decimal("10")),
        "BABA": MarketSpec("BABA", "equity", Decimal("2")),
        "BB": MarketSpec("BB", "equity", Decimal("10")),
        "CBRS": MarketSpec("CBRS", "equity", Decimal("2")),
        "COIN": MarketSpec("COIN", "equity", Decimal("5")),
        "CRCL": MarketSpec("CRCL", "equity", Decimal("2")),
        "CRWV": MarketSpec("CRWV", "equity", Decimal("10")),
        "EWY": MarketSpec("EWY", "equity", Decimal("2")),
        "GOOGL": MarketSpec("GOOG", "equity", Decimal("5")),
        "HOOD": MarketSpec("HOOD", "equity", Decimal("5")),
        "INTC": MarketSpec("INTC", "equity", Decimal("3")),
        "META": MarketSpec("META", "equity", Decimal("5")),
        "MRVL": MarketSpec("MRVL", "equity", Decimal("10")),
        "MSFT": MarketSpec("MSFT", "equity", Decimal("5")),
        "MSTR": MarketSpec("MSTR", "equity", Decimal("10")),
        "MU": MarketSpec("MU", "equity", Decimal("3")),
        "NFLX": MarketSpec("NFLX", "equity", Decimal("10")),
        "NVDA": MarketSpec("NVDA", "equity", Decimal("5")),
        "PLTR": MarketSpec("PLTR", "equity", Decimal("10")),
        "SKHYNIX": MarketSpec("SK HYNIX", "equity", Decimal("2")),
        "SNDK": MarketSpec("SNDK", "equity", Decimal("3")),
        "SPCX": MarketSpec("SPCX", "equity", Decimal("2")),
        "TSLA": MarketSpec("TSLA", "equity", Decimal("5")),
        "US100": MarketSpec("US100", "index", Decimal("25")),
        "US500": MarketSpec("US500", "index", Decimal("25")),
        "BRENT": MarketSpec("BRENT", "commodity", Decimal("25")),
        "WTI": MarketSpec("WTI", "commodity", Decimal("25")),
        "XAG": MarketSpec("XAG", "commodity", Decimal("75")),
        "XAU": MarketSpec("XAU", "commodity", Decimal("200")),
        "AUDUSD": MarketSpec("AUD/USD", "forex", Decimal("100")),
        "EURUSD": MarketSpec("EUR/USD", "forex", Decimal("500")),
        "GBPUSD": MarketSpec("GBP/USD", "forex", Decimal("100")),
        "NZDUSD": MarketSpec("NZD/USD", "forex", Decimal("100")),
        "USDCAD": MarketSpec("USD/CAD", "forex", Decimal("100")),
        "USDCHF": MarketSpec("USD/CHF", "forex", Decimal("100")),
        "USDCNH": MarketSpec("USD/CNH", "forex", Decimal("20")),
        "USDJPY": MarketSpec("USD/JPY", "forex", Decimal("500")),
        "USDSEK": MarketSpec("USD/SEK", "forex", Decimal("100")),
        "USDSGD": MarketSpec("USD/SGD", "forex", Decimal("100")),
        "USDKRW": MarketSpec("USD/KRW", "forex", Decimal("100")),
    },
    "grvt": {
        "AAVE": MarketSpec("AAVE_USDT_Perp", "crypto", Decimal("10")),
        "ADA": MarketSpec("ADA_USDT_Perp", "crypto", Decimal("10")),
        "AI16Z": MarketSpec("AI16Z_USDT_Perp", "crypto", Decimal("50")),
        "ARB": MarketSpec("ARB_USDT_Perp", "crypto", Decimal("10")),
        "ASTER": MarketSpec("ASTER_USDT_Perp", "crypto", Decimal("10")),
        "ATOM": MarketSpec("ATOM_USDT_Perp", "crypto", Decimal("10")),
        "AVAX": MarketSpec("AVAX_USDT_Perp", "crypto", Decimal("10")),
        "AVNT": MarketSpec("AVNT_USDT_Perp", "crypto", Decimal("10")),
        "AXS": MarketSpec("AXS_USDT_Perp", "crypto", Decimal("10")),
        "BARD": MarketSpec("BARD_USDT_Perp", "crypto", Decimal("10"), class_inferred=True),
        "BASED": MarketSpec("BASED_USDT_Perp", "crypto", Decimal("10")),
        "BCH": MarketSpec("BCH_USDT_Perp", "crypto", Decimal("10")),
        "BERA": MarketSpec("BERA_USDT_Perp", "crypto", Decimal("10")),
        "BLESS": MarketSpec("BLESS_USDT_Perp", "crypto", Decimal("10")),
        "BNB": MarketSpec("BNB_USDT_Perp", "crypto", Decimal("10")),
        "BONK": MarketSpec("KBONK_USDT_Perp", "crypto", Decimal("10"), Decimal("1000")),
        "BTC": MarketSpec("BTC_USDT_Perp", "crypto", Decimal("50")),
        "CC": MarketSpec("CC_USDT_Perp", "crypto", Decimal("10"), class_inferred=True),
        "CFX": MarketSpec("CFX_USDT_Perp", "crypto", Decimal("10")),
        "COAI": MarketSpec("COAI_USDT_Perp", "crypto", Decimal("10"), class_inferred=True),
        "CRV": MarketSpec("CRV_USDT_Perp", "crypto", Decimal("10")),
        "DOGE": MarketSpec("DOGE_USDT_Perp", "crypto", Decimal("10")),
        "DOT": MarketSpec("DOT_USDT_Perp", "crypto", Decimal("10")),
        "EDGE": MarketSpec("EDGE_USDT_Perp", "crypto", Decimal("10"), class_inferred=True),
        "EIGEN": MarketSpec("EIGEN_USDT_Perp", "crypto", Decimal("10")),
        "ENA": MarketSpec("ENA_USDT_Perp", "crypto", Decimal("10")),
        "ETH": MarketSpec("ETH_USDT_Perp", "crypto", Decimal("50")),
        "FARTCOIN": MarketSpec("FARTCOIN_USDT_Perp", "crypto", Decimal("10")),
        "FIL": MarketSpec("FIL_USDT_Perp", "crypto", Decimal("10")),
        "GIGGLE": MarketSpec("GIGGLE_USDT_Perp", "crypto", Decimal("10")),
        "GRVT": MarketSpec("GRVT_USDT_Perp", "crypto", Decimal("10")),
        "H": MarketSpec("H_USDT_Perp", "crypto", Decimal("10"), class_inferred=True),
        "HBAR": MarketSpec("HBAR_USDT_Perp", "crypto", Decimal("10")),
        "HYPE": MarketSpec("HYPE_USDT_Perp", "crypto", Decimal("10")),
        "ICP": MarketSpec("ICP_USDT_Perp", "crypto", Decimal("10")),
        "IP": MarketSpec("IP_USDT_Perp", "crypto", Decimal("10")),
        "JUP": MarketSpec("JUP_USDT_Perp", "crypto", Decimal("10")),
        "KAIA": MarketSpec("KAIA_USDT_Perp", "crypto", Decimal("10")),
        "KAITO": MarketSpec("KAITO_USDT_Perp", "crypto", Decimal("10")),
        "LA": MarketSpec("LA_USDT_Perp", "crypto", Decimal("10"), class_inferred=True),
        "LDO": MarketSpec("LDO_USDT_Perp", "crypto", Decimal("10")),
        "LINEA": MarketSpec("LINEA_USDT_Perp", "crypto", Decimal("10")),
        "LINK": MarketSpec("LINK_USDT_Perp", "crypto", Decimal("10")),
        "LIT": MarketSpec("LIT_USDT_Perp", "crypto", Decimal("10")),
        "LTC": MarketSpec("LTC_USDT_Perp", "crypto", Decimal("10")),
        "MEGA": MarketSpec("MEGA_USDT_Perp", "crypto", Decimal("10")),
        "MNT": MarketSpec("MNT_USDT_Perp", "crypto", Decimal("50")),
        "MON": MarketSpec("MON_USDT_Perp", "crypto", Decimal("10")),
        "MOODENG": MarketSpec("MOODENG_USDT_Perp", "crypto", Decimal("10")),
        "MORPHO": MarketSpec("MORPHO_USDT_Perp", "crypto", Decimal("10")),
        "NEAR": MarketSpec("NEAR_USDT_Perp", "crypto", Decimal("10")),
        "ONDO": MarketSpec("ONDO_USDT_Perp", "crypto", Decimal("10")),
        "OP": MarketSpec("OP_USDT_Perp", "crypto", Decimal("10")),
        "PAXG": MarketSpec("PAXG_USDT_Perp", "crypto", Decimal("10")),
        "PENDLE": MarketSpec("PENDLE_USDT_Perp", "crypto", Decimal("10")),
        "PENGU": MarketSpec("PENGU_USDT_Perp", "crypto", Decimal("10")),
        "PEPE": MarketSpec("KPEPE_USDT_Perp", "crypto", Decimal("10"), Decimal("1000")),
        "POL": MarketSpec("POL_USDT_Perp", "crypto", Decimal("10")),
        "POPCAT": MarketSpec("POPCAT_USDT_Perp", "crypto", Decimal("10")),
        "PROVE": MarketSpec("PROVE_USDT_Perp", "crypto", Decimal("10")),
        "PUMP": MarketSpec("PUMP_USDT_Perp", "crypto", Decimal("10")),
        "RESOLV": MarketSpec("RESOLV_USDT_Perp", "crypto", Decimal("10")),
        "SAHARA": MarketSpec("SAHARA_USDT_Perp", "crypto", Decimal("10")),
        "SEI": MarketSpec("SEI_USDT_Perp", "crypto", Decimal("10")),
        "SHIB": MarketSpec("KSHIB_USDT_Perp", "crypto", Decimal("10"), Decimal("1000")),
        "SOL": MarketSpec("SOL_USDT_Perp", "crypto", Decimal("20")),
        "STRK": MarketSpec("STRK_USDT_Perp", "crypto", Decimal("10")),
        "SUI": MarketSpec("SUI_USDT_Perp", "crypto", Decimal("10")),
        "TAO": MarketSpec("TAO_USDT_Perp", "crypto", Decimal("10")),
        "TON": MarketSpec("TON_USDT_Perp", "crypto", Decimal("10")),
        "TRUMP": MarketSpec("TRUMP_USDT_Perp", "crypto", Decimal("10")),
        "UNI": MarketSpec("UNI_USDT_Perp", "crypto", Decimal("10")),
        "VINE": MarketSpec("VINE_USDT_Perp", "crypto", Decimal("10")),
        "VIRTUAL": MarketSpec("VIRTUAL_USDT_Perp", "crypto", Decimal("10")),
        "W": MarketSpec("W_USDT_Perp", "crypto", Decimal("10")),
        "WIF": MarketSpec("WIF_USDT_Perp", "crypto", Decimal("10")),
        "WLD": MarketSpec("WLD_USDT_Perp", "crypto", Decimal("10")),
        "WLFI": MarketSpec("WLFI_USDT_Perp", "crypto", Decimal("10")),
        "XAUT": MarketSpec("XAUT_USDT_Perp", "crypto", Decimal("10")),
        "XLM": MarketSpec("XLM_USDT_Perp", "crypto", Decimal("10")),
        "XMR": MarketSpec("XMR_USDT_Perp", "crypto", Decimal("10")),
        "XPL": MarketSpec("XPL_USDT_Perp", "crypto", Decimal("10")),
        "XRP": MarketSpec("XRP_USDT_Perp", "crypto", Decimal("20")),
        "ZEC": MarketSpec("ZEC_USDT_Perp", "crypto", Decimal("10")),
        "ZEN": MarketSpec("ZEN_USDT_Perp", "crypto", Decimal("10")),
        "ZK": MarketSpec("ZK_USDT_Perp", "crypto", Decimal("10")),
        "AAOI": MarketSpec("AAOI_USDT_Perp", "equity", Decimal("10")),
        "AAPL": MarketSpec("AAPL_USDT_Perp", "equity", Decimal("10")),
        "AMAT": MarketSpec("AMAT_USDT_Perp", "equity", Decimal("10")),
        "AMD": MarketSpec("AMD_USDT_Perp", "equity", Decimal("10")),
        "AMZN": MarketSpec("AMZN_USDT_Perp", "equity", Decimal("10")),
        "ANTHROPIC": MarketSpec("ANTHROPIC_USDT_Perp", "equity", Decimal("10")),
        "ARM": MarketSpec("ARM_USDT_Perp", "equity", Decimal("10")),
        "ASML": MarketSpec("ASML_USDT_Perp", "equity", Decimal("10")),
        "ASTS": MarketSpec("ASTS_USDT_Perp", "equity", Decimal("10")),
        "AVGO": MarketSpec("AVGO_USDT_Perp", "equity", Decimal("10")),
        "BABA": MarketSpec("BABA_USDT_Perp", "equity", Decimal("10")),
        "BBX": MarketSpec("BBX_USDT_Perp", "equity", Decimal("10"), class_inferred=True),
        "BE": MarketSpec("BE_USDT_Perp", "equity", Decimal("10")),
        "BMNR": MarketSpec("BMNR_USDT_Perp", "equity", Decimal("10")),
        "BRKB": MarketSpec("BRKB_USDT_Perp", "equity", Decimal("10")),
        "CBRS": MarketSpec("CBRS_USDT_Perp", "equity", Decimal("10")),
        "COHR": MarketSpec("COHR_USDT_Perp", "equity", Decimal("10")),
        "COIN": MarketSpec("COIN_USDT_Perp", "equity", Decimal("10")),
        "CRCL": MarketSpec("CRCL_USDT_Perp", "equity", Decimal("10")),
        "CRM": MarketSpec("CRM_USDT_Perp", "equity", Decimal("10")),
        "CRWD": MarketSpec("CRWD_USDT_Perp", "equity", Decimal("10")),
        "CRWV": MarketSpec("CRWV_USDT_Perp", "equity", Decimal("10")),
        "CSCO": MarketSpec("CSCO_USDT_Perp", "equity", Decimal("10")),
        "DELL": MarketSpec("DELL_USDT_Perp", "equity", Decimal("10")),
        "DIS": MarketSpec("DIS_USDT_Perp", "equity", Decimal("10")),
        "FLNC": MarketSpec("FLNC_USDT_Perp", "equity", Decimal("10")),
        "GLW": MarketSpec("GLW_USDT_Perp", "equity", Decimal("10")),
        "GOOGL": MarketSpec("GOOGL_USDT_Perp", "equity", Decimal("10")),
        "GS": MarketSpec("GS_USDT_Perp", "equity", Decimal("10")),
        "HANMI": MarketSpec("HANMI_USDT_Perp", "equity", Decimal("10")),
        "HD": MarketSpec("HD_USDT_Perp", "equity", Decimal("10")),
        "HOOD": MarketSpec("HOOD_USDT_Perp", "equity", Decimal("10")),
        "HYUNDAI": MarketSpec("HYUNDAI_USDT_Perp", "equity", Decimal("10")),
        "IBM": MarketSpec("IBM_USDT_Perp", "equity", Decimal("10")),
        "INTC": MarketSpec("INTC_USDT_Perp", "equity", Decimal("10")),
        "IREN": MarketSpec("IREN_USDT_Perp", "equity", Decimal("10")),
        "JPM": MarketSpec("JPM_USDT_Perp", "equity", Decimal("10")),
        "LGELECTRONICS": MarketSpec("LGELECTRONICS_USDT_Perp", "equity", Decimal("10")),
        "LITE": MarketSpec("LITE_USDT_Perp", "equity", Decimal("10")),
        "LLY": MarketSpec("LLY_USDT_Perp", "equity", Decimal("10")),
        "META": MarketSpec("META_USDT_Perp", "equity", Decimal("10")),
        "MRVL": MarketSpec("MRVL_USDT_Perp", "equity", Decimal("10")),
        "MSFT": MarketSpec("MSFT_USDT_Perp", "equity", Decimal("10")),
        "MSTR": MarketSpec("MSTR_USDT_Perp", "equity", Decimal("10")),
        "MU": MarketSpec("MU_USDT_Perp", "equity", Decimal("10")),
        "NAVER": MarketSpec("NAVER_USDT_Perp", "equity", Decimal("10")),
        "NBIS": MarketSpec("NBIS_USDT_Perp", "equity", Decimal("10")),
        "NFLX": MarketSpec("NFLX_USDT_Perp", "equity", Decimal("10")),
        "NOK": MarketSpec("NOK_USDT_Perp", "equity", Decimal("10")),
        "NOW": MarketSpec("NOW_USDT_Perp", "equity", Decimal("10")),
        "NVDA": MarketSpec("NVDA_USDT_Perp", "equity", Decimal("10")),
        "NVO": MarketSpec("NVO_USDT_Perp", "equity", Decimal("10")),
        "ONDS": MarketSpec("ONDS_USDT_Perp", "equity", Decimal("10")),
        "OPENAI": MarketSpec("OPENAI_USDT_Perp", "equity", Decimal("10")),
        "ORCL": MarketSpec("ORCL_USDT_Perp", "equity", Decimal("10")),
        "PLTR": MarketSpec("PLTR_USDT_Perp", "equity", Decimal("10")),
        "PYPL": MarketSpec("PAYP_USDT_Perp", "equity", Decimal("10"), class_inferred=True),
        "QCOM": MarketSpec("QCOM_USDT_Perp", "equity", Decimal("10")),
        "QNTX": MarketSpec("QNTX_USDT_Perp", "equity", Decimal("10"), class_inferred=True),
        "RIVER": MarketSpec("RIVER_USDT_Perp", "equity", Decimal("10"), class_inferred=True),
        "RKLB": MarketSpec("RKLB_USDT_Perp", "equity", Decimal("10")),
        "SAMSUNG": MarketSpec("SAMSUNG_USDT_Perp", "equity", Decimal("10")),
        "SAMSUNGEM": MarketSpec("SAMSUNGEM_USDT_Perp", "equity", Decimal("10")),
        "SKHYNIX": MarketSpec("SKHY_USDT_Perp", "equity", Decimal("10")),
        "SNDK": MarketSpec("SNDK_USDT_Perp", "equity", Decimal("10")),
        "SPCX": MarketSpec("SPCX_USDT_Perp", "equity", Decimal("10")),
        "STRC": MarketSpec("STRC_USDT_Perp", "equity", Decimal("10"), class_inferred=True),
        "TSLA": MarketSpec("TSLA_USDT_Perp", "equity", Decimal("10")),
        "TSM": MarketSpec("TSM_USDT_Perp", "equity", Decimal("10")),
        "UBER": MarketSpec("UBER_USDT_Perp", "equity", Decimal("10")),
        "USAR": MarketSpec("USAR_USDT_Perp", "equity", Decimal("10"), class_inferred=True),
        "V": MarketSpec("V_USDT_Perp", "equity", Decimal("10")),
        "WDC": MarketSpec("WDC_USDT_Perp", "equity", Decimal("10")),
        "WMT": MarketSpec("WMT_USDT_Perp", "equity", Decimal("10")),
        "DRAM": MarketSpec("DRAM_USDT_Perp", "index", Decimal("10")),
        "EWJ": MarketSpec("EWJ_USDT_Perp", "index", Decimal("10")),
        "EWT": MarketSpec("EWT_USDT_Perp", "index", Decimal("10")),
        "EWY": MarketSpec("EWY_USDT_Perp", "index", Decimal("10")),
        "EWZ": MarketSpec("EWZ_USDT_Perp", "index", Decimal("10")),
        "IWM": MarketSpec("IWM_USDT_Perp", "index", Decimal("10")),
        "KODEX200": MarketSpec("KODEX200_USDT_Perp", "index", Decimal("10")),
        "KORU": MarketSpec("KORU_USDT_Perp", "index", Decimal("10")),
        "QQQ": MarketSpec("QQQ_USDT_Perp", "index", Decimal("10")),
        "SOXL": MarketSpec("SOXL_USDT_Perp", "index", Decimal("10")),
        "SPY": MarketSpec("SPY_USDT_Perp", "index", Decimal("10")),
        "STXX": MarketSpec("STXX_USDT_Perp", "index", Decimal("10"), class_inferred=True),
        "URNM": MarketSpec("URNM_USDT_Perp", "index", Decimal("10")),
        "UVXY": MarketSpec("UVXY_USDT_Perp", "index", Decimal("10")),
        "XLE": MarketSpec("XLE_USDT_Perp", "index", Decimal("10")),
        "BRENT": MarketSpec("BZ_USDT_Perp", "commodity", Decimal("25")),
        "COPPER": MarketSpec("COPPER_USDT_Perp", "commodity", Decimal("25")),
        "NATGAS": MarketSpec("NATGAS_USDT_Perp", "commodity", Decimal("25")),
        "WTI": MarketSpec("CL_USDT_Perp", "commodity", Decimal("25")),
        "XAG": MarketSpec("XAG_USDT_Perp", "commodity", Decimal("25")),
        "XAU": MarketSpec("XAU_USDT_Perp", "commodity", Decimal("25")),
        "XPD": MarketSpec("XPD_USDT_Perp", "commodity", Decimal("25")),
        "XPT": MarketSpec("XPT_USDT_Perp", "commodity", Decimal("25")),
    },
    "pacifica": {
        "2Z": MarketSpec("2Z", "crypto", Decimal("3"), class_inferred=True),
        "AAVE": MarketSpec("AAVE", "crypto", Decimal("10")),
        "ADA": MarketSpec("ADA", "crypto", Decimal("10")),
        "ARB": MarketSpec("ARB", "crypto", Decimal("10")),
        "ASTER": MarketSpec("ASTER", "crypto", Decimal("10")),
        "AVAX": MarketSpec("AVAX", "crypto", Decimal("10")),
        "BCH": MarketSpec("BCH", "crypto", Decimal("10")),
        "BNB": MarketSpec("BNB", "crypto", Decimal("20")),
        "BONK": MarketSpec("kBONK", "crypto", Decimal("10"), Decimal("1000")),
        "BP": MarketSpec("BP", "crypto", Decimal("3"), class_inferred=True),
        "BTC": MarketSpec("BTC", "crypto", Decimal("50")),
        "CHIP": MarketSpec("CHIP", "crypto", Decimal("3"), class_inferred=True),
        "CRV": MarketSpec("CRV", "crypto", Decimal("10")),
        "DOGE": MarketSpec("DOGE", "crypto", Decimal("20")),
        "ENA": MarketSpec("ENA", "crypto", Decimal("10")),
        "ETH": MarketSpec("ETH", "crypto", Decimal("50")),
        "FARTCOIN": MarketSpec("FARTCOIN", "crypto", Decimal("10")),
        "HYPE": MarketSpec("HYPE", "crypto", Decimal("20")),
        "ICP": MarketSpec("ICP", "crypto", Decimal("5")),
        "JUP": MarketSpec("JUP", "crypto", Decimal("10")),
        "KAITO": MarketSpec("KAITO", "crypto", Decimal("5")),
        "LDO": MarketSpec("LDO", "crypto", Decimal("10")),
        "LINK": MarketSpec("LINK", "crypto", Decimal("10")),
        "LIT": MarketSpec("LIT", "crypto", Decimal("10")),
        "LTC": MarketSpec("LTC", "crypto", Decimal("10")),
        "MEGA": MarketSpec("MEGA", "crypto", Decimal("3"), class_inferred=True),
        "MON": MarketSpec("MON", "crypto", Decimal("3")),
        "NEAR": MarketSpec("NEAR", "crypto", Decimal("10")),
        "PAXG": MarketSpec("PAXG", "crypto", Decimal("10")),
        "PENGU": MarketSpec("PENGU", "crypto", Decimal("5")),
        "PEPE": MarketSpec("kPEPE", "crypto", Decimal("10"), Decimal("1000")),
        "PIPPIN": MarketSpec("PIPPIN", "crypto", Decimal("3"), class_inferred=True),
        "PUMP": MarketSpec("PUMP", "crypto", Decimal("10")),
        "SHIB": MarketSpec("kSHIB", "crypto", Decimal("10"), Decimal("1000")),
        "SOL": MarketSpec("SOL", "crypto", Decimal("20")),
        "STRK": MarketSpec("STRK", "crypto", Decimal("5")),
        "SUI": MarketSpec("SUI", "crypto", Decimal("10")),
        "TAO": MarketSpec("TAO", "crypto", Decimal("10")),
        "TRUMP": MarketSpec("TRUMP", "crypto", Decimal("10")),
        "UNI": MarketSpec("UNI", "crypto", Decimal("10")),
        "VIRTUAL": MarketSpec("VIRTUAL", "crypto", Decimal("5")),
        "VVV": MarketSpec("VVV", "crypto", Decimal("10"), class_inferred=True),
        "WIF": MarketSpec("WIF", "crypto", Decimal("5")),
        "WLD": MarketSpec("WLD", "crypto", Decimal("5")),
        "WLFI": MarketSpec("WLFI", "crypto", Decimal("5")),
        "XMR": MarketSpec("XMR", "crypto", Decimal("10")),
        "XPL": MarketSpec("XPL", "crypto", Decimal("10")),
        "XRP": MarketSpec("XRP", "crypto", Decimal("20")),
        "ZEC": MarketSpec("ZEC", "crypto", Decimal("10")),
        "ZK": MarketSpec("ZK", "crypto", Decimal("5")),
        "ZRO": MarketSpec("ZRO", "crypto", Decimal("5")),
        "CRCL": MarketSpec("CRCL", "equity", Decimal("10")),
        "GOOGL": MarketSpec("GOOGL", "equity", Decimal("10")),
        "HOOD": MarketSpec("HOOD", "equity", Decimal("10")),
        "MSTR": MarketSpec("MSTR", "equity", Decimal("10")),
        "MU": MarketSpec("MU", "equity", Decimal("10")),
        "NVDA": MarketSpec("NVDA", "equity", Decimal("10")),
        "PLTR": MarketSpec("PLTR", "equity", Decimal("10")),
        "SAMSUNG": MarketSpec("SAMSUNG", "equity", Decimal("10")),
        "SKHYNIX": MarketSpec("SKHYNIX", "equity", Decimal("10")),
        "SNDK": MarketSpec("SNDK", "equity", Decimal("10")),
        "SPCX": MarketSpec("SPCX", "equity", Decimal("10")),
        "TSLA": MarketSpec("TSLA", "equity", Decimal("10")),
        "DRAM": MarketSpec("DRAM", "index", Decimal("10")),
        "URNM": MarketSpec("URNM", "index", Decimal("10")),
        "US500": MarketSpec("SP500", "index", Decimal("20")),
        "COPPER": MarketSpec("COPPER", "commodity", Decimal("10")),
        "NATGAS": MarketSpec("NATGAS", "commodity", Decimal("10")),
        "WTI": MarketSpec("CL", "commodity", Decimal("10")),
        "XAG": MarketSpec("XAG", "commodity", Decimal("10")),
        "XAU": MarketSpec("XAU", "commodity", Decimal("10")),
        "XPT": MarketSpec("PLATINUM", "commodity", Decimal("10")),
        "EURUSD": MarketSpec("EURUSD", "forex", Decimal("50")),
        "USDJPY": MarketSpec("USDJPY", "forex", Decimal("50")),
    },
    "jupiter": {
        "BTC": MarketSpec("wBTC", "crypto", Decimal("250")),
        "ETH": MarketSpec("ETH (wETH custody)", "crypto", Decimal("250")),
        "SOL": MarketSpec("SOL", "crypto", Decimal("250")),
    },
    "ondo": {
        "BTC": MarketSpec("BTC-USD.P", "crypto", Decimal("25")),
        "ETH": MarketSpec("ETH-USD.P", "crypto", Decimal("25")),
        "HYPE": MarketSpec("HYPE-USD.P", "crypto", Decimal("10")),
        "ONDO": MarketSpec("ONDO-USD.P", "crypto", Decimal("10")),
        "SOL": MarketSpec("SOL-USD.P", "crypto", Decimal("10")),
        "AAPL": MarketSpec("AAPL-USD.P", "equity", Decimal("20")),
        "AMD": MarketSpec("AMD-USD.P", "equity", Decimal("10")),
        "AMZN": MarketSpec("AMZN-USD.P", "equity", Decimal("10")),
        "ARM": MarketSpec("ARM-USD.P", "equity", Decimal("10")),
        "AVGO": MarketSpec("AVGO-USD.P", "equity", Decimal("10")),
        "BABA": MarketSpec("BABA-USD.P", "equity", Decimal("10")),
        "BB": MarketSpec("BB-USD.P", "equity", Decimal("10")),
        "CBRS": MarketSpec("CBRS-USD.P", "equity", Decimal("10")),
        "COIN": MarketSpec("COIN-USD.P", "equity", Decimal("10")),
        "CRCL": MarketSpec("CRCL-USD.P", "equity", Decimal("10")),
        "CRWV": MarketSpec("CRWV-USD.P", "equity", Decimal("10")),
        "CXMT": MarketSpec("CXMT-USD.P", "equity", Decimal("10")),
        "GLW": MarketSpec("GLW-USD.P", "equity", Decimal("10")),
        "GOOGL": MarketSpec("GOOGL-USD.P", "equity", Decimal("10")),
        "HOOD": MarketSpec("HOOD-USD.P", "equity", Decimal("10")),
        "IBM": MarketSpec("IBM-USD.P", "equity", Decimal("10")),
        "INTC": MarketSpec("INTC-USD.P", "equity", Decimal("10")),
        "LITE": MarketSpec("LITE-USD.P", "equity", Decimal("10")),
        "META": MarketSpec("META-USD.P", "equity", Decimal("10")),
        "MRVL": MarketSpec("MRVL-USD.P", "equity", Decimal("10")),
        "MSFT": MarketSpec("MSFT-USD.P", "equity", Decimal("10")),
        "MSTR": MarketSpec("MSTR-USD.P", "equity", Decimal("10")),
        "MU": MarketSpec("MU-USD.P", "equity", Decimal("10")),
        "NBIS": MarketSpec("NBIS-USD.P", "equity", Decimal("10")),
        "NFLX": MarketSpec("NFLX-USD.P", "equity", Decimal("10")),
        "NVDA": MarketSpec("NVDA-USD.P", "equity", Decimal("10")),
        "ORCL": MarketSpec("ORCL-USD.P", "equity", Decimal("10")),
        "PLTR": MarketSpec("PLTR-USD.P", "equity", Decimal("10")),
        "SAMSUNG": MarketSpec("SMSN-USD.P", "equity", Decimal("10")),
        "SKHYNIX": MarketSpec("SKHY-USD.P", "equity", Decimal("10")),
        "SNDK": MarketSpec("SNDK-USD.P", "equity", Decimal("5")),
        "SPCX": MarketSpec("SPCX-USD.P", "equity", Decimal("10")),
        "TSLA": MarketSpec("TSLA-USD.P", "equity", Decimal("10")),
        "TSM": MarketSpec("TSM-USD.P", "equity", Decimal("10")),
        "DRAM": MarketSpec("DRAM-USD.P", "index", Decimal("10")),
        "EWY": MarketSpec("EWY-USD.P", "index", Decimal("10")),
        "QQQ": MarketSpec("QQQ-USD.P", "index", Decimal("20")),
        "SOXL": MarketSpec("SOXL-USD.P", "index", Decimal("10")),
        "SPY": MarketSpec("SPY-USD.P", "index", Decimal("20")),
        "US100": MarketSpec("US100-USD.P", "index", Decimal("25")),
        "US500": MarketSpec("US500-USD.P", "index", Decimal("25")),
        "BRENT": MarketSpec("BRENT-USD.P", "commodity", Decimal("15")),
        "COPPER": MarketSpec("COPPER-USD.P", "commodity", Decimal("15")),
        "NATGAS": MarketSpec("NATGAS-USD.P", "commodity", Decimal("10")),
        "WTI": MarketSpec("WTI-USD.P", "commodity", Decimal("20")),
        "XAG": MarketSpec("XAG-USD.P", "commodity", Decimal("25")),
        "XAU": MarketSpec("XAU-USD.P", "commodity", Decimal("25")),
    },
    "hyperliquid": {
        # Native DEX: crypto perps from POST meta (2026-08-19). HIP-3 xyz FX
        # verified 2026-09-04: xyz:EUR mid ~1.16 (EURUSD), xyz:GBP ~1.35 (GBPUSD),
        # xyz:JPY ~156 (USDJPY), xyz:KRW ~1356 (USDKRW). Not EURGBP — that cross
        # is not a xyz market.
        "EURUSD": MarketSpec("xyz:EUR", "forex", Decimal("20")),
        "GBPUSD": MarketSpec("xyz:GBP", "forex", Decimal("20")),
        "USDJPY": MarketSpec("xyz:JPY", "forex", Decimal("20")),
        "USDKRW": MarketSpec("xyz:KRW", "forex", Decimal("20")),
        "XAU": MarketSpec("xyz:GOLD", "commodity", Decimal("20")),
        "XAG": MarketSpec("xyz:SILVER", "commodity", Decimal("20")),
        "WTI": MarketSpec("xyz:CL", "commodity", Decimal("20")),
        "BRENT": MarketSpec("xyz:BRENTOIL", "commodity", Decimal("20")),
        "0G": MarketSpec("0G", "crypto", Decimal("3")),
        "2Z": MarketSpec("2Z", "crypto", Decimal("3")),
        "AAVE": MarketSpec("AAVE", "crypto", Decimal("10")),
        "ACE": MarketSpec("ACE", "crypto", Decimal("3")),
        "ADA": MarketSpec("ADA", "crypto", Decimal("10")),
        "AERO": MarketSpec("AERO", "crypto", Decimal("3")),
        "AIXBT": MarketSpec("AIXBT", "crypto", Decimal("3")),
        "ALGO": MarketSpec("ALGO", "crypto", Decimal("5")),
        "ALT": MarketSpec("ALT", "crypto", Decimal("3")),
        "ANIME": MarketSpec("ANIME", "crypto", Decimal("3")),
        "APE": MarketSpec("APE", "crypto", Decimal("5")),
        "APEX": MarketSpec("APEX", "crypto", Decimal("3")),
        "APT": MarketSpec("APT", "crypto", Decimal("10")),
        "AR": MarketSpec("AR", "crypto", Decimal("5")),
        "ARB": MarketSpec("ARB", "crypto", Decimal("10")),
        "ASTER": MarketSpec("ASTER", "crypto", Decimal("5")),
        "ATOM": MarketSpec("ATOM", "crypto", Decimal("5")),
        "AVAX": MarketSpec("AVAX", "crypto", Decimal("10")),
        "AVNT": MarketSpec("AVNT", "crypto", Decimal("5")),
        "AXS": MarketSpec("AXS", "crypto", Decimal("5")),
        "AZTEC": MarketSpec("AZTEC", "crypto", Decimal("3")),
        "BABY": MarketSpec("BABY", "crypto", Decimal("3")),
        "BANANA": MarketSpec("BANANA", "crypto", Decimal("3")),
        "BCH": MarketSpec("BCH", "crypto", Decimal("10")),
        "BERA": MarketSpec("BERA", "crypto", Decimal("5")),
        "BIGTIME": MarketSpec("BIGTIME", "crypto", Decimal("3")),
        "BIO": MarketSpec("BIO", "crypto", Decimal("3")),
        "BLUR": MarketSpec("BLUR", "crypto", Decimal("3")),
        "BNB": MarketSpec("BNB", "crypto", Decimal("10")),
        "BOME": MarketSpec("BOME", "crypto", Decimal("3")),
        "BONK": MarketSpec("kBONK", "crypto", Decimal("10"), Decimal("1000")),
        "BRETT": MarketSpec("BRETT", "crypto", Decimal("3")),
        "BSV": MarketSpec("BSV", "crypto", Decimal("3")),
        "BTC": MarketSpec("BTC", "crypto", Decimal("40")),
        "CAKE": MarketSpec("CAKE", "crypto", Decimal("3")),
        "CASHCAT": MarketSpec("CASHCAT", "crypto", Decimal("3")),
        "CC": MarketSpec("CC", "crypto", Decimal("3")),
        "CELO": MarketSpec("CELO", "crypto", Decimal("3")),
        "CFX": MarketSpec("CFX", "crypto", Decimal("5")),
        "CHIP": MarketSpec("CHIP", "crypto", Decimal("3")),
        "COMP": MarketSpec("COMP", "crypto", Decimal("5")),
        "CRV": MarketSpec("CRV", "crypto", Decimal("10")),
        "DASH": MarketSpec("DASH", "crypto", Decimal("5")),
        "DOGE": MarketSpec("DOGE", "crypto", Decimal("10")),
        "DOT": MarketSpec("DOT", "crypto", Decimal("10")),
        "DYDX": MarketSpec("DYDX", "crypto", Decimal("5")),
        "DYM": MarketSpec("DYM", "crypto", Decimal("3")),
        "EIGEN": MarketSpec("EIGEN", "crypto", Decimal("5")),
        "ENA": MarketSpec("ENA", "crypto", Decimal("10")),
        "ENS": MarketSpec("ENS", "crypto", Decimal("5")),
        "ETC": MarketSpec("ETC", "crypto", Decimal("5")),
        "ETH": MarketSpec("ETH", "crypto", Decimal("25")),
        "ETHFI": MarketSpec("ETHFI", "crypto", Decimal("5")),
        "FARTCOIN": MarketSpec("FARTCOIN", "crypto", Decimal("10")),
        "FET": MarketSpec("FET", "crypto", Decimal("5")),
        "FIL": MarketSpec("FIL", "crypto", Decimal("5")),
        "FLOKI": MarketSpec("kFLOKI", "crypto", Decimal("5"), Decimal("1000")),
        "FOGO": MarketSpec("FOGO", "crypto", Decimal("3")),
        "GALA": MarketSpec("GALA", "crypto", Decimal("3")),
        "GAS": MarketSpec("GAS", "crypto", Decimal("3")),
        "GMT": MarketSpec("GMT", "crypto", Decimal("3")),
        "GMX": MarketSpec("GMX", "crypto", Decimal("3")),
        "GOAT": MarketSpec("GOAT", "crypto", Decimal("3")),
        "GRAM": MarketSpec("GRAM", "crypto", Decimal("5")),
        "GRASS": MarketSpec("GRASS", "crypto", Decimal("3")),
        "GRIFFAIN": MarketSpec("GRIFFAIN", "crypto", Decimal("3")),
        "HBAR": MarketSpec("HBAR", "crypto", Decimal("5")),
        "HEMI": MarketSpec("HEMI", "crypto", Decimal("3")),
        "HMSTR": MarketSpec("HMSTR", "crypto", Decimal("3")),
        "HYPE": MarketSpec("HYPE", "crypto", Decimal("10")),
        "HYPER": MarketSpec("HYPER", "crypto", Decimal("3")),
        "ICP": MarketSpec("ICP", "crypto", Decimal("5")),
        "IMX": MarketSpec("IMX", "crypto", Decimal("5")),
        "INIT": MarketSpec("INIT", "crypto", Decimal("3")),
        "INJ": MarketSpec("INJ", "crypto", Decimal("5")),
        "IO": MarketSpec("IO", "crypto", Decimal("3")),
        "IOTA": MarketSpec("IOTA", "crypto", Decimal("3")),
        "JTO": MarketSpec("JTO", "crypto", Decimal("5")),
        "JUP": MarketSpec("JUP", "crypto", Decimal("10")),
        "KAITO": MarketSpec("KAITO", "crypto", Decimal("5")),
        "KAS": MarketSpec("KAS", "crypto", Decimal("3")),
        "LAYER": MarketSpec("LAYER", "crypto", Decimal("3")),
        "LDO": MarketSpec("LDO", "crypto", Decimal("5")),
        "LINEA": MarketSpec("LINEA", "crypto", Decimal("3")),
        "LINK": MarketSpec("LINK", "crypto", Decimal("10")),
        "LIT": MarketSpec("LIT", "crypto", Decimal("5")),
        "LTC": MarketSpec("LTC", "crypto", Decimal("10")),
        "LUNC": MarketSpec("kLUNC", "crypto", Decimal("3"), Decimal("1000")),
        "MANTA": MarketSpec("MANTA", "crypto", Decimal("3")),
        "ME": MarketSpec("ME", "crypto", Decimal("3")),
        "MEGA": MarketSpec("MEGA", "crypto", Decimal("3")),
        "MELANIA": MarketSpec("MELANIA", "crypto", Decimal("3")),
        "MEME": MarketSpec("MEME", "crypto", Decimal("3")),
        "MERL": MarketSpec("MERL", "crypto", Decimal("3")),
        "MET": MarketSpec("MET", "crypto", Decimal("3")),
        "MINA": MarketSpec("MINA", "crypto", Decimal("3")),
        "MNT": MarketSpec("MNT", "crypto", Decimal("5")),
        "MON": MarketSpec("MON", "crypto", Decimal("5")),
        "MOODENG": MarketSpec("MOODENG", "crypto", Decimal("3")),
        "MORPHO": MarketSpec("MORPHO", "crypto", Decimal("5")),
        "MOVE": MarketSpec("MOVE", "crypto", Decimal("3")),
        "NEAR": MarketSpec("NEAR", "crypto", Decimal("10")),
        "NEIRO": MarketSpec("kNEIRO", "crypto", Decimal("3"), Decimal("1000")),
        "NEO": MarketSpec("NEO", "crypto", Decimal("5")),
        "NIL": MarketSpec("NIL", "crypto", Decimal("3")),
        "NOT": MarketSpec("NOT", "crypto", Decimal("3")),
        "NXPC": MarketSpec("NXPC", "crypto", Decimal("3")),
        "ONDO": MarketSpec("ONDO", "crypto", Decimal("10")),
        "OP": MarketSpec("OP", "crypto", Decimal("5")),
        "ORDI": MarketSpec("ORDI", "crypto", Decimal("3")),
        "PAXG": MarketSpec("PAXG", "crypto", Decimal("10")),
        "PENDLE": MarketSpec("PENDLE", "crypto", Decimal("5")),
        "PENGU": MarketSpec("PENGU", "crypto", Decimal("5")),
        "PEOPLE": MarketSpec("PEOPLE", "crypto", Decimal("3")),
        "PEPE": MarketSpec("kPEPE", "crypto", Decimal("10"), Decimal("1000")),
        "PNUT": MarketSpec("PNUT", "crypto", Decimal("3")),
        "POL": MarketSpec("POL", "crypto", Decimal("5")),
        "POLYX": MarketSpec("POLYX", "crypto", Decimal("3")),
        "POPCAT": MarketSpec("POPCAT", "crypto", Decimal("3")),
        "PROVE": MarketSpec("PROVE", "crypto", Decimal("3")),
        "PUMP": MarketSpec("PUMP", "crypto", Decimal("10")),
        "PURR": MarketSpec("PURR", "crypto", Decimal("3")),
        "PYTH": MarketSpec("PYTH", "crypto", Decimal("5")),
        "RENDER": MarketSpec("RENDER", "crypto", Decimal("5")),
        "RESOLV": MarketSpec("RESOLV", "crypto", Decimal("3")),
        "REZ": MarketSpec("REZ", "crypto", Decimal("3")),
        "RSR": MarketSpec("RSR", "crypto", Decimal("3")),
        "RUNE": MarketSpec("RUNE", "crypto", Decimal("5")),
        "S": MarketSpec("S", "crypto", Decimal("5")),
        "SAGA": MarketSpec("SAGA", "crypto", Decimal("3")),
        "SAND": MarketSpec("SAND", "crypto", Decimal("5")),
        "SEI": MarketSpec("SEI", "crypto", Decimal("5")),
        "SHIB": MarketSpec("kSHIB", "crypto", Decimal("10"), Decimal("1000")),
        "SKR": MarketSpec("SKR", "crypto", Decimal("3")),
        "SKY": MarketSpec("SKY", "crypto", Decimal("3")),
        "SNX": MarketSpec("SNX", "crypto", Decimal("3")),
        "SOL": MarketSpec("SOL", "crypto", Decimal("20")),
        "SOPH": MarketSpec("SOPH", "crypto", Decimal("3")),
        "US500": MarketSpec("SPX", "crypto", Decimal("5")),
        "STBL": MarketSpec("STBL", "crypto", Decimal("3")),
        "STRK": MarketSpec("STRK", "crypto", Decimal("5")),
        "STX": MarketSpec("STX", "crypto", Decimal("5")),
        "SUI": MarketSpec("SUI", "crypto", Decimal("10")),
        "SUPER": MarketSpec("SUPER", "crypto", Decimal("3")),
        "SUSHI": MarketSpec("SUSHI", "crypto", Decimal("3")),
        "SYRUP": MarketSpec("SYRUP", "crypto", Decimal("3")),
        "TAO": MarketSpec("TAO", "crypto", Decimal("5")),
        "TIA": MarketSpec("TIA", "crypto", Decimal("5")),
        "TNSR": MarketSpec("TNSR", "crypto", Decimal("3")),
        "TRB": MarketSpec("TRB", "crypto", Decimal("3")),
        "TRUMP": MarketSpec("TRUMP", "crypto", Decimal("10")),
        "TRX": MarketSpec("TRX", "crypto", Decimal("10")),
        "TURBO": MarketSpec("TURBO", "crypto", Decimal("3")),
        "UMA": MarketSpec("UMA", "crypto", Decimal("3")),
        "UNI": MarketSpec("UNI", "crypto", Decimal("10")),
        "USUAL": MarketSpec("USUAL", "crypto", Decimal("3")),
        "VINE": MarketSpec("VINE", "crypto", Decimal("3")),
        "VIRTUAL": MarketSpec("VIRTUAL", "crypto", Decimal("5")),
        "VVV": MarketSpec("VVV", "crypto", Decimal("3")),
        "W": MarketSpec("W", "crypto", Decimal("5")),
        "WCT": MarketSpec("WCT", "crypto", Decimal("3")),
        "WIF": MarketSpec("WIF", "crypto", Decimal("5")),
        "WLD": MarketSpec("WLD", "crypto", Decimal("10")),
        "WLFI": MarketSpec("WLFI", "crypto", Decimal("5")),
        "XAI": MarketSpec("XAI", "crypto", Decimal("3")),
        "XLM": MarketSpec("XLM", "crypto", Decimal("5")),
        "XMR": MarketSpec("XMR", "crypto", Decimal("5")),
        "XPL": MarketSpec("XPL", "crypto", Decimal("10")),
        "XRP": MarketSpec("XRP", "crypto", Decimal("20")),
        "YGG": MarketSpec("YGG", "crypto", Decimal("3")),
        "ZEC": MarketSpec("ZEC", "crypto", Decimal("10")),
        "ZEN": MarketSpec("ZEN", "crypto", Decimal("5")),
        "ZETA": MarketSpec("ZETA", "crypto", Decimal("3")),
        "ZK": MarketSpec("ZK", "crypto", Decimal("5")),
        "ZORA": MarketSpec("ZORA", "crypto", Decimal("3")),
        "ZRO": MarketSpec("ZRO", "crypto", Decimal("5")),
    },
    "ostium": {
        # 75 active perp markets on Arbitrum. Captured from docs.ostium.com 2026-08-29.
        # Asset classes: crypto, stocks, ETFs, commodities, indices, forex.
        # Opening fees vary 3–10 bps by asset class; no closing fee.
        # Crypto
        "BTC": MarketSpec("BTC/USD", "crypto", Decimal("250")),
        "ETH": MarketSpec("ETH/USD", "crypto", Decimal("250")),
        "SOL": MarketSpec("SOL/USD", "crypto", Decimal("150")),
        "DOGE": MarketSpec("DOGE/USD", "crypto", Decimal("100")),
        "XRP": MarketSpec("XRP/USD", "crypto", Decimal("100")),
        "AVAX": MarketSpec("AVAX/USD", "crypto", Decimal("75")),
        "LINK": MarketSpec("LINK/USD", "crypto", Decimal("75")),
        "ADA": MarketSpec("ADA/USD", "crypto", Decimal("75")),
        "MATIC": MarketSpec("MATIC/USD", "crypto", Decimal("75")),
        "DOT": MarketSpec("DOT/USD", "crypto", Decimal("50")),
        "UNI": MarketSpec("UNI/USD", "crypto", Decimal("50")),
        "NEAR": MarketSpec("NEAR/USD", "crypto", Decimal("50")),
        "ARB": MarketSpec("ARB/USD", "crypto", Decimal("50")),
        "OP": MarketSpec("OP/USD", "crypto", Decimal("50")),
        # Forex
        "EURUSD": MarketSpec("EUR/USD", "forex", Decimal("500")),
        "GBPUSD": MarketSpec("GBP/USD", "forex", Decimal("500")),
        "USDJPY": MarketSpec("USD/JPY", "forex", Decimal("500")),
        "AUDUSD": MarketSpec("AUD/USD", "forex", Decimal("500")),
        "USDCAD": MarketSpec("USD/CAD", "forex", Decimal("500")),
        "USDCHF": MarketSpec("USD/CHF", "forex", Decimal("500")),
        "EURGBP": MarketSpec("EUR/GBP", "forex", Decimal("500")),
        "EURJPY": MarketSpec("EUR/JPY", "forex", Decimal("500")),
        "GBPJPY": MarketSpec("GBP/JPY", "forex", Decimal("500")),
        "NZDUSD": MarketSpec("NZD/USD", "forex", Decimal("500")),
        # Commodities
        "XAU": MarketSpec("XAU/USD", "commodity", Decimal("250")),
        "XAG": MarketSpec("XAG/USD", "commodity", Decimal("150")),
        "WTI": MarketSpec("WTI/USD", "commodity", Decimal("100")),
        "BRENT": MarketSpec("BRENT/USD", "commodity", Decimal("100")),
        "NATGAS": MarketSpec("NATGAS/USD", "commodity", Decimal("75")),
        "COPPER": MarketSpec("COPPER/USD", "commodity", Decimal("75")),
        # Indices
        "US500": MarketSpec("US500/USD", "index", Decimal("150")),
        "US100": MarketSpec("US100/USD", "index", Decimal("150")),
        "US30": MarketSpec("US30/USD", "index", Decimal("150")),
        "EU50": MarketSpec("EU50/USD", "index", Decimal("100")),
        "JP225": MarketSpec("JP225/USD", "index", Decimal("100")),
        "UK100": MarketSpec("UK100/USD", "index", Decimal("100")),
        "GER40": MarketSpec("GER40/USD", "index", Decimal("100")),
        # Stocks
        "AAPL": MarketSpec("AAPL/USD", "equity", Decimal("50")),
        "MSFT": MarketSpec("MSFT/USD", "equity", Decimal("50")),
        "AMZN": MarketSpec("AMZN/USD", "equity", Decimal("50")),
        "GOOGL": MarketSpec("GOOGL/USD", "equity", Decimal("50")),
        "TSLA": MarketSpec("TSLA/USD", "equity", Decimal("50")),
        "NVDA": MarketSpec("NVDA/USD", "equity", Decimal("50")),
        "META": MarketSpec("META/USD", "equity", Decimal("50")),
        "NFLX": MarketSpec("NFLX/USD", "equity", Decimal("50")),
        "AMD": MarketSpec("AMD/USD", "equity", Decimal("50")),
        "COIN": MarketSpec("COIN/USD", "equity", Decimal("50")),
        # ETFs
        "SPY": MarketSpec("SPY/USD", "equity", Decimal("50")),
        "QQQ": MarketSpec("QQQ/USD", "equity", Decimal("50")),
        "IWM": MarketSpec("IWM/USD", "equity", Decimal("50")),
        "GLD": MarketSpec("GLD/USD", "equity", Decimal("50")),
        "TLT": MarketSpec("TLT/USD", "equity", Decimal("50")),
    },
}

AVANTIS_PAUSED: dict[str, MarketSpec] = {
    # Pair slots that exist in the Avantis registry but carry a closed-all-week schedule
    # (";C,C,C,C,C,C,C;"), no feed asset type and zero open interest. Third-party trackers
    # still list several of these as tradeable. Treat as NOT hedgeable, but keep them here
    # so the engine can say "paused, may relist" instead of "never existed".
    "APE": MarketSpec("APE", "unknown", Decimal("10"), Decimal(1), False),
    "APT": MarketSpec("APT", "unknown", Decimal("15"), Decimal(1), False),
    "ARKM": MarketSpec("ARKM", "unknown", Decimal("10"), Decimal(1), False),
    "BONK": MarketSpec("BONK", "unknown", Decimal("20"), Decimal(1), False),
    "BRETT": MarketSpec("BRETT", "unknown", Decimal("5"), Decimal(1), False),
    "CHILLGUY": MarketSpec("CHILLGUY", "unknown", Decimal("5"), Decimal(1), False),
    "DYM": MarketSpec("DYM", "unknown", Decimal("10"), Decimal(1), False),
    "FET": MarketSpec("FET", "unknown", Decimal("100"), Decimal(1), False),
    "GOAT": MarketSpec("GOAT", "unknown", Decimal("5"), Decimal(1), False),
    "ORDI": MarketSpec("ORDI", "unknown", Decimal("10"), Decimal(1), False),
    "PEPE": MarketSpec("PEPE", "unknown", Decimal("20"), Decimal(1), False),
    "POPCAT": MarketSpec("POPCAT", "unknown", Decimal("10"), Decimal(1), False),
    "SHIB": MarketSpec("SHIB", "unknown", Decimal("20"), Decimal(1), False),
    "STX": MarketSpec("STX", "unknown", Decimal("10"), Decimal(1), False),
    "USD/BRL": MarketSpec("USD/BRL", "unknown", Decimal("20"), Decimal(1), False),
    "USD/IDR": MarketSpec("USD/IDR", "unknown", Decimal("20"), Decimal(1), False),
    "USD/INR": MarketSpec("USD/INR", "unknown", Decimal("20"), Decimal(1), False),
    "USD/KRW": MarketSpec("USD/KRW", "unknown", Decimal("50"), Decimal(1), False),
    "USD/MXN": MarketSpec("USD/MXN", "unknown", Decimal("20"), Decimal(1), False),
    "USD/TRY": MarketSpec("USD/TRY", "unknown", Decimal("20"), Decimal(1), False),
    "USD/TWD": MarketSpec("USD/TWD", "unknown", Decimal("20"), Decimal(1), False),
    "USD/ZAR": MarketSpec("USD/ZAR", "unknown", Decimal("20"), Decimal(1), False),
    "ZK": MarketSpec("ZK", "unknown", Decimal("10"), Decimal(1), False),
    "ZORA": MarketSpec("ZORA", "unknown", Decimal("10"), Decimal(1), False),
}

ASSET_ALIASES: dict[str, str] = {
    # Alias -> canonical base asset. Reconcile with / merge into hedge_scanner/assets.py,
    # which owns generic suffix stripping; this table owns the cross-venue collisions.
    # 1000x memecoin contracts -- see contract_multiplier, the alias only fixes the NAME
    "KPEPE": "PEPE",
    "1000PEPE": "PEPE",
    "KBONK": "BONK",
    "1000BONK": "BONK",
    "KSHIB": "SHIB",
    "1000SHIB": "SHIB",
    # Metals
    "GOLD": "XAU",
    "XAUUSD": "XAU",
    "GC": "XAU",
    "SILVER": "XAG",
    "XAGUSD": "XAG",
    "SI": "XAG",
    "PLATINUM": "XPT",
    "XPTUSD": "XPT",
    "PALLADIUM": "XPD",
    # Energy
    "CL": "WTI",
    "USOIL": "WTI",
    "USOILSPOT": "WTI",
    "WTIV6": "WTI",
    "CRUDE": "WTI",
    "BZ": "BRENT",
    "BRENTV6": "BRENT",
    "UKOIL": "BRENT",
    "NG": "NATGAS",
    "NATURALGAS": "NATGAS",
    "HG": "COPPER",
    # Equity indices. WARNING: canonicalizing SP500->US500 merges an index-scale market with an ETF-scale one. Check SCALE_MISMATCH before sizing.
    "SPX": "US500",
    "SP500": "US500",
    "SPX500": "US500",
    "ES": "US500",
    "NDX": "US100",
    "NAS100": "US100",
    "NASDAQ100": "US100",
    "NQ": "US100",
    # Share classes and ticker spellings
    "GOOG": "GOOGL",
    "BRK.B": "BRKB",
    "SKHY": "SKHYNIX",
    "SK HYNIX": "SKHYNIX",
    "000660": "SKHYNIX",
    "SMSN": "SAMSUNG",
    "005930": "SAMSUNG",
    "SPACEX": "SPCX",
    "PAYP": "PYPL",
    "LGELEC": "LGELECTRONICS",
    # Forex -- Avantis stores these as separate from/to fields
    # Forex -- Avantis stores these as separate from/to fields.
    # HIP-3 (xyz:EUR, xyz:GBP, xyz:JPY) stamps the USD-quoted currency only;
    # compound crosses (GBPJPY, EURGBP) stay two-legged and must not hit these.
    "EUR/USD": "EURUSD",
    "EUR": "EURUSD",
    "GBP/USD": "GBPUSD",
    "GBP": "GBPUSD",
    "AUD/USD": "AUDUSD",
    "AUD": "AUDUSD",
    "NZD/USD": "NZDUSD",
    "NZD": "NZDUSD",
    "USD/JPY": "USDJPY",
    "JPY": "USDJPY",
    "USD/CAD": "USDCAD",
    "CAD": "USDCAD",
    "USD/CHF": "USDCHF",
    "CHF": "USDCHF",
    "USD/SEK": "USDSEK",
    "SEK": "USDSEK",
    "USD/SGD": "USDSGD",
    "SGD": "USDSGD",
    "USD/CNH": "USDCNH",
    "CNH": "USDCNH",
    "USD/KRW": "USDKRW",
    "KRW": "USDKRW",
    # Wrapped assets -- Jupiter identifies markets by mint, so wBTC/wETH arrive as tickers
    "WBTC": "BTC",
    "XBT": "BTC",
    "WETH": "ETH",
    "WSOL": "SOL",
    "MATIC": "POL",
    "RNDR": "RENDER",
    # Avantis Upside Perps: same underlying, different cost model (profit share, not fees)
    "BTC_UPSIDE": "BTC",
    "ETH_UPSIDE": "ETH",
    "SOL_UPSIDE": "SOL",
    "XRP_UPSIDE": "XRP",
    "HYPE_UPSIDE": "HYPE",
}

NOT_ALIASED_ON_PURPOSE: dict[str, str] = {
    # Economically related but distinct instruments. Merging these would net away real basis.
    "PAXG": "tokenized gold (Paxos) -- issuer and redemption basis vs spot XAU",
    "XAUT": "tokenized gold (Tether) -- same",
    "SPY": "S&P 500 ETF -- ~1/10 the index level, see SCALE_MISMATCH",
    "QQQ": "Nasdaq 100 ETF -- ~1/41 the index level, see SCALE_MISMATCH",
    "SAMSUNGEM": "GRVT lists this alongside SAMSUNG; likely Samsung Electro-Mechanics",
}

SCALE_MISMATCH: dict[str, str] = {
    # Same canonical asset, different price scale per venue. Hedges MUST be sized in USD
    # notional, never in units. Values are the approximate index/ETF ratio at capture.
    "US500": "Ondo US500 and Pacifica SP500 quote index points (~7730); Avantis US500 and GRVT SPY quote the SPY ETF (~772). Ratio ~10x.",
    "US100": "Ondo US100 quotes index points (~29471); Avantis US100 and GRVT QQQ quote the QQQ ETF (~716). Ratio ~41x.",
    "WTI": "Avantis prices Commodities.WTIV6 (a dated futures contract, 81.80 at capture); GRVT/Pacifica/Ondo price spot-linked feeds (~84.9). ~3.7% basis, not a unit-size difference.",
    "BRENT": "Avantis prices Commodities.BRENTV6 (dated futures, 88.97); Ondo prices spot-linked BRENT (90.16).",
}


def canonical_base(symbol: str) -> str:
    """Single book key for any venue-native stamp.

    HIP-3 ``xyz:GOLD`` → ``XAU``, Ostium ``EUR`` → ``EURUSD``, ``USD/JPY`` →
    ``USDJPY``. Crosses keep both legs: ``EURGBP`` is not ``EURUSD``.
    """
    if not symbol:
        return ""
    return resolve_asset(normalize_base_asset(symbol))


def same_asset(left: str, right: str) -> bool:
    """True when two venue-native tickers are the same underlying book.

    ``EUR`` aliases to ``EURUSD`` (Avantis/Ostium USD-quoted euro). ``EURGBP``
    does not. ``xyz:GOLD`` and ``XAU`` match via ``normalize_base_asset``.
    """
    if not left or not right:
        return False
    return canonical_base(left) == canonical_base(right)


def resolve_asset(symbol: str) -> str:
    """Resolve a venue-native symbol or ticker alias to a canonical base asset.

    Only applies the explicit alias table -- generic suffix stripping
    (``BTC_USDT_Perp`` -> ``BTC``) belongs to ``hedge_scanner.assets``.

    >>> resolve_asset("kPEPE")
    'PEPE'
    >>> resolve_asset("CL")
    'WTI'
    >>> resolve_asset("BTC")
    'BTC'
    """
    if not symbol:
        return ""
    key = symbol.strip().upper()
    return ASSET_ALIASES.get(key, key)


def venues_listing(base_asset: str) -> list[str]:
    """Venues with a live market for ``base_asset``, in canonical venue order.

    >>> venues_listing("BTC")
    ['avantis', 'grvt', 'pacifica', 'jupiter', 'ondo']
    >>> venues_listing("XPD")
    ['grvt']
    >>> venues_listing("NOT_A_REAL_ASSET")
    []
    """
    asset = canonical_base(base_asset)
    return [v for v in VENUES if asset in VENUE_MARKETS[v]]


def can_hedge(base_asset: str, venue: str) -> bool:
    """Whether ``venue`` lists a live market for ``base_asset``.

    The feasibility gate: call this before pricing a hedge leg. A paused Avantis pair
    returns False -- see ``AVANTIS_PAUSED`` to tell "paused" from "never listed".

    >>> can_hedge("SOL", "avantis")
    True
    >>> can_hedge("PEPE", "avantis")
    False
    >>> can_hedge("JPM", "avantis")
    False
    """
    market = VENUE_MARKETS.get(venue, {}).get(canonical_base(base_asset))
    return market is not None and market.active


def market_for(base_asset: str, venue: str) -> MarketSpec | None:
    """The ``MarketSpec`` for an asset on a venue, or None if it is not listed."""
    return VENUE_MARKETS.get(venue, {}).get(canonical_base(base_asset))
