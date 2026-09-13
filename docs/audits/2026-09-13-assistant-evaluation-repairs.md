# Assistant evaluation repairs — September 13, 2026

**The final controlled run passed 108/120 cases (exactly 90%), meeting the unchanged
90% threshold; exit status 0.** Twelve cases remain failed. This is one fixture-based
measurement, not complete assistant acceptance. This follow-up records the repairs
and their limits. The
[September 12 acceptance audit](2026-09-12-assistant-acceptance.md) remains the
historical record of the original **102/120 (85%)** result and cleanup incident.
That result has not been rescored. The intermediate 109/120 result below is a
diagnostic result from before the final contract corrections, not current acceptance.

## Changes and measurement contract

The [evaluator](../../scripts/eval_assistant.py) supplies missing catalog/watch
routes, coherent per-case session state, supported live/archived deletion and batch
outcomes, clock-consistent archives/events/schedule previews, and valid restore
preconditions. Later corrections align pipeline structures and health statistics,
ready/merge outcome flags, session detail, notifications, suggestions, adoption,
and opaque template identities with their existing application contracts. Canonical
serializers, schemas, and resolvers are reused where available. Adoption is simulated
as enabled inside the fixture; no live feature flag or process is changed.

Ordered evidence retains tool arguments, exact model-visible results, failures,
narration, final answers, and the same rendered route context supplied to the
assistant. The judge receives these as untrusted evidence, separately from the
unchanged rubric. Required/forbidden tool checks and native failures remain
authoritative even when the rubric judge returns PASS. Both assistant and judge
use one native attempt (`retries=0`), preventing evidence from failed and retried
turns being pooled into a pass. Native cleanup is awaited before scratch removal.

Both native consumers use isolated helper configuration and working directories
with inherited settings disabled. The assistant has fixture tools only; the judge
has none. Incremental redacted evidence uses private directories and files
(0700/0600); completed and interrupted cases survive an incomplete run. Public
summaries omit private traces, account locations, and credential material.

Three [runtime tool descriptions](../../server/assistant/tools.py) were corrected:
`list_stage_models` describes structural stages and points to existing health data;
`get_session` no longer promises pending permission/question IDs; `answer_question`
requires a known question ID/options or user input. These are corrections to
available capabilities, not new endpoint behavior or instructions to force a
benchmark tool sequence.

The [case file](../superpowers/evals/assistant/cases.jsonl) retains **120 standard
cases and five separate operator cases**. All existing `id`, `prompt`, `route`,
`rubric`, `expect_tools`, `forbid_tools`, and `operator` values are unchanged. One
setup-only metadata change makes the restore scenario possible. Defaults remain
**Sonnet, a Haiku judge, and a 90% threshold**. Regression tests freeze these values.

| SHA-256 receipt | Value |
|---|---|
| Original complete case file | `b5ca691336d554d80b63a8b46843ccd56f4a1c3cdeed260c432b293be0ac4288` |
| Setup-corrected complete case file | `b224764dd87ceb21761e4f026cd9c858209a7a9f87847363f61d527cfb0d75ae` |
| Unchanged semantic projection | `db4445721e49cce3cdc8670da408943fb460b6daf3d05b45af3634d14155120b` |

The original baseline used SDK **0.2.105**, CLI **2.1.269**. Follow-up runs use SDK
**0.2.105** and the installed, unmodified CLI **2.1.270**. This version difference
limits attribution of score changes solely to these repairs.

## Preserved attempts

| Attempt / private run identifier | Tested commit | Result and disposition |
|---|---|---|
| 1 / `2026-09-12-lbvbi1dr` | `10050e7a` | Invalid authentication environment: five native authentication failures, then 115 helper skips; zero fixture calls. The launcher mistakenly inherited a fake unit-test D-Bus address. Restoring the working inherited bus restored canonical keyring access without account changes. The recorded 0/120 is not a quality measurement. |
| 2 / `2026-09-12-kwhsojxm` | `10050e7a` | Stopped after a confirmed live-deletion fixture defect: 34 completed, 32 passed, two failed, plus one interrupted judge. No complete score. |
| 3 / `2026-09-12-0rcdwkyt` | `82c165e5` | Stopped after offline reproduction of retry/evidence pooling: 23 completed, 22 passed, one failed, plus one interrupted judge. This does not establish that every completed case retried. No complete score. |
| 4 / `2026-09-12-75cu8cik` | `57428a1c` | Completed **109/120 (90.8%)**. All 11 failures remain failures. This preceded the pipeline/detail/context/notification/suggestion/adoption/template corrections; it is diagnostic, not acceptance for the final code. |
| 5 / `2026-09-13-bhnx9k85` | `a4ec87ea` | Completed **108/120 (90%)**, exit 0; the unchanged numeric threshold was met exactly. See the [final report](2026-09-13-assistant-evaluation-results.md). |

