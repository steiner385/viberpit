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
4. **Credentials are SHARED from dobby, never copied.** *(Supersedes the earlier just-in-time-push
   decision — see "Why sharing beats copying" below.)* Each account's config dir
   (`~/.claude-accounts/cfg/<account>/`) lives canonically on dobby and is mounted by every worker,
   so exactly **one** `.credentials.json` exists per account. Workers hold no copy to drift.
5. **No account-to-host affinity. Every account is usable from every node, concurrently.** This
   replaces the earlier affinity decision, which existed only to prevent refresh-token desync — a
   problem decision 4 now removes at its root rather than scheduling around.
6. **Repo locality is a first-class dispatch input**, not an afterthought. Sessions work inside
   `<repo_root>/.ccswitch/worktrees/`, and the hosts hold different repos (dobby:
   `/mnt/ssk-ssd/tony/GitHub/…`; rocky: `~/GitHub/…`). A dispatcher blind to this strands sessions.
7. **Rocky does not join the tailnet.** It is reached over the LAN from dobby only, and never serves
   user-facing traffic. Tailscale identity auth (`server/auth/middleware.py`) remains the edge's
   user-facing gate, unchanged; mTLS guards only the second hop.

### Why sharing beats copying (rationale for decisions 4-5)

The hazard was never *concurrent refresh* — it was **divergent copies**.

Anthropic refresh tokens are single-use. With one config dir per host, a lost race is terminal:
host A rotates the token, host B still holds the old one, B's token is now permanently dead, and
nothing tells B — the `needs-reauth` cascade recorded in `specs/001-ccswitch-web/quickstart.md`
T060 finding #2. With a single shared file, the loser of the same race simply re-reads and sees the
winner's fresh token. The race still happens; it stops being fatal.

Sharing therefore reduces cross-host risk to **exactly the single-host risk already accepted
today**, and it does so without any exclusivity mechanism — which is what makes decision 5 possible.

**Refresh serialization is weaker than it looks.** `src/ccswitch/accounts.py:100` holds per-account
locks as `threading.Lock` — *in-process only*. It does not protect two processes on one host (the
literal "CLI + server" cause of T060), let alone two hosts. Once Postgres lands (1b), upgrade these
to Postgres advisory locks, which span hosts. Known limit: the `claude` binary refreshes tokens
itself inside `CLAUDE_CONFIG_DIR`, and Principle I forbids patching it — so no lock we own can cover
that path. This is precisely why the shared file matters: it changes the *consequence* of a race we
cannot fully prevent.

**Mount mechanics.** `.credentials.json` sits directly in `cfg/<account>/` beside `.claude.json`.
Symlinking the single file is NOT viable: config saves are typically write-temp-then-rename, and a
rename replaces a symlink with a real file, silently breaking the link. The **directory** must be
mounted. That also puts `.claude.json` (~36 KB, rewritten every few minutes) on the network —
negligible bandwidth, but it makes the mount a per-session dependency. Harden it (systemd mount unit
with retry, or autofs) and define the behavior when it vanishes mid-turn; do not leave that to
chance.

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
        │  daemon  server/ipc/  ──  CLAUDE_CONFIG_DIR mounted FROM dobby     │
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

### 3. Shared credential mount + cross-host refresh lock

Substantially **smaller** than originally scoped: sharing removes the push protocol, the tmpfs
materialization, the write-back path, and its conflict resolution entirely. What remains:

- Mount each account's `cfg/<account>/` from dobby on every worker, hardened against transient
  loss (systemd mount unit with retry, or autofs) — the mount is a per-session dependency.
- Define and test the mid-turn mount-loss behavior. A session that cannot read `.credentials.json`
  must fail loudly and recoverably, never silently re-auth or half-write.
- Replace the in-process `threading.Lock` in `src/ccswitch/accounts.py:100` with a Postgres advisory
  lock so ccswitch's own refresh path serializes across hosts. Depends on 1b.

**Ships:** every account usable from every node, concurrently, with one canonical credentials file.

### 4. Host registry + dispatch policy

