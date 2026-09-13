import { useState, type ReactNode } from 'react'
import { IssueDispositionContext } from './IssueDispositionContext'
import { IssueDispositionDialog } from './IssueDispositionDialog'

/** Page lifetime owns the editor. Polling can remove a row or its entire
 * attention group without discarding a draft or a pending mutation's error. */
export function IssueDispositionProvider({ children }: { children: ReactNode }) {
  const [itemId, setItemId] = useState<string | null>(null)
  return <IssueDispositionContext.Provider value={setItemId}>
    {children}
    {itemId && <IssueDispositionDialog key={itemId} itemId={itemId} onClose={() => setItemId(null)} />}
  </IssueDispositionContext.Provider>
}
