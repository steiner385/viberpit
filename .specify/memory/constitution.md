<!--
SYNC IMPACT REPORT
Version change: 1.1.0 → 1.2.0   [amendment]
Bump rationale (MINOR): Principles I–IV and the session-model constraint generalized from Claude
  Code alone to any native agent engine (Claude Code and OpenAI Codex). No principle is removed or
  weakened: Principle I keeps the absolute no-fork/no-patch/no-scrape rule and adds a gated
  allowance for experimental vendor surfaces; Principle II keeps per-process, per-account isolation
  and adds a single-credential-authority model that preserves the same guarantee — no long-lived
  credential off its owning host, no shared runtime home — by a different mechanism. Materially
  expanded guidance, hence MINOR. Driven by the native OpenAI Codex integration design.
Modified principles:
  - I. Black-Box Integration (generalized to native agent binaries; surfaces described by role;
    experimental surfaces gated on owner approval plus version pin, startup probe, kill switch,
    and fail-closed behavior)
  - II. Account = Engine-Scoped Identity (account defined as an engine-scoped identity spanning
    an authority home and host-local runner homes; credential-authority paragraph added)
  - III. Verify Against Reality (live smoke verification required for any native agent session)
  - IV. Security First (scope stated as native agent tools and paid AI-agent accounts)
Modified sections:
  - Technology & Architecture Constraints → Session model (PTY/xterm.js description replaced by
    daemon-owned engine drivers emitting normalized events; stable session id preserved)
Prior history: 1.1.0 (2026-06-19) exposure-conditional auth; 1.0.0 (2026-06-19) initial
  ratification — all core principles and governance.
Added sections: none
Removed sections: none
Templates requiring updates:
  - .specify/templates/plan-template.md  — ⚠ pending (spec-kit not initialized; file absent)
  - .specify/templates/spec-template.md  — ⚠ pending (spec-kit not initialized; file absent)
  - .specify/templates/tasks-template.md — ⚠ pending (spec-kit not initialized; file absent)
  When those templates are generated, add a "Constitution Check" step that asserts Principles I–VII.
Deferred TODOs: none.
-->

# ccswitch Constitution

ccswitch is a multi-account AI coding-agent session manager: a web-hosted, mobile-accessible
interface that runs and switches between live native agent sessions spread across several paid
AI-agent accounts, rotating accounts automatically to stay within each provider's usage limits.
This constitution defines the non-negotiable principles and governance for the project.

## Core Principles

### I. Black-Box Integration — Never Fork or Patch Native Tools/Binaries

The vendor-provided binary MUST be treated as an unmodified, external dependency. Integrate ONLY
through each vendor's supported, documented surfaces: its CLI, its per-account configuration
contract, its structured session/event protocol, and its usage/auth endpoints. An experimental,
alpha, or beta capability MAY be used only when the owner has explicitly approved it AND it carries
a pinned version, a startup contract probe, a kill switch, and fail-closed behavior. The project
MUST NOT fork, patch, repackage, or scrape the internal state of the bundled artifact.

Rationale: AI coding platforms typically ship as minified, proprietary, continuously-released
artifacts (no public source). Coupling to their internals is a perpetual reverse-engineering
treadmill; composing around unmodified, supported surfaces is the only maintainable path.

### II. Account = Engine-Scoped Identity; Strict Per-Process Isolation

An account is exactly one logical, engine-scoped identity. Claude Code maps that identity to one
`CLAUDE_CONFIG_DIR`; Codex maps it to one authority `CODEX_HOME` plus separate host-local runner
homes. Account selection MUST be per-process via the engine's config-dir environment variable. The
shared global credential file (e.g. `~/.claude/.credentials.json`) MUST NEVER be mutated in place.
Per-account identity and runtime state — credentials, client config, `daemon.*`, `sessions/`,
`agents/`, `tasks/`, history — MUST stay isolated per account; only static configuration and
conversation transcripts (needed for resume) MAY be shared, via an explicit allowlist. Unknown/new
config-dir entries default to isolated.