Every attempt remains preserved; none was rescored. Runs 2 and 3 stopped gracefully
after verified defects, retaining partial evidence. Private journal SHA-256 receipts:

| Attempt | SHA-256 |
|---|---|
| 1 | `a766548ec040584d7ad00f02aef986e5205e8c5756df2900c9dc1a56ec381d39` |
| 2 | `2218d6a27bb38bba7be92af1f60c6651126c858fe1160f54f3a0d9496bdc7c7d` |
| 3 | `7591bec2731a73fb085027aec4986b1c79799062f7df8145033d7725bdafc2b0` |
| 4 | `197c615e415cacc7b4f95149707cde39b346d1a00e3c28eff4c9763e329829a4` |
| 5 | `ce0142eafb53faeb14108f24561506c048358e3ebd0761e94f0fbb2e174f3e40` |

## Diagnostic run 4 failures

The [complete diagnostic report](2026-09-12-assistant-evaluation-diagnostic-results.md)
retains all 120 grades. These notes distinguish confirmed contract defects from
observed model/judge behavior; they do not replace any recorded grade.

| Failed case | Evidence-grounded diagnosis |
|---|---|
| `backlog-runner-activity` | Omitted required `list_automations`, despite a passing rubric. The known automation ID is valid without discovery; no runtime bug established. |
| `chip-summarize-all` | Excess commentary failed the one-line requirement. The judge also treated completed, nonarchived records as outside the live collection; that rationale conflicts with collection semantics. |
| `chip-slowest-checks` | Pipeline fixtures/descriptions supplied invented performance fields; the real health API has p90, not requested p95. Corrected data cannot establish the unavailable metric. |
| `chip-why-stuck` | Judge lacked the assistant's route context. Detail fixture also invented pending permission data. Context evidence and detail contracts were corrected separately. |
| `create-notification-rule-slack` | Channel ID was passed as action, without `channel_id`; the old fixture falsely accepted it. The judge's objection to canonical `needs_input` was inaccurate, but the invalid arguments remain a defect. |
| `delete-notification-rule` | Explicitly requested target was missing from fixture setup. Model declined; stateful target and missing/repeated-delete behavior were corrected. |
| `preview-schedule` | Correct times, but no explicit confirmation that nothing was saved. Preserved communication failure; no fixture defect established. |
| `suggest-next-prompt` | Generic success supplied no suggestion text. Corrected to the actual suggestions response and empty/missing outcomes. |
| `adopt-session` | Requested candidate was absent. Corrected representative eligible/ineligible candidates and fixture-only adoption transitions. |
| `render-template` | Skipped required discovery because fixture ID equaled name; also used a date inconsistent with the later fixture clock. IDs now have opaque UUID shape. |
| `starter-notify-account-limited` | Chose a valid push rule instead of the required account watch/drawer path. Preserved tool-selection and communication failure; valid rules were not rejected to force the benchmark. |

## Final validation

The final run started **September 13, 2026 at 04:55:57 a.m. EDT**, from clean commit
`a4ec87ea747642493f4f12a1ab25594bf4aa8967`. Its selected-case SHA-256 was
`170eeaf09a214fc97837962ec36c0961c4b22a24a5ddb915409d2f7929727d1b`. SDK, CLI, models, full-case and semantic
hashes are recorded above. All 120 cases completed; the journal contains no native
failure, interrupted-case, or tool-boundary-failure events. This does not mean every
application tool call succeeded: correctly rejected fixture requests are retained.
Journal and private directory permissions were verified as 0600 and 0700.

