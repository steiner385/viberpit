// Remote sessions: Claude Code Remote Control sessions ViberPit does not run (an
// interactive `claude` with /remote-control, another SDK host, another machine),
// listed by `GET /api/remote-sessions` and driven through its prompt/answer endpoints.
// Its own module, like `remoteControlHooks`, so it ships only in the lazy remote chunks
// (the list section and the remote page) rather than the entry bundle, and so the
// suites that hand-mock `@/api/hooks` never have to learn it.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import { QK } from './hooks'

export type RemoteSessionStatus = 'running' | 'idle' | 'requires_action' | 'pending'

/** One row of `GET /api/remote-sessions` (server/remote_sessions/listing.py). */
export interface RemoteSession {
  /** The claude.ai session id (`session_…`) — never a ViberPit session id. */
  id: string
  account: string
  title: string
  status: RemoteSessionStatus
  connection: 'connected' | 'disconnected'
  /** The opaque Remote Control machine id claude.ai tags the session with. */
  machineId: string | null
  /** The ViberPit host whose config carries `machineId`, else the operator's label for
   *  it (Settings `remoteMachineLabels`), else null. */
  machineName: string | null
  origin: 'repl' | 'sdk' | 'auto' | null
  /** The first `owner/repo` claude.ai reports for the session, and its branch. */
  project: string | null
  branch: string | null
  /** Every `{repo, branch}` when the session reports more than one, else null. */
  projects: { repo: string; branch: string | null }[] | null
  /** Usually null: claude.ai sends an empty cwd for Remote Control sessions. */
  cwd: string | null
  model: string | null
  /** The claude.ai link ("Open in Claude app"). Untrusted: gate with `isSafeHttpUrl`. */
  url: string | null
  updatedAt: string | null
  needsYou: boolean
  unread: boolean
}

export interface RemoteSessionsList {
  /** The kill switch. Off → no sessions, no accounts. */
  enabled: boolean
  /** Server-sorted: needs-you first, then connected, then most recent. */
  sessions: RemoteSession[]
  accounts: { account: string; status: string }[]
}

export const REMOTE_SESSIONS_KEY = ['remote-sessions'] as const

/** ~15 s while the feature is on; a switched-off server is re-asked rarely. */
const POLL_MS = 15_000
const DISABLED_POLL_MS = 300_000

export function useRemoteSessions() {
  return useQuery<RemoteSessionsList>({
    queryKey: REMOTE_SESSIONS_KEY,
    queryFn: () => api.get<RemoteSessionsList>('/remote-sessions'),
    refetchInterval: (query) => (query.state.data?.enabled === false ? DISABLED_POLL_MS : POLL_MS),
    staleTime: 10_000,
  })
}

/** Name a Remote Control machine ViberPit does not run on: `PUT /api/settings
 *  {remoteMachineLabels: {id: label}}` (a patch — other labels are kept; a blank label
 *  removes it). The list re-reads so the rows pick the name up. */
export function useNameRemoteMachine() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { machineId: string; label: string }>({
    mutationFn: ({ machineId, label }) =>
      api.put('/settings', { remoteMachineLabels: { [machineId]: label } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: REMOTE_SESSIONS_KEY })
      qc.invalidateQueries({ queryKey: QK.settings })
    },
  })
}

/** The `/ws` session id of a remote session's live view (the server's `remote:` branch). */
export function remoteWsId(id: string): string {
  return `remote:${id}`
}

export interface RemoteAnswerResult {
  ok: true
  /** False while a multi-question ask still waits for its other answers. */
  sent: boolean
  account: string
  kind: 'permission' | 'question'
}

export type RemoteAnswerBody =
  | { askId: string; decision: 'allow' | 'deny'; message?: string }
  | { askId: string; selected: string[]; mode?: 'option' | 'freeform' | 'chat' }

/** `POST /api/remote-sessions/{id}/prompt {text}`. Refusals throw `ApiRequestError`
 *  with `{detail: {code, message}}`. */
export function sendRemotePrompt(id: string, text: string) {
  return api.post<{ ok: true; account: string }>(
    `/remote-sessions/${encodeURIComponent(id)}/prompt`, { text },
  )
}

/** `POST /api/remote-sessions/{id}/answer` — a permission decision or a question's
 *  selection. Needs an open `/ws` view of the session (409 `REMOTE_SESSION_NOT_WATCHED`). */
export function sendRemoteAnswer(id: string, body: RemoteAnswerBody) {
  return api.post<RemoteAnswerResult>(
    `/remote-sessions/${encodeURIComponent(id)}/answer`, body,
  )
}
