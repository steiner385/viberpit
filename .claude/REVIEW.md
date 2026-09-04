# ccswitch PR Review Policy

Version-controlled review policy for ccswitch Initiative PRs.
Loaded by `server.sdlc.review_policy` and injected into `pr_doctor` review
sessions (AI-Native SDLC design §9 Phase 4, issue #806).

## What reviews check for

Reviews **pass** findings in these categories only (anything else is out of scope):

| Category | Description |
|---|---|
| `bug` | Logic errors, off-by-one, type mismatches, unhandled edge cases that will misfire in production. |
| `security` | Injection risks (SQL, shell, template), credential exposure, auth bypass, insecure defaults, SSRF, OWASP Top 10. |
| `compliance` | ToS violations, licence incompatibilities, GDPR/CCPA data-handling issues, constitution-IV non-negotiables. |

Reviews **do not** report on:
- Style preferences, naming, whitespace, comment wording.
- Performance micro-optimisations without a measured regression.
- Test coverage percentage (CI gates coverage; reviewers surface *missing test cases* for specific bugs, not blanket coverage shortfalls).
- Generated code (any file under `web/src/components/ui/`, `web/dist/`, `.venv/`, `node_modules/`).
- CI-enforced rules (anything `ruff`, `mypy`, or `eslint` already catches and that CI is green on).

## Severity tags

Every finding **must** carry exactly one tag in the PR comment:

| Tag | Meaning | Blocks merge |
|---|---|---|
| `[critical]` | Silent data loss, privilege escalation, or a crash path in production. | Yes |
| `[major]` | Clear bug or security hole that will be hit under normal use. | Yes |
| `[minor]` | Correctness issue in an edge case; negligible security impact. | No — fix encouraged |
| `[nit]` | Stylistic; take-or-leave. Counts toward the nit cap. | No |

## Nit cap

A review **must not** post more than **3 `[nit]`** findings per PR. Additional nit-level
observations may be batched into a single "further nits" comment rather than individual
threads to avoid flooding the review.

## Separation of duties

The agent that authored a PR's commits **cannot** approve that PR. If pr_doctor runs
a review session and the reviewing agent identity matches the authoring identity, it
**must** post findings as review comments (never as an approving review) and leave
the approval to a human or a different agent identity (see `governance.evaluate_release`
for the same principle applied to releases).

## `@claude` comment-driven fixes

A reviewer (human or agent) may request a fix by commenting on the PR review with:

```
@claude fix <description>
```

On seeing this, pr_doctor spawns a fix session that:
1. Reads the original PR diff and the `@claude` comment thread.
2. Pushes a commit that addresses the comment.
3. Resolves the review thread (posts a reply).

The fix agent **cannot** approve the PR — separation of duties as above.

## Feeding CLAUDE.md corrections

When a review finding of severity `[major]` or higher is seen for the **same pattern**
in two or more distinct PRs, pr_doctor automatically opens a PR to add a
`## Correction` entry to `CLAUDE.md` (or the relevant section) documenting the
anti-pattern and its fix. This closes the loop so the same mistake is not repeated.

### Entry format

```markdown
## Corrections

- **[pattern-slug]** — <one-line description of the anti-pattern>.
  Seen in: <PR #N>, <PR #M>.
  Fix: <one-line fix description>.
```
