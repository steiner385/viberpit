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
  acceptance check is run against its result. These need a live Claude session and
  API access, so execution is **out-of-band**, not in the PR gate.
  `--kind agent` is **LIST ONLY**; it never executes an agent or writes an execution report.
  One example is included to show the shape.

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
separately; the runner never installs dependencies or launches paid agent sessions.
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
