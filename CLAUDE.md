# cc-account-switcher — Agent Context

`ccswitch` is a multi-account Claude Max session manager: a CLI core plus a web-hosted,
mobile-accessible interface for running and switching between live `claude` sessions across
several accounts, with automatic usage-aware account rotation.

**Governance:** all work MUST comply with the project constitution at
`.specify/memory/constitution.md` (v1.1.0, Principles I–VII). Notably: never fork/patch the Claude
binary (I); account = `CLAUDE_CONFIG_DIR`, strict per-process isolation (II); verify against the
real binary/API, not assumptions (III); security-first, exposure-conditional auth (IV);
TDD + quality gates (V); single source of truth / no dead code (VI); modern vetted deps (VII).

<!-- SPECKIT START -->
Active feature: **001-ccswitch-web** — Multi-Account Claude Session Manager (Web).
- Spec: `specs/001-ccswitch-web/spec.md`
- Plan: `specs/001-ccswitch-web/plan.md`
- Research: `specs/001-ccswitch-web/research.md`
- Data model: `specs/001-ccswitch-web/data-model.md`
- Contracts: `specs/001-ccswitch-web/contracts/`
- Quickstart: `specs/001-ccswitch-web/quickstart.md`

Stack: Python FastAPI/Uvicorn + SQLAlchemy(SQLite) backend · React+Vite+TS + Tailwind/shadcn +
xterm.js frontend · sessions are native `claude` in PTYs bridged to the browser over WebSocket ·
Tailscale-identity auth (v1) with a passkey path deferred to public hosting.
Build phase-by-phase: P0 config-dir launch correctness → P1 backend engine+API → P2 frontend
(dashboard+terminal) → P3 structured view → P4 public-hosting hardening.
<!-- SPECKIT END -->
