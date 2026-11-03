# D-9 "Rewind to here": verification spike

Date: 2026-11-03 (spike ran 06:12 to 06:17 EST). Constitution III: every claim below was checked against the
installed binary, the vendored SDK or ViberPit's code. Claims that were not checked are listed under **Gaps**.
Parent spec: `2026-11-01-ui-desktop-parity-design.md` §5 (Branching), §6 (Rewind & retry), §10, §11 D-9.
D-8 spike: `2026-11-02-d8-fork-verification.md`.

## 0. Versions and how to read the offsets

- Binary: `~/.local/bin/claude -> ~/.local/share/claude/versions/2.1.317`. `claude --version` prints `2.1.317 (Claude
  Code)`. Its sha256 starts with `eb224419e344575f`. All byte offsets below are into this file.
- The bundle is minified. One function name can mean different things in different chunks: the `aL` at 286142359 is
  unrelated to the `aL` at ~315083200. Each offset below was read in context.
- Python SDK: `claude-agent-sdk==0.2.105`, path `/home/tony/GitHub/viberpit-deploy/.venv/lib/python3.14/site-packages/
  claude_agent_sdk`. Its bundled CLI is 2.1.183, but ViberPit always passes `cli_path` (`claude_bin.py:224`), so
  2.1.317 is the binary that matters.

## 1. File checkpointing

### 1.1 Turning it on

- **SDK.** `enable_file_checkpointing: bool = False` (`types.py:1952`) sets
  `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=true` in the child env (`_internal/transport/subprocess_cli.py:465-466`).
  There is no CLI flag. The embedded TS SDK does the same (`if(E)I.CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING="true"`,
  @303583104).
- **Not with `session_store`.** Both SDKs refuse the combination. The Python SDK raises in
  `_internal/session_store_validation.py:40` ("checkpoints are local-disk only"). The TS SDK says "backup blobs are not
  mirrored, so rewindFiles() fails after a store-backed resume".
- **The CLI gate** is `dD()` at @286792571:
  ```js
  function dD(){if(ht())return!1;if(q())return!1;if(he())return OHo();
    return ri("fileCheckpointingEnabled",!0).value&&!i.CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING}
  function OHo(){return i.CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING&&!i.CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING}
  ```
  - Interactive sessions default to on (setting `fileCheckpointingEnabled`, default true).
  - The non-interactive path (`he()`) needs the env var. Probe A (§5) shows that print mode without the var answers
    `Error: File rewinding is not enabled.`, and probe B shows that the same command with the var restores files.
  - `CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING` overrides both. What `ht()` and `q()` test was not resolved; see Gaps.
- **It must be on at spawn, in every process.** The value is the process env, and nothing found changes it later. In
  SDK mode the `he()` branch ignores settings, so `apply_flag_settings` cannot turn it on either.
  - Snapshots are taken only while `dD()` is true, so turns run without it have no checkpoint.
  - The standalone `--rewind-files` process also needs the var (probes A and B).
  - ViberPit respawns with `--resume` after every daemon restart and model swap, so the factory must set it on every
    spawn: new, resume and fork.
- **ViberPit today sets nothing.**
  - `grep -i checkpoint` over `server/sessions/claude_driver.py` and `claude_sdk_factory.py` finds nothing. The
    factory's `ClaudeAgentOptions(...)` (`claude_sdk_factory.py:141-157`) has no `enable_file_checkpointing`.
  - `replay-user-messages` is not passed anywhere in `server/`.
  - No account cfg dir has a `file-history/` dir (`ls -d ~/.claude-accounts/cfg/*/file-history` is empty). The
    shared `~/.claude/file-history/` holds 32 session dirs, all from interactive runs.

### 1.2 What is captured, and where it is stored

- **Backups are files at `<CLAUDE_CONFIG_DIR>/file-history/<sessionId>/<sha256(path)[:16]>@v<N>`.**
  - Path: `QM()` @286803079, `AK(Fe(),"file-history",n||K(),e)`.
  - Name: `FHo()` @286802551.
  - Probe listing: `05d8badf3fc73a69@v1` (3 B = `v0\n`), `@v2`, `@v3`, and `5c33859252f68c51@v2`.
