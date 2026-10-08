# Restricted Impeccable guidance comparison

This continues the [existing three-project pilots](impeccable-pilot-evidence.md).
It evaluates a specific remaining question: does selected Impeccable guidance
improve identification of known defects without inventing defects on a clean UI?
It does not repeat installation, product interviews or the historical design work.

## Experiment boundary

Use the existing clean, mobile-overflow and reduced-motion synthetic HTML
fixtures. Each receives the same review task twice: baseline and selected
guidance. Alternate which arm runs first across cases. The initial smoke is one
repeat per arm and case, six native calls; it establishes executability and
provides observations, not a statistically reliable estimate of effectiveness.

The treatment contains only the locked `operate`, `clarify` and `harden`
references. Load their exact bytes after validating the current framework lock.
Do not invoke `SKILL.md`, the Impeccable launcher, native detector, live browser
helpers, exported agents or hooks. Guidance is untrusted task data subordinate to
the fixed review instructions. Neither arm receives the fixture's expected answer
or identifying filename. The scorer compares allowlisted finding IDs against the
reviewed fixture expectations; raw false-positive and false-negative counts
remain visible. No aesthetic score or automatic admission is inferred.

The native agent receives HTML as text and returns structured findings. It does
not change files or run the page. The separate browser calibration establishes
the fixtures' observed behavior; this experiment tests source-based review, not
screenshot perception, visual redesign quality or successful UI repair.

## Native execution and credentials

The installed Claude CLI was probed through its real `--help` and `--version`:
version 2.1.292 documents `--bare`, which skips settings/plugin hooks, plugin sync,
auto-memory and `CLAUDE.md` discovery. It also explicitly refuses OAuth/keychain
authentication in this mode. A dedicated Anthropic evaluation API key must be
provided through the normal runtime secret mechanism, never in a prompt, report,
command-line argument or committed file. Do not substitute an ambient paid account
or select a different provider to avoid this requirement.

Use canonical stateless helper options plus `--bare`: no built-in tools, no MCP
servers, no loaded settings and no native session persistence. Each call has a
fresh scratch working/config directory and a minimal child environment. The
runner must reject advertised/requested tools and missing execution evidence.
This is a tool-free inference boundary, not an OS/network sandbox; the trusted
native process still contacts Anthropic to perform the requested inference.

Require an explicit model identifier, single attempts, bounded turns and time,
and a configured total budget no greater than $2 for this initial smoke. Reserve
budget before dispatch and stop on failure, missing cost information or exhausted
budget. Native budget enforcement and reported dollar costs are provider
mechanisms/estimates, not a guarantee about final billing. No retries may silently
increase the number of calls or pool evidence from different attempts.

## Evidence and interpretation

Retain package/fixture/prompt digests, requested and observed model/CLI/SDK identity,
call ordering, outcome, duration, observed usage and cost, and the exact scoring
counts. Missing telemetry remains unknown rather than zero. Reports are local
runner observations, not authenticated SDLC attestations. Raw native exceptions,
authentication data, account paths and unbounded model text do not belong in a
shareable result.

A missing key, unavailable native capability, timeout, missing initialization,
unexpected tool, malformed answer or exhausted budget is a failed/incomplete
execution—not a zero-score completed experiment. Report this separately from a
completed review that misses a real defect or flags a clean fixture.

## Running the comparison

After provisioning the dedicated key through the runtime secret setup, choose
an exact dated model ID supported by that account and a new private output path:

```bash
python scripts/run_evals.py --kind agent --execute \
  --case framework-impeccable-guidance-review \
  --model "$EVAL_MODEL_ID" --max-budget-usd 2 --timeout 120 \
  --report /private/evals/new-guidance-comparison.json
```

`--kind agent` without `--execute` only lists cases. Ordinary static/browser CI
never makes these provider calls. The report destination is exclusively reserved
with mode 0600 before dispatch; an existing file, symlink or unwritable destination
fails before spending. Interrupted runs retain an incomplete marker, not a success
claim. Final output uses the reserved descriptor. This is not a per-call recovery
journal; a forcibly killed run must not be resumed by replaying presumed successes.

## Current status

The runner is implemented and independently reviewed. All 50 focused offline
runner tests passed, including SDK command construction, nested-generator cleanup,
tool rejection, budgets, paired scoring and output reservation. The actual CLI
entry point without a key returned exit 1, `missing-api-key`, `executed: false`,
zero calls and a private report. This proves rejection, not live model behavior.

On 2026-10-08 a dedicated key successfully listed available API models. The first
live attempt used `claude-sonnet-4-5-20250929`, CLI 2.1.293 and SDK 0.2.105,
and stopped after one incomplete call. Two bounded diagnostic attempts identified
the native assistant error as `billing_error`; no completed inference, scoring or
cost telemetry was obtained. Total reservations were $0.533334 (rounded up for
reporting): the initial $0.333334 plus two $0.10 diagnostic reservations. Actual
charges are unknown; these reservations must not be recycled silently.

After API credits were added, a successful native result exposed an exact JSON
code fence around the findings. The parser now accepts that single envelope while
retaining strict schema, finding-ID and duplicate validation; 61 focused tests and
independent review passed. A diagnostic successful call reported $0.0582865.

A subsequent paired attempt completed three calls: baseline and guidance both
returned no findings on the clean fixture, and guidance found the seeded mobile
overflow. Their reported costs totaled $0.22745715. The fourth call (overflow
baseline) failed without usable cost telemetry, and execution stopped before the
reduced-motion pair. This is partial execution evidence, not a completed comparison
or an effectiveness result. Successful calls established real native inference,
identity checks, tool-free execution, parsing and scoring; failure diagnosis and a
complete paired run remain outstanding. The runner now emits allowlisted failure
reasons (including invalid JSON) while keeping arbitrary native diagnostics
redacted; all 79 focused tests pass.

Across all attempts, total reservations are $1.656667 (rounded up), reported costs
are $0.28574365, and failed-call costs remain unknown. Further paid work is paused
pending additional evaluation budget. Impeccable remains candidate. The earlier
browser calibration and historical pilots remain separate evidence.
