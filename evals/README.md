# ViberPit eval suite

The CI regression harness for **agent-configuration** changes (AI-Native SDLC
design §7, issue #805). The playbook's rule: *a change to how the agent behaves —
`CLAUDE.md`, `AGENTS.md`, skills, hooks, `.claude/**`, or `.codex/**` — must not regress a corpus of real
ViberPit tasks, and a pass-rate threshold gates the merge.*

`.github/workflows/evals.yml` runs `scripts/run_evals.py` on every PR that touches
those paths. The pure scoring core is `server/sdlc/evals.py` (`aggregate` → a
pass-rate gate); this directory is the corpus it scores.

## Case format

One YAML file per case under `evals/cases/` (and `evals/regressions/` for
incident-derived cases):

```yaml
id: agent-context-documents-four-step-gate  # unique, kebab-case
description: The canonical agent context documents the four-step lint-and-test gate.
kind: static                                # "static" | "agent" | "browser"
tags: [agent-config, ci-gate]
check: grep -q "ruff check src/ server/" CLAUDE.md   # trusted repository command
```

- **`static`** — the acceptance check is a shell command (run from the repo root)
  that must exit `0`. Deterministic, no LLM, runnable in CI. These are the merge
  gate.
- **`agent`** — the case is a real ViberPit task an agent must complete; the
  acceptance check is run against its result. These need native-engine/API access,
  so quota-consuming execution is opt-in, never part of the PR gate.
  `--kind agent` remains **LIST ONLY** without `--execute`.
  Only `framework-impeccable-guidance-review` currently supports execution;
  other cases remain examples and fail closed if execution is requested.

## Running locally

```bash
python scripts/run_evals.py                 # static gate, threshold 1.0 (CI default)
python scripts/run_evals.py --threshold 0.95
python scripts/run_evals.py --kind agent    # LIST ONLY; does not execute agents
```

Exit code is non-zero when the pass-rate is below the threshold, so it doubles as
a CI gate.

## Browser acceptance calibration

`python scripts/run_evals.py --kind browser --timeout 120 --report /tmp/ui-calibration.json`
executes trusted repository browser `check` commands from the repository root.
The existing Evals workflow also runs a separate `browser-evals` job on relevant
configuration, fixture, and web dependency changes, and uploads the JSON report
on success or failure when produced. This job does not change branch protection
or constitute framework admission. Browser
cases are opt-in and do not change the default static gate. Provision browser tools
separately; the runner never installs dependencies or launches subscription agent sessions.
The timeout is per case, positive and at most 3600 seconds. On POSIX a timeout kills
the command's process group, including Chromium descendants.

Commands must exit zero and emit one JSON object on stdout (send diagnostics to stderr):

```json
{"schema_version":1,"report_kind":"ui-acceptance-calibration","executed":true,
 "passed":true,"browser":{"name":"chromium","version":"observed version"},
 "fixtures":[{"id":"clean","sha256":"<64 lowercase hexadecimal characters>",
 "expected_failures":[],"observed_failures":[],"passed":true}]}
```

Each fixture passes only when its observed failure identifiers equal its expected
identifiers; seeded broken fixtures therefore test detector sensitivity. The runner
rejects empty or malformed reports, duplicate fixture identifiers, invalid hashes,
contradictory statuses, timeouts, and unavailable tools. Empty corpora fail the gate.
The optional report contains executed case results, durations, Git revision, YAML
hashes, and validated browser output. These are **runner observations**, not
authenticated admission evidence, native-agent compatibility results, or proof that
Impeccable improves an agent's work. The Git revision alone does not attest a clean
checkout. No engine identity, credentials, or token/cost measurements are invented.

## Regression convention (design §7, §11)

**Every production incident adds a regression eval.** When an incident is resolved,
drop a `static` case under `evals/regressions/` whose acceptance check would have
failed before the fix (name it `incident-<n>-<slug>.yaml`). The corpus only grows,
and a re-introduction of the same class of bug is caught at the gate.

## Growth target

The seed set is a starting point. The target (design §7) is **20–50 real ViberPit
tasks with acceptance checks**; grow the corpus toward it as more agent-config
invariants and incident regressions are captured.

## Restricted paired guidance comparison

This is a controlled source-review comparison, crediting the existing project
pilots. It does not invoke the native Impeccable skill, edit UI, run generated
code, measure visual design quality, or admit a framework.

```bash
python scripts/run_evals.py --kind agent --execute \
  --case framework-impeccable-guidance-review \
  --model claude-sonnet-4-20250514 --max-budget-usd 1 --timeout 120 \
  --report /private/new-guidance-report.json
```

The model argument must be an explicit dated model ID available to the caller;
the example is not an availability claim. Use an existing Anthropic subscription
account in ViberPit, optionally selected with `--account NAME`. The canonical
helper picker otherwise selects one ready account. That account is fixed for the
whole comparison; there is no automatic rotation or retry. The worker excludes
API keys and alternate-provider overrides. Native auth status must identify a
Claude.ai or canonical first-party OAuth-token authentication before inference;
missing or stale credentials stop the
run. The previously provisioned evaluation API key is not read. Run with the daemon's
state-directory and fleet-role environment so the canonical helper can find its
broker envelope. In broker mode, native auth status may omit the subscription
tier; the report preserves that unknown value.

Each of three existing synthetic fixtures (clean, mobile overflow, reduced motion)
gets one baseline and one guidance review, with alternating order. Both arms see
the same task and HTML, blind fixture IDs, and finding vocabulary. Only the
guidance arm receives `operate`, `clarify`, and `harden` reference text, loaded
after contract/lock validation and checked against locked file hashes. HTML and
reference text are untrusted data; the skill entry point and native binaries are
never invoked. Expected findings remain outside model prompts.

Each call runs in a separate Python worker with an allowlisted environment, fresh
working directories, the canonical helper scratch configuration and Claude binary, and shared
stateless-helper options: no environment-access tools, no MCP servers, no settings sources, and no
session persistence. `--safe-mode` disables user customizations, including
project instructions, installed skills/plugins and hooks, while preserving native
subscription authentication. `--disable-slash-commands` disables skills explicitly;
`--system-prompt-snapshot off` prevents prompt recording. Native built-in plugins
and managed policy remain vendor-controlled; safe mode is not a claim that every
built-in plugin disappears. Preflight rejects missing required CLI
flags; stream observations reject every tool except the native `StructuredOutput`
formatter, wrong models, missing initialization,
empty/failed results, malformed finding IDs, and missing/exhausted cost. These
are native configuration and observed boundaries, **not an OS/network sandbox**.
The trusted CLI still makes provider calls. Its JSON-schema output mode advertises
one native `StructuredOutput` formatter; that is the only permitted tool name,
and its arguments and final structured result must satisfy the findings schema.
No shell, file, network or server-side tool is permitted. Freeform result text is
not scored. Each call allows at most two native turns for the formatting step. Reports contain no prompts, keys,
paths to runtime state, or raw native diagnostics.

The `--max-budget-usd` value is a positive, finite native estimated-workload
ceiling of at most USD 2, not an API credit allocation or subscription quota limit. Six fixed reservations
are made, one per call, with an equal native SDK `max_budget_usd` cap; unused
reservations are never recycled. Any failure or unknown cost stops later calls,
without retries. This is a scheduling bound plus the provider's native cost
estimate, **not a subscription quota or billing guarantee**. Timeout applies per call (at most
300 seconds), with a bounded extra worker cleanup/preflight allowance.

The new report path must not exist; output is mode 0600. The runner exclusively
reserves and flushes an incomplete marker before any comparison call, then writes
the final report through that owned descriptor. Invalid destinations fail before
calls. An interrupted marker is not evidence that no calls incurred charges. Keep it outside public
artifact uploads. It records source/guidance/fixture/prompt digests, requested and
observed models, native/SDK versions, per-call timing, observed tokens/costs, and
raw true-positive/false-positive/false-negative counts. Unknown usage remains
null. `completed` means all six valid reviews were observed; it is not a
quality pass or promotion. Nonzero exit means incomplete/invalid execution.
No fabricated human rubric score, statistical significance, or framework
admission result is produced. Evaluation of UI edits,
visual judgment, engine compatibility, and runtime enforcement remain separate.
