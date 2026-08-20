# Multi-host session fleet (dobby + rocky) — Umbrella Design

**Date:** 2026-08-20
**Status:** Approved (brainstorm) → decomposed into four sub-projects, each needing its own spec + plan
**Scope:** Umbrella only. This document commits the target architecture, the cross-cutting
decisions, and the build order. It deliberately does NOT contain implementation detail — each
sub-project below gets its own design doc and implementation plan.

## Goal

Run `claude` sessions on **both** `dobby` and `rocky`, dispatching each new session to the better
host, while `dobby` remains the single owner of the web edge, the database, and account credentials.

The driver is **compute offload**. Measured on dobby 2026-08-20:

- Load average `12.74 / 16.62 / 17.71` against **16 cores**.
- The single largest CPU consumer was `node … prisma generate` at **159%**, running inside
  `.ccswitch/worktrees/KinDash/d37b515d/` — i.e. ccswitch session work, not something else.
- `ccswitch-daemon.service` held **2.23 GB** RSS and ~1.3 cores sustained.
- dobby is also a desktop: 1Password, claude-desktop, Chrome, `tracker-extract` and a 2.7 GB JVM
  compete with sessions for the same cores.

## Host inventory (verified 2026-08-20)

| | dobby (master) | rocky (worker) |
|---|---|---|
| Cores | 16 | 8 |
| RAM total / available | 19 GB / 10 GB | 31 GB / 28 GB |
| OS | Linux 7.0.0-28-generic | Ubuntu 24.04.4 LTS |
| Free disk | 50 GB on `/`, 325 GB on `/mnt/ssk-ssd` (repos live here) | 176 GB on `/` |
| Tailscale | yes | **no** (LAN only) |
| Reachable as | tailnet `dobby` / `100.102.11.121` | `rocky.lan`, `192.168.243.246`, `fd5b:…` (LAN only) |
| `claude` / `uv` / ccswitch checkout | yes | **none** |
| `node` / `git` / `python3` | yes | yes |
| Passwordless SSH `tony@rocky` | **already working** (verified under `BatchMode=yes`) | — |

Adding rocky **trades cores for memory**: parallelism halves (16 → 8) but free RAM nearly triples
(10 GB → 28 GB). This matters because `ccswitch-daemon.service` declares `MemoryHigh=10G` /
`MemoryMax=12G` while dobby only *has* ~10 GB available — the daemon is currently sized for memory
it cannot obtain. Build-heavy sessions (prisma/pnpm/vitest) prefer dobby's cores; memory-heavy
session fleets prefer rocky. The dispatcher must know this.

## Current architecture (verified)

- **Edge** (`server/app.py`) and **daemon** (`server/ipc/`) are two processes sharing one SQLite
  file: `CCSWITCH_DB_URL=sqlite+aiosqlite:////home/tony/.claude-accounts/ccswitch.db`.
  The daemon is a first-class DB writer — `server/ipc/serve.py:96-97` calls `init_db` directly.
- The live daemon runs from `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv`, **not**
  the `WorkingDirectory` its unit file names. The deploy worktree is the real runtime root.
- **A cross-host daemon transport already exists.** `server/ipc/paths.py` accepts `unix:///path`
  (default) or `https://host:port`; `server/ipc/tls.py` implements mutual TLS. Specced in
  `docs/superpowers/specs/2026-06-26-cross-host-daemon-mtls-design.md`, operator recipe in
  `specs/001-ccswitch-web/quickstart.md:22`.
- The CN/SAN client allowlist that the 2026-06-26 design deferred to "v2" **has since shipped**:
  `client_allowlist()` at `server/ipc/tls.py:25` reads `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST`.
- `#204` made the `/vnc` WebSocket transport-agnostic, so browser-based reauth on a remote host's
  Xvfb streams to the dobby-served UI with **no new code**.
- Reauth is already proxied daemon-ward: `server/app.py:393-396` selects `RemoteReauthManager`.

### What does NOT exist today

- **Any host-pool concept.** `sessiond_endpoint()` resolves exactly one `Endpoint`. A repo-wide
  search for `remote_host` / `ssh_host` / host-pool naming returns only a spec mention.
- **Any host dimension in selection.** `src/ccswitch/selector.py` ranks *accounts* by headroom only.
- **Postgres support.** No `asyncpg` and no `postgres` reference in `pyproject.toml`,
  `server/config.py`, or `server/db/__init__.py`. Postgres is anticipated in CLAUDE.md, not wired.

### Host-local couplings that multi-host must resolve

