// Module-level "is the sessions list on-screen?" flag. Set only by the Dashboard
// (see useSessionsViewing); read by useSessions' queryFn to add ?viewing=1 so the
// server regenerates summaries only while someone is actually looking.
let viewing = false

export function setSessionsViewing(v: boolean): void {
  viewing = v
}

export function isSessionsViewing(): boolean {
  return viewing
}
