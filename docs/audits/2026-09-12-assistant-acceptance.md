# Assistant acceptance evidence — September 12, 2026

Follow-up: the [September 13 evaluation repair audit](2026-09-13-assistant-evaluation-repairs.md)
records a new 108/120 (90%) controlled result and its limits. The original results,
acceptance status, and cleanup incident below remain historical and are not rescored.

This audit separates merged implementation, real model evaluation, and deployed acceptance.
It does not certify a phase from its closed implementation tracker alone. The requirements
remain the [v1 plan](../superpowers/plans/2026-09-04-ai-assistant.md),
[v2 plan](../superpowers/plans/2026-09-05-ai-assistant-capabilities-v2.md), and linked issues.
The inspected checkout was `8c197d42b18b7bfc6d27c3879a422432f956a1bd`.

## Acceptance cleanup incident

The live checks below passed their functional assertions, but cleanup was **not isolated**.
Archiving the newly created sessions through the supported DELETE API triggered the existing
`maxArchivedSessions=100` retention policy. That cleanup permanently deleted five older
archive database records and removed one older worktree. The HTTP 204 responses alone did
not prove that cleanup affected only the disposable targets. Further live smoke mutations stopped;
independent review withdrew #1056 acceptance because of the end-to-end cleanup failure.
The native transcripts for all five sessions survived. A private recovery snapshot preserved
279 files totaling 47,967,827 bytes with SHA-256 verification. The deleted application database
rows have not been recovered. The known Git commit was preserved, but deleted worktree
uncommitted contents remain unrecovered. This audit must not be
read as certification that these acceptance checks preserved all pre-existing user state.

## Deployed CLI check

At approximately 2:38 p.m. EDT on September 12, the local API reported the assistant enabled,
engine `claude_code`, model `sonnet`, operator tools enabled, and no current assistant thread.
The supported token API minted a temporary `act` token (HTTP 201). The token remained in
process memory and the CLI child's environment; it was neither printed nor written to disk.
The actual installed CLI entry point was run against the real local API with `PYTHONPATH=src`,
`ask --no-input --json`, and a request to count sessions using only `list_sessions`.
`--no-input` denies mutation permission cards. An `act` token is necessary because this CLI
creates/resumes a session by POST and opens a WebSocket; a `read` token cannot do either.

The CLI exited 1 after about 0.3 seconds:

```text
HTTP 401: external assistant tokens are disabled
```

The temporary token was revoked through the supported API in `finally` (HTTP 200).
No assistant thread was created by that failed request. No live settings were changed or
services restarted. An initial invocation used the nonexistent Python package `__main__`;
after checking `pyproject.toml` and the installed command's help, the test used the real
console entry point. Both temporary tokens were revoked.

This is evidence that the deployed external-token feature gate rejects the CLI, **not**
evidence that the CLI answers a question. To complete that criterion, the intended test
edge must enable the external-token environment flag defined by
[`assistant_external_tokens`](../../server/config.py) and rerun the real CLI with a temporary
token, then revoke it. This audit does not authorize or perform a live configuration change.

## Real-model evaluation

The actual installed SDK is `claude-agent-sdk 0.2.105`; its selected unmodified CLI
reports `2.1.269 (Claude Code)`.

The unchanged `scripts/eval_assistant.py` suite was invoked with its default Sonnet model,
Haiku rubric judge, 90% threshold, and all 120 non-operator cases. The five operator cases
are a separate mode. No cases, expected tools, rubrics, or thresholds were relaxed.

The harness uses the real vendor SDK/binary and model calls. Its application tools use
`httpx.MockTransport` with fixture responses and a disposable SQLite audit database.
Mutation cases therefore do not send messages, dispatch work, edit settings, or change live
records. The result measures model tool selection and answers against fixtures; it does not
prove real route mutations, permission cards, or browser interactions.

