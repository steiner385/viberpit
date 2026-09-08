# Spike: can a RESUMED Codex thread re-declare `dynamicTools`? (2026-09-06)

**Question.** A ccswitch coding session on Codex declares its two ccswitch tools
(`session_context`, `report_feedback`) as `dynamicTools` on `thread/start`. Every later
attach resumes the thread instead of starting one. Can a resumed thread get the tool
surface back — through `thread/resume`, or through any other app-server method?

**Answer: no, on codex-cli 0.153.2, and the failure mode is silent.** `dynamicTools`
exists on `ThreadStartParams` and nowhere else in the v2 surface, and `thread/resume`
does not reject the unknown field — it accepts and ignores it. So there is nothing to
send *and* no error we could detect if we sent it anyway.

Binary: `/usr/bin/codex`, `codex-cli 0.153.2` (the pinned version — `scripts/codex-cli.sha256`).
Both arms below ran with a throwaway `CODEX_HOME` and an unauthenticated account.

## 1. The declared contract — every v2 params type, from the live binary

```
$ codex app-server generate-json-schema --experimental --out /tmp/codex_spike_schema
```

Sweeping every `v2/*.json` for a property whose name contains `tool` or `dynamic`:

| schema | tool-ish properties |
| --- | --- |
| `ThreadStartParams` | **`dynamicTools`** |
| `TurnStartParams` | `toolOutput` (a turn *input*, not a declaration) |
| `McpServerToolCallParams` | `tool` (call one MCP server's tool) |
| `AppsReadParams` | `includeTools` |
| `ModelProviderCapabilitiesReadResponse` | `namespaceTools` |

The four plausible "re-declare later" candidates all lack it:

```
ThreadResumeParams        -> approvalPolicy approvalsReviewer baseInstructions config cwd
                             developerInstructions excludeTurns history initialTurnsPage
                             model modelProvider path permissions personality
                             runtimeWorkspaceRoots sandbox serviceTier threadId
ThreadForkParams          -> ... baseInstructions developerInstructions ... (no dynamicTools)
ThreadSettingsUpdateParams-> approvalPolicy approvalsReviewer collaborationMode cwd effort
                             model multiAgentMode permissions personality sandboxPolicy
                             serviceTier summary threadId
TurnSettingsUpdateParams  -> approvalsReviewer effort model serviceTier summary threadId turnId
```

The full client-request method index (246 methods) contains no `tools/*`-shaped method
at all — no `thread/dynamicTools/set`, no `thread/tools/declare`. The only tool-related
methods are `mcpServer/tool/call` and `hooks/list`, neither of which declares a
host-executed tool on an existing thread.

## 2. Live probe against the running app-server

Throwaway client (deleted; the transport is a ~40-line copy of
`scripts/spike_assistant_codex.py`'s `Spike`), `initialize` + `initialized`, then:

```jsonc
>> thread/start {"cwd":"/tmp/…","sandbox":"read-only","approvalPolicy":"untrusted",
                 "dynamicTools":[{"type":"function","name":"session_context",
                                  "description":"probe","inputSchema":{"type":"object",…}}]}
== {"result":{"thread":{"id":"01a07997-…","status":{"type":"idle"},"path":"…/rollout-….jsonl",…}}}
```

`thread/start` accepted the declaration with no auth and no warning. Then, on that same
thread:

```jsonc
>> thread/resume {"threadId":"01a07997-…","path":"…/rollout-….jsonl","cwd":"/tmp/…",
                  "sandbox":"read-only","approvalPolicy":"untrusted","dynamicTools":[…]}
== {"error":{"code":-32600,"message":"no rollout found for thread id 01a07997-…"}}

>> thread/resume {…same, WITHOUT dynamicTools}                      # control
== {"error":{"code":-32600,"message":"no rollout found for thread id 01a07997-…"}}

>> thread/resume {"threadId":"01a07997-…","path":"…","cwd":"/tmp/…",
                  "ccswitchNonsenseField":123}                       # unknown-field control
== {"error":{"code":-32600,"message":"no rollout found for thread id 01a07997-…"}}
```

Three readings:

1. **A resume cannot be completed here.** The rollout file a thread resumes *from* is
   only written once the thread has run a turn, and a turn needs real ChatGPT
   credentials, which this probe did not have. So the "does a resumed thread come back
   with the tools?" arm is **not reachable without credentials** — recorded as a limit,
   not papered over.
2. **`dynamicTools` on `thread/resume` is not refused.** The arm carrying it and the arm
   carrying a garbage `ccswitchNonsenseField` both produced the *same* error as the
   clean control — the rollout lookup, not a deserialization complaint. The params type
   tolerates unknown fields, so sending `dynamicTools` on resume would be silently
   dropped. **A silent drop is worse than a rejection**: ccswitch would believe the
   surface was declared while the model could not see it.
3. **Duplicate names are refused server-side**, matching ccswitch's own
   `ThreadStartParams.__post_init__` guard rather than only being caught locally:

```jsonc
>> thread/start {…,"dynamicTools":[spec, spec]}
== {"error":{"code":-32600,"message":"duplicate dynamic tool name: session_context"}}
```

## 3. Conclusion — the resume gap stands as designed

* **Fresh `thread/start` declares the surface.** Implemented in
  `CodexAgentDriver.start` from `CodexSessionTools.dynamic_tools`.
* **A resumed coding thread has no ccswitch tools, and keeps its thread anyway.** The
  assistant answers this same vendor gap by never resuming
  (`ASSISTANT_CODEX_RESUME_UNSUPPORTED`); a coding session cannot make that trade,
  because its thread *is* the work — losing the context to keep two optional tools is
  the wrong side of the trade. So it resumes without them and records
  `codex.driver.session_tools_unavailable_on_resume` once, at INFO, so the gap is
  measurable in the journal rather than invisible.
* **Nothing is sent on `thread/resume`.** Sending the field "just in case" would be
  dropped silently and would make the code read as though resumed threads were covered.
* **Not chosen: `thread/fork`.** It has `baseInstructions` but no `dynamicTools` either,
  so forking would lose the surface *and* the thread identity.
* **Re-check trigger.** `server/tests/test_codex_dynamic_tools.py`
  `test_thread_resume_has_no_dynamic_tools_field` fails the moment a future codex-cli
  grows the field on resume, at which point this decision should be revisited
  deliberately. The credential-gated arm (reading a resumed thread's *effective* tool
  list) is worth running during the next interactive Codex smoke
  (`scripts/smoke_codex_interactive.py`), which already has a real account.
