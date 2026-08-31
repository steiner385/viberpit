# ccswitch eval suite

The CI regression harness for **agent-configuration** changes (AI-Native SDLC
design §7, issue #805). The playbook's rule: *a change to how the agent behaves —
`CLAUDE.md`, skills, hooks, `.claude/**` — must not regress a corpus of real
ccswitch tasks, and a pass-rate threshold gates the merge.*

`.github/workflows/evals.yml` runs `scripts/run_evals.py` on every PR that touches
those paths. The pure scoring core is `server/sdlc/evals.py` (`aggregate` → a
pass-rate gate); this directory is the corpus it scores.

## Case format

One YAML file per case under `evals/cases/` (and `evals/regressions/` for
incident-derived cases):

```yaml
id: claude-md-documents-four-step-gate      # unique, kebab-case
description: CLAUDE.md documents the four-step lint-and-test gate.
kind: static                                # "static" | "agent"
tags: [agent-config, ci-gate]
check: grep -q "ruff check src/ server/" CLAUDE.md   # static only; exit 0 == pass
```

- **`static`** — the acceptance check is a shell command (run from the repo root)
  that must exit `0`. Deterministic, no LLM, runnable in CI. These are the merge
  gate.
- **`agent`** — the case is a real ccswitch task an agent must complete; the
  acceptance check is run against its result. These need a live Claude session and
  API access, so they run **out-of-band** (`--kind agent`), not in the PR gate.
  One example is included to show the shape.

## Running locally

```bash
python scripts/run_evals.py                 # static gate, threshold 1.0 (CI default)
python scripts/run_evals.py --threshold 0.95
python scripts/run_evals.py --kind agent    # list/execute agent-mode cases
```

Exit code is non-zero when the pass-rate is below the threshold, so it doubles as
a CI gate.

## Regression convention (design §7, §11)

**Every production incident adds a regression eval.** When an incident is resolved,
drop a `static` case under `evals/regressions/` whose acceptance check would have
failed before the fix (name it `incident-<n>-<slug>.yaml`). The corpus only grows,
and a re-introduction of the same class of bug is caught at the gate.

## Growth target

The seed set is a starting point. The target (design §7) is **20–50 real ccswitch
tasks with acceptance checks**; grow the corpus toward it as more agent-config
invariants and incident regressions are captured.
