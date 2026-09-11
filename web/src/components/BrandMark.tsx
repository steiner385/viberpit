import { BRAND_NAME, BRAND_NAME_PARTS } from '@/brand'

/**
 * ViberPit's mark: the mascot's head (sunglasses, tongue, a corner of the
 * laptop) on a Deep Night plate — the same PNG the web app manifest installs
 * to a home screen, so the tab, the home screen and the rail all show one
 * image. Decorative: pair it with `Wordmark` or visible brand text.
 *
 * The plate is rendered into `public/icons/*.png` by `scripts/gen-icons.sh`
 * from `public/brand/viberpit-mascot.png`; edit the crop there, not here.
 */
export function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <img
      src="/icons/icon-192.png"
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      draggable={false}
      className="shrink-0 select-none rounded-sm"
    />
  )
}

/**
 * The two-colour wordmark, "Viber" in ink and "Pit" in brand green.
 *
 * The full name is present once as visually-hidden text and the coloured
 * halves are `aria-hidden`, so a screen reader announces "ViberPit" as one
 * word and `getByText(BRAND_NAME)` finds one node. The green half is a
 * logotype — WCAG 1.4.3 exempts it from the contrast minimum — which is the
 * only reason `text-brand` is acceptable on text here and nowhere else.
 */
export function Wordmark({ className }: { className?: string }) {
  const [head, tail] = BRAND_NAME_PARTS
  return (
    <span className={className}>
      <span className="sr-only">{BRAND_NAME}</span>
      <span aria-hidden="true">
        {head}
        <span className="text-brand">{tail}</span>
      </span>
    </span>
  )
}