Where an engine's credential refresh cannot be safely performed by independent hosts at once,
exactly one credential authority MAY own the long-lived credential for an account. That authority
MUST hold the only canonical copy, outside any runner-mounted or exported path; MUST distribute
only short-lived access material, kept in process memory and never written to argv, environment,
or disk; and MUST NEVER transmit a refresh token to a runner. Runner processes MUST use a
host-local config directory and MUST NEVER share one. If the authority is unavailable, a session
holding an accepted token MAY continue until that token is rejected or a refresh is required; new
authentication and refresh MUST fail closed, and falling back to shared credentials remains
forbidden.

Rationale: per-process selection is the only concurrency-safe way to run many sessions on different
accounts at once; sharing identity/daemon state silently collapses sessions onto one account. A
single credential authority is permitted only where an engine refreshes inside a black box that
races when run concurrently; it preserves the same guarantee — no long-lived credential off its
owning host, no shared runtime home — through a different mechanism.

### III. Verify Against Reality, Not Assumptions

The behavior of every external surface — CLI commands, endpoint URLs, response shapes, on-disk
layout — MUST be verified against the actual binary or live API before it is relied upon.
Mock-only tests are insufficient evidence for boundary behavior. Any feature that launches or
drives a real native agent session MUST include a live smoke verification before it is considered done.

Rationale: every production bug in this project's history (`claude /login` vs `claude auth login`,
an empty-ledger `list`, `resets_at: null`) traced to a plausible-but-unverified assumption that
passed its mocked unit tests. Reality is the only authority on a boundary we do not own.

### IV. Security First — Authenticated, Secrets-Safe, Never Open

This service executes code (via native agent tools) across paid AI-agent accounts; it is a
remote-code-execution surface and MUST be treated as one:

- Every endpoint and WebSocket MUST be gated by an identity control appropriate to the deployment.
  Network-enforced identity (e.g. a private tailnet) MAY be the sole gate for a private-network
  deployment; app-level strong authentication (passkey/WebAuthn with signed server-side sessions)
  is REQUIRED before any public or non-private exposure. No deployment is ever reachable by an
  unidentified caller.
- The server MUST NOT be exposed on a public interface without explicit, documented hardening
  (app-level auth + TLS + rate-limited login). Default exposure is loopback reached over a private
  network (e.g. Tailscale).
- Secrets (account tokens, signing keys, passwords) come from the environment or a secret store,
  are NEVER committed, and are NEVER written to logs or error messages.
- OAuth tokens are stored with least-exposure at rest (per-account files mode 0600; hash one-time
  values, encrypt replayable secrets) and refreshed via supported endpoints only.
- An app-level strong-authentication path (hashed/credential-less login, signed sessions) MUST
  exist and be enable-able from day one; turning it on is a hard prerequisite for any non-private
  exposure, so the private→public transition is a configuration step, not a security rewrite.

Rationale: an RCE-capable service bound to financial accounts has no tolerable failure mode for an
unidentified caller or leaked secrets. Allowing network-enforced identity to gate a private-network
deployment keeps v1 simple without ever permitting anonymous access, while the required app-auth
path keeps public hosting reachable without a rewrite.

### V. Test-Driven Quality Gates

Test-Driven Development is the default: write the failing test, watch it fail, implement the
minimum to pass, refactor. Tests live beside the code they cover. Non-negotiable gates: 100%
unit-test pass rate, zero linter/type-checker errors, and strong typing (`any`/untyped escapes
minimized and justified). Security- and money-critical paths (auth, account selection, token
handling, billing-adjacent logic) MUST reach ≥85% coverage with explicit negative/abuse cases.

Rationale: this code spends real usage quota and runs tools on the host; correctness is a safety
property, not a nicety.

### VI. Single Source of Truth — No Parallel Implementations, No Dead Code

Before adding any constant, utility, config list, component, or service, search for an existing
source of truth and route through it. There MUST be exactly one canonical implementation of any
concept; a second "correct" copy is a defect. When a file, route, component, or feature is
replaced, the old version MUST be deleted in the same change — no half-dead code behind flags or
"leave it for now." When a fix reveals a repeatable failure shape, capture it as a reusable
fix-pattern note and audit for siblings.