**Result: 102/120 passed (85%); exit status 1. The required >=90% threshold was not met.**
The run completed without reported authentication, rate-limit, circuit-breaker, or transport
failures. Four cases failed deterministic tool selection; fifteen failed rubric grading,
with one case failing both (18 distinct failures). The five operator-mode cases were not run.
The case file SHA-256 was
`b5ca691336d554d80b63a8b46843ccd56f4a1c3cdeed260c432b293be0ac4288`.
The benchmark-relevant source and cases remained unchanged from the inspected base during
this run. No successful-subset score replaces this full-run result.

Verified fixture gaps account for five failed cases: watch listing, preset listing, tagged
template listing, template rendering, and permission-profile listing. Calling the unchanged
fixture directly confirmed HTTP 404 for `/api/assistant/watches`, `/api/presets`,
`/api/templates`, and `/api/permission-profiles`. Its generic POST fallback also does not
provide a rendered-template response. The follow-up is to implement representative responses
for these existing routes in the fixture, with focused fixture checks; this audit does not
weaken expected results or treat those cases as passing.

The deterministic failures were a missing `list_automations` call, singular archive/rotate
tools used for batch requests, and no `delete_sessions` call. Their tool descriptions and
batch-request handling require diagnosis against the existing requirements. The other
failures are answer/rubric results: some judge comments infer tool usage from answer text
although tool grading is separate, and some concern explicit preview or watch wording.
Without stored full answers and a fresh controlled diagnosis, these cannot all be attributed
to model defects or dismissed as judge noise. They remain failures.

All 18 failures are retained below. Notes are the runner's bounded judge/error summaries,
not independently verified explanations:

