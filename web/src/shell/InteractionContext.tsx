/* eslint-disable react-refresh/only-export-components -- mixed export file
   (InteractionProvider component + useInteractive hook), same convention as
   pipeline/StatusStrip.tsx and pipeline/PrRow.tsx */
import * as React from 'react'

/**
 * Whether the UI accepts input. False in kiosk, where the app is a wall display
 * with no user in front of it.
 *
 * Defaults to TRUE outside a provider on purpose: a component rendered without
 * the shell (every unit test) must behave normally. A default of `false` would
 * silently make the whole suite read-only.
 */
const InteractionContext = React.createContext<boolean>(true)

export function InteractionProvider({
  interactive,
  children,
}: {
  interactive: boolean
  children: React.ReactNode
}) {
  return <InteractionContext.Provider value={interactive}>{children}</InteractionContext.Provider>
}

export function useInteractive(): boolean {
  return React.useContext(InteractionContext)
}
