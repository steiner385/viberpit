// Export PDF's print-only pieces (spec 2026-11-05 M6). The stamp: "Snapshot
// <local date-time> · <product> · ViberPit". The print must stand without the
// browser's own header and footer, so the stamp carries the time, in the browser's
// locale and time zone, re-read on `beforeprint` so a page left open prints when
// it printed. `PrintMore`: the "+N more" past a print cap, in the DOM so a test
// sees it, shown only in print (print.css).
import { useEffect, useState } from 'react'
import { BRAND_NAME } from '@/brand'

const stamp = () => new Date().toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export function PrintStamp({ product }: { product: string }) {
  const [at, setAt] = useState(stamp)
  useEffect(() => {
    const onBeforePrint = () => setAt(stamp())
    window.addEventListener('beforeprint', onBeforePrint)
    return () => window.removeEventListener('beforeprint', onBeforePrint)
  }, [])
  return (
    <p data-print="only" className="text-xs text-ink-muted">
      Snapshot {at} · {product} · {BRAND_NAME}
    </p>
  )
}

/** "+N more" once `count` passes the print `cap`; nothing when the list fits. */
export function PrintMore({ count, cap, className }: { count: number; cap: number; className?: string }) {
  return count > cap ? <span data-print="only" className={className}>+{count - cap} more</span> : null
}
