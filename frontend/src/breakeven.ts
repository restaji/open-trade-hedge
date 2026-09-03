import type { Position } from "./types";

export type BreakevenResult =
  | { kind: "unhedgeable"; rawDays?: number }
  | { kind: "days"; days: number; label: string };

/** Avantis open + close + spread on the hedge notional. */
export function avantisRoundtripUsd(p: Position): number | null {
  const hf = p.hedge_funding;
  const q = p.avantis_quote;
  if (hf?.cover_usd != null) return Math.abs(Number(hf.cover_usd));
  if (!q) return null;
  const hn =
    p.hedge_notional_usd != null
      ? Math.abs(Number(p.hedge_notional_usd))
      : Math.abs(p.notional_usd || 0);
  const bps = (q.open_fee_bps || 0) + (q.close_fee_bps || 0) + (q.spread_bps || 0);
  if (hn <= 0 || bps <= 0) return null;
  return (hn * bps) / 10000;
}

/**
 * breakeven_days = avantis_roundtrip_cost / net_carry_per_day
 * net_carry_per_day must be post-hedge USD/day (both legs).
 */
export function computeBreakeven(
  roundtripUsd: number | null | undefined,
  netCarryPerDay: number | null | undefined,
): BreakevenResult {
  const cost = Number(roundtripUsd ?? 0);
  const carry = Number(netCarryPerDay ?? 0);

  if (!Number.isFinite(carry) || carry <= 0 || !Number.isFinite(cost) || cost <= 0) {
    return { kind: "unhedgeable" };
  }

  const days = cost / carry;
  if (!Number.isFinite(days) || days <= 0) {
    return { kind: "unhedgeable" };
  }

  if (days > 365) {
    return { kind: "unhedgeable", rawDays: days };
  }

  if (days < 1) {
    return { kind: "days", days, label: "<1d" };
  }

  return { kind: "days", days, label: days.toFixed(1) + "d" };
}

export function breakevenHedgeable(be: BreakevenResult): boolean {
  return be.kind === "days";
}

export function breakevenCellClass(be: BreakevenResult): string {
  if (be.kind === "unhedgeable") return "dim";
  if (be.days <= 7) return "breakeven-soon";
  return "";
}

export function breakevenTooltip(be: BreakevenResult): string | undefined {
  if (be.kind === "unhedgeable" && be.rawDays != null) {
    return be.rawDays.toFixed(1) + " days";
  }
  return undefined;
}

export function breakevenLabel(be: BreakevenResult): string {
  return be.kind === "unhedgeable" ? "Unhedgeable" : be.label;
}