Rationale: N-parallel-implementations and orphaned code are the most expensive, slowest-to-debug
failure shape; preventing them is cheaper than detecting them.

### VII. Modern, Vetted Dependencies — Use Libraries, Don't Reinvent

There is NO artificial dependency-count limit. Hard problems (terminal-over-network, WebSocket
framing, UI components, auth, ORM) MUST be solved with mature, actively-maintained,
license-compatible libraries rather than hand-rolled code. New dependencies SHOULD be evaluated for
maintenance health and security posture before adoption.

Rationale: for a code-executing service, well-worn libraries (FastAPI, an established ORM, a
component kit) are a smaller risk surface than novel, fragile, bespoke implementations.

## Technology & Architecture Constraints

- **Backend:** Python with an async web framework (FastAPI/Starlette + Uvicorn), Pydantic models,
  an SQL ORM (SQLAlchemy/SQLModel) over SQLite for local use, designed to swap to managed Postgres
  when hosted. Session orchestration uses the standard library where libraries don't fit.
- **Frontend:** React + Vite + TypeScript, Tailwind + a modern component kit (shadcn/ui), a typed
  data-fetching layer (TanStack Query), and a normalized session/transcript view, built to static
  assets the backend serves.
- **Repository layout (monorepo):** `ccswitch/` (account/usage/selection/config-dir core, reused as
  a library), `server/` (web backend: session manager, WS bridge, API, auth, db), `web/` (React
  frontend), `docs/` (specs and plans).
- **Configuration & portability:** all host/port/secrets/feature config via environment; no
  hardcoded paths or secrets. The app MUST run identically on localhost+Tailscale and on a cloud
  host (e.g. Railway) by configuration alone.
- **Session model:** each session is driven by daemon-owned engine drivers, scoped per-account and
  emitting normalized events. Native protocol types, raw frames, and unvalidated payloads MUST NOT
  leak upward; normalized engine identity, capabilities, model/reasoning/permission settings, and
  allow-listed `engine_metadata` MAY. A stable session id is preserved across rotation.

## Development Workflow & Quality Gates

- **Branching & merge:** never commit directly to the default branch; all changes land via pull
  requests from feature branches. Force-pushes use `--force-with-lease` only.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, …). Bugs and unexpected
  behavior get a tracking issue referenced from the fixing commit/PR.
- **Phased delivery:** ship in vertical phases that each produce working, independently-testable
  software (foundation → backend → frontend → enhancements). A phase is not "done" until its tests
  pass and (for session-launching work) its live smoke passes.
- **UI standards:** user-facing surfaces are mobile-first and meet WCAG 2.1 AA — keyboard
  operability, sufficient contrast, semantic structure, visible focus, and `prefers-reduced-motion`
  respected. New components ship with their tests (and stories where a visual component warrants).
- **Review:** changes are reviewed against this constitution before merge; a reviewer may block on
  any principle violation.

## Governance

This constitution supersedes ad-hoc convention. All specs, plans, and implementations MUST comply
with Principles I–VII; where a spec-kit plan/spec/tasks template exists, it MUST carry a
"Constitution Check" gate asserting these principles.

- **Amendments:** proposed via pull request that edits this file, states the rationale, and updates
  the version and Sync Impact Report. Amendments touching a principle's meaning require explicit
  acknowledgement in the PR description.
- **Versioning (semantic):** MAJOR for backward-incompatible governance/principle removals or
  redefinitions; MINOR for a new principle/section or materially expanded guidance; PATCH for
  clarifications and non-semantic refinements.
- **Compliance review:** at each phase boundary and before any public exposure, verify the build
  against Principles II (isolation), III (verification), IV (security), and V (quality gates) and
  record the result in the phase's plan or PR.

**Version**: 1.2.0 | **Ratified**: 2026-06-19 | **Last Amended**: 2026-09-04