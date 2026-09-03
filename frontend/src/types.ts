export type SourceCarry = {
  funding_8h_bps: number | null;
  borrow_8h_bps: number | null;
  net_8h_bps: number | null;
  usd_24h?: number | null;
};

export type SourceLiq = {
  liq_price: number | null;
  distance_pct: number | null;
  cross_margin_risk?: string | null;
};

export type AvantisQuote = {
  market: string;
  side: string;
  funding_rate_8h_bps: number | null;
  borrow_rate_8h_bps: number | null;
  fee_tier?: string | null;
  open_fee_bps: number | null;
  close_fee_bps: number | null;
  spread_bps: number | null;
};

export type HedgeFunding = {
  source_apr_pct: number | null;
  hedge_apr_pct: number | null;
  net_apr_pct: number | null;
  net_8h_bps: number | null;
  source_usd_24h?: number | null;
  hedge_usd_24h?: number | null;
  earn_usd_24h: number | null;
  cover_bps: number | null;
  cover_usd: number | null;
  breakeven_hours: number | null;
};

export type Position = {
  venue: string;
  market: string;
  base_asset: string;
  side: string;
  size_base: number | null;
  notional_usd: number;
  entry_price: number;
  mark_price: number;
  liquidation_price: number | null;
  leverage: number | null;
  collateral_usd: number | null;
  unrealized_pnl_usd: number | null;
  funding_paid_usd: number | null;
  margin_mode: string | null;
  hedge_role?: string | null;
  hedge_side?: string | null;
  hedge_notional_usd?: number | null;
  can_hedge_on_avantis?: boolean;
  avantis_quote?: AvantisQuote | null;
  avantis_unavailable?: string | null;
  hedge_funding?: HedgeFunding | null;
  source_carry?: SourceCarry | null;
  source_liq?: SourceLiq | null;
  liq_distance_pct?: number | null;
};

export type SelfHedge = {
  base_asset: string;
  long_notional_usd: number;
  short_notional_usd: number;
  net_notional_usd: number;
  offsetting_notional_usd: number;
  unwind_fee_bps: number;
  unwind_fee_usd: number;
  fully_offset: boolean;
};

export type ScanFilter = {
  kept: number;
  total: number;
  not_hedgeable_on_avantis: number;
  not_paying_funding: number;
  no_carry_data: number;
};

export type VenueError = {
  venue: string;
  message: string;
  kind?: string;
};

export type ScanData = {
  positions: Position[];
  errors: VenueError[];
  filter: ScanFilter | null;
  self_hedge_findings: SelfHedge[];
  error?: string;
};
