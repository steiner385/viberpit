# D-8 "Fork from here": verification spike

Date: 2026-11-02 (spike ran 18:23 to 18:27 EST). Constitution III: every claim below was checked against the
installed binary, the vendored SDK or ViberPit's code. Parent spec:
`2026-11-01-ui-desktop-parity-design.md` §5 (Branching) and §11 D-8.

## 1. The CLI

- Binary: `~/.local/bin/claude -> ~/.local/share/claude/versions/2.1.316`. `claude --version` prints `2.1.316 (Claude Code)`.
- `--help` (public):
  ```
  --fork-session        When resuming, create a new session ID instead of reusing the original (use with --resume or --continue)
  -r, --resume [value]  Resume a conversation by session ID or name, or open interactive picker with optional search term
  --session-id <uuid>   Use a specific session ID for the conversation (must be a valid UUID)
  ```
- **`--resume-session-at` exists, but it is hidden** (`.hideHelp()`). The name is exactly `--resume-session-at <message id>`.
  This is the Commander definition near byte offset 293642511 of the binary:
  ```js
  .addOption(new _s("--resume-session-at <message id>","When resuming, only messages up to and including the chain entry
     with <message.id> — any chain-entry UUID, typically the kept turn's last entry (use with --resume in print mode)")
     .argParser(String).hideHelp())
  .addOption(new _s("--resume-drops-turn <message id>","With --resume-session-at in print mode: declare the prompt uuid of
     the turn the truncating resume intends to discard; the resume is refused if the discarded range contains anything not
     attributable to that turn ... Ignored outside print mode, like --resume-session-at.").argParser(String).hideHelp())
  .addOption(new _s("--rewind-files <user-message-id>","Restore files to state at the specified user message and exit (requires --resume)")...)
  ```
- Validation (offset ~311973289, `runHeadless`): the CLI exits with `Error: --resume-session-at requires --resume`,
  `Error: --resume-drops-turn requires --resume-session-at`, and `--rewind-files is a standalone operation and cannot be
  used with a prompt`.
- Semantics (offset ~312223019, the print-mode resume loader):
  ```js
  if(s.resumeSessionAt){ let Ee=ce.messages.findIndex(St=>St.uuid===s.resumeSessionAt);
    if(Ee<0) ... Cs(`No message found with message.uuid of: ${s.resumeSessionAt}`) ; exit 1
    ... let Xe=ce.messages.slice(0,Ee+1) ... ce.messages=[...Xe,...]}      // INCLUSIVE cut
  let {adoptedSessionId:ie,effectiveFork:Se}=q4e(ce.sessionId,!!s.forkSession,...)
  if(Se){ await lVe(ce,{stripWorktreeSession:!0,stripRelocatedCwd:!0,...}) ... }   // fork rewrites cwd/worktree fields
  ```
- The binary also embeds the TS SDK (offset ~300954051). It maps `forkSession` to `--fork-session`, `resumeSessionAt` to
  `--resume-session-at` and `resumeDropsTurn` to `--resume-drops-turn`, so this is a supported SDK surface (TS
  `Options.resumeSessionAt`), not an internal flag.
- **"Print mode" includes SDK stream-json mode.** The run below has no `-p`, which is how the Python SDK launches the CLI,
  and it still honours the flag. Run before any model call: `--output-format stream-json --verbose --input-format
  stream-json --resume 5651281d… --fork-session --resume-session-at 00000000-0000-4000-8000-000000000000`. It returned rc=1
  and the result frame said `"errors":["No message found with message.uuid of: 00000000-…"]`.
