# Codex full-auto unattended execution

User approved 2026-09-09: Codex full-auto grants danger-full-access + never, including filesystem/network authority. Keep explicit governance restrictions, stricter modes and assistant isolation.

RCA: workspace-write/never prevented completion; #1452 changed never to on-request, restoring completion through human approvals. request_external deliberately prompts. Tests encoded the sandbox choice instead of unattended workflow completion.

1. Canonical profile: reuse full-access/never for full-auto; align automation eligibility, obsolete network opt-in, descriptions and regression tests.
2. Lifecycle: validate effective native policy at start/resume/profile transitions; cover restoration/recovery and fail closed on mismatch.
3. Acceptance: real pinned binary exercises edits, worktree commits, network and local test servers without routine approvals; preserve managed denials. Record push/PR evidence and remaining external constraints honestly.
4. Audit sibling assumptions/instructions, complete backend/frontend gates and independent review, publish ready PR with auto-merge.

Constitution: supported vendor API only, isolated account runtime/credential authority, real binary verification, preserved governance and auth, TDD, canonical profiles, no new dependencies.

Existing sessions adopt on safe resume/rebuild; do not interrupt active turns or restart a busy daemon.
