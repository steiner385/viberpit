import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { DispatchBar } from '@/components/DispatchBar'
import { DispatchError } from '@/components/dispatchError'
import { SessionComposer } from '@/components/session/SessionComposer'
import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'
import { useCreateSession, useEngineOffers, useGithubRepos, useModels } from '@/api/hooks'
import { useEnabledFleetHosts } from '@/api/fleet'
import { DEFAULT_AGENT_ENGINE, type Account, type AgentEngine } from '@/api/types'
import { setLastRepo } from '@/lib/lastRepo'
import { isPendingSessionId } from '@/lib/pendingSessions'
import { NAV_BY_ID } from '@/shell/navDefs'
import { useOpenSessionId } from '@/shell/openSessionId'
import { sessionOpenState } from '@/session/sessionNavState'
import type { SessionListState } from '@/features/sessions/useSessionListState'
import { describeCreateSessionFailure } from '@/features/sessions/createSessionFailure'

export interface PromptDockProps {
  /**
   * The Sessions list state, owned by the shell and shared with `ContextList`.
   *
   * ONE cursor: ↑/↓, Alt+digit and ←/→ typed at the prompt move the same
   * highlight the list column renders its selection from. Both old shells wired
   * their prompt bar and their list to a single `useListNavigation`; a second
   * handle here would put the two regions permanently out of step.
   */
  list: SessionListState
  /** Accounts offered in the Account dropdown; the shell already holds them for
   *  the list column, so they arrive as a prop rather than a second query. */
  accounts?: Account[]
  /** Tab on an empty prompt — the shell owns `listMode`; `ContextList` reads it. */
  onEnterList: () => void
  /** `?` on an empty prompt — the shell owns the shortcuts sheet. */
  onShowHelp: () => void
  /**
   * A `409` saying every account is rate-limited, with the earliest reset (or
   * null when the daemon did not say). The shell owns that state — it feeds
   * `useAppBanners({ allLimitedReset })` — so the dock reports rather than
   * renders.
   */
  onAllLimited: (earliestReset: string | null) => void
  /** Bumped by the shell to pull focus back to the prompt (e.g. after detach). */
  autoFocusKey?: unknown
}

/**
 * `app.dock` — the prompt bar at the foot of the Sessions section.
 *
 * ## One dock, two faces
 *
 * With a session open it is that session's composer; with none it is the
 * new-session dispatch bar. That was already true in both old layouts —
 * `DesktopShell`'s `bottomPrompt` (`DesktopShell.tsx:559-587`) plus its
 * `BottomComposer` context bridge (`:117-120`), and `Dashboard`'s `DispatchBar`
 * (`Dashboard.tsx:767-792`) — but as two independent copies, which is how they
 * drifted (see DEC-2 below).
 *
 * The branch is not cosmetic. `SessionComposer` reads
 * `useSessionRuntimeContext()`, which throws outside a provider, so choosing it
 * on a route with no runtime takes the whole page down. The answer therefore
 * comes from `useOpenSessionId()` — the SAME function `PageHost` gates the
 * provider on. Two derivations of "is a session open" is the one way this
 * component can fail catastrophically, so there is only one.
 *
 * ## Visibility is not the dock's business
 *
 * Nothing here reads `useViewport().mode`. Whether `app.dock` exists at all —
 * hidden in kiosk, present only in the Sessions section (DEC-10) — is the
 * shell's decision, made once in Task 16's region-visibility matrix. A local
 * `if (kiosk) return null` here would be a second owner of that rule, and the
 * two would eventually disagree.
 *
 * ## The mobile safe area
 *
 * The region adds NO bottom inset of its own. `DispatchBar` (`:285`) and
 * `Composer` (`:234`) each already set
 * `paddingBottom: calc(env(safe-area-inset-bottom) + 0.5rem)` on the surface
 * they paint, and a wrapper inset would stack on top of it — roughly 34px plus
 * 0.5rem, twice, under the input on an edge-to-edge phone. `PromptDock.test.tsx`
 * asserts the inset on the element that actually paints the bottom edge in each
 * branch, so removing it from either child fails in the region that would look
 * broken.
 *
 * ## Drafts do not survive a session switch
 *
 * `app.main` is keyed on the session id (each session gets a clean reducer and
 * attach/detach cycle), so switching sessions remounts the dock and drops an
 * unsent composer draft. That is exactly what ships today — `DesktopShell` keyed
 * the entire shell on the same id — so it is carried over deliberately, not
 * inherited by accident. Preserving drafts needs a per-session store OUTSIDE the
 * keyed subtree (a `useSessionDrafts` handle held by `AppShell` and read by
 * `Composer`), which is a behaviour change to a component both old layouts still
 * ship; it is tracked as follow-up rather than smuggled into this migration.
 * `PromptDock.test.tsx` pins the current behaviour so the decision is visible.
 */