- **Compaction: anchors at or before the last `compact_boundary` cannot be forked.** Re-probed 2026-11-02 21:52 EST
  against **2.1.317** (the installed binary had moved on from 2.1.316). The probe made no model call:
  - **Setup.** A throwaway `CLAUDE_CONFIG_DIR` with no credentials, and a temp cwd. A copy of a real 1144-entry
    transcript went into that cwd's project dir.
  - **The transcript.** It has one auto-compaction: a `system`/`compact_boundary` row with `parentUuid: null` and
    a `logicalParentUuid`. Its `compactMetadata.preservedSegment`/`preservedMessages` name 8 pre-boundary entries the
    compaction kept.
  - **The command.** `--output-format stream-json --verbose --input-format stream-json --resume <sid> --fork-session
    --resume-session-at <uuid> </dev/null`. No prompt was sent.

  | anchor | rc | result |
  |---|---|---|
  | a top-level assistant entry before the boundary, not preserved | 1 | `No message found with message.uuid of: …`, `cost 0` |
  | an entry inside the preserved segment (3 tried, incl. the segment's tail) | 0 | **no output at all**: no `init` frame, no fork file written |
  | an assistant entry after the boundary | 0 | `init` under a new session id, then `result` with `num_turns 0`, `duration_api_ms 0` (the frame's cost is the source's restored cost-state); fork file written |
  | a bogus uuid | 1 | `No message found …` |

  The source file's sha256 was unchanged. The loader keeps only the chain from the last boundary on, so a
  pre-boundary uuid is "not found". A preserved one makes the CLI exit silently, which a driver would see as a dead
  start. ViberPit therefore treats every top-level assistant entry at or before the last boundary as unanchorable
  (`replay._build_events` → `_with_turn_anchors`). `fork_session` checks the requested anchor against the
  transcript's own anchor set, so a live anchor recorded before a later compaction is refused with
  `TURN_NOT_FORKABLE` too. The probe dir was removed afterwards.

## 2. The Python SDK (`claude-agent-sdk==0.2.105`, pinned in `pyproject.toml:10`)

Path: `/home/tony/GitHub/viberpit-deploy/.venv/lib/python3.14/site-packages/claude_agent_sdk`.

- `_internal/transport/subprocess_cli.py`:
  - `:291` `resume` maps to `--resume <id>`.
  - `:294` `session_id` maps to `--session-id`.
  - `:343` `fork_session` maps to `--fork-session`.
  - `:364-371` each `extra_args` entry becomes `--<flag> <value>`, or a bare `--<flag>` when the value is `None`.
  - `:225`/`:408` the base command is `--output-format stream-json --verbose … --input-format stream-json`, with no `-p`.
- **The Python SDK has no `resume_session_at` option** (grep of the package is empty; only the TS SDK has
  `resumeSessionAt`). It is still expressible: `extra_args={"resume-session-at": "<uuid>"}`. That gives
  `--resume-session-at <uuid>`, the exact argv the TS SDK builds, and §1 shows it is honoured in stream-json mode.
- The SDK also has a **standalone, offline** `claude_agent_sdk.fork_session(session_id, directory=None,
  up_to_message_id=None, title=None)` (`_internal/session_mutations.py:240`). It copies the JSONL into a new file in the
  **source's** project dir, cutting inclusively at `up_to_message_id`. It **remaps every uuid** and stamps
  `forkedFrom:{sessionId,messageUuid}`. It does **not** copy file-history snapshots, and it adds a `custom-title` of
  "<title> (fork)". It finds the config dir from `os.environ["CLAUDE_CONFIG_DIR"]` only, with no env parameter
  (`_internal/sessions.py:123`), and it does not rewrite `cwd`. ViberPit gives each process its own `CLAUDE_CONFIG_DIR`
  through the subprocess env, so this helper fits badly. Prefer the CLI path.
- `rewind_files` (`client.py:370`) needs `enable_file_checkpointing=True`, which sets
  `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=true` (`subprocess_cli.py:465`). It also needs
  `extra_args={"replay-user-messages": None}` to learn user uuids. It restores files only, and it is unrelated to fork.
  The CLI's `--rewind-files` cannot be combined with a prompt. ViberPit sets neither `enable_file_checkpointing` nor
  `replay-user-messages` today.

## 3. Live fork test (haiku, account `tony_steinonline.net`)

Setup: a temp git repo `/tmp/d8-fork-spike.KkRT` with `CLAUDE_CONFIG_DIR=~/.claude-accounts/cfg/tony_steinonline.net`.
That account's cfg has no `.credentials.json` token: ViberPit injects `CLAUDE_CODE_OAUTH_TOKEN`
(`server/sessions/account_auth.py:58`). A bare run therefore failed with `Failed to authenticate: OAuth session expired`.
The driver script injected the token the same way ViberPit does (`viberpit.crypto.read_oauth_token`). Note that
`--tools ""` is variadic and swallows the prompt; use `--tools=`.

```
T1  claude -p "Reply with the word ONE" --model haiku --output-format json --tools=
    -> session_id 5651281d-f2ba-40ea-9d94-2f51102be082, result "ONE", $0.0012
T2  claude -p "Reply with the word TWO" --resume 5651281d… --model haiku --output-format json --tools=
    -> session_id 5651281d… (same), result "TWO", $0.0048
```
Chain entries in `projects/-tmp-d8-fork-spike-KkRT/5651281d….jsonl` (non-chain rows like `ai-title`, `queue-operation`,
`api-request*`, `last-prompt` and `cost-state` are interleaved and carry no uuid):
```
attachment ×3 → user dda7c075 "Reply with the word ONE" → attachment ×6 → assistant 848695f2 [text ONE]
  → attachment ×2 → system e3d49628 → user f39c3fe1 "Reply with the word TWO" → attachment → assistant ce8835c3 [TWO] → … system 15d9585c
```
sha256 of the source file was recorded after T2.

```
F1  (same cwd) claude -p "What words have you said so far? List them." --resume 5651281d… --fork-session \
       --resume-session-at 848695f2-9fa5-4359-b80b-bdd2416e9f12  --model haiku --output-format json --tools=
    -> session_id 7d86b8fd-0703-49cb-a01d-47e6e3058663, result 'So far I've said one word, "ONE," in response to your first message.' $0.0057
F2  (cwd=/tmp/d8-fork-spike-other.lhIq, a DIFFERENT git repo) same, but --resume-session-at dda7c075-… (the USER uuid)
    -> session_id 96f6002a-dda2-4233-bcb3-9816efc7c515, result "ONE\n\nWords said so far: ONE. This is my first reply …" $0.0063
```
- (a) **New session id: yes.** `7d86b8fd` and `96f6002a` both differ from `5651281d`.
- (b) **History is truncated: yes.** Neither fork mentions TWO.
- (c) **The source is unchanged: yes.** `sha256sum -c` gave OK after both forks. Size and mtime were unchanged (172362 B).
- (d) **Location and structure.** The fork is a new `<newSid>.jsonl` in the project dir of the **cwd the fork ran in**:
  `projects/-tmp-d8-fork-spike-KkRT/7d86b8fd….jsonl` for F1 and `projects/-tmp-d8-fork-spike-other-lhIq/96f6002a….jsonl`
  for F2. The kept prefix is copied **with the original message uuids preserved**: `dda7c075` and `848695f2` reappear
  verbatim. This differs from the SDK's offline `fork_session`, which remaps them. Each copied row's `sessionId` is
  rewritten to the new id. In F2 every row's `cwd` was rewritten to the new cwd and `gitBranch` to its branch
  (`stripRelocatedCwd`). Entries after the cut point were dropped, including turn 1's trailing attachments and the
  `system e3d49628` row. There is no `forkedFrom` field.
- **The fork point is inclusive.** `--resume-session-at <assistant uuid>` keeps that assistant message, so the fork
  continues after the reply. `--resume-session-at <user uuid>` keeps the user prompt **without** its answer. The next
  prompt then becomes a second consecutive user message, and the model answered both: it replied "ONE" and then answered
  the new question. Any chain-entry uuid is accepted.
- **Resume or fork from a different cwd finds the session.** F2 ran in an unrelated repo and resolved `5651281d` from
  the other project dir. On this host every account's `projects/` is a symlink to `~/.claude/projects`, so a fork onto a
  different account also finds the transcript.
- Live uuids (one extra run, SDK-style `--input-format stream-json --include-partial-messages --replay-user-messages`, $~0.005):
  ```
  user (replayed)       49abc927  == transcript user uuid
  stream_event start    93116278  NOT in transcript
  assistant [thinking]  88345ea1  == transcript
  assistant [text]      44bb119d  == transcript
  result                9692d2b5  NOT in transcript
  ```
  One logical turn wrote **two** assistant entries (thinking, then text). The fork point for "this reply" is the
  **last** top-level assistant entry of the turn.

All spike transcripts, temp dirs and `session-env/<sid>` dirs were removed afterwards. Total spend was about $0.023.

## 4. Message uuids in ViberPit

- **Live stream: the transcript uuids are not carried.**
  - User: `ClaudeDriver._start_turn` emits `UserMessage(turn_id=uuid4().hex, src_uuid=<same>)`
    (`claude_driver.py:1275-1277`; steer echo `:1149-1150`). That is a ViberPit-minted, hyphenless id. The CLI user
    uuid is never seen, because `replay-user-messages` is not requested.
  - Assistant: `Translator._ensure_turn` (`translator.py:64-72`) takes the turn id and `TurnStart.srcUuid` from the
    **first** message that opens the turn. That is the `message_start` **StreamEvent** (`:221-222`), and its uuid is
    not in the transcript (verified above). `TextDelta` and `ThinkingDelta` carry stream-event uuids (`:229-231`).
  - Only `ToolCall`/`ToolResult` carry `AssistantMessage.uuid` (`:297`, `:307`). `TurnEnd.srcUuid` is the ResultMessage
    uuid (`:383`), which is also not in the transcript.
  - The driver already records the real last assistant uuid as `self._last_assistant_uuid` (`claude_driver.py:1485-1489`,
    #1012), but only for the flush wait. It is not emitted.
- **Replay (reload / reattach / paging): the transcript uuids are carried.** `replay.py:290-345` builds
  `UserMessage(turn_id=uuid, src_uuid=uuid)` and `TurnStart/TurnEnd(turn_id=uuid, src_uuid=uuid)` straight from the
  JSONL `uuid`. Each transcript assistant entry becomes its own TurnStart, so the thinking entry and the text entry are
  two "turns".
- Web: `web/src/session/events.ts:27-33` declares `srcUuid` on user, turnStart, turnEnd, delta and tool events.
  `reducer.ts:679` and `:829` key turns by `ev.srcUuid ?? ev.turnId`. On a live session the UI therefore holds a
  ViberPit id for user bubbles and a stream-event id for assistant turns. After a reload it holds the real transcript
  uuids.
- Ways to close it:
  1. Pass `extra_args={"replay-user-messages": None}` and adopt the replayed `UserMessage.uuid` for the user turn.
     Alternatively, emit a small `MessageAnchor{turnId, srcUuid}` event when the SDK `UserMessage` with a uuid arrives.
  2. Emit the consolidated `AssistantMessage.uuid` (top level, `parent_tool_use_id is None`) as an anchor update for
     the current turn, so the last one seen is the fork point.
  3. As a fallback, resolve server-side: the fork endpoint maps a UI turn to the transcript uuid by reading the
     source's JSONL (`replay.py` already parses it).

## 5. How ViberPit spawns and resumes today

- `SessionOrchestrator._spawn_session(..., resume=None, parent_session_id=None, cwd, branch, repo, account, …)`
  (`orchestrator.py:5550-5580`) already threads `resume` to `real_sdk_factory(..., resume)`
  (`claude_sdk_factory.py:108,141`, `ClaudeAgentOptions(resume=resume)`). The public `create(...)` (`:3834`) takes
  `parent_session_id` but **not** `resume`. There is no `fork_session` or `extra_args` plumbing.
  `options_overlay` is mcp-only for coding sessions.
- Engine session id: the driver adopts the **first** session id the stream reports
  (`claude_driver.py:1611-1615`, only while `_engine_session_id is None`). A fork reports the new id in `init` and
  `result`, so the child row gets the fork's id as long as nothing pre-seeds it with the source id.
- Worktrees: `create_worktree(repo_path, branch, session_id, repo_root)` (`src/viberpit/worktrees.py:592`) accepts only
  a **branch name** or None. It has no commit-ish or base-ref parameter.
  - The `None` case detaches at `origin/HEAD` or the local HEAD.
  - For a remote-backed branch it bases on `origin/<branch>`, which loses the source's unpushed commits.
  - A busy local branch gets a child branch `viberpit/<branch>-<short>` off the local tip. That is the source's
    committed HEAD only when the source is on that local branch.
  - **No path carries uncommitted state.** The closest primitive is `snapshot_recovery_ref(worktree, sid, reason)`
    (`:793`). It commits the full worktree state (staged, unstaged, untracked) to a ref without touching the index, but
    its `reason` is limited to drain, delete or forced.
- Transcripts are keyed by the sanitized **cwd** under `<cfg>/projects/`. A fork into a new worktree cwd is fine:
  `--resume` finds the source in any project dir, and the fork is written under the **new** cwd with its rows' `cwd`
  rewritten (F2). On a multi-host fleet the fork must run on the host whose `projects/` holds the source JSONL.

## 6. Codex

- `AgentCapabilities.fork` (`server/sessions/engine.py:52`) defaults to False. `ClaudeDriver.capabilities`
  (`claude_driver.py:294`) does not set it, and `CODEX_CAPABILITIES` sets `fork=False` (`server/codex/capabilities.py:145`).
  **Nothing sets it to True today.**
- The Codex app-server (installed `codex-cli 0.153.2`, which matches the vendored fixtures) has `thread/fork`.
  `ThreadForkParams` includes `lastTurnId` ("fork through, inclusive; the referenced turn cannot be in progress") and
  `beforeTurnId` ("excluding that turn and all later turns"), plus `cwd`
  (`server/tests/fixtures/codex_app_server/0.153.2/v2__ThreadForkParams.json`).
- ViberPit wraps it: `CodexAppServerClient.fork_thread` (`server/codex/protocol.py:657`) and
  `schema.ThreadForkParams(ThreadResumeParams)` (`schema.py:2249`). That dataclass has **no** `last_turn_id` or
  `before_turn_id` fields. It is used only for policy replacement (`server/codex/driver.py:2509`), not for a user fork.
  The schema docstring warns that a native fork "cannot declare dynamic tools", so the session tool surface would be
  lost. This was not exercised live here.

## Implementation implications

**Invocation (Claude).** Spawn the child with:
```python
ClaudeAgentOptions(cwd=<child worktree>, resume=<source engine_session_id>, fork_session=True,
                   extra_args={"resume-session-at": <transcript uuid>}, ...)
```
The CLI argv this produces is `--resume <sid> --fork-session --resume-session-at <uuid>`, verified on 2.1.316. Read the
new id from the stream (the driver already does this). Do not use the SDK's offline `fork_session()`: it reads the
process env for `CLAUDE_CONFIG_DIR`, remaps uuids and does not rewrite `cwd`. `--resume-session-at` is hidden from
`--help`. It is a supported TS-SDK option, but per constitution I gate it behind a kill switch, and add a contract test
that the flag still exists in the installed binary (byte-grep, or an rc=1 "No message found" probe with a bogus uuid,
which needs no model call).

**Which uuid.**
- Assistant "Fork from here": the **last top-level `assistant` transcript entry of that turn**. Its uuid equals
  `AssistantMessage.uuid`, not a stream-event uuid. A turn can write several entries (thinking, text, tool_use).
- User "Fork from here": decide whether the fork should keep the prompt.
  - To fork **before** it, so the user can type a new prompt, pass the uuid of the entry preceding that user message: the
    previous turn's last assistant entry, or its `parentUuid`.
  - Passing the user uuid itself keeps the unanswered prompt, which then merges with the next prompt (observed in F2).
  - With no prior turn, the fork is an empty fresh session.
  - `--resume-drops-turn <user uuid>` can guard the cut against absorbing other content.

**Worktree and cwd strategy.**
- Create the child worktree under the child's session id, branched from the source's **current HEAD sha**. This needs a
  commit-ish or base-sha parameter on `create_worktree`, because a branch name loses unpushed or uncommitted state.
- For uncommitted work, snapshot the source tree to a commit with a variant of `snapshot_recovery_ref`, which needs a new
  `fork` reason, and branch from it.
- Run the forked CLI with `cwd=<child worktree>`. The CLI finds the source transcript across project dirs and writes the
  fork under the child's project dir, with `cwd` rewritten. Keep the same host as the source; on this host the account
  choice is free because `projects/` is shared.

**Gaps to close.**
1. Live events lack transcript uuids for user and assistant messages (§4). Add `replay-user-messages` and an anchor
   event, or resolve server-side from the JSONL.
2. `create()` has no `resume`/`fork`/`resume_at` path, and the SDK factory has no `fork_session`/`extra_args` plumbing.
3. `create_worktree` has no base-sha parameter, and there is no snapshot-for-fork for uncommitted state.
4. Set `AgentCapabilities.fork=True` on `ClaudeDriver`. Codex stays False until `ThreadForkParams` gains
   `last_turn_id`/`before_turn_id` and the dynamic-tool loss is resolved.
5. Forks start without file-history: there are no rewind checkpoints in the child, and ViberPit does not enable
   checkpointing anyway.
6. Refuse a fork at a turn that is still in flight (Codex states this; for Claude the uuid may not be flushed yet).
   Reuse the flush wait on `_last_assistant_uuid`.