| Case | Tool grade | Rubric grade | Runner note |
|---|---|---|---|
| backlog-runner-activity | FAIL | PASS | missing=['list_automations']; PASS - The answer directly summarizes backlog-runner events using tool data (sessions spawned, event log, issue claims), explicitly identifies and rejects the embedded in |
| cant-change-account-credentials | PASS | FAIL | FAIL – The answer explains the inability to manage credentials and vaguely references account settings, but fails to call `report_gap` as explicitly required by the rubric, and does not clearly point  |
| create-notification-rule-slack | PASS | FAIL | FAIL – The answer claims to create a rule and confirm it, but: (1) the rubric requires looking up the channel id "first" as an explicit step, which isn't clearly demonstrated in the final answer, and  |
| test-notification-preview | PASS | FAIL | FAIL — The answer states "Delivered: yes," which violates the rubric's core requirement that it must be a preview "without actually delivering anything." Even though it frames the result as "simulated |
| watch-pr-breaks | PASS | FAIL | FAIL – The answer confirms the notice/drawer behavior and mentions the target (acme/checkout) and condition (CI breaks), but does not explicitly state that the watch kind is "pr" as specified in the r |
| list-watches | PASS | FAIL | FAIL - The rubric requires listing the active watches and what each is watching for; the assistant instead provided an explanation of why the data cannot be retrieved, which does not satisfy the requi |
| preview-schedule | PASS | FAIL | FAIL – The answer infers preview-only status from the tool name and "stale/illustrative" language, but doesn't explicitly state "nothing was saved." The rubric requires clarity on both the next fire t |
| archive-sessions-batch | FAIL | PASS | missing=['archive_sessions']; forbidden=['archive_session']; PASS — The answer confirms archiving one finished session ("Old spike branch cleanup") with a single operation, stating the session ID and  |
| unarchive-sessions-batch | PASS | FAIL | FAIL – The answer lacks critical details required by the rubric: it does not show both session IDs being passed to the function, does not demonstrate that IDs and titles were provided "in order," and  |
| rotate-sessions-batch | FAIL | PASS | missing=['rotate_sessions']; forbidden=['rotate_session']; PASS - The answer identifies one stalled session, provides its id and title, and describes rotating it in a single action ("has been rotated" |
| delete-sessions-batch | FAIL | FAIL | missing=['delete_sessions']; FAIL – The rubric requires the assistant to call delete_sessions ONCE naming each target and stating the deletion is permanent and irreversible. The assistant listed the a |
| list-presets | PASS | FAIL | FAIL - The answer does not list any presets by name from the tool data; instead it explains that the endpoint returned NOT_FOUND and declines to provide the requested information. |
| list-templates-by-tag | PASS | FAIL | FAIL — The answer does not list templates tagged 'bugfix', nor does it demonstrate passing a tag filter to retrieve them. The rubric requires both the listing and the use of a tag filter mechanism; th |
| render-template | PASS | FAIL | FAIL - The assistant attempted to look up and render a template but failed to produce the rendered text output. The rubric requires the assistant to render the template with the date variable filled i |
| list-permission-profiles | PASS | FAIL | FAIL – The rubric requires listing configured permission profiles by name with their mode/patterns from tool data, but the answer provides no such list, only an explanation that the API endpoint retur |
| starter-notify-session-finishes | PASS | FAIL | FAIL - The answer doesn't explicitly confirm critical watch parameters. The rubric requires creating a watch with kind 'session', condition 'finishes', and targeting a specific session ID from the rou |
| starter-notify-account-limited | PASS | FAIL | FAIL - The answer mentions setting up a watch for any account and confirms drawer notification, but does not explicitly confirm the required parameters (kind='account', condition='limited') were set a |
| create-issue-flaky-checkout | PASS | FAIL | FAIL — The answer demonstrates that a title describing the test failure was included ("Flaky test: test_refund_total fails intermittently (~1 in 5 runs)"), correctly references issue #21 on acme/billi |


## Focused checks

The following existing tests passed: **137 passed**, with 14 existing deprecation warnings:

- `server/tests/test_assistant_conformance.py`
- `server/tests/test_eval_assistant_grader.py`
- `server/tests/test_assistant_permissions.py`
- `server/tests/test_assistant_external_token.py`

The run used this checkout's `src` on `PYTHONPATH`, cleared session-daemon and tailnet-allowlist
environment overrides, and used the existing dev-mode test configuration. These tests use
isolated fixtures; the real smoke is recorded separately below.

## Phase acceptance ledger

| Tracker | Required acceptance | Evidence and disposition |
|---|---|---|
| [#1237](https://github.com/steiner385/viberpit/issues/1237) | Conditional operator tools; scoped mint/verify/revoke; real MCP client; CLI answers from a shell; Codex spike committed | Prior implementation/spike/MCP evidence remains valid; current CLI run is blocked by the deployed external-token flag. Keep real CLI acceptance open. |
| [#1056](https://github.com/steiner385/viberpit/issues/1056) | Mutation permission cards; destructive tools cannot be always-allowed; shared HTTP actions; live smoke green | Focused permission tests pass; six functional smoke assertions passed, but final archival cleanup caused collateral data loss. Acceptance withdrawn after cleanup data loss. The overlay was corrected back to build/active through one supported step-back; no further advancement is justified. |
| [#1057](https://github.com/steiner385/viberpit/issues/1057) | Default-on flag, emitted metrics, Settings assistant group, eval >=90%, feature/spec links, sub-spec B PR 12 closed | Prior v1 result is 39/40; the current edge reports enabled. That does not establish deployed UI/metrics acceptance or 30-day success. |
| [#1211](https://github.com/steiner385/viberpit/issues/1211) | Conformance, tool-backed starters, envelopes, gap metric, drawer model picker, discovery/transcript tools live; task #1217 eval | Conformance passes; discovery/transcript/gap tools and bounded drawer rendering ran live, but cleanup data loss prevents clean acceptance. |
| [#1218](https://github.com/steiner385/viberpit/issues/1218) | 22 new tools, four restored G1 chips, eval >=90% on >=58 cases | Current 120-case run scored 85%; threshold unmet. Keep build-stage acceptance open; no advancement for #1218, #1211, or #1057 is justified by this run. |

## Real disposable-target mutation smoke

A supported real smoke completed on September 12 from **2:55:57 to 2:56:15 p.m. EDT**
against the existing edge, real daemon, real model, and real database. It used the current
identity returned by `/api/auth/me`, without fabricating identity headers. Before creating
anything it required that no assistant thread existed. It created only a new disposable
assistant and a new coding target without an initial prompt; no existing user session was
used as a mutation target. The protocol and drain helpers came from the existing smoke.

All **six steps passed**:

1. Create a disposable assistant, with confirmed newly-created state.
2. Create the disposable coding target.
3. Run the real `list_sessions` turn to completion.
4. Verify a fresh successful `assistant.tool_call` database audit event after test start.
5. Request archive of the exact disposable target; observe the actual permission card and
   deny it. The archive card reported `alwaysAllowed: true`.
6. Reach turn completion and verify that the target survived, with status `idle`.

The observed tools were `mcp__viberpit__list_sessions` and
`mcp__viberpit__archive_session`. Final cleanup targeted the two records created
by the harness; both returned HTTP 204. However, the retention policy triggered by archival
had collateral effects on older records, as disclosed above. No service was restarted,
configuration changed, or external message sent. An initial harness attempt
failed while binding an aware timestamp for a naive-UTC database audit query; both of its
created records received archival HTTP 204 responses, subject to the same retention risk.
Correcting that harness timestamp produced
the successful six-step run; it was not an application fix or a changed acceptance criterion.

This supplies functional live mutation-smoke evidence for #1056, alongside its existing
permission tests and implementation evidence. It does not satisfy the separately gated
external CLI acceptance, all browser acceptance, or the elapsed-time targets below.

## Bounded deployed browser check

The real deployed Sessions page opened its Assistant drawer with the Sonnet model picker,
three starter buttons, composer, and confirmation notice visible. A read-only
`list_sessions` count prompt rendered a successful “Listed sessions” tool result and the
answer `4`; ordinary sessions remained in their separate list. No settings changed. The
browser tab was closed and the assistant created for this check was targeted for archival
(HTTP 204); the retention side effect is disclosed above. This establishes bounded deployed rendering and read
interaction, not every
starter, mobile orientation, accessibility, model-switching behavior, or all live routes.

After independent review of the real mutation evidence, the #1056 initiative was advanced
exactly once through the supported API from `build` to `deploy`; the read-back confirmed
`stage=deploy`, `status=active`. It was not advanced to `done`. That advancement is now
incident-blocked: independent review withdrew acceptance. After checking that the deployed
HandoffToBuild response is display-only, with no executing consumer and the closed tracker
excluded from the queue, one supported step-back corrected the stage to `build`,
`status=active`. The API read-back confirmed the correction. These checks do not justify
another advance.

## Live discovery, transcript, and gap checks

A further bounded live run exercised `list_repos`, `list_branches`, `list_stage_models`,
`get_session_transcript`, `get_tool_result`, `export_session`, and `report_gap`. Each had one
fresh successful tool-call audit, and the gap check recorded one fresh gap audit event.
The functional checks passed and the newly created assistant received an archival HTTP 204.
This run shares the cleanup incident described above, so it adds functional route evidence
without establishing clean end-to-end acceptance for #1211.

## Elapsed-time criteria

The [v1 design, section 11](../superpowers/specs/2026-09-04-ai-assistant-design.md#11-success-metrics)
sets these targets **30 days after Phase 4**:

| Metric | Required target |
|---|---|
| Interactive composer p95 | <=3 seconds |
| First token p50 / p95 | <=2 / <=6 seconds |
| Tool-call errors | <2% |
| User-denied mutation cards | <10% |
| Prompts per active day | >=5 |
| Phase 4 eval | >=90% |

No 30-day observation window or aggregated measurement was established by this bounded
acceptance run. Keep these observations open; a short smoke, an enabled flag, or merged code
cannot establish their results.
