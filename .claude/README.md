# `.claude/` — SDLC governance control plane

Version-controlled agent policy for ccswitch (AI-Native SDLC design §7, issue #805).
This tree is the **enforcement** side of the governance core in
`server/sdlc/governance.py` (the pure decisions); everything here wires those
decisions into live Claude sessions and CI.

## Files

| File | Purpose |
|---|---|
| `settings.json` | Project settings. Wires the **prod-deploy approval gate** as a `PreToolUse` hook on `Bash`. Overridable by a user's own settings — for the non-negotiables, see `managed-settings.json`. |
| `managed-settings.json` | **Non-overridable** deny rules (constitution IV). Kept in exact sync with `server.sdlc.governance.MANAGED_DENY`; `test_sdlc_governance_config.py` fails CI if they drift. |
| `hooks/prod-deploy-gate.py` | The gate. Blocks a production deploy command unless `RELEASE_APPROVAL` is set (and the approver differs from the change author). Reuses `governance.evaluate_release`; fails **closed**. |
| `REVIEW.md` | **PR review policy** (Phase 4, issue #806). Loaded by `server.sdlc.review_policy` and injected into `pr_doctor` review sessions. Defines: allowed finding categories (bug / security / compliance), severity tags and their merge-blocking status, nit cap, excluded file patterns, separation-of-duties rules, `@claude` comment-driven fix contract, and the CLAUDE.md correction-seeding format. |

## Permission tiers by environment

`governance.permission_tier(env)` maps an environment to an existing ccswitch
session mode — a policy over the permission engine that already exists, not a new
one:

| Environment | Session mode | Behaviour |
|---|---|---|
| `dev` | `full-auto` | Approve every tool, including shell — fast local loop. |
| `staging` | `acceptEdits` | Auto-accept file edits; shell/network still prompt. |
| `prod` | `interactive` | Ask a human for every tool — nothing is automatic. |

## The prod-deploy gate

A production deploy (a `Bash` command with a deploy verb targeting prod, or run
with `CCSWITCH_DEPLOY_ENV=prod`) is denied unless:

1. `RELEASE_APPROVAL=<change/incident ref>` is set in the deploy environment, **and**
2. the approver (`CCSWITCH_RELEASE_APPROVER`) differs from the change author
   (`CCSWITCH_RELEASE_AUTHOR`) — separation of duties (design §4 step 6). The agent
   that wrote the change cannot authorize its own release.

The deny message carries both the reason and the unblock route.

## Evals

The CI eval suite (`evals/`, `.github/workflows/evals.yml`) gates any change under
`CLAUDE.md`, `.claude/**`, or the skills/hooks tree on a pass-rate threshold. See
`evals/README.md`.
