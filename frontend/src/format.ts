export function nf(n: number, d: number): string {
  return n.toLocaleString("en-US", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function usd(n: number | null | undefined, d?: number): string {
  if (n == null) return "—";
  const v = Number(n);
  const dec = d != null ? d : Math.abs(v) >= 1000 ? 0 : 2;
  return (v < 0 ? "-$" : "$") + nf(Math.abs(v), dec);
}

export function signedUsd(n: number | null | undefined): string {
  if (n == null) return "—";
  const v = Number(n);
  if (v === 0) return "$0";
  const dec = Math.abs(v) >= 1000 ? 0 : 2;
  return (v > 0 ? "+$" : "-$") + nf(Math.abs(v), dec);
}

export function compactUsd(n: number | null | undefined): string {
  const v = Math.abs(Number(n || 0));
  if (v >= 1e9) return "$" + nf(v / 1e9, 2) + "B";
  if (v >= 1e6) return "$" + nf(v / 1e6, 2) + "M";
  if (v >= 1e3) return "$" + nf(v / 1e3, 1) + "K";
  return "$" + nf(v, 2);
}

export function price(n: number | null | undefined): string {
  if (n == null) return "—";
  const v = Math.abs(Number(n));
  return "$" + nf(Number(n), v >= 100 ? 2 : v >= 1 ? 4 : 6);
}

export function bps(n: number | null | undefined, d = 1): string {
  if (n == null) return "—";
  return nf(n, d) + " bps";
}

export function pct(n: number | null | undefined, d = 1): string {
  if (n == null) return "—";
  return nf(n, d) + "%";
}

export function tone(v: number | null | undefined): "up" | "down" | "dim" {
  const n = Number(v || 0);
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "dim";
}

export function signedBps(n: number | null | undefined, d = 2): string {
  if (n == null) return "—";
  const v = Number(n);
  if (v === 0) return nf(0, d) + " bps";
  return (v > 0 ? "+" : "") + nf(v, d) + " bps";
}

export function signedPct(n: number, d: number): string {
  return (n > 0 ? "+" : "") + nf(n, d) + "%";
}

/** Daily carry USD, whole dollars only — cents are noise at /24h scale. */
export function carryDailyUsd(n: number | null | undefined): string {
  if (n == null) return "";
  const v = Number(n);
  const whole = Math.round(Math.abs(v));
  if (v > 0) return "+$" + nf(whole, 0) + "/24h";
  if (v < 0) return "-$" + nf(whole, 0) + "/24h";
  return "$0/24h";
}

export function venueLabel(v: string | null | undefined): string {
  const s = String(v || "source").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function usdFrom8hBps(
  notional: number,
  bps8h: number | null | undefined,
  hours: number,
): number {
  return (Number(notional) * Number(bps8h || 0)) / 10000 * (hours / 8);
}

/** Accrued funding/borrow/marginFee over 24h. Holder-signed USD. Not fees. */
export function fundingUsd24(
  notional: number,
  funding8h: number | null | undefined,
  borrow8h: number | null | undefined = 0,
): number {
  return usdFrom8hBps(notional, Number(funding8h || 0) - Number(borrow8h || 0), 24);
}

export function hoursLabel(h: number | null | undefined): string {
  if (h == null) return "never";
  const v = Number(h);
  if (v === 0) return "now";
  if (v < 24) return nf(v, 1) + " h";
  if (v < 168) return nf(v / 24, 1) + " d";
  return nf(v / 168, 1) + " w";
}

/** Holder all-in %/h. Printed + = that side pays. Color follows holder money-in. */
export function venueRatePctH(
  venue: string,
  holderBps8h: number | null | undefined,
  borrow8h: number | null | undefined,
): { print: number; holder: number; digits: number } {
  const holder = (Number(holderBps8h || 0) - Number(borrow8h || 0)) / 8 / 100;
  const v = venue.toLowerCase();
  const digits = v === "avantis" || v === "avantis_upside" ? 8 : 4;
  return { print: -holder, holder, digits };
}

export function sourceNet8h(
  funding: number | null | undefined,
  borrow: number | null | undefined,
): number {
  return Number(funding || 0) - Number(borrow || 0);
}

export function aprFrom8h(bps8h: number): number {
  return bps8h * 10.95;
}
