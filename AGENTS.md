# ccswitch — Codex Agent Context

Before doing any work, read `CLAUDE.md` in full. It is the canonical, cross-agent
project context despite its historical filename. Follow its governance, architecture,
testing, completion workflow, and working norms.

## Codex adaptations

- Treat references to “Claude” as references to the active coding agent unless they
  specifically describe the Claude binary, Anthropic API, Claude sessions, or
  `CLAUDE_CONFIG_DIR`.
- Do not add a Claude `Co-Authored-By` trailer to Codex-authored commits. Use the
  configured Git identity and add an AI attribution trailer only when the user supplies
  one. This overrides only the attribution sentence in `CLAUDE.md`.
- Repo-scoped reusable workflows live under `.agents/skills/`. Invoke the matching
  skill when `CLAUDE.md` names one.
- Codex lifecycle policy is wired by `.codex/config.toml`; command restrictions live
  in `.codex/rules/`. Keep both aligned with the governance source of truth in
  `server/sdlc/governance.py`.

## Code Review Rules

Before reviewing a pull request or branch, read `.claude/REVIEW.md` in full and apply
it as the provider-neutral review policy. Report only allowed categories, use its
severity tags, respect the nit cap and exclusions, and preserve separation of duties.
