/** Cost-vs-earn bar geometry (from funding-arb balance()). */
export function balanceBar(
  legABps: number,
  legBBps: number,
  scale: number,
): {
  segs: Array<{ side: "a" | "b"; left: number; width: number }>;
  tick: number;
  over: "left" | "right" | null;
} {
  const segs: Array<{ side: "a" | "b"; left: number; width: number }> = [];
  let left = 50;
  let right = 50;
  let over: "left" | "right" | null = null;

  for (const [side, earn] of [
    ["a", -legABps],
    ["b", -legBBps],
  ] as const) {
    const width = (Math.abs(earn) / scale) * 50;
    if (width <= 0) continue;

    let from: number;
    let to: number;
    if (earn >= 0) {
      from = right;
      to = right + width;
      right = to;
    } else {
      to = left;
      from = left - width;
      left = from;
    }
    if (to > 100) over = "right";
    if (from < 0) over = "left";

    const a = Math.max(0, Math.min(100, from));
    const b = Math.max(0, Math.min(100, to));
    if (b > a) segs.push({ side, left: a, width: b - a });
  }

  const net = -(legABps + legBBps);
  const raw = 50 + (net / scale) * 50;
  if (raw > 100) over = "right";
  if (raw < 0) over = "left";

  return { segs, tick: Math.min(100, Math.max(0, raw)), over };
}

/** Single max |value| across visible rows — keeps carry bars comparable. */
export function maxBarScale(mags: number[]): number {
  if (mags.length === 0) return 1;
  return Math.max(...mags.map((m) => Math.abs(m)), 1e-6);
}

export function bps8hToDaily(bps8h: number): number {
  return bps8h * 3;
}