| Coupling | Location | Problem |
|---|---|---|
| SQLite DB | `~/.claude-accounts/ccswitch.db` (WAL) | Two daemons + edge cannot share a WAL file across hosts |
| Account store | `~/.claude-accounts/cfg`, `store/` | Edge reads it locally (`server/api/accounts.py:26-31`); remote daemons need it to launch |
| Attachments | edge `server/api/attachments.py`, daemon `server/sessions/attachments.py` | Both sides write |
| Worktrees | `<repo_root>/.ccswitch/worktrees/` (`src/ccswitch/worktrees.py:158`) | Repo sets differ per host |
| Archive home | `/mnt/hdd-storage/ccswitch-archives` | dobby-local mount |
| gh config dir | `~/.ccswitch/gh` (`CCSWITCH_GH_CONFIG_DIR`) | per-host |

## Decisions

1. **dobby is the master.** It keeps the web edge, the database, and the canonical credentials.
   rocky is a pure session worker: it runs a daemon and nothing user-facing.
2. **Postgres replaces SQLite**, running on dobby. This is a hard prerequisite, not a preference —
   the moment a second daemon exists, a single-writer WAL file on one host is unviable.
3. **mTLS is the edge→daemon transport** for every remote daemon, using the shipped
   `https://host:port` endpoint plus the `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST` CN pin. Trust anchor
   is an operator-controlled private CA; the CA key is kept off both dobby and rocky where practical.
4. **Credentials are pushed just-in-time and never persisted on a worker.** dobby holds the only
   durable copy. At session start the edge decrypts with `CCSWITCH_MASTER_KEY` and pushes the
   account's material over the mTLS channel; the worker materializes a `CLAUDE_CONFIG_DIR` on
   **tmpfs**, and rotated refresh tokens are written back to dobby before teardown. No plaintext and
   no durable ciphertext lands on rocky's disk.
5. **An account is active on at most one host at a time (affinity).** Anthropic refresh tokens are
   single-use; `specs/001-ccswitch-web/quickstart.md` T060 finding #2 records concurrent refreshers
   desyncing all three accounts into `needs-reauth`. Affinity is the mitigation; a centralized
   refresher is the deferred alternative if affinity proves too restrictive.
6. **Repo locality is a first-class dispatch input**, not an afterthought. Sessions work inside
   `<repo_root>/.ccswitch/worktrees/`, and the hosts hold different repos (dobby:
   `/mnt/ssk-ssd/tony/GitHub/…`; rocky: `~/GitHub/…`). A dispatcher blind to this strands sessions.
7. **Rocky does not join the tailnet.** It is reached over the LAN from dobby only, and never serves
   user-facing traffic. Tailscale identity auth (`server/auth/middleware.py`) remains the edge's
   user-facing gate, unchanged; mTLS guards only the second hop.

## Target architecture

```
            tailnet
               │
        ┌──────▼────────────────────── dobby (master) ──────────────────────┐
        │  edge  server/app.py   ──  Postgres  ──  ~/.claude-accounts        │
        │        (UI + API)          (canonical)     (canonical creds)       │
        │            │                                                       │
        │            ├── unix:///run/ccswitch/sessiond.sock ── local daemon   │
        └────────────┼───────────────────────────────────────────────────────┘
                     │ mTLS  https://rocky.lan:9443
        ┌────────────▼───────────── rocky (worker) ─────────────────────────┐
        │  daemon  server/ipc/  ──  tmpfs CLAUDE_CONFIG_DIR (JIT creds)      │
        │          claude PTYs  ──  ~/GitHub/… + .ccswitch/worktrees         │
        │          Xvfb (reauth) ─  Postgres client → dobby                  │
        └───────────────────────────────────────────────────────────────────┘
```

The edge holds a **map** of host → `Endpoint` rather than a single endpoint, and dispatch selects a
host before selecting an account. dobby's own daemon keeps its Unix socket — the local path stays
zero-overhead and every existing single-host deployment is unaffected.

## Sub-projects

Each needs its own design doc + implementation plan. Build in this order; each is independently
valuable and independently shippable.

### 1a. CI retention + app-session prune

Discovered while planning the migration (measured 2026-08-20): the live DB is **7.8 GB** and
growing ~1 GB/day. `ci_test_result` holds 11,845,027 rows — 11,567,799 of them written in the
preceding 7 days — of which only **542 are `failed`**; 430,779 are true duplicates by
`(run_id, suite, test_id)`. `app_sessions` holds 2,204,240 rows, **2,162,193 expired**, minted at
~45,000/day because `_resolve_or_create_app_session` inserts a row for every cookie-less request.
The existing `retention_days` default of 90 never bites, since almost nothing is that old.

