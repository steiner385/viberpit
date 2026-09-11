// web/src/lib/ciVerdict.ts
//
// The CI verdict vocabulary is the deploy controller's (server/deploy/steps.py
// ci_verdict): green | red | pending | unrun | unavailable. Every surface that
// shows a verdict to a human — the deploy banner, Settings › Deploy, the
// Pipeline's main lane — renders it through this one label map, so the banner's
// "CI pending" and the lane's "CI pending" are the same words by construction.
//
// Only "unrun" needs translating: it means the commit's CI run was cancelled before
// any job started, so no check will ever arrive. Showing that as "pending" tells the
// operator to wait for something that is not coming — the one verdict that never
// resolves on its own.
const CI_VERDICT_LABELS: Record<string, string> = { unrun: 'never ran' }
export const ciLabel = (verdict: string): string => CI_VERDICT_LABELS[verdict] ?? verdict