Ruff and full server mypy passed; the core suite passed **681 tests** and the full
server suite passed **9,534 tests, with 33 skipped** (622.21 seconds). Independent
implementation reviews covered the evidence, isolation, zero-retry, context, and
fixture-contract corrections, including focused regressions. These results precede
this documentation-only receipt; final public-receipt review and PR delivery are
separate steps, not claimed completed here.

The [complete final report](2026-09-13-assistant-evaluation-results.md) retains all
120 original component grades and bounded runner notes. All twelve failures remain
failures; this table adds evidence-grounded interpretation, without rescoring:

| Failed case | Remaining result and limitation |
|---|---|
| `why-session-stuck` | Required migration-permission explanation was absent. Truthful detail lacks pending data; generic transcript describes test execution, inconsistent with the seeded migration scenario. This is incomplete scenario coverage, not proof of a runtime diagnosis failure. |
| `backlog-runner-activity` | Required `list_automations` was omitted despite rubric PASS. The known ID worked; no discovery precondition is established by the real API. |
| `answer-question` | Detail provided no actionable question ID/options. Assistant asked for them and did not invoke the required answer action. Refusal to invent an ID does not satisfy the task. |
| `resolve-permission-allow` | Detail and transcript supplied no permission ID. Assistant declined to invent one; the required approval remains unperformed. |
| `fix-pr` | After reading a healthy open PR and its owner, the assistant requested additional confirmation instead of invoking the explicitly requested fix. Green checks are not a canonical prerequisite blocking that request. |
| `chip-autopilot-last-hour` | No events were returned. Judge rejected the lack of an explicit time-window calculation; no misclassified event or native/API failure was demonstrated. |
| `chip-slowest-checks` | Compared openly labeled p50/p90 data. Frozen rubric requires unavailable p95; the answer did not establish that metric. |
| `chip-why-stuck` | Judge received and acknowledged the correct route context. Generic transcript/tool output described failed tests rather than pending migration permission; assistant also overstated absence of a pending card. Scenario/pending-state coverage remains incomplete. |
| `create-notification-rule-slack` | Four guessed action values were correctly rejected. Assistant never used canonical `action=channel`, logged a gap, and created no rule. The API supports the correct call; existing tool wording/schema do not clearly expose that action vocabulary. |
| `chip-ci-regressed` | Used CI/CD-doctor findings instead of required `list_stage_models`; rubric PASS did not override the missing tool. Structural stage models also lack the legacy rubric's regression metrics. |
| `export-session-transcript` | Export succeeded without truncation; judge rejected a reformatted rendition despite preserved substantive content. This is a formatting/faithfulness interpretation residual, not a demonstrated export defect. |
| `delete-sessions-batch` | Selected the intended two targets and warned about permanence, but asked for confirmation instead of invoking required batch deletion. No deletion occurred. |

## Remaining interpretation and acceptance limits

The frozen criteria still demand capabilities absent from the current API: p95
where health provides p90; regressions from structural stage-model data; and pending
permission/question IDs absent from session detail. Do not invent fields, relabel
metrics, loosen criteria, or infer success from a rubric PASS despite these gaps.
`injection-get-session-summary` now receives the real empty detail summary, so a
PASS does **not** exercise its intended injected-summary attack. Injection coverage
on other paths cannot substitute for it. Notification rule creation likewise does
not prove effective routing: existing first-match priority/order can select an
earlier rule. No actual delivery is tested.

Safety [PR #1627](https://github.com/steiner385/viberpit/pull/1627) merged as
`fdb5634670bc978ea09f75fbf455a86dd4eac137`, closing #1605 with fail-closed retention
checks and an explicit disposable-environment requirement. This follow-up did not
rerun shared live smoke, change live configuration, or enable external CLI access.

Real model runs use `httpx.MockTransport` application tools and a disposable audit
database. They establish fixture-based tool/answer results, not live mutations,
permission-card behavior, or real adoption. The five operator-mode cases are outside
these runs. No phase advancement, complete browser/live/CLI acceptance, 30-day
metrics, recovery completion, or rebrand acceptance is established here. The earlier
cleanup incident and unrecovered data remain documented in the original audit.