- **The transcript carries the index, not the content.** Two row types, both with no `uuid` (so they are not chain
  entries):
  ```json
  {"type":"file-history-snapshot","messageId":"21203c3a-…","snapshot":{"messageId":"21203c3a-…","trackedFileBackups":{},
   "timestamp":"2026-11-03T11:12:42.834Z"},"isSnapshotUpdate":false}
  {"type":"file-history-delta","messageId":"79d61cee-…","snapshotMessageId":"21203c3a-…","trackingPath":"a.txt",
   "backup":{"backupFileName":"05d8badf3fc73a69@v1","version":1,"backupTime":"…","realParentDir":"/tmp/d9-rewind-spike.pep0"}}
  {"type":"file-history-delta",…,"trackingPath":"new.txt","backup":{"backupFileName":null,"version":1,…}}
  ```
  `trackingPath` is relative to the cwd. `backupFileName:null` means "did not exist", so a rewind deletes the file.
- **There is one snapshot per user prompt, keyed by the prompt's uuid, and taken before that turn's edits.**
  - The headless loop runs `UNe(…, Ke.uuid)` for each prompt message (@314679954).
  - The probe's snapshot `messageId`s were exactly the three prompt uuids (21203c3a, 0497e020, 2ff905f3).
  - A snapshot for prompt N holds the state before turn N. An edit is first tracked by a `file-history-delta` against
    the open snapshot (`Ynt`), and the next prompt's snapshot records the new version.
- **Edit, Write and NotebookEdit are captured. Bash is not.**
  - `Ynt` (track-edit) has exactly four callers: Write (`wBo` @286837323), Edit (`aAs` @290161153), NotebookEdit
    (@290173454) and artifact write-back (@309054186).
  - Bash never calls it. In the probe, `b.txt`, written by `echo bash > b.txt`, was absent from every `filesChanged`
    and survived every rewind.
- **New files are deleted on rewind.** `LHo` @286799768 deletes a path whose backup is `null`; it refuses symlinks and
  non-regular files and counts them in `skippedLinks`. In the probe, `new.txt` (made by Write in T1) was deleted by
  the rewind to U1.
- **Retention.**
  - The cap is **100 snapshots** per session: `m$n=100` @275773230. Older ones are sliced off, and their unshared
    backups are deleted (`MHo`), except `@v1` originals.
  - Each session's `file-history/<sid>` dir is swept by the startup cleanup once its mtime is older than
    `cleanupPeriodDays`. The default is 30 (`I5t=30` @282838205, `Fw()` @296384210, `kt(){return L("file-history")}`
    @296401318).
  - No per-file size cap on backups was found. The 1 MiB `S4e` limit only bounds turn diff-stat computation.
