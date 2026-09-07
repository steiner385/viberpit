/** ViberPit logo mark: two session lanes; the live signal switches lanes, changing
 *  color (account identity) as it crosses. Decorative — pair it with visible
 *  "ViberPit" text; screen readers get the text, not the glyph.
 *  Keep the geometry in sync with public/favicon.svg (same glyph as a standalone file). */
export function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <svg viewBox="0 0 32 32" width={size} height={size} aria-hidden="true" className="shrink-0">
      <defs>
        <linearGradient id="ccswitch-brand-grad" x1="6" y1="11" x2="26" y2="21" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#22d3ee" />
          <stop offset="1" stopColor="#8b5cf6" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="7" fill="#0e0f14" />
      <g fill="none" strokeLinecap="round">
        <path d="M6 11h20M6 21h20" stroke="#3f4152" strokeWidth="2.4" />
        <path d="M6 11h4c6 0 6 10 12 10h4" stroke="url(#ccswitch-brand-grad)" strokeWidth="3.2" />
      </g>
    </svg>
  )
}
