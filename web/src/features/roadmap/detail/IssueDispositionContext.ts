import { createContext } from 'react'

/** Requests a review from the stable page owner, never from a polled list row. */
export const IssueDispositionContext = createContext<((itemId: string) => void) | null>(null)
