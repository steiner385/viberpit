# Remote sessions: ViberPit picks up Remote Control sessions started elsewhere — plan

**Goal:** Claude Code Remote Control sessions running anywhere on the operator's accounts (an
interactive `claude` with `/remote-control`, another SDK host, another machine) appear in
ViberPit. You can watch them live and drive them from ViberPit: send prompts, and answer
approvals and questions.

## Verified (2026-11-25, claude 2.1.331, read-only probes + throwaway session E10)

- **List.** `GET https://api.anthropic.com/v1/sessions`
  - Headers: `Authorization: Bearer <full-scope claude.ai access token>`,
    `anthropic-version: 2023-06-01`, `anthropic-beta: ccr-byoc-2025-07-29`,
    `x-organization-uuid: <org>`.
  - Returns `{data, first_id, last_id, has_more}`. Each session has `id` (`session_…`),
    `title`, `session_status` (running | idle | requires_action | pending | archived),
    `connection_status` (connected | disconnected), `environment_kind` (bridge |
    anthropic_cloud), `origin`, `tags`, `session_url`, `status_bucket`, `unread`,
    `updated_at`, `session_context{cwd,model,…}`.
  - Remote Control sessions are `environment_kind == "bridge"`. Tags:
    - `remote-control-sdk` (an SDK host, e.g. ViberPit's own);
    - `remote-control-repl` (interactive `/remote-control`);
    - `remote-control-auto` (started automatically);
    - `machine-id:<uuid>` (the host machine).
  - At the time of the probe, 7 were connected.
- **Events.** `GET /v1/sessions/{id}/events` returns `{data, first_id, last_id, has_more}`.
  - Events are in **the SDK stream-json format ViberPit already translates**: `system`,
    `user`, `assistant`, `control_request`, `control_cancel_request`, `result`,
    `rate_limit_event`.
  - Paging (verified on a throwaway session, R1): an event's cursor id is its `uuid`.
    `after_id=<uuid>` returns the events after it, oldest first; `limit` pages; `has_more`
    says more follow; `last_id` is the last event's uuid. `after_id=<last_id>` returns
    `data: []` when nothing is new.
  - `/v1/sessions/{id}/events/stream` answers **404** for a Remote Control (`bridge`)
    session, with or without `managed-agents-2026-04-01` and `?beta=true` (verified, R1): it
    is the managed-agents surface. The CLI's own teleport poller reads
    `/v1/code/sessions/{id}/events?sort_order=asc&cursor=<sequence_num>` (2.1.332 binary).
    ViberPit polls `/v1/sessions/{id}/events?after_id=` instead.
  - The list (`GET /v1/sessions`) pages the same way: 200 per page by default, `limit` and
    `after_id` (a session id). It is not ordered by activity.
- **Send a prompt (verified, E10).**
  - Request: `POST /v1/sessions/{id}/events` with the same headers and body
    `{"events":[{"uuid":<uuid4>,"session_id":<id>,"type":"user","parent_tool_use_id":null,"message":{"role":"user","content":<text>}}]}`.
  - Response: 200. The CLI received the prompt on its stream and answered.
  - No device attestation was needed for a session on the same account.
- **Approvals and question answers (verified, R1, throwaway sessions).**
  - The `can_use_tool` request lands in the events log as `{type: "control_request",
    request_id, request: {subtype: "can_use_tool", tool_name, input, tool_use_id,
    display_name, description, permission_suggestions, blocked_path, …}, uuid, session_id,
    created_at}`.
  - The answer is `POST /v1/sessions/{id}/events` with `{"events":[{"uuid":<uuid4>,
    "session_id":<id>,"type":"control_response","response":{"subtype":"success",
    "request_id":<request_id>,"response":<decision>}}]}` — the body the CLI's
    `sendControlResponseToRemoteSession` builds (`W7n` in 2.1.332). Allow is
    `{"behavior":"allow","updatedInput":<input>,"tool_name":<tool>}`; deny is
    `{"behavior":"deny","message":<text>}`. 200; the CLI sent `control_cancel_request` to its
    own stdio host and ran the tool; a `control_response` event then appears in the log.
  - AskUserQuestion answered the way a ViberPit session answers it (deny with the
    `[User answered via the question UI] …` message): the model read the chosen option and
    replied with it.
- **Credentials.** The per-account browser-login slot token, which has
  `user:sessions:claude_code`, plus the org UUID from the store `.claude.json`. Both are already
  managed by the refresh authority (#3463, #3477). Use them on the primary only, through
  existing helpers (`account_auth`), and never log them.

## Constraints

- **Principle I.** This is a private, undocumented API. Gate it behind a kill switch,
  `VIBERPIT_REMOTE_SESSIONS_ENABLED`, **default false**. On any unexpected shape or status,
  degrade (log once, show "unavailable") and never crash.
- **Principle II.** Use the token only on the refresh-authority host (the primary daemon). The
  edge reaches it through daemon RPC, like every other session surface.
- **Ownership.** ViberPit does not own these sessions. There is no rotation, no respawn and no
  archive; only watch, prompt and answer.
- **Exclude ViberPit's own sessions.** Match their stored `remote_control_url` or session id
  against the listed `session_url` / `id`.
- **Person-only.** Sending a prompt or an answer into a session on another machine acts on that
  machine. Use the same `require_human` gate as the Remote Control enable, honouring the
  local-host opt-in. Audit each send.

## Task R1 — server

- **Client module** (e.g. `server/remote_sessions/`) using httpx with the headers above:
  - `list_remote_sessions(account)`: filter `environment_kind == "bridge"`, not archived,
    `connection_status == "connected"` (also keep recently-disconnected sessions, marked
    `disconnected`), exclude ViberPit-owned.
  - `fetch_events(account, sid, after=None)`: paginated.
  - `stream_events(account, sid)`: SSE. Probe the shape first on a throwaway session.
  - `send_prompt(account, sid, text)`.
  - `send_control_response(account, sid, request_id, response)`. Verify the shape on a
    throwaway session.
- **Poller on the primary daemon.** Every ~15 s per signed-in account, cache the listing.
  Back off on 4xx/5xx. Do nothing while the kill switch is off.
- **Live view.** Per watched session, a single-flight SSE (or poll) task feeds events through
  the existing `Translator` into the normal ViberPit event stream for a synthetic id
  `remote:<session_id>`, so the web chat renders them unchanged. Map `control_request` /
  `control_cancel_request` to permission and question cards, answerable through
  `send_control_response`.
- **API** (edge → daemon RPC):
  - `GET /api/remote-sessions` lists them with account, title, status, connection, machine,
    origin tag, cwd, url and needs-you.
  - The WS/attach path opens a remote session's live view.
  - `POST /api/remote-sessions/{id}/prompt` and `/answer`, both person-only and audited.
- **Tests.** Use a fake HTTP transport (respx or httpx `MockTransport`) for list, filter, exclude,
  events, send and errors, plus the kill switch. Add an integration test gated on an env var for
  a live throwaway session.

### R1 — as built (the contract R2 builds on)

Code: `server/remote_sessions/` (`client`, `listing`, `translate`, `service`, `view`),
`server/api/remote_sessions.py`, `server/ipc/remote_sessions_rpc.py`, the `/ws` branch in
`server/bridge/ws.py`, `RemoteOrchestrator` / `InProcessOrchestratorAdapter`
`remote_session*` methods (primary-routed by `FleetOrchestrator`). Tests:
`server/tests/test_remote_sessions.py` (fake claude.ai over `httpx.MockTransport`) and
`server/tests/test_remote_sessions_live.py` (`VIBERPIT_TEST_REMOTE_SESSIONS=1`: a throwaway
CLI is listed, streams, runs a prompt, resolves a tool approval and a question).

**`GET /api/remote-sessions`** (any authenticated identity) → 200
`{enabled: bool, sessions: RemoteSession[], accounts: [{account, status}]}`; account
`status` is `ok | no_login | pending | error:<kind>`. Kill switch off →
`{enabled: false, sessions: [], accounts: []}`. `RemoteSession`:
`{id: "session_…", account, title, status: "running"|"idle"|"requires_action"|"pending",
connection: "connected"|"disconnected", machineId: string|null, machineName: string|null,
origin: "repl"|"sdk"|"auto"|null, project: string|null, branch: string|null, projects:
[{repo, branch}]|null, cwd: string|null, model: string|null, url: string|null (the
claude.ai link, "Open in Claude app"), updatedAt: ISO string|null, needsYou: bool, unread:
bool}`, sorted needs-you first, then connected, then most recent.

*Project and machine (added 2026-11-26).* A bridge session's `session_context.cwd` is an empty
string, so `cwd` is usually null; the project is `external_metadata.current_branches`
(`{"owner/repo": "branch"}`): `project`/`branch` are its first entry, `projects` every entry
when there are several. claude.ai reports the machine only as the tag `machine-id:<uuid>` —
the CLI's `remoteControlMachineId` in the `.claude.json` of the config dir the session ran
from (not the separate `machineID` key). Every daemon answers `remote_control_machine_ids`
(`{host, machineIds: {id: "home"|"account"}}`, read from the host user's `~/.claude.json`
and each account config dir under `cfg/`); the edge fans it out (`FleetOrchestrator.
remote_machine_hosts`, unreachable hosts skipped, cached 5 min) and sets `machineName` to the
reporting host, else the operator's label from Settings `remoteMachineLabels` (`PUT
/api/settings {remoteMachineLabels: {id: label|null}}`, a patch), else null. The web Remote
group offers "Name this machine" for an id with no name. Code:
`server/remote_sessions/machines.py`; tests `server/tests/test_remote_session_machines.py`.

**Live view: the ordinary `/ws` socket, session id `remote:<id>`.**
- `{"type":"attach","sessionId":"remote:session_…"}` → `replayReset` (with `gen`), the
  snapshot's wire events, `replayComplete` (`earliestTurn: 0, totalTurns: 0`), then live
  frames stamped `sessionId`, `seq`, `gen` (and `srcSeq` in daemon mode) exactly like a
  ViberPit session. Every frame is a normal wire event from the existing `Translator`:
  `user` (with `remote: true`), `turnStart`, `turnModel`, `textDelta` (whole text blocks),
  `thinkingDelta`, `toolCall`, `toolResult`, `turnAnchor`, `turnEnd`, `sessionMeta`,
  `status` (`state`: `in_progress | waiting_input | idle`), `error`, and the cards below.
- Permission card: `permissionRequest` with `permId` = the CLI `request_id`,
  `alwaysAllowed: false`, `sessionGrantEffective: false` (no rules, no session grant);
  settled by `permissionResolved` (`allowed` true/false; `via: "claude_app"` and possibly
  `allowed: null` when it was settled outside ViberPit).
- Question cards: one `question` per question of an AskUserQuestion call, `questionId` =
  `<request_id>:<index>`; settled by `questionResolved` (`selected` = the labels ViberPit
  sent, or `null` with `via: "claude_app"`).
- `{"type":"detach","sessionId":"remote:…"}` stops the view. Any other frame type on a
  `remote:` id → `error` `REMOTE_SESSION_FRAME_UNSUPPORTED` (prompts and answers use REST).
- A refused attach → `error` with a code below, then `replayComplete`.
- `error` `remote_stream_reset` mid-stream: the watcher restarted; re-attach.
  `error` `remote_session_gone`: claude.ai no longer has the session.

**`POST /api/remote-sessions/{id}/prompt`** `{text}` (1–100000 chars) → 200
`{ok: true, account}`. **`POST /api/remote-sessions/{id}/answer`** →
200 `{ok: true, sent: bool, account, kind: "permission"|"question"}`:
- permission: `{askId: <permId>, decision: "allow"|"deny", message?: string}`;
- question: `{askId: <questionId>, selected: string[], mode?: "option"|"freeform"|"chat"}`.
  A multi-question call is sent once every question has an answer (`sent: false` until then).
- Needs an open view (`/ws` attach) of that session: the watcher holds the pending asks.

Both are person-only (403 `SESSION_CONTROL_HUMAN_ONLY`, local-host opt-in honoured) and
audited (`remote_session.prompt` `{session_id, chars, account?, outcome}`,
`remote_session.answer` `{session_id, ask_id, decision, account?, kind?, outcome}`; never the
prompt text). Refusals: `{detail: {code, message}}` — `REMOTE_SESSIONS_DISABLED` 409,
`REMOTE_SESSION_NOT_FOUND` 404, `REMOTE_SESSION_NOT_WATCHED` 409, `REMOTE_ASK_NOT_PENDING`
409, `REMOTE_SESSION_BAD_REQUEST` 400, `REMOTE_SESSIONS_UNAVAILABLE` 502; a malformed body
422.

## Task R2 — web

- **Sessions list.** A "Remote" group, collapsible, listing these sessions as
  `<title> · <machine/origin> · <status>`, with needs-you badges and "Open in Claude app".
- **Opening one.** It uses the existing session view in a remote mode:
  - shows the transcript;
  - offers a composer to send prompts;
  - answers permission and question cards through the new endpoints;
  - hides ViberPit-only controls (account, model, rotation, archive, rewind) with a clear
    "Running on <machine> — controlled via Remote Control" banner.
- **Bundle budget.** Put the remote view in a lazy chunk; `initialJs` has no headroom.

## Verification

Focused tests; ruff and mypy; web typecheck, lint, vitest for the touched dirs, build and budget.
Live checks:
1. A throwaway Remote Control session appears in ViberPit's list.
2. Its transcript streams.
3. A prompt sent from ViberPit runs.
4. A tool approval answered in ViberPit resolves in the CLI.
