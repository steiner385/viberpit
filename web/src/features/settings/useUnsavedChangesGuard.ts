import * as React from 'react'
import { useBlocker } from 'react-router-dom'

/**
 * The unsaved-changes guard every Settings editor shares: while `dirty`, an
 * in-app navigation is blocked (the returned blocker drives SaveBar's
 * "Unsaved changes" dialog) and a reload or tab close asks the browser to
 * confirm. `useSettingsForm` uses it for the form sections; an editor with its
 * own save model (Memory, etag-based) uses it directly.
 */
export function useUnsavedChangesGuard(dirty: boolean): ReturnType<typeof useBlocker> {
  const blocker = useBlocker(dirty)

  React.useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  return blocker
}