export function PromptDock(props: PromptDockProps) {
  const openSessionId = useOpenSessionId()
  return (
    <div data-region="app.dock" className="shrink-0">
      {openSessionId ? <LiveComposer /> : <NewSessionBar {...props} />}
    </div>
  )
}

/**
 * Context bridge — the composer is rendered inside `PageHost`'s
 * `SessionRuntimeProvider`, so it reads the runtime the transcript is already
 * driving rather than opening a second one. (Replaces `DesktopShell`'s
 * `BottomComposer`.)
 */
function LiveComposer() {
  const rt = useSessionRuntimeContext()
  return <SessionComposer rt={rt} />
}

/**
 * The new-session half. The repo/model/host queries live here rather than in
 * `PromptDock` so they are not mounted at all while a session is open — the
 * `/fleet/hosts` poll in particular has nothing to do on a session route.
 */
function NewSessionBar({
  list,
  accounts = [],
  onEnterList,
  onShowHelp,
  onAllLimited,
  autoFocusKey,
}: PromptDockProps) {
  const navigate = useNavigate()
  const reposQuery = useGithubRepos()
  // The bar owns the engine/account picks; the model list is engine- AND
  // account-scoped (Codex's comes from the account's own app-server), so the bar
  // reports its context up and the query lives here beside the other fetches.
  // While Codex has no account the query is disabled — asking is a 400, and the
  // bar renders "select an account first" instead.
  const [modelContext, setModelContext] = useState<{ engine: AgentEngine; account: string | null }>(
    { engine: DEFAULT_AGENT_ENGINE, account: null },
  )
  const { data: models = [] } = useModels(modelContext.engine, modelContext.account)
  const engines = useEngineOffers()
  // DEC-2: the Runner picker was phone-only — `Dashboard` fetched the hosts and
  // `DesktopShell.handleDispatch` had no `host` parameter at all. One dock means
  // one DispatchBar, so desktop gains it rather than mobile losing it.
  const hosts = useEnabledFleetHosts()
  const createSession = useCreateSession()
  const { ordering, nav } = list

  // Stable identity: it is a dependency of the bar's report effect, so a fresh
  // closure each render would re-fire it on every keystroke.
  const handleEngineContext = useCallback(
    (context: { engine: AgentEngine; account: string | null }) => {
      setModelContext((prev) =>
        prev.engine === context.engine && prev.account === context.account ? prev : context)
    },
    [],
  )

  function openSession(id: string, initialPrompt?: string) {
    // An optimistic placeholder has no session on the server yet — opening one
    // would attach a socket to an id the daemon has never heard of. Both old
    // shells guarded their own "open" entry point with this; Task 18 restored it
    // here after `KeyboardSessions.test.tsx`'s Enter/Alt+digit cases were
    // re-hosted on the shell and turned red.
    if (isPendingSessionId(id)) return
    // Path from `NAV_DEFS`, not a literal, so the dock cannot drift from the
    // route table the rest of the shell is built from.
    navigate(`${NAV_BY_ID.sessions.path}/${id}`, sessionOpenState(initialPrompt))
  }

  function handleDispatch({
    initialPrompt, repo, account, model, host, attachmentIds, engine, reasoningEffort,
  }: {
    initialPrompt: string
    repo: string | null
    account: string | null
    model: string | null
    host: string | null
    attachmentIds: string[]
    engine: AgentEngine
    reasoningEffort: string | null
  }): Promise<void> {
    const repoInfo = repo ? (reposQuery.data ?? []).find((r) => r.nameWithOwner === repo) : undefined
    return new Promise((resolve, reject) => {
      createSession.mutate(
        {
          initialPrompt,
          repo: repo ?? undefined,
          branch: repoInfo?.defaultBranch ?? undefined,
          account: account ?? undefined,
          model: model ?? undefined,
          host: host ?? undefined,
          attachmentIds: attachmentIds.length ? attachmentIds : undefined,
          // Sent even for the default so `useCreateSession`'s optimistic
          // placeholder carries the real engine — a Codex card must never be
          // briefly labelled Claude while the daemon spins the runtime up.
          engine,
          reasoningEffort: reasoningEffort ?? undefined,
        },
        {
          onSuccess: (session) => {
            if (repo) setLastRepo(repo)
            if (session.usageUnverified) {
              toast("Couldn't verify account usage — started anyway.")
            }
            openSession(session.id, initialPrompt)
            resolve()
          },
          // The 409 triage is DesktopShell's; `Dashboard` merely rejected, so the
          // phone layout silently swallowed "every account is limited". The union
          // keeps the richer branch — the same call the list column made in Task 9.
          //
          // The reading is `describeCreateSessionFailure`, shared with the New
          // Session dialog. "All accounts limited" is the shell banner's to show,
          // so the bar is told nothing; everything else is said inline in the bar
          // (`useCreateSession` opts out of the global toast, so this IS the
          // user's only copy of the message).
          onError: (err) => {
            const failure = describeCreateSessionFailure(err)
            if (failure.kind === 'all-limited') {
              onAllLimited(failure.earliestReset)
              reject(err)
              return
            }
            reject(new DispatchError(failure.message))
          },
        },
      )
    })
  }

  return (
    <DispatchBar
      repos={reposQuery.data ?? []}
      reposLoading={reposQuery.isLoading}
      reposError={reposQuery.isError}
      accounts={accounts}
      models={models}
      hosts={hosts}
      engines={engines}
      onDispatch={handleDispatch}
      onEngineContextChange={handleEngineContext}
      onNavigate={nav.navigate}
      onOpenHighlighted={() => {
        if (nav.highlightedSession) openSession(nav.highlightedSession.id)
      }}
      onExpandHighlighted={() => {
        if (nav.highlightedGroupKey) ordering.expandGroup(nav.highlightedGroupKey)
      }}
      onCollapseHighlighted={() => {
        if (nav.highlightedGroupKey) ordering.collapseGroup(nav.highlightedGroupKey)
      }}
      onToggleHighlighted={() => {
        if (nav.highlightedGroupKey) ordering.toggleCollapse(nav.highlightedGroupKey)
      }}
      onEnterList={() => {
        // The cursor is the dock's to seed (it owns the shared nav handle); the
        // mode flag is the shell's, because the list column renders from it.
        if (nav.highlightedIndex < 0) nav.setHighlightedIndex(0)
        onEnterList()
      }}
      onJump={(index) => {
        const s = ordering.orderedSessions[index]
        if (s) {
          nav.highlightSession(s.id)
          openSession(s.id)
        }
      }}
      onShowHelp={onShowHelp}
      onEscape={() => nav.setHighlightedIndex(-1)}
      activeDescendantId={
        nav.highlightedSession ? `row-${nav.highlightedSession.id}`
        : nav.highlightedGroupKey ? `group-header-${nav.highlightedGroupKey}`
        : undefined
      }
      activeDescendantIsHeader={!!nav.highlightedGroupKey}
      autoFocusKey={autoFocusKey}
      isPending={createSession.isPending}
    />
  )
}
