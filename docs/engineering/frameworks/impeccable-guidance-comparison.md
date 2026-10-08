# Restricted Impeccable guidance comparison

This continues the [existing three-project pilots](impeccable-pilot-evidence.md).
It asks whether selected Impeccable guidance improves identification of seeded
source-level defects without inventing defects on a clean UI. It does not repeat
installation or historical design work, invoke the Impeccable executable, change
UI, or establish framework admission.

## Experiment boundary

Three existing synthetic HTML fixtures cover clean UI, mobile overflow and reduced
motion. Each receives one baseline and one guidance review. Alternate which arm
runs first across fixtures. Both arms receive the same task, HTML, blind fixture
identifier and finding vocabulary; only the guidance arm receives the locked
`operate`, `clarify` and `harden` references. Validate the Portable Agent Contract,
framework lock and exact reference hashes before execution. Expected findings
remain outside model prompts.

This is source review, not browser repair or screenshot-based design judgment.
The separate browser calibration establishes fixture behavior. Six calls with one
repeat per arm establish executability and observations, not statistical evidence
of effectiveness. Preserve false positives and false negatives; do not treat a
completed experiment as a quality pass or promotion.

## Subscription authentication and isolation

Use an existing ViberPit Anthropic subscription account. `--account` takes its
internal account ID, which may differ from the Accounts page display nickname.
Without it, the canonical helper picker selects one ready account. Resolve auth
and host-local scratch configuration through `server/sessions/account_auth.py`,
freeze that account for all six calls, and never automatically rotate or retry.

Run with the daemon's nonsecret state-directory and fleet-role settings. On the
verified rocky deployment these are `XDG_STATE_HOME=/var/lib/viberpit/state` and
`VIBERPIT_DAEMON_ROLE=worker`. A plain shell lacking those settings can read stale
browser credentials instead of the active broker envelope. Such a failure does
not establish that the managed account requires reauthentication. Do not source
an entire service secret file, rewrite HOME, or create a parallel credential store.

The worker receives an allowlisted environment containing only the selected
canonical authentication and basic runtime settings. API keys, ambient OAuth
credentials and provider overrides are excluded; there is no API-funded fallback.
Native auth status must report logged-in first-party Claude.ai Max/Pro, or
`oauth_token` authentication with the canonical OAuth launch credential present.
The broker path can omit the subscription tier; preserve that value as unknown.

Use native `--safe-mode`, `--disable-slash-commands`,
`--system-prompt-snapshot off` and canonical stateless helper options. Installed
custom instructions, skills, plugins and hooks are disabled, along with MCP and
session persistence. Native built-in plugins and managed policy remain
vendor-controlled. Every call has a fresh working directory. This is a native
configuration boundary, not an OS/network sandbox.

## Structured results and limits

Require the native JSON-schema output mode with the exact finding-ID vocabulary.
It advertises a `StructuredOutput` formatter. That is the only permitted tool;
validate its arguments and require the final structured result to satisfy the
same schema. No shell, file, network or server-side tool is permitted. Reject
missing initialization, other tools, model changes, extra fields, invalid or
duplicate IDs, missing structured output and failed results. Freeform prose and
Markdown fences are not parsed or scored as a fallback.

Allow at most two native turns for the formatting step, six calls, single
attempts, and per-call timeouts of at most 300 seconds. `--max-budget-usd` is a
native estimated-workload cap of at most 2, not an API credit allocation or a
subscription quota guarantee. Reserve an equal share before each call; never
recycle reservations within a run. Missing cost telemetry stays unknown and stops
execution; zero is valid telemetry, not proof that quota was unused.

## Running and retaining evidence

After selecting the deployment's runtime settings and a ready account:

```bash
python scripts/run_evals.py --kind agent --execute \
  --case framework-impeccable-guidance-review \
  --account "$EVAL_ACCOUNT_ID" \
  --model "$EVAL_MODEL_ID" --max-budget-usd 2 --timeout 180 \
  --report /private/evals/new-guidance-comparison.json
```

The model must be an exact supported dated ID. `--kind agent` without `--execute`
only lists cases; ordinary static/browser CI never makes model calls. Exclusively
reserve a new report with mode 0600 before dispatch and flush an incomplete marker.
Invalid destinations fail before calls; final output uses the same owned file
descriptor. An interrupted marker is not evidence that no calls occurred. This
is not a per-call recovery journal: do not replay presumed successes.

Reports retain input hashes, model/CLI/SDK identity, auth mode and reported tier,
call order, observed tools, timing, token/cost estimates and raw scoring counts.
They exclude credentials, account paths, raw model prose and arbitrary native
errors. Failure reasons are bounded diagnostic codes. Keep raw reports private;
record reviewed summaries and report digests in project evidence. Never pool
partial attempts into a purported complete paired run.

## Verification record

On 2026-10-08, CLI 2.1.293 and SDK 0.2.105 successfully ran a subscription canary
using the canonical broker credential. With marker instructions in both
`CLAUDE.md` and `AGENTS.md`, the answer was `NONE`, and the synthetic SessionStart
hook did not run. Initialization advertised no tools, MCP servers, skills or
slash commands. Native built-in plugins remained visible as documented.
This is observed evidence for that version and configuration, not every future
vendor release. The canary's native estimated cost was $0.00444.

An initial subscription comparison completed four reviews, then stopped at an
invalid JSON response. Guidance produced a false positive on the clean fixture;
both arms identified mobile overflow. Preserve that incomplete attempt separately.
A subsequent native structured-output probe correctly returned `reduced-motion`
through `StructuredOutput`, with two turns and estimated cost $0.0376413. This
motivated requiring schema output instead of relying on formatting instructions.

The fresh structured-output subscription run completed all six calls on
2026-10-08 with `claude-sonnet-4-5-20250929`, CLI 2.1.293, SDK
0.2.105, stable broker OAuth identity metadata and only the native
formatter advertised. Each arm found both seeded defects with zero false
positives and zero false negatives. The clean fixture produced no findings.

| Arm | True positives | False positives | False negatives | Native estimated cost |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 2 | 0 | 0 | $0.1133963 |
| Guidance | 2 | 0 | 0 | $0.1957119 |

The private report's SHA-256 is
`1dbca82420a003e8b7c52eddce61e917785789de51b282f0f43fb6c9c844aa45`.
The runner digest in that report matches the verified implementation. The report
retains all six call observations and input digests; it is not pooled with earlier
attempts. Earlier API-funded experiments and the first incomplete subscription
attempt remain excluded from these totals.

All 125 focused tests, independent review, Ruff, mypy, static evals and the
Portable Agent Contract check passed. This establishes executable, isolated
subscription comparison, not statistical evidence that guidance improves quality.
Impeccable stays candidate; this harness does not enable hooks or enforce its
design judgment.
