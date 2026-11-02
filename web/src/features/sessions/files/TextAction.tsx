import type { ReactNode } from 'react'

/** The Files tab's inline text action: Retry, and the narrow layout's way back to the file list. */
export function TextAction({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex min-h-target items-center gap-1 rounded-md px-2 text-xs font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      {children}
    </button>
  )
}
