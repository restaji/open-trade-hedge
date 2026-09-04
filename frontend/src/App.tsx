import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Position, ScanData, SelfHedge } from "./types";
import { VenueBadge } from "./Venue";
import {
  avantisRoundtripUsd,
  breakevenCellClass,
  breakevenHedgeable,
  breakevenLabel,
  breakevenTooltip,
  computeBreakeven,
} from "./breakeven";
import { VerdictPanel } from "./verdict";
import {
  aprFrom8h,
  bps,
  carryDailyUsd,
  compactUsd,
  nf,
  price,
  signedPct,
  signedUsd,
  sourceNet8h,
  tone,
  usd,
  fundingUsd24,
  venueRatePctH,
} from "./format";

const TABLE_HEADERS: { key: string; label: string; title?: string; align?: "left" }[] = [
  { key: "market", label: "Market", align: "left" },
  { key: "venue", label: "Venue", align: "left" },
  { key: "notional", label: "Notional" },
  { key: "lev", label: "Lev" },
  { key: "mark", label: "Mark" },
  { key: "pnl", label: "PnL" },
  { key: "paid", label: "Paid" },
  { key: "fund", label: "Funding", title: "24h %" },
  { key: "fees", label: "Fees" },
  { key: "carry", label: "Carry (APR)", title: "Net APR · daily $/24h" },
  { key: "breakeven", label: "Breakeven" },
];

const TABLE_COLS = TABLE_HEADERS.length + 2; /* chevron + toggle */

function positionRowId(p: Position, index: number): string {
  return `${p.venue}|${p.market ?? ""}|${p.base_asset}|${p.side}|${index}`;
}

function openStorageKey(wallet: string): string {
  return "hs.open." + wallet.trim().toLowerCase();
}

