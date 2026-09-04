import { HedgeButton } from "./Venue";
import {
  type BreakevenResult,
  breakevenLabel,
  computeBreakeven,
} from "./breakeven";
import { nf, signedUsd, usd } from "./format";

const FLIP_SCENARIOS = [10, 25, 50] as const;

function scenarioCarryLabel(n: number): string {
  return "+$" + nf(n, 0) + "/24h";
}

function netCarryLine(n: number | null | undefined): string {
  if (n == null) return "—";
  return signedUsd(n) + "/24h";
}

function breakevenDaysPhrase(be: BreakevenResult): string {
  if (be.kind !== "days") return "";
  if (be.days < 1) return "under 1 day";
  return be.days.toFixed(1) + " days";
}

export function VerdictPanel({
  hedgeable,
  be,
  net24,
  roundtripUsd,
  tradeUrl,
}: {
  hedgeable: boolean;
  be: BreakevenResult;
  net24: number | null;
  roundtripUsd: number | null;
  tradeUrl: string;
}) {
  const costExit = roundtripUsd != null ? usd(roundtripUsd) : "—";

  return (
    <aside className="detail-verdict">
      <div className="verdict-hd">Verdict</div>

      {hedgeable && be.kind === "days" ? (
        <>
          <p className="verdict-title">
            Hedgeable · breakeven in {be.label}
          </p>
          <p className="verdict-lede">
            Net carry {netCarryLine(net24)} · round-trip {costExit}
          </p>
          <p className="verdict-note">
            Carry covers fees after {breakevenDaysPhrase(be)}, then earns{" "}
            {net24 != null ? signedUsd(net24) + "/24h" : "—"}.
          </p>
        </>
      ) : (
        <>
          <p className="verdict-title dim">Unhedgeable</p>
          <p className="verdict-lede">
            {net24 != null && net24 <= 0 ? (
              <>Net carry is {netCarryLine(net24)}. Fees are never recovered.</>
            ) : net24 != null && net24 > 0 && be.kind === "unhedgeable" && be.rawDays != null ? (
              <>
                Net carry is {netCarryLine(net24)}. Fees take over a year to recover.
              </>
            ) : (
              <>Net carry is {netCarryLine(net24)}. Fees are never recovered.</>
            )}
          </p>

          {roundtripUsd != null && roundtripUsd > 0 && (
            <div className="verdict-block">
              <div className="verdict-block-hd">Breakeven if carry flips</div>
              <table className="verdict-scenarios">
                <tbody>
                  {FLIP_SCENARIOS.map((carry) => {
                    const flip = computeBreakeven(roundtripUsd, carry);
                    return (
                      <tr key={carry}>
                        <td>{scenarioCarryLabel(carry)}</td>
                        <td>{flip.kind === "days" ? breakevenLabel(flip) : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <p className="verdict-exit">
            Cost to exit now <span className="verdict-exit-amt">{costExit}</span>
          </p>
        </>
      )}

      <div className="verdict-foot">
        <HedgeButton
          href={tradeUrl}
          disabled={!hedgeable}
          title={
            hedgeable ? undefined : "Net carry is negative — fees never recovered"
          }
        />
      </div>
    </aside>
  );
}
