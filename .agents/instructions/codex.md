## Codex adaptations

- Treat references to “Claude” as references to the active coding agent unless they
  specifically describe the Claude binary, Anthropic API, Claude sessions, or
  `CLAUDE_CONFIG_DIR`.
- Do not add a Claude `Co-Authored-By` trailer to Codex-authored commits. Use the
  configured Git identity and add an AI attribution trailer only when the user
  supplies one. This overrides only the attribution rule in the shared guidance.
- Repo-scoped reusable workflows live under `.agents/skills/`; invoke the matching
  skill when the shared guidance names one.
- Codex lifecycle policy is wired by `.codex/config.toml`; command restrictions live
  in `.codex/rules/`. Keep both aligned with the governance source of truth in
  `server/sdlc/governance.py`.

## Code review

Before reviewing a pull request or branch, read `.claude/REVIEW.md` in full and apply
it as the provider-neutral review policy. Report only allowed categories, use its
severity tags, respect the nit cap and exclusions, and preserve separation of duties.
