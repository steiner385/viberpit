# Agent SDLC governance control plane

Version-controlled agent policy for ViberPit (AI-Native SDLC design §7, issue #805).
This tree is the **enforcement** side of the governance core in
`server/sdlc/governance.py` (the pure decisions); everything here wires those
decisions into live Claude sessions and CI.

Codex uses the same governance core through `AGENTS.md`, `.codex/config.toml`,
`.codex/rules/`, and `.agents/skills/`. `CLAUDE.md` and `.claude/REVIEW.md` remain
the canonical cross-agent context and review-policy documents to avoid parallel copies.

## Files

| File | Purpose |
|---|---|
| `settings.json` | Project settings. Wires the **prod-deploy approval gate** as a `PreToolUse` hook on `Bash`. Overridable by a user's own settings — for the non-negotiables, see `managed-settings.json`. |
| `managed-settings.json` | **Non-overridable** deny rules (constitution IV). Kept in exact sync with `server.sdlc.governance.MANAGED_DENY`; `test_sdlc_governance_config.py` fails CI if they drift. |
| `hooks/prod-deploy-gate.py` | The gate. Blocks a production deploy command unless `RELEASE_APPROVAL` is set (and the approver differs from the change author). Reuses `governance.evaluate_release`; fails **closed**. |
| `REVIEW.md` | **PR review policy** (Phase 4, issue #806). Loaded by `server.sdlc.review_policy` and injected into `pr_doctor` review sessions. Defines: allowed finding categories (bug / security / compliance), severity tags and their merge-blocking status, nit cap, excluded file patterns, separation-of-duties rules, `@claude` comment-driven fix contract, and the CLAUDE.md correction-seeding format. |

## Impeccable (vendored design skill)

[Impeccable](https://impeccable.style) (`pbakaus/impeccable`, Apache-2.0) is installed as a
project skill for both harnesses with its documented installer, no hooks:

```bash
npx --yes impeccable@4.1.0 install -y --project --no-hooks --providers=claude,codex
```

- Skill build **4.3.1**, engine **0.1.5** (`.agents/skills/impeccable/scripts/VERSION`). The
  installer emits a *different* build per harness (`.claude/skills/impeccable` for Claude,
  `.agents/skills/impeccable` + `agents/*.toml` for Codex), so unlike our own skills the Claude
  copy is a real directory, not a symlink — `contract.json` lists it under
  `skills.provider_built` and `agentctl check` only verifies both trees exist. Claude sub-agents
  land in `agents/impeccable-*.md` beside the role projections.
- The engine is a stripped native binary fetched from the GitHub release `engine-v<VERSION>`
  and sha256-verified against its sidecar. It is **gitignored** (`scripts/bin/`); the launcher
  (`scripts/impeccable`) falls back to `~/.impeccable/bin/<VERSION>/` and downloads on first use.
  Sandboxes without egress need `IMPECCABLE_BIN` pointing at a preinstalled engine.
- Hooks are deliberately **not** installed: they run the engine after every `Edit|Write` and on
  `Stop` in every session, including the automated ones this repo dispatches. Opt in per
  checkout with `/impeccable hooks on` (writes the untracked `.claude/settings.local.json`).
- `/impeccable init` (writes `PRODUCT.md`) and `/impeccable document` (`DESIGN.md`) are
  interactive and have not been run.
- Update: re-run the install command with the new version, then `python3 .agents/agentctl.py
  check .` and confirm `git status` shows no `scripts/bin/` files.

## Permission tiers by environment

`governance.permission_tier(env)` maps an environment to an existing ViberPit
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
