# Spec 002 — research record

## Cross-host resume drill (Phase 4, Task 17 — constitution Principle III gate)

**Question the plan makes this task answer before Tasks 18–19 may build on it:** does the
Claude CLI resume a conversation by `claude_session_id` on a *different* host, from a composed
per-account config dir, when the worktree sits at a different (or recreated) path — and is the
transcript actually present in that dir on the other host?

**Answer (2026-11-27): yes to both, verified against the real binary; FR-204 keeps its
resume-preferred ordering.** The evidence is the D-17 verification spike, which ran this exact
drill with a real model turn on the second host
(`docs/superpowers/specs/2026-11-05-d17-move-session-verification.md`):

| Claim | Evidence | Where |
|---|---|---|
| `--resume <id>` ignores the cwd: a transcript written under one project slug resumes from another cwd | Probe P1 against CLI 2.1.318 (dobby) and 2.1.293 (rocky), no model, API stub recording the first request: both sent the full prior conversation | D-17 §B.3 |
| The per-account `CLAUDE_CONFIG_DIR` — `projects/` included — is **one shared directory on both hosts**, so nothing has to be copied for the transcript to be present | `ls ~/.claude | md5sum`, `projects/` entry count and `history.jsonl` size/mtime identical on dobby and rocky; every live rocky session's `<id>.jsonl` found on dobby at its slug (6/6) | D-17 §B.3 |
| A real cross-host resume of a session whose worktree was **recreated** on the other host works with a model turn (the codeword probe answered from the prior conversation) | Gate 2 — PASS | D-17 §G |
| Version skew between the two hosts' CLIs does not break the resume | Probe P2: a 2.1.318 transcript resumed by 2.1.293 | D-17 §B.3 |
| Lookup cost when the cwd slug differs: the fast path is `projects/<slug(cwd)>/<id>.jsonl`; the glob fallback over sshfs is ~5 s, the CLI's own scan ~7–8 s | Gate 3 — PASS with caveat | D-17 §G |
| Codex threads do **not** travel: `thread/resume` has no path or history input, the rollout lives in a host-local `CODEX_HOME` | app-server schema 0.153.2 | D-17 §B.4 |

**Consequences carried into Tasks 18–19 (`server/sessions/adoption.py`):**

- Adoption keeps the worktree **path** identical (the row's `cwd`), so `_find_transcript`'s fast
  path and the CLI's own slug lookup both hit; the worktree itself is recreated from the branch by
  `_ensure_session_cwd` on the new host (unpushed work stays on the dead host and the continuity
  prompt says so).
- Transcript presence is checked with `_find_transcript` on the per-account dir **before** the
  driver build; present → resume by id, absent → a fresh conversation with the continuity prompt.
  A lookup that *raises* is treated as present so `ensure_driver`'s own fallback (which only
  starts fresh once the file is proven gone) decides — a flaky mount must never orphan a
  transcript.
- A Codex session adopted to another host is always a fresh thread (with the continuity prompt).

**Re-running the drill:** `scripts/verify-cross-host-resume.sh` (manual, not CI) — on host A
creates a session, runs one no-model turn against a local API stub, notes the id; on host B (or a
second config dir) recreates a worktree at a different path and resumes by id through the real
binary, asserting the stub sees the prior conversation. It writes nothing into a real account dir.
