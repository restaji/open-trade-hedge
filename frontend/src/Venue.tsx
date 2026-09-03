import { useState } from "react";
import { venueLabel } from "./format";
import avantisIcon from "./assets/venues/avantis.png";
import grvtIcon from "./assets/venues/grvt.png";
import jupiterIcon from "./assets/venues/jupiter.png";
import ondoIcon from "./assets/venues/ondo.svg";
import pacificaIcon from "./assets/venues/pacifica.svg";
import variationalIcon from "./assets/venues/variational.png";

/** Known venue slugs → bundled 16px icons (one copy, shared by badges + hedge btn). */
export const VENUE_ICONS: Record<string, string> = {
  jupiter: jupiterIcon,
  pacifica: pacificaIcon,
  grvt: grvtIcon,
  variational: variationalIcon,
  ondo: ondoIcon,
  avantis: avantisIcon,
};

const VENUE_TONE: Record<string, string> = {
  jupiter: "venue-jup",
  pacifica: "venue-pa",
  grvt: "venue-gr",
  variational: "venue-va",
  ondo: "venue-on",
  avantis: "venue-ava",
};

export function resolveVenueSlug(raw: string): string | null {
  const s = String(raw || "").toLowerCase().trim();
  if (VENUE_ICONS[s]) return s;
  for (const slug of Object.keys(VENUE_ICONS)) {
    if (s.includes(slug)) return slug;
  }
  return null;
}

/** 16px venue mark — shared by table badges and hedge button. */
export function VenueMark({ venue, slug: slugIn }: { venue?: string; slug?: string }) {
  const slug = slugIn ?? (venue ? resolveVenueSlug(venue) : null);
  const label = venueLabel(venue || slug || "source");
  const icon = slug ? VENUE_ICONS[slug] : undefined;
  const tone = (slug && VENUE_TONE[slug]) || "venue-other";
  const [iconOk, setIconOk] = useState(true);

  if (icon && iconOk) {
    return (
      <img
        className="venue-ico"
        src={icon}
        alt=""
        width={16}
        height={16}
        onError={() => setIconOk(false)}
      />
    );
  }

  return <span className={"venue-mark " + tone}>{label.charAt(0)}</span>;
}

/**
 * Venue mark plus name. Reuses funding-arb icon paths; letter tile when slug
 * is missing from the registry.
 */
export function VenueBadge({ venue }: { venue: string }) {
  const slug = resolveVenueSlug(venue);
  const label = venueLabel(venue);
  const tone = (slug && VENUE_TONE[slug]) || "venue-other";

  return (
    <span className={`venue-badge ${tone}`}>
      <VenueMark venue={venue} slug={slug ?? undefined} />
      {label}
    </span>
  );
}

export function HedgeButton({
  href,
  disabled,
  title,
}: {
  href: string;
  disabled?: boolean;
  title?: string;
}) {
  const inner = (
    <>
      <VenueMark slug="avantis" venue="avantis" />
      Hedge on Avantis
    </>
  );
  if (disabled) {
    return (
      <span className="hedge-btn disabled" title={title} aria-disabled="true">
        {inner}
      </span>
    );
  }
  return (
    <a className="hedge-btn" href={href} target="_blank" rel="noopener noreferrer" title={title}>
      {inner}
    </a>
  );
}