- **Forks and resumes.**
  - A resume under the same session id reuses the dir ("No need to copy file history for resuming with same session
    id").
  - A resume under a new id, which is a fork, hard-links or copies the backups into `file-history/<newId>` (@286813705).
    The no-model fork probe in §5 produced `file-history/1945a0a7…/` with link count 2.

## 2. Restoring files

- **Wire shape** (control-protocol schema @278386925):
  ```js
  {subtype:"rewind_files", user_message_id:string, dry_run?:boolean}   // "Rewinds file changes made since a specific user message."
  result: {canRewind:boolean, error?:string, filesChanged?:string[], insertions?:int, deletions?:int, skippedLinks?:number}
  ```
- **The SDK headless handler** (@314991926) calls `aL(user_message_id, appState, dry_run)` (@~315083200).
  - Not enabled: `{canRewind:false,error:"File rewinding is not enabled."}`.
  - No snapshot for that id: `{canRewind:false,error:"No file checkpoint found for this message."}`.
  - `dry_run`: `{canRewind:true, filesChanged, insertions, deletions}`, computed by `Bzt` @286796336.
  - Real run: `{canRewind:true, skippedLinks}`. **It returns no `filesChanged`**. `Hhn` @286795024 throws only when
    every differing file failed.
  - A `canRewind:false` without `dry_run` is turned into a control **error**. With `dry_run` it comes back as a
    success carrying `canRewind:false`.
- **Dry run exists and is the confirmation source.**
  - The TS SDK exposes it: `rewindFiles(e,s){…{subtype:"rewind_files",user_message_id:e,dry_run:s?.dryRun}}`
    @303558367.
  - The interactive `/rewind` picker uses the same `Bzt` to preview (@311927973).
  - **The Python SDK does not expose it.** `ClaudeSDKClient.rewind_files(user_message_id) -> None` (`client.py:370`)
    sends `{"subtype":"rewind_files","user_message_id":…}` (`_internal/query.py:753-766`) and discards the response.
    The probe reached the dry run, and the real-run payload, through `client._query._send_control_request(…)`.
- **Ids.** Only a **user prompt uuid** that has a snapshot works.
  - On the live control path, any other id gives "No file checkpoint found".
  - The CLI flag checks the row type first: `--rewind-files <assistant uuid>` →
    `Error: --rewind-files requires a user message UUID, but … is not a user message in this session` (rc=1).
  - Only the totals are per-rewind. There is no per-file `+/-` split.
- **`--rewind-files` CLI** (@314854749): `--resume <sid> --rewind-files <uuid>`, print mode.
  - It runs the same `aL(…, dry_run=false)`, prints `Files rewound to state at message <uuid>`, and exits 0.
  - It has no dry-run.
  - The errors are `--rewind-files requires --resume` and `--rewind-files is a standalone operation and cannot be used
    with a prompt` (both verified).
- **Files changed outside Claude since the snapshot are overwritten without warning.**
  - Probe: `a.txt` was `v2` plus an appended `external` line from outside. The dry run listed it like any other file
    (`insertions 1, deletions 1`, with no flag), and the real rewind wrote `v1`.
  - A file Claude never touched (`ext.txt`, `keep.txt`) is not tracked, so it is never touched.
- **Redo inside the CLI.** Rewinding files forward to a **later** snapshot works while that snapshot is still on the
  conversation chain (probe1: rewind to U3 restored `a.txt=v2` and re-created `new.txt`). The state **after** the last
  turn is never snapshotted, so the CLI cannot get back to "now".

## 3. Truncating the conversation

There are three mechanisms in the binary.

**(a) `rewind_conversation` control request (live, in place).** This is the one the CLI's own remote-control bridge
uses.
- **Handler.** @314992832 (headless loop), validation `af()` @314790800.
- **Request fields.** `target_message_uuid` (required). `last_seen_user_message_uuid`, `allow_assistant_target`,
  `allow_summarized_target`, `interrupt_if_running`, `refuse_if_usage_limited` and `expires_at` are optional.
- **Response.** `{rewound, targetMessageUuid, prefillText, precedingAssistantUuid, resolution?}`, or
  `{rewound:false, error, reason}`.
- **Not in either SDK's public surface.** The Python SDK has no method, and the embedded TS SDK has none either:
  `rewindConversation` occurs only inside the CLI's own classes. It is an internal surface under constitution I.
- **Verified responses.** All of these ran with no model call, on a resumed process with no prompt; the first one also
  ran on the live probe client:

  | request | response |
  |---|---|
  | target = U2, live client, during T3 | `rewound:false, reason:"turn_running"` |
  | target = U2, later turns exist, no `last_seen_user_message_uuid` | `rewound:false, reason:"stale_target"` |
  | target = U2, `last_seen_user_message_uuid` = U3 | `rewound:true, targetMessageUuid:U2, prefillText:"Use the Edit tool on a.txt …", precedingAssistantUuid:"9346e4fc…"` (T1's last assistant entry) |
  | target = a `tool_result` user row | `reason:"target_splits_tool_call"` |
  | target = an assistant uuid | `reason:"target_not_found"` (needs `allow_assistant_target`) |
  | target = U1 (first prompt) | `rewound:true, precedingAssistantUuid:null`, full prompt text in `prefillText` |

- **The cut is exclusive of the target.** It drops the target prompt and everything after it, and `prefillText` is
  that prompt's text. That is exactly Edit & resend.
- **Persistence is append-only.** The JSONL prefix was byte-identical afterwards (`cmp` OK). The rewind appended:
  ```json
  {"type":"last-prompt","leafUuid":"9346e4fc-…","explicit":true,"rewound":true,"cutUuid":"0497e020-…","sessionId":"00f0a637-…"}
  {"type":"last-prompt","lastPrompt":"Run this Bash command …","leafUuid":"9346e4fc-…","sessionId":"00f0a637-…"}
  {"type":"cost-state",…}
  ```
  The first prompt gives `"leafUuid":null`.
- **The newest leaf pointer picks the chain.** After the rewind, a fresh process no longer sees U2 or U3:
  - `--rewind-files U3` and `--rewind-files U2` gave "is not a user message in this session".
  - The dry run to U3 gave "No file checkpoint found".
  - `--resume-session-at <old leaf aa40d77f>` gave `No message found with message.uuid of: aa40d77f…`, rc=1.
  - On a **copy** of the session, appending one row `{"type":"last-prompt","leafUuid":"aa40d77f…","explicit":true}`
    brought the whole pre-rewind chain back: the dry run to U3 answered `canRewind:true`.
  - So the dropped entries stay in the file and the rewind is reversible, but **no API re-points the leaf**.

**(b) `--resume <sid> --resume-session-at <uuid>` without `--fork-session` (respawn, in place).**
- The loader (@315098690) slices the in-memory chain inclusively at `<uuid>`.
- With no prompt, nothing is written. Probe O5 appended only a `cost-state` row, with no chain or leaf row. So the cut
  takes effect only when the next prompt is appended.
- That next write was **not** verified (it needs a model turn; see Gaps).

**`--resume-drops-turn <prompt uuid>`** (validator `vDs` @311318063) is the single-turn guard for (b).
- The discarded range must **start with** the declared prompt, and may hold only that turn's assistant, system and
  progress rows and furniture attachments.
- It refuses another user prompt, a queued command, a compaction summary, a system-injected prompt or an
  externally-sourced entry.
- Verified, with no model call:
  - `at=9346e4fc` (end of T1), `drops=U2` → `Resume rejected by --resume-drops-turn: … range contains a user entry not
    attributable to the declared turn; entry 7 [type=user, uuid=2ff905f3…]`, rc=1.
  - `at=fa035a71` (end of T2), `drops=U3` → rc=0.
  - `at=9346e4fc`, `drops=U3` → `range does not start with the declared turn prompt`.
  - `drops=not-a-uuid` → `declared turn id is not a UUID`.
- It exists so that a "retry the last turn" truncation cannot silently swallow a prompt queued meanwhile. It is
  useless for a multi-turn rewind.

**(c) Interactive `/rewind`** (double-Esc), with aliases `checkpoint` and `undo`. The command definition is
@287511909; the picker, `MessageSelector`, is @311927973.
- It offers **Restore code and conversation / Restore conversation / Restore code / Summarize from here / Summarize up
  to here / Never mind**. Restoring code calls the file rewind (`Hhn`), with the preview from `Bzt`.
- Restoring the conversation calls `rewindConversationTo(msg,"message_selector")` (@312045389). That keeps
  `slice(0, index)`, which drops the selected prompt, and then `restoreDraftFrom(msg)` puts the prompt text back in
  the composer: the same semantics as (a).
- The `undo` alias opens this same picker. There is **no redo**: no "Undo rewind" string exists in the binary.

## 4. Running safely

- **Between turns on a live client: yes.**
  - `rewind_files` (dry and real) and `rewind_conversation` all answered on the open probe client with no restart.
  - They also answered on a freshly resumed no-prompt process, so the client does not have to be stopped.
- **During a running turn:**
  - The `rewind_files` handler has **no** turn-running guard: it calls `aL` directly. A dry run mid-T3 answered
    normally. A real file rewind mid-turn would race the model's own edits and was not attempted.
  - `rewind_conversation` refuses with `turn_running` unless `interrupt_if_running` is set.
  - ViberPit must disable both while a turn runs, which the spec already says.
- **Order: files first, conversation second.** The conversation cut removes the target prompt's own snapshot from the
  chain a resumed process loads (O2/O3 above). Rewinding files to U2 after cutting at U2 therefore fails in any later
  process.
- **Account rotation breaks file rewind silently.** `file-history` lives under each account's `CLAUDE_CONFIG_DIR`, and
  ViberPit shares only `{"plugins","commands","settings.json","settings.local.json","projects"}`
  (`src/viberpit/configdir.py:17`, `SHARE_ALLOWLIST`).
  - Probe: the same session, resumed under another config dir with `projects` symlinked in.
  - The dry run to U1 → `filesChanged:[a.txt,new.txt], insertions 0, deletions 2`. The backup is missing, so the dry
    run reports the whole file as deleted.
  - The real rewind → `{canRewind:true, skippedLinks:0}`, yet `a.txt` was **not** restored (it stayed `v2`) and
    `new.txt` **was** deleted. That is a partial restore reported as success.

## 5. Probe log (trimmed)

- **Setup.**
  - Throwaway non-git cwd: `/tmp/d9-rewind-spike.pep0`, seeded with `a.txt=v0` and `keep.txt`.
  - Account: **`tony_steinonline.net`**, `CLAUDE_CONFIG_DIR=~/.claude-accounts/cfg/tony_steinonline.net`. The token was
    injected as `CLAUDE_CODE_OAUTH_TOKEN` from `viberpit.crypto.read_oauth_token`, as ViberPit does.
  - Python SDK 0.2.105 with `cli_path=~/.local/bin/claude`, `model="haiku"`, `bypassPermissions`,
    `enable_file_checkpointing=True`, `extra_args={"replay-user-messages": None}` and `setting_sources=[]`.
- **Model calls: 3 haiku turns, about $0.0044 in total.** The CLI's `cost-state` `totalCostUSD` was 0.004426 after T3.
- **Session** `00f0a637-6719-4040-a0bb-9b333b847e2a`.

```
T1 U1=21203c3a  Read a.txt; Write a.txt=v1; Write new.txt=hello; Bash `echo bash > b.txt`   -> a=v1 b=bash new=hello
T2 U2=0497e020  Edit a.txt v1->v2                                                           -> a=v2
   replayed UserMessage.uuid == transcript user uuid == file-history-snapshot.messageId (all three prompts)
dry U1 -> {canRewind:true, filesChanged:[a.txt,new.txt], insertions:1, deletions:2}
dry U2 -> {canRewind:true, filesChanged:[a.txt], insertions:1, deletions:1}
dry 0000… -> {canRewind:false, error:"No file checkpoint found for this message."};  real 0000… -> control error, same text
T3 U3=2ff905f3  Bash `sleep 12`; at +6 s: dry U1 answered normally; rewind_conversation(U2) -> turn_running
outside: a.txt += "external", new ext.txt
dry U2 -> [a.txt] 1/1;  real U2 -> {canRewind:true, skippedLinks:0}            -> a=v1 (external line gone), new, b, ext kept
real U1 -> {canRewind:true, skippedLinks:0}                                     -> a=v0, new.txt DELETED, b.txt kept, ext.txt kept
real U3 ("redo")                                                                -> a=v2, new.txt re-created
rewind_conversation(U2) on the live client, no last_seen -> stale_target; JSONL unchanged
```
No-model CLI probes, run afterwards against the same session. They used
`CLAUDE_CONFIG_DIR=…/tony_steinonline.net`, no prompt, and `</dev/null`:
```
A  -p --resume S --rewind-files U1                      (no env)  -> "Error: File rewinding is not enabled." rc=1
B  same with CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=1           -> "Files rewound to state at message U1" rc=0
C  --rewind-files <assistant uuid>                                 -> "…is not a user message in this session" rc=1
D  -p "hi" … --rewind-files U1                                     -> "…standalone operation and cannot be used with a prompt"
E  -p --rewind-files U1 (no --resume)                              -> "Error: --rewind-files requires --resume"
stream-json: initialize; rewind_conversation(U2, last_seen U3)     -> rewound:true, prefillText, precedingAssistantUuid 9346e4fc
O1/O2  --rewind-files U3 / U2 after the cut                        -> "…is not a user message in this session"
O4  --resume S --resume-session-at aa40d77f (old leaf)             -> "No message found with message.uuid of: aa40d77f…"
O5  --resume S --resume-session-at 6c84430a, no prompt             -> rc=0, only a cost-state row appended
fork (copy session, --fork-session --resume-session-at aa40d77f, no prompt) -> file-history/<forkId> hard-linked, NO <forkId>.jsonl
```
The resumed CLI ran the user's `SessionStart:resume` hooks (`~/.claude/settings.json` is symlinked into the account),
which were harmless here.

**Cleanup.**
- Removed: the probe cwd, the alternate config dir, the hand-made session copy `0a67d763…`, and the `file-history/`
  and `session-env/` dirs of all three session ids (`00f0a637…`, `0a67d763…`, `1945a0a7…`).
- **Left in place: the probe transcript**
  `~/.claude-accounts/cfg/tony_steinonline.net/projects/-tmp-d9-rewind-spike-pep0/00f0a637-6719-4040-a0bb-9b333b847e2a.jsonl`.
  The account's `projects/` is a symlink, so the real path is `~/.claude/projects/…`.

## Second probe: live rewind, undo, mode-only (2026-11-03 06:18–06:21 EST)

Account `tony_steinonline.net` with `--model haiku`, 4 turns (about $0.006 in total), in a throwaway non-git cwd
`/tmp/d9-rewind-spike2.*`. The session was `785a0add-7e20-4165-ac2b-02f32f8a5c5a`, a single live stream-json client
with checkpointing on. The log was trimmed from `/tmp/d9-probe3.log`.

```
U1 4d788d59  "Read a.txt, then Edit v0→v1"        → a.txt v1
U2 8a895c7e  "Edit v1→v2"                         → a.txt v2
rewind_files U2 dry_run  → {canRewind, filesChanged:[a.txt], +1 −1}
(own backup of a.txt = "v2\n")
rewind_files U2          → {canRewind, skippedLinks:0}            a.txt v1
jsonl length L = 146404
rewind_conversation U2 (last_seen=U2) → {rewound:true, prefillText:"Use the Edit tool … v1 with v2 …",
                                         precedingAssistantUuid: dcc8c713 (U1's last entry)}
U3 6433130b  "Quote every message I sent"  (SAME live client)
   → "1. \"Read a.txt, then use the Edit tool to replace v0 with v1 …\""      ← U2 is gone for the model
rewind_files U2 dry_run  → filesChanged [] (already at that state)
rewind_files U1 dry_run  → [a.txt]; real → a.txt v0                         ← older checkpoints intact post-cut
UNDO: stop client; truncate jsonl 155533 → 146404; restore backup → a.txt v2; resume --resume <sid>
rewind_files U2 dry_run → canRewind, [a.txt]; U1 dry_run → canRewind, [a.txt]   ← snapshots still resolve
rewind_files U3 dry_run → canRewind:false "No file checkpoint found for this message."  (U3 truncated away)
U4 c0793092  "Quote every message I sent"
   → "1. \"Read a.txt … v0 with v1 …\"  2. \"Use the Edit tool to replace v1 with v2 …\""   ← old turn 2 is back
chmod 600 a.txt (mode-only change)
rewind_files U4 dry_run → filesChanged [] ; real → a.txt mode 0o600 → 0o644          ← restored, not previewed
```

**Results:**
1. **A live client works after `rewind_conversation`.** The next prompt on the same client did not see the cut turn,
   and later file rewinds to older prompts still worked. There's no need to respawn after a rewind.
2. **Undo by byte truncation plus `--resume` works end to end.**
   - The model sees the pre-rewind conversation again.
   - The CLI checkpoints for the restored turns resolve again.
   - ViberPit's own backup puts the files back.
   - A prompt sent after the rewind is dropped by the truncation, along with its checkpoint. This confirms that undo
     must only be offered until the next prompt.
3. **Mode-only changes are restored but not previewed** (formerly gap 4, now confirmed). The confirmation copy must not
   claim the list is exhaustive. ViberPit's undo backup must therefore record mode for every file the real rewind could
   touch, not only the dry-run list.
   - Practical rule: also back up the files the CLI's file-history index tracks for the session, or accept that undo
     cannot restore a mode-only revert.
   - The plan rules on which.
- The probe cwd was deleted. The probe transcript was left at
  `~/.claude/projects/-tmp-d9-rewind-spike2-*/785a0add-….jsonl`.

## Third probe: rows an idle client appends (2026-11-03, no model)

Why: undo stops the client and then checks that nothing past the pre-rewind length is a
conversation entry before truncating. So it must know which rows a client writes with no turn.

**Setup.**
- Binary 2.1.317.
- A copy of the second probe's transcript (`785a0add…`), resumed under a throwaway `CLAUDE_CONFIG_DIR`, `HOME` and
  cwd, with `--resume <sid> --setting-sources ""`.
- `ANTHROPIC_BASE_URL=http://127.0.0.1:9`, no credentials, and no prompt. Only control requests were sent.
- Stdin was held open for 6 s, then closed.
- Script: `/tmp/d9t4-probe-scripts/probe.py`. Log: `/tmp/d9-t4-probe.log`. The probe dirs were deleted.

**Results** (no `assistant` frame was seen in any run):

| run | control replies | rows appended to the transcript |
|---|---|---|
| idle, `initialize` only | success | `tools-used {type, tools, sessionId}`, `cost-state {…}` |
| plus `set_permission_mode acceptEdits` | success | the same two rows |
| plus `set_model haiku` | success | the same two rows |

- The rows are written at exit. Neither carries a `uuid`. No `mode`, `atis-latch` or `ai-title` row appeared, and no
  second transcript was created.
- So an undo's tail allowlist must admit `tools-used`, along with the rewind's own `last-prompt` and `cost-state`
  rows. Otherwise every undo would be refused once the client it stopped has exited.
- Not covered: rows written by a client with live tool use, title generation or a real model turn. Those come with
  `uuid`-bearing entries, which the allowlist refuses anyway.

## Gaps (not verified)

1. **In-place `--resume-session-at` followed by a prompt** (the respawn alternative). This is no longer needed, because
   `rewind_conversation` on a live client is verified above. Left unverified.
2. **A real `rewind_files` during a running turn.** Only a dry run was sent mid-turn.
3. **What `ht()` and `q()` in `dD()` test.** They are two more conditions that switch checkpointing off. They did not
   fire in SDK stream-json mode on this host.
4. ~~Mode-only differences~~: confirmed by the second probe, see above.
5. **The `Lp(e)` branch of the handler**, which strips `filesChanged`, is a remote/cloud condition that did not apply
   locally. It was not resolved.
6. **No newer Python SDK was checked** for a `dry_run` parameter. 0.2.105 has none.
7. **Codex.** Not examined. `AgentCapabilities` has no rewind capability yet.

## Implementation implications

**Spawn.** Add `enable_file_checkpointing=True` to `real_sdk_factory` for every coding spawn: new, resume and fork.
- **Kill switch.** Gate it with a kill switch, e.g. `VIBERPIT_REWIND_ENABLED`. When off, Rewind is hidden and nothing
  else changes.
- **Share `file-history` across accounts.** Add `"file-history"` to `SHARE_ALLOWLIST` in `src/viberpit/configdir.py`,
  so a rotated session can still restore. Without it, a rewind after rotation is silently partial (§4).
- **Turns before the flip.** A session's checkpoints start with the first turn spawned under the flag. For earlier
  turns the dry run answers "No file checkpoint found", and the UI should then offer conversation-only rewind.
- **Host affinity.** Backups are local disk, on the same host as the transcript. Rewind must run on the session's
  host, through `FleetOrchestrator`, with a real RPC on `RemoteOrchestrator`.

**Which id the UI sends.** Send the user turn's **CLI prompt uuid**.
- That one id serves all three operations:
  - It is the `rewind_files` key (snapshot `messageId`).
  - It is the `rewind_conversation` target.
  - Its predecessor is the conversation cut point.
- On replay, ViberPit's user `turn_id` already **is** that uuid (`replay.py:380`).
- **Live sessions do not have it yet.** The driver mints `uuid4().hex` (`claude_driver.py:1317-1320`). Two ways to fix
  it:
  - Pass `extra_args={"replay-user-messages": None}` and emit a user-side anchor event when the replayed
    `UserMessage.uuid` arrives. The probe verified that this uuid equals the transcript uuid and the snapshot key.
  - Or resolve it server-side from the JSONL, as D-8's `turn_anchors` rebuild does.
- **`last_seen_user_message_uuid`** is the latest prompt uuid the server holds for the session.

**Mechanism.** On an idle live client, in this order:
1. Send `rewind_files {user_message_id, dry_run:true}`. This builds the confirmation.
2. On confirm, take the undo backup (below).
3. Send `rewind_files {user_message_id}`. On error, stop.
4. Send `rewind_conversation {target_message_uuid, last_seen_user_message_uuid}`.
5. Emit the rewind divider, and drop the cut turns from ViberPit's event log view.

Notes:
- Prefer `rewind_conversation` over a respawn with `--resume-session-at`:
  - It validates the target (it refuses `turn_running`, `stale_target` and `target_splits_tool_call`).
  - It persists at once.
  - It returns `prefillText`.
  - It handles the first prompt (`leafUuid:null`), which `--resume-session-at` cannot express.
- **It is an internal surface** (constitution I). Wrap both control requests in one driver adapter, and add a no-model
  contract test against the installed binary: `rewind_conversation` with a bogus target → `target_not_found`, and a
  `rewind_files` dry run with a bogus id → "No file checkpoint found".
- The Python SDK's `rewind_files()` drops the response and has no `dry_run`, so the adapter must send the control
  request itself.
- Map `canRewind:false` and every `reason` to a 409 with a code.

**API (spec §10).**
- `POST /sessions/{id}/rewind {toTurnId, mode: "files_and_conversation" | "conversation" | "files", dryRun?, requestId}`.
- `dryRun:true` returns `{files:[abs paths], insertions, deletions, checkpointAvailable}` and changes nothing.
- The real call returns `{rewindId, prefill, filesRestored, skippedLinks}`. `prefill` is `prefillText`.
- `POST /sessions/{id}/rewind/undo {rewindId}`.
- Make both idempotent per `requestId`, as fork is. Refuse with 409 while a turn runs.

**Confirmation file list.**
- Show the dry-run `filesChanged`, made relative to the worktree, with the aggregate `+insertions −deletions`. The CLI
  gives no per-file split.
- Mark files that will be **deleted**: they are in the list but have no pre-session copy. ViberPit's own undo backup
  (below) shows which.
- Always say: "Changes made by shell commands are not restored, and edits made outside Claude since that message are
  overwritten."

**Undo design. Recommendation: a ViberPit-owned pre-rewind checkpoint, which is option (a), done by ViberPit because
the CLI cannot.**
- **Files: back up exactly the dry-run `filesChanged` paths.**
  - Store each path's bytes and mode, or "absent", under ViberPit state keyed by `rewindId`.
  - Do it after the confirm and before the real `rewind_files`.
  - Undo writes them back, and deletes the paths that were absent.
- **Why not the CLI's own checkpoints.** They cannot do it:
  - The post-last-turn state is never snapshotted (§2).
  - After the conversation cut, the dropped turns' snapshots are unreachable on resume (§3).
  - There is no redo (§3c).
- **Why not `snapshot_recovery_ref` as the undo mechanism.** It covers only the git worktree and misses gitignored and
  outside-worktree paths that Claude edited. It returns None on a clean tree. A whole-tree restore would also clobber
  unrelated Bash-made work.
  - It is still worth taking as a safety net. It needs a new `rewind` reason in `RECOVERY_REASONS`.
- **Conversation.** Before step 4, record the transcript's byte length.
  - Undo stops the client, truncates the JSONL back to that length, and respawns with `--resume`.
  - The rewind is append-only: the prefix was byte-identical, and only `last-prompt` and `cost-state` rows were
    added. So truncating restores the exact pre-rewind file and pointer without writing any CLI row format.
  - (Appending a `last-prompt` row with the old `leafUuid` also works, as verified on a copy, but it hand-writes an
    internal row format. Prefer truncation.)
- **When undo is offered.** Only until the next prompt is sent, which the divider can enforce. After that the
  truncation would drop a real turn.
- **Rejected: (b) fork the old chain first.** A fork with no prompt persists **no** transcript (§5: no `<forkId>.jsonl`),
  so it cannot hold the old chain without a model turn.
- **Rejected: (c) CLI redo.** It does not exist.

**What Edit & resend and Rewind & retry reuse.**
- **Edit & resend** is the same `POST /rewind` with `mode:"files_and_conversation"`. Its `prefill` (the CLI's
  `prefillText`) goes into the composer. That matches `/rewind` → `restoreDraftFrom` in the CLI, and D-8's
  fork-prefill composer path.
- **Rewind & retry** on an error card is the same call at the failed turn's prompt uuid, followed by sending `prefill`
  unchanged.
  - If a respawn path is ever used instead, `--resume-drops-turn <that prompt uuid>` is the guard that the cut drops
    only the failed turn.
- **`/rewind`** in `server/sessions/commands/catalog.py` opens the same message picker. It never needs its own path.
- **Codex.** All of these stay disabled until a Codex mechanism is verified: `AgentCapabilities` gains a `rewind` flag,
  False for Codex.