The genuinely new capability. A host registry (endpoint, health, capacity, repo inventory,
capability flags), health/liveness probing, and a dispatcher that ranks hosts on free memory, load,
repo locality — then composes that with `selector.py`'s existing account ranking. Decision 5
removes the affinity dimension, so host choice and account choice are now genuinely **independent**:
`rank_accounts` runs unchanged and host ranking is a separate, composable filter rather than a
joint optimization over (host, account) pairs. Must preserve the split-brain guard documented in `deploy/ccswitch.service.d/remote.conf`:
when the edge cannot reach a daemon it must never silently fall back to an in-process orchestrator
over the same database. That failure caused the 2026-06-23 "Ready while working" incident, and N
hosts multiply the opportunities for it.

**Ships:** intelligent multi-host dispatch — the end state.

## Cross-cutting concerns

- **Security (Principle IV).** The change widens the daemon's RPC surface to the LAN and exposes
  each account's credentials over a network mount. mTLS with `ssl_cert_reqs=CERT_REQUIRED` enforces
  auth at the TLS handshake before any ASGI handler runs; the CN allowlist narrows it from "any
  CA-signed client" to "the edge". No insecure fallback: a missing TLS file must fail loud at
  startup. The credential mount is a separate trust surface from the mTLS channel and needs its own
  transport protection and access scoping — it must not be world-readable on the LAN. Note that
  rocky already stores live Claude credentials at `~/.claude/.credentials.json` for the user's own
  use, so this introduces no new *class* of exposure on that host, but it does place additional
  accounts within reach of anything that compromises the mount. Sub-project 3 requires its own
  security review before merge (`security-self-review` skill).
- **Testing (Principle V).** Each sub-project is TDD'd. Postgres needs the suite run against a real
  Postgres, not only SQLite, or the migration's value is unverified. Dispatch logic must be unit
  testable without two live hosts (fake host registry entries).
- **Rollback.** Every sub-project keeps its predecessor path runnable: #1 retains the SQLite file,
  #2 leaves `unix://` as dobby's default, #4 degrades to single-host when the registry has one entry.
- **Principle VI.** Each sub-project deletes the path it replaces in the same PR — no parallel
  single-host and multi-host dispatch implementations coexisting.

## Open questions

1. **Repo locality: hard filter or dispatch-and-clone?** `ensure_clone` (`src/ccswitch/gitrepos.py:335`)
   is already lazy and idempotent — it clones when missing, fetches when present — so auto-clone on a
   cold host works today with no new code. The question is whether the dispatcher should *avoid*
   paying that cost. Clones run 750-830 MB per owner, but `find_local_reference` (`:305`) passes
   `--reference-if-able <ref> --dissociate` when a local copy exists, and rocky's `~/GitHub` already
   holds KinDash, Capacinator and others — so cold cost is bimodal: fast for repos rocky mirrors,
   a full network clone otherwise. A hard filter is likely self-defeating: rocky starts with almost
   no ccswitch clones, so it would strand nearly every session on dobby and defeat the offload goal.
   Candidate answers: soft preference (warm hosts score higher, cold hosts still eligible),
   background pre-provisioning of a configured repo set, or ignore locality entirely.
   **Unresolved — decide before sub-project 4's spec.**

2. **Mount-loss behavior mid-turn.** Decision 4 makes the mount a per-session dependency. What a
   live session should do when `cfg/<account>/` becomes unreachable mid-turn is undefined. Resolve
   during sub-project 3.

## Deferred (explicitly not in scope)

- Rocky joining the tailnet or serving user-facing traffic.
- A third+ worker host (the design generalizes, but only dobby + rocky are provisioned).
- Certificate rotation / ACME automation; certs are operator-minted and manually renewed.
- A centralized token-refresh service — moot under decision 4; a shared file needs no central
  refresher, and the `claude` binary's own refresh cannot be routed through one regardless.
- Live session **migration** between hosts. Dispatch chooses a host at session creation; moving a
  running session across hosts is a separate, much harder problem.
