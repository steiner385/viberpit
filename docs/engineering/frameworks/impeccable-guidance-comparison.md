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

The default execution path uses an existing ViberPit Anthropic subscription
account. An optional `--account` chooses it explicitly; otherwise the canonical
helper picker chooses a ready account. Resolve credentials and host-local scratch
configuration through `server/sessions/account_auth.py:helper_env`, then freeze
that account for the entire paired sequence. No automatic account rotation occurs.

The worker receives a minimal allowlisted environment with the selected config and
its canonical OAuth authentication. API keys and provider overrides are excluded;
there is no API-funded fallback. Native `auth status --json` must identify a
logged-in first-party Claude.ai subscription before inference. A cached logged-in
status alone does not establish token validity: inference can still report expired
authentication, and must stop without retrying or switching accounts.

CLI 2.1.293 advertises `--safe-mode`, which disables user customizations while
preserving authentication. Use it with canonical stateless helper options,
`--disable-slash-commands` and `--system-prompt-snapshot off`: no built-in tools,
MCP configuration, settings sources, skills or native session persistence. Safe
mode disables installed custom plugins/hooks and project instructions; native
built-in plugins and managed policy remain vendor-controlled. It does not imply
an OS/network sandbox or permission to bypass managed policy. Every call uses a
fresh working directory and the canonical helper scratch config, without copying
credentials into a new ad hoc store or rewriting HOME.

Require an explicit model identifier, six calls at most, single attempts, bounded
turns and per-call timeouts. `--max-budget-usd` retains the native estimated-workload
cap (at most 2), not an API credit allocation or subscription quota guarantee.
Reservations are never recycled within a run. Stop on failure, missing cost
telemetry or exhausted estimated budget. Report native estimated costs separately
from billed charges; zero is valid telemetry, not proof that quota was unused.

## Evidence and interpretation

Retain package/fixture/prompt digests, requested and observed model/CLI/SDK identity,
call ordering, outcome, duration, observed usage and cost, and the exact scoring
counts. Missing telemetry remains unknown rather than zero. Reports are local
runner observations, not authenticated SDLC attestations. Raw native exceptions,
authentication data, account paths and unbounded model text do not belong in a
shareable result.

An unavailable subscription account, unavailable native capability, timeout, missing initialization,
unexpected tool, malformed answer or exhausted budget is a failed/incomplete
execution—not a zero-score completed experiment. Report this separately from a
completed review that misses a real defect or flags a clean fixture.

## Running the comparison

Choose a ready subscription account, an exact dated model ID supported by it,
and a new private output path:

```bash
python scripts/run_evals.py --kind agent --execute \
  --case framework-impeccable-guidance-review \
  --account "$EVAL_ACCOUNT" \
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
are $0.28574365, and failed-call costs remain unknown. API-funded work has stopped; these historical reservations are not reused by
the subscription experiment. Impeccable remains candidate. The earlier
browser calibration and historical pilots remain separate evidence.

## Subscription transition

The user requested subscription-backed execution on 2026-10-08. The actual CLI
reported Claude.ai / firstParty / Max authentication for a canonical ready account.
A safe-mode canary probe initialized without tools, MCP servers or slash commands;
the synthetic SessionStart hook did not run. Native built-in plugins remained
visible, as documented by safe mode. Two eligible accounts then reported expired
OAuth authentication before successful inference, so instruction isolation and
complete paired execution are not yet verified. Reauthentication and a successful
canary probe remain prerequisites to claiming a clean subscription baseline.

The subscription migration passes 105 focused tests plus the existing eval-runner
tests and independent review. Offline tests cover API-key and ambient OAuth
exclusion, native subscription status, absent/lapsed broker envelopes, fixed
account selection, no automatic fallback, and the safe-mode launch options.

The updated CLI entry point was exercised with the available account state and
stopped with `unverified-subscription-auth`, `executed: false`, before inference.
This verifies rejection only; reauthenticated successful inference is still pending.