Migrating that into Postgres would be slow and would inherit the growth, so this lands first:
dedupe JUnit ingestion, fold passed/skipped results into per-day counts (keeping failures raw),
reuse live app sessions, and compact the existing backlog.

**Plan:** `docs/superpowers/plans/2026-08-20-ci-retention-and-session-prune.md`
**Ships:** a bounded database, ~90% smaller, with the growth stopped at source.

### 1b. Postgres migration

Replace SQLite with Postgres on dobby. Add `asyncpg`. Verify the generic `JSON` columns map cleanly
(preserving `JSON(none_as_null=True)`, `server/db/models.py:80`, which exists so `None` persists as
SQL NULL rather than the literal `'null'`). `init_db` already calls `create_all` unconditionally and
guards its SQLite-only repair passes behind `if url.startswith("sqlite")`, so `_reconcile_sqlite_columns`
and `_assert_no_schema_drift` correctly no-op on Postgres. One-shot data copy with both services
stopped, verified by per-table row counts before cutover, with the SQLite file retained for rollback.

**Ships:** ccswitch on Postgres, single-host, no behavior change. **Unblocks:** everything else.
**Depends on:** 1a — migrate a compacted DB, not 7.8 GB of prunable rows.

### 2. Rocky as a provisioned session host

Provision rocky (repo, venv, `claude`, Xvfb, node, systemd unit, `/etc/ccswitch/ccswitch.env`),
mint the CA plus server/client certs, and point the edge at `https://rocky.lan:9443`. The server
cert's SAN **must** be `DNS:rocky.lan, IP:192.168.243.246` — the quickstart flags hostname mismatch
as the top footgun. Also moves the deploy path: `server.deploy` restarts the daemon with a local
`systemctl` today and must reach rocky over SSH, which is what makes the already-working passwordless
`tony@rocky` load-bearing infrastructure rather than a convenience.

**Ships:** sessions running on rocky instead of dobby (single remote daemon — the shipped mTLS
feature, exercised end to end). **Note:** until #3, this requires accounts reachable on rocky, so
sub-project 2 is validated with a single throwaway account before #3 generalizes it.

### 3. Credential distribution + account/host affinity

Implement decision 4 (JIT push, tmpfs config dir, write-back of rotated tokens) and decision 5
(affinity). Includes the write-back protocol and its failure modes: a worker that dies mid-session
must not strand a rotated refresh token that dobby never recorded.

**Ships:** any account usable on any host, with no plaintext at rest on workers.

### 4. Host registry + dispatch policy

The genuinely new capability. A host registry (endpoint, health, capacity, repo inventory,
capability flags), health/liveness probing, and a dispatcher that ranks hosts on free memory, load,
repo locality, and account affinity — then composes that with `selector.py`'s existing account
ranking. Must preserve the split-brain guard documented in `deploy/ccswitch.service.d/remote.conf`:
when the edge cannot reach a daemon it must never silently fall back to an in-process orchestrator
over the same database. That failure caused the 2026-06-23 "Ready while working" incident, and N
hosts multiply the opportunities for it.

**Ships:** intelligent multi-host dispatch — the end state.

## Cross-cutting concerns

- **Security (Principle IV).** The change widens the daemon's RPC surface to the LAN and adds a
  credential-transport path. mTLS with `ssl_cert_reqs=CERT_REQUIRED` enforces auth at the TLS
  handshake before any ASGI handler runs; the CN allowlist narrows it from "any CA-signed client" to
  "the edge". No insecure fallback: a missing TLS file must fail loud at startup. Sub-project 3
  requires its own security review before merge (`security-self-review` skill).
- **Testing (Principle V).** Each sub-project is TDD'd. Postgres needs the suite run against a real
  Postgres, not only SQLite, or the migration's value is unverified. Dispatch logic must be unit
  testable without two live hosts (fake host registry entries).
- **Rollback.** Every sub-project keeps its predecessor path runnable: #1 retains the SQLite file,
  #2 leaves `unix://` as dobby's default, #4 degrades to single-host when the registry has one entry.
- **Principle VI.** Each sub-project deletes the path it replaces in the same PR — no parallel
  single-host and multi-host dispatch implementations coexisting.

## Deferred (explicitly not in scope)

- Rocky joining the tailnet or serving user-facing traffic.
- A third+ worker host (the design generalizes, but only dobby + rocky are provisioned).
- Certificate rotation / ACME automation; certs are operator-minted and manually renewed.
- A centralized token-refresh service (only if affinity proves too restrictive).
- Live session **migration** between hosts. Dispatch chooses a host at session creation; moving a
  running session across hosts is a separate, much harder problem.