function loadOpenIds(wallet: string): Set<string> {
  try {
    const raw = sessionStorage.getItem(openStorageKey(wallet));
    if (!raw) return new Set();
    const arr = JSON.parse(raw) as string[];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

function saveOpenIds(wallet: string, ids: Set<string>): void {
  try {
    sessionStorage.setItem(openStorageKey(wallet), JSON.stringify([...ids]));
  } catch {
    /* ignore */
  }
}

function restoreOpenIds(positions: Position[], wallet: string): Set<string> {
  const saved = loadOpenIds(wallet);
  if (saved.size === 0) return new Set();
  const next = new Set<string>();
  positions.forEach((p, i) => {
    const id = positionRowId(p, i);
    if (saved.has(id)) next.add(id);
  });
  return next;
}

function cls(v: number | null | undefined): string {
  return tone(v);
}

function signClass(v: number | null | undefined): string {
  const n = Number(v || 0);
  if (n > 0) return "signed-pos";
  if (n < 0) return "signed-neg";
  return "";
}

const ASSET_NAMES: Record<string, string> = {
  BTC: "Bitcoin",
  ETH: "Ethereum",
  SOL: "Solana",
  XRP: "XRP",
  AVAX: "Avalanche",
  BNB: "BNB",
  DOGE: "Dogecoin",
  LINK: "Chainlink",
  ARB: "Arbitrum",
  XAU: "Gold",
  XAG: "Silver",
  EURUSD: "EUR/USD",
  GBPUSD: "GBP/USD",
  AUDUSD: "AUD/USD",
  NZDUSD: "NZD/USD",
  USDJPY: "USD/JPY",
  USDCAD: "USD/CAD",
  USDCHF: "USD/CHF",
};

const ASSET_CLASS_LABEL: Record<string, string> = {
  crypto: "Crypto",
  commodity: "Commodities",
  forex: "Forex",
  equity: "Equities",
  index: "Indices",
};

const FX_CCY = new Set([
  "AUD",
  "CAD",
  "CHF",
  "CNH",
  "EUR",
  "GBP",
  "JPY",
  "NZD",
  "SEK",
  "SGD",
  "BRL",
  "IDR",
  "INR",
  "KRW",
  "MXN",
  "TRY",
  "TWD",
  "ZAR",
  "USD",
]);

function assetDisplayName(base: string): string {
  const up = (base || "").toUpperCase();
  if (ASSET_NAMES[up]) return ASSET_NAMES[up];
  if (/^[A-Z]{6}$/.test(up) && FX_CCY.has(up.slice(0, 3)) && FX_CCY.has(up.slice(3))) {
    return `${up.slice(0, 3)}/${up.slice(3)}`;
  }
  return base;
}

function marketSubtitle(p: Position): string {
  const name = assetDisplayName(p.base_asset);
  const klass = ASSET_CLASS_LABEL[(p.asset_class || "").toLowerCase()];
  return klass ? `${name} · ${klass}` : name;
}

function sideLabel(side: string): string {
  return side.charAt(0).toUpperCase() + side.slice(1).toLowerCase();
}

function netCarryDailyDetail(n: number | null | undefined): string {
  if (n == null) return "—";
  return signedUsd(n) + "/24h";
}

function BreakevenCell({ p }: { p: Position }) {
  const roundtrip = avantisRoundtripUsd(p);
  const netDaily = netFundingUsd24(p);
  const be = computeBreakeven(roundtrip, netDaily);
  if (roundtrip == null && netDaily == null) {
    return (
      <td className="num breakeven-col">
        <span className="dim">—</span>
      </td>
    );
  }
  return (
    <td
      className={"num breakeven-col " + breakevenCellClass(be)}
      title={breakevenTooltip(be)}
    >
      {breakevenLabel(be)}
    </td>
  );
}

function CarryCell({ apr, earn }: { apr: number | null; earn: number | null }) {
  if (apr == null && earn == null) {
    return (
      <td className="num carry-cell">
        <span className="dim">—</span>
      </td>
    );
  }
  const sign = signClass(earn ?? apr);
  const parts: string[] = [];
  if (apr != null) parts.push(signedPct(apr, 1));
  if (earn != null) parts.push(carryDailyUsd(earn));
  return (
    <td className={"num carry-cell " + sign}>
      <span className="carry-line">{parts.join(" · ")}</span>
    </td>
  );
}

function Rate({
  venue,
  funding,
  borrow,
}: {
  venue: string;
  funding: number | null | undefined;
  borrow: number | null | undefined;
}) {
  const { print, holder, digits } = venueRatePctH(venue, funding, borrow);
  return <span className={cls(holder)}>{signedPct(print * 24, digits)}</span>;
}

/** Detail block: holder-signed rate — color matches economic direction. */
function DetailRate({
  venue,
  funding,
  borrow,
}: {
  venue: string;
  funding: number | null | undefined;
  borrow: number | null | undefined;
}) {
  const { holder, digits } = venueRatePctH(venue, funding, borrow);
  return <span className={cls(holder)}>{signedPct(holder * 24, digits)}</span>;
}

function Money({ n }: { n: number | null | undefined }) {
  return <span className={cls(n)}>{signedUsd(n)}</span>;
}

function AprNeutral({ n }: { n: number | null | undefined }) {
  if (n == null) return null;
  return <span>{signedPct(n, 1)}</span>;
}

function CostLine({ usdVal, bpsVal }: { usdVal: string; bpsVal: string }) {
  return (
    <span className="cost">
      {usdVal} <span className="mut">{bpsVal}</span>
    </span>
  );
}

function hedgeNotional(p: Position): number {
  if (p.hedge_notional_usd != null) return Math.abs(Number(p.hedge_notional_usd));
  return Math.abs(p.notional_usd || 0);
}

/** Source-leg 24h accrual on the open position (funding/borrow/marginFee only). */
function sourceFundingUsd24(p: Position): number | null {
  const sc = p.source_carry;
  if (!sc) return null;
  return fundingUsd24(Math.abs(p.notional_usd || 0), sc.funding_8h_bps, sc.borrow_8h_bps);
}

/** Avantis-leg 24h accrual on the hedge size. */
function hedgeFundingUsd24(p: Position): number | null {
  const q = p.avantis_quote;
  if (!q) return null;
  return fundingUsd24(hedgeNotional(p), q.funding_rate_8h_bps, q.borrow_rate_8h_bps || 0);
}

/** Net 24h funding = source + Avantis. Fees (open/close/spread) are not in this. */
function netFundingUsd24(p: Position): number | null {
  const src = sourceFundingUsd24(p);
  const hedge = hedgeFundingUsd24(p);
  if (src == null && hedge == null) return null;
  return (src || 0) + (hedge || 0);
}

function liqPriceOf(p: Position): number | null {
  if (p.liquidation_price != null) return p.liquidation_price;
  return p.source_liq?.liq_price ?? null;
}

function avantisTradeUrl(p: Position): string {
  const market = p.avantis_quote?.market || "";
  const asset =
    market.includes("/") ? market.replace("/", "-") : (p.base_asset || "") + "-USD";
  return "https://www.avantisfi.com/trade?asset=" + encodeURIComponent(asset);
}

function findingFor(findings: SelfHedge[], p: Position): SelfHedge | undefined {
  return findings.find((f) => f.base_asset === p.base_asset);
}

function hasContent(n: ReactNode): boolean {
  return n != null && n !== false;
}

function DetailRow({
  label,
  jupiter,
  avantis,
  net,
}: {
  label: string;
  jupiter?: ReactNode;
  avantis?: ReactNode;
  net?: boolean;
}) {
  if (!hasContent(jupiter) && !hasContent(avantis)) return null;
  return (
    <div className={"drow" + (net ? " drow-net" : "")}>
      <div className="dlabel">{label}</div>
      <div className="dval">{jupiter}</div>
      <div className="dval">{avantis}</div>
    </div>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="dsection">
      <div className="dsec-hd">{title}</div>
      {children}
    </div>
  );
}

function PositionDetail({
  p,
  findings,
}: {
  p: Position;
  findings: SelfHedge[];
}) {
  const q = p.avantis_quote;
  const sc = p.source_carry;
  const hn = hedgeNotional(p);
  const hf = p.hedge_funding;
  const f = p.hedge_role === "offsetting" ? findingFor(findings, p) : undefined;
  const src24 = sourceFundingUsd24(p);
  const hedge24 = hedgeFundingUsd24(p);
  const net24 = netFundingUsd24(p);
  const openBps = q?.open_fee_bps || 0;
  const closeBps = q?.close_fee_bps || 0;
  const spreadBps = q?.spread_bps || 0;
  const toUsd = (b: number) => (hn * b) / 10000;
  const feesUsd =
    hf?.cover_usd != null
      ? hf.cover_usd
      : q
        ? toUsd(openBps + closeBps + spreadBps)
        : f?.unwind_fee_usd ?? null;
  const liqP = liqPriceOf(p);
  const entryJ = p.entry_price != null ? price(p.entry_price) : undefined;
  const liqJ = liqP != null ? price(liqP) : undefined;

  if (p.hedge_role === "offsetting" && f) {
    return (
      <div className="detail">
        <div className="dgrid">
          <div className="dhead" />
          <div className="dhead dval dhead-venue">
            <VenueBadge venue={p.venue} />
          </div>
          <div className="dhead dval">Offset</div>
          <DetailSection title="Position">
            <DetailRow label="Long" jupiter={usd(f.long_notional_usd)} />
            <DetailRow label="Short" jupiter={usd(f.short_notional_usd)} />
            <DetailRow label="Offset" jupiter={usd(f.offsetting_notional_usd)} />
            <DetailRow label="Net" jupiter={<span>{signedUsd(f.net_notional_usd)}</span>} />
            <DetailRow
              label="Unwind"
              jupiter={
                <CostLine usdVal={usd(f.unwind_fee_usd)} bpsVal={bps(f.unwind_fee_bps)} />
              }
            />
          </DetailSection>
        </div>
      </div>
    );
  }

  const sizeJ =
    p.size_base != null
      ? nf(p.size_base, Math.abs(p.size_base) >= 1000 ? 2 : 4) + " " + p.base_asset
      : undefined;
  const sizeA = q && p.hedge_role !== "offsetting" ? usd(hn) : undefined;
  const collatJ = p.collateral_usd != null ? usd(p.collateral_usd) : undefined;
  const marginJ = p.margin_mode || undefined;
  const rateJ = sc ? (
    <DetailRate venue={p.venue} funding={sc.funding_8h_bps} borrow={sc.borrow_8h_bps} />
  ) : undefined;
  const rateA = q ? (
    <DetailRate venue="avantis" funding={q.funding_rate_8h_bps} borrow={q.borrow_rate_8h_bps} />
  ) : undefined;
  const fundJ = src24 != null ? <Money n={src24} /> : undefined;
  const fundA = hedge24 != null ? <Money n={hedge24} /> : undefined;
  const fundAprJ = hf?.source_apr_pct != null ? <AprNeutral n={hf.source_apr_pct} /> : undefined;
  const fundAprA = hf?.hedge_apr_pct != null ? <AprNeutral n={hf.hedge_apr_pct} /> : undefined;

  const showCosts = q && hf;
  const roundtripUsd = avantisRoundtripUsd(p);
  const be = computeBreakeven(roundtripUsd, net24);
  const hedgeable = breakevenHedgeable(be);
  const showTrade = showCosts;

  const breakevenDetail = (
    <span className={breakevenCellClass(be)} title={breakevenTooltip(be)}>
      {breakevenLabel(be)}
    </span>
  );

  return (
    <div className="detail">
      <div className="detail-main">
        <div className="dgrid">
        <div className="dhead" />
        <div className="dhead dval dhead-venue">
          <VenueBadge venue={p.venue} />
        </div>
        <div className="dhead dval dhead-venue">
          <VenueBadge venue="avantis" />
        </div>

        <DetailSection title="Position">
          <DetailRow label="Size" jupiter={sizeJ} avantis={sizeA} />
          <DetailRow label="Entry" jupiter={entryJ} />
          <DetailRow label="Liq" jupiter={liqJ} />
          <DetailRow label="Collat" jupiter={collatJ} />
          <DetailRow label="Margin" jupiter={marginJ} />
        </DetailSection>

        <DetailSection title="Funding">
          <DetailRow label="Net rate (24h %)" jupiter={rateJ} avantis={rateA} />
          <DetailRow label="Funding 24h" jupiter={fundJ} avantis={fundA} />
          {net24 != null && (
            <DetailRow
              label="Net funding 24h"
              net
              jupiter={hedge24 == null ? <Money n={net24} /> : undefined}
              avantis={hedge24 != null ? <Money n={net24} /> : undefined}
            />
          )}
          {(fundAprJ || fundAprA) && (
            <DetailRow label="Funding APR (ann.)" jupiter={fundAprJ} avantis={fundAprA} />
          )}
        </DetailSection>

        {showCosts && (
          <DetailSection title="Costs">
            <DetailRow
              label="Open"
              avantis={<CostLine usdVal={usd(toUsd(openBps))} bpsVal={bps(openBps)} />}
            />
            <DetailRow
              label="Close"
              avantis={<CostLine usdVal={usd(toUsd(closeBps))} bpsVal={bps(closeBps)} />}
            />
            <DetailRow
              label="Spread"
              avantis={<CostLine usdVal={usd(toUsd(spreadBps))} bpsVal={bps(spreadBps)} />}
            />
            <DetailRow
              label="Total"
              avantis={
                <span className="cost">
                  {usd(feesUsd)} <span className="mut">{bps(hf.cover_bps)}</span>
                  {q.fee_tier && q.fee_tier !== "n/a" ? (
                    <span className="mut"> {q.fee_tier}</span>
                  ) : null}
                </span>
              }
            />
            <DetailRow
              label="Round-trip (Avantis)"
              avantis={roundtripUsd != null ? usd(roundtripUsd) : "—"}
            />
            <DetailRow
              label="Net carry"
              avantis={
                net24 != null ? (
                  <span className={signClass(net24)}>{netCarryDailyDetail(net24)}</span>
                ) : (
                  "—"
                )
              }
            />
            <DetailRow label="Breakeven" avantis={breakevenDetail} />
          </DetailSection>
        )}

        {!q && p.hedge_role !== "offsetting" && (
          <div className="drow dmsg">
            <div className="dlabel" />
            <div className="dval" />
            <div className="dval dim">
              {p.avantis_unavailable || (p.can_hedge_on_avantis ? "No quote." : "Unlisted.")}
            </div>
          </div>
        )}

          </div>
      </div>

      {showTrade && roundtripUsd != null && (
        <VerdictPanel
          hedgeable={hedgeable}
          be={be}
          net24={net24}
          roundtripUsd={roundtripUsd}
          tradeUrl={avantisTradeUrl(p)}
        />
      )}
    </div>
  );
}

function PositionRow({
  p,
  findings,
  open,
  onToggle,
  rowIndex,
}: {
  p: Position;
  findings: SelfHedge[];
  open: boolean;
  onToggle: () => void;
  rowIndex: number;
}) {
  const hf = p.hedge_funding;
  const f = p.hedge_role === "offsetting" ? findingFor(findings, p) : undefined;
  const sc = p.source_carry;
  const net8 = sc ? sourceNet8h(sc.funding_8h_bps, sc.borrow_8h_bps) : 0;
  const apr = hf ? hf.net_apr_pct : sc ? aprFrom8h(net8) : null;
  const earn = netFundingUsd24(p);
  const fees =
    hf?.cover_usd != null ? usd(hf.cover_usd) : f?.unwind_fee_usd != null ? usd(f.unwind_fee_usd) : "—";

  return (
    <>
      <tr
        className={"prow" + (open ? " open" : "") + (rowIndex % 2 === 1 ? " alt" : "")}
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        tabIndex={0}
        role="button"
        aria-expanded={open}
      >
        <td className="chev" aria-hidden="true">
          <span className="chev-ico">▸</span>
        </td>
        <td className="txt-left market-cell">
          <div className="mkt-line1">
            <span className="sym">{p.base_asset}</span>
            <span className={"side-tag " + p.side}>{sideLabel(p.side)}</span>
          </div>
          <span className="mkt-sub">{marketSubtitle(p)}</span>
        </td>
        <td className="txt-left">
          <VenueBadge venue={p.venue} />
        </td>
        <td className="num">{usd(Math.abs(p.notional_usd))}</td>
        <td className="num mut">{p.leverage ? nf(p.leverage, 1) + "x" : "—"}</td>
        <td className="num">{price(p.mark_price)}</td>
        <td className={"num " + signClass(p.unrealized_pnl_usd)}>{signedUsd(p.unrealized_pnl_usd)}</td>
        <td className={"num " + signClass(p.funding_paid_usd)}>
          {p.funding_paid_usd == null ? "—" : signedUsd(p.funding_paid_usd)}
        </td>
        <td className="num">
          {sc ? (
            <Rate venue={p.venue} funding={sc.funding_8h_bps} borrow={sc.borrow_8h_bps} />
          ) : (
            <span className="dim">—</span>
          )}
        </td>
        <td className="num mut">{fees}</td>
        <CarryCell apr={apr} earn={earn} />
        <BreakevenCell p={p} />
        <td className="row-pad" aria-hidden="true" />
      </tr>
      {open && (
        <tr className="prow-detail">
          <td colSpan={TABLE_COLS} className="detail-cell">
            <PositionDetail p={p} findings={findings} />
          </td>
        </tr>
      )}
    </>
  );
}

function SkeletonRows({ n = 3 }: { n?: number }) {
  return (
    <>
      {Array.from({ length: n }, (_, i) => (
        <tr key={i} className="skel-row">
          <td className="chev" />
          <td><span className="skel skel-md" /></td>
          <td><span className="skel skel-sm" /></td>
          <td className="num"><span className="skel skel-num" /></td>
          <td className="num"><span className="skel skel-xs" /></td>
          <td className="num"><span className="skel skel-num" /></td>
          <td className="num"><span className="skel skel-num" /></td>
          <td className="num"><span className="skel skel-num" /></td>
          <td className="num"><span className="skel skel-num" /></td>
          <td className="num"><span className="skel skel-num" /></td>
          <td className="num carry-cell"><span className="skel skel-num" /></td>
          <td className="num breakeven-col"><span className="skel skel-num" /></td>
          <td className="row-pad" />
        </tr>
      ))}
    </>
  );
}

function PositionsHead({
  allExpanded,
  onToggleAll,
}: {
  allExpanded: boolean;
  onToggleAll: () => void;
}) {
  return (
    <tr>
      <th className="chev" aria-hidden="true" />
      {TABLE_HEADERS.map((h) => (
        <th
          key={h.key}
          className={h.align === "left" ? "txt-left" : "num"}
          title={h.title}
        >
          {h.label}
        </th>
      ))}
      <th className="head-toggle">
        <button type="button" className="expand-toggle" onClick={onToggleAll}>
          {allExpanded ? "Collapse all" : "Expand all"}
        </button>
      </th>
    </tr>
  );
}

function formatScanTime(ts: number): string {
  return new Date(ts).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export default function App() {
  const [addr, setAddr] = useState("");
  const [scanning, setScanning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<ScanData | null>(null);
  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set());
  const [lastScanAt, setLastScanAt] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const poll = useRef<number | null>(null);
  const tick = useRef<number | null>(null);
  const booted = useRef(false);

  useEffect(() => {
    const q = new URLSearchParams(location.search).get("a");
    let saved = "";
    try {
      saved = localStorage.getItem("hs.addr") || "";
    } catch {
      /* ignore */
    }
    const initial = q || saved;
    booted.current = true;
    if (initial) {
      setAddr(initial);
      void runScan(initial);
    }
    return () => {
      if (poll.current) clearInterval(poll.current);
      if (tick.current) clearInterval(tick.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const applyMarks = useCallback(async (positions: Position[]) => {
    try {
      const resp = await fetch("/api/prices");
      const json = await resp.json();
      const px = json.prices || {};
      return positions.map((p) => {
        const venuePx = px[p.venue] || {};
        const next =
          (p.market != null && venuePx[p.market] != null ? venuePx[p.market] : venuePx[p.base_asset]) as
            | number
            | undefined;
        if (next == null || !p.size_base) return p;
        const copy = { ...p, mark_price: next, notional_usd: p.size_base * next };
        if (p.side === "long") copy.unrealized_pnl_usd = p.size_base * (next - p.entry_price);
        else copy.unrealized_pnl_usd = p.size_base * (p.entry_price - next);
        const hn = hedgeNotional(copy);
        const src24 = fundingUsd24(
          Math.abs(copy.notional_usd || 0),
          copy.source_carry?.funding_8h_bps,
          copy.source_carry?.borrow_8h_bps,
        );
        const hedge24 = copy.avantis_quote
          ? fundingUsd24(
              hn,
              copy.avantis_quote.funding_rate_8h_bps,
              copy.avantis_quote.borrow_rate_8h_bps || 0,
            )
          : 0;
        if (copy.source_carry) {
          copy.source_carry = { ...copy.source_carry, usd_24h: src24 };
        }
        if (copy.hedge_funding) {
          copy.hedge_funding = {
            ...copy.hedge_funding,
            cover_usd: hn * (copy.hedge_funding.cover_bps || 0) / 10000,
            source_usd_24h: src24,
            hedge_usd_24h: hedge24,
            earn_usd_24h: src24 + hedge24,
          };
        }
        return copy;
      });
    } catch {
      return positions;
    }
  }, []);

  async function runScan(raw: string) {
    const trimmed = raw.trim();
    if (!trimmed) return;
    setScanning(true);
    setError(null);
    setElapsed(0);
    if (tick.current) clearInterval(tick.current);
    const t0 = Date.now();
    tick.current = window.setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 250);
    if (poll.current) {
      clearInterval(poll.current);
      poll.current = null;
    }
    try {
      const resp = await fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          addresses: trimmed.split(/[,\s]+/).filter(Boolean),
        }),
      });
      const json: ScanData = await resp.json();
      if (json.error) throw new Error(json.error);
      try {
        localStorage.setItem("hs.addr", trimmed);
      } catch {
        /* ignore */
      }
      const u = new URL(location.href);
      u.searchParams.set("a", trimmed);
      history.replaceState(null, "", u);
      const positions = (json.positions || []).slice().sort((a, b) => {
        const ao = a.hedge_role === "offsetting" ? 1 : 0;
        const bo = b.hedge_role === "offsetting" ? 1 : 0;
        if (ao !== bo) return ao - bo;
        const an = a.hedge_funding?.net_apr_pct ?? -1e9;
        const bn = b.hedge_funding?.net_apr_pct ?? -1e9;
        return bn - an || Math.abs(b.notional_usd || 0) - Math.abs(a.notional_usd || 0);
      });
      json.positions = positions;
      setData(json);
      setOpenIds(restoreOpenIds(positions, trimmed));
      setLastScanAt(Date.now());
      poll.current = window.setInterval(async () => {
        setData((prev) => {
          if (!prev) return prev;
          void applyMarks(prev.positions).then((next) => {
            setData((cur) => (cur ? { ...cur, positions: next } : cur));
          });
          return prev;
        });
      }, 5000);
      void applyMarks(positions).then((next) => {
        setData((cur) => (cur ? { ...cur, positions: next } : cur));
      });
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (tick.current) {
        clearInterval(tick.current);
        tick.current = null;
      }
      setScanning(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void runScan(addr);
  }

  const positions = data?.positions || [];
  const findings = data?.self_hedge_findings || [];
  const filter = data?.filter;
  const errors = data?.errors || [];

  const earn = useMemo(
    () => positions.reduce((s, p) => s + (netFundingUsd24(p) || 0), 0),
    [positions],
  );
  const gross = positions.reduce((s, p) => s + Math.abs(p.notional_usd || 0), 0);
  const pnl = positions.reduce((s, p) => s + (p.unrealized_pnl_usd || 0), 0);
  const paid = positions.reduce((s, p) => s + (p.funding_paid_usd || 0), 0);

  const rowIds = useMemo(
    () => positions.map((p, i) => positionRowId(p, i)),
    [positions],
  );
  const allExpanded = rowIds.length > 0 && rowIds.every((id) => openIds.has(id));

  function toggleRow(id: string) {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      if (addr.trim()) saveOpenIds(addr.trim(), next);
      return next;
    });
  }

  function toggleAllRows() {
    const next = allExpanded ? new Set<string>() : new Set(rowIds);
    setOpenIds(next);
    if (addr.trim()) saveOpenIds(addr.trim(), next);
  }

  const hidden: string[] = [];
  if (filter?.not_paying_funding) hidden.push(filter.not_paying_funding + " not paying");
  if (filter?.not_hedgeable_on_avantis) hidden.push(filter.not_hedgeable_on_avantis + " unlisted");
  if (filter?.no_carry_data) hidden.push(filter.no_carry_data + " no rate");

  const stale = lastScanAt != null && now - lastScanAt > 60_000;
  const showEmpty = booted.current && !addr.trim() && !scanning && !data && !error;
  const showZero = data && !positions.length && !scanning;

  return (
    <div className="wrap">
      <header className="scan-head">
        <div className="scan-head-top">
          <h1>Hedge Scanner</h1>
          {lastScanAt != null && (
            <span className={"fresh" + (stale ? " stale" : "")}>
              {formatScanTime(lastScanAt)}
              {stale ? " · stale" : ""}
            </span>
          )}
        </div>
        <form className="search" onSubmit={onSubmit}>
          <input
            value={addr}
            onChange={(e) => setAddr(e.target.value)}
            spellCheck={false}
            autoComplete="off"
            aria-label="Wallet address"
            placeholder="0x… or Solana address"
            size={44}
          />
          <button type="submit" className="btn-scan" disabled={scanning || !addr.trim()}>
            {scanning ? (elapsed ? `${elapsed}s` : "…") : "Scan"}
          </button>
        </form>
      </header>

      {showEmpty && (
        <div className="panel empty">
          <p>Enter a wallet address to scan open perp positions.</p>
        </div>
      )}

      {error && (
        <div className="panel error">
          <p className="down">{error}</p>
          <button type="button" className="btn-retry" onClick={() => void runScan(addr)} disabled={!addr.trim()}>
            Retry
          </button>
        </div>
      )}

      {scanning && !data && (
        <div className="book">
          <table className="positions">
            <thead>
              <PositionsHead allExpanded={false} onToggleAll={() => {}} />
            </thead>
            <tbody>
              <SkeletonRows />
            </tbody>
          </table>
          <p className="load-msg">Scanning{elapsed ? ` · ${elapsed}s` : ""}</p>
        </div>
      )}

      {showZero && (
        <div className="panel empty">
          <p>
            {filter?.total
              ? `${filter.total} open positions` + (hidden.length ? ` · ${hidden.join(" · ")}` : ".")
              : "No positions found."}
          </p>
        </div>
      )}

      {positions.length > 0 && (
        <>
          <section className="summary">
            <div className="summary-left">
              <div className="summary-primary">
                {earn !== 0 || positions.some((p) => p.hedge_funding) ? (
                  <>
                    <span className={"big " + signClass(earn)}>
                      {earn > 0 ? usd(earn) : signedUsd(earn)}
                    </span>
                    <span className="suffix">/24h</span>
                  </>
                ) : (
                  <span className="big dim">—</span>
                )}
              </div>
              <p className="summary-meta">
                net funding /24h · {positions.length} open{" "}
                {positions.length === 1 ? "trade" : "trades"} · {compactUsd(gross)} size
              </p>
            </div>
            <dl className="summary-stats">
              <div>
                <dt>PnL</dt>
                <dd className={signClass(pnl)}>{signedUsd(pnl)}</dd>
              </div>
              <div>
                <dt>Paid</dt>
                <dd className={signClass(paid)}>{signedUsd(paid)}</dd>
              </div>
            </dl>
          </section>

          <div className="book">
            <table className="positions">
              <thead>
                <PositionsHead allExpanded={allExpanded} onToggleAll={toggleAllRows} />
              </thead>
              <tbody>
                {positions.map((p, i) => {
                  const rowId = positionRowId(p, i);
                  return (
                    <PositionRow
                      key={rowId}
                      p={p}
                      findings={findings}
                      open={openIds.has(rowId)}
                      onToggle={() => toggleRow(rowId)}
                      rowIndex={i}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {findings.length > 0 && (
        <div className="aside">
          <h4>Offset</h4>
          {findings.map((f) => (
            <div key={f.base_asset}>
              {f.base_asset} {usd(f.long_notional_usd)} / {usd(f.short_notional_usd)} off{" "}
              {usd(f.offsetting_notional_usd)} net <Money n={f.net_notional_usd} /> {usd(f.unwind_fee_usd)}{" "}
              {bps(f.unwind_fee_bps)}
            </div>
          ))}
        </div>
      )}

      {filter && hidden.length > 0 && (
        <div className="aside">
          <h4>Hidden</h4>
          {filter.kept}/{filter.total} · {hidden.join(", ")}
        </div>
      )}

      {errors.length > 0 && (
        <div className="aside">
          <h4>Not read</h4>
          {errors.map((e) => (
            <div key={e.venue}>
              <VenueBadge venue={e.venue} /> {e.message}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
