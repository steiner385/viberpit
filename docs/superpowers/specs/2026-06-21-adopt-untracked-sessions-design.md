# Adopt Untracked Live Claude Sessions — Design

**Date:** 2026-06-21
**Feature branch:** `002-adopt-untracked-sessions`
**Status:** Approved design, pending implementation plan

## Problem

Claude sessions get started outside ccswitch (e.g. an interactive `claude` / `claude agents`
launched directly in a terminal). These have live processes but are **not tracked** by the
ccswitch instance: they don't appear on the dashboard, aren't usage-aware, and can't be rotated
or controlled. We want a way to bring such untracked live sessions **under ccswitch jurisdiction**.

## Hard constraint discovered (verify-against-reality, Principle III)

ccswitch drives a session by **spawning its own SDK subprocess and owning its stdin/stdout**
(`SessionDriver` → `sdk_factory`). A process ccswitch did **not** fork cannot have its stdio
re-parented — an OS constraint, not a ccswitch limitation. A TUI `claude` also does not speak the
`stream-json` protocol the SDK client requires. **Literal takeover of a running process's I/O is
impossible.**

What *is* portable is the conversation behind the process: a `claude_session_id` transcript under
that account's `CLAUDE_CONFIG_DIR` (`<cfg>/projects/*/<sid>.jsonl`). ccswitch can resume it via the
SDK's `resume=<sid>` — the same path `ensure_driver` already uses for detached sessions.

### Live-process recon (point-in-time snapshot)

Of 21 `claude`-ish processes observed, they bucket as:
1. **ccswitch-owned (already tracked):** `claude_agent_sdk/_bundled/claude … --input-format stream-json`.
2. **Untracked live sessions (targets):** interactive `claude` / `claude agents` (no stream-json).
3. **Helper noise:** `--bg-pty-host`, `--bg-spare`, `daemon run`, `claude auth login`.
4. **Unrelated:** Storybook/esbuild, etc.

Two observations drive the design:
- **Processes churn fast** (one TUI PID exited between two scans seconds apart). Takeover must be
  **anchored to the transcript** (which persists), not the PID (which doesn't).
- **The live TUI ran under default `~/.claude`**, which is *not* a managed ccswitch account (those
  live under `~/.claude-accounts/cfg/<name>`).

## Decisions (locked with the user)

| Decision | Choice | Implication |
|---|---|---|
| Handoff model | **Force takeover now** | ccswitch terminates the original process tree, then resumes the conversation under its own driver for immediate live control. |
| Account mapping | **Skip unmapped** | Only adopt sessions whose `CLAUDE_CONFIG_DIR` maps to a known ccswitch account. Sessions on default `~/.claude` are reported but skipped. |
| Resume cwd | **Resume in original cwd** | Documented exception to the worktree-isolation guarantee; flagged on the record (`adopted_in_place`) so it's auditable. |
| Trigger / UI | **API + dashboard review** | Backend scans and returns candidates; dashboard shows per-session "Adopt (force-takeover)" with an explicit confirm. Per-session consent before any process is killed. |

## Architecture

Three units, each with one purpose and a well-defined interface:

### 1. Discovery — `server/sessions/adopt.py` (new)

Pure-ish, unit-testable via an injected process lister (no real `/proc` in tests).

```python
@dataclass(frozen=True)
class AdoptionCandidate:
    pid: int | None
    account: str | None          # resolved ccswitch account, or None if unmapped
    cwd: str
    claude_session_id: str
    transcript_path: str
    name: str                    # from transcript ai-title, else basename(cwd)
    mapped: bool                 # account is not None
    tracked: bool                # claude_session_id already in ccswitch DB
    skip_reason: str | None      # "unmapped config dir" | "already tracked" | None

def discover_candidates(
    known_accounts: list[str],
    tracked_sids: set[str],
    proc_lister: Callable[[], Iterable[ProcInfo]] = _proc_lister,  # injectable
) -> list[AdoptionCandidate]:
    ...
```

Algorithm:
1. Enumerate processes via `proc_lister` (default reads `/proc/*/cmdline`).
2. **Classify** — keep a process iff its cmdline is a `claude` invocation **without**
   `--input-format stream-json` (excludes ccswitch's own SDK drivers) and **not** a helper
   (`--bg-pty-host`, `--bg-spare`, `daemon run`, `auth login`).
3. For each candidate read `CLAUDE_CONFIG_DIR` (default `~/.claude` if unset) and `cwd` from
   `/proc/<pid>/{environ,cwd}`.
4. **Map config dir → account:** compare the realpath of `CLAUDE_CONFIG_DIR` against
   `paths.account_cfg_dir(name)` for each known account. No match → `mapped=False`,
   `skip_reason="unmapped config dir"`.
5. **Resolve `claude_session_id`:** glob `<cfg>/projects/*/<*>.jsonl`, restrict to the project dir
   matching `cwd`, pick the **most-recently-modified** transcript. Unresolvable → exclude with
   reason.
6. **Dedup:** if the `claude_session_id` is already in `tracked_sids`, set `tracked=True`,
   `skip_reason="already tracked"`.
7. Return *all* candidates (mapped + unmapped + tracked), each flagged. Nothing silently dropped —
   the dashboard shows skip reasons; a silent cap would read as "covered everything" when it isn't.

Adoptable = `mapped and not tracked`.

### 2. Takeover — `SessionOrchestrator.adopt_session(...)`

```python
async def adopt_session(self, claude_session_id: str, identity: str) -> SessionRecord:
    ...
```

1. **Re-resolve** the candidate from the transcript (the PID may have died since discovery —
   tolerated).
2. **Terminate** the original process tree if still alive: SIGTERM the candidate + its helper
   children (pty-host/spare), wait a bounded grace period, escalate to SIGKILL. A vanished PID is
   treated as success. Audit-logged (`process.terminate`).
3. **Register**: write a `Session` DB row + in-memory `SessionRecord` in the **original cwd**, no
   worktree, `status="idle"`, `adopted_in_place=True`.
4. **Resume**: build a `SessionDriver` with `resume=<claude_session_id>` in the original cwd, via a
   resume path that **skips worktree creation** (the conversation's uncommitted edits live in the
   real tree). Reuses the existing resume/fresh-fallback behavior from `ensure_driver`.
5. Return the `SessionRecord`.

### 3. API — `server/api/sessions.py`

- `GET  /api/sessions/adoptable` → `{ candidates: [...] }` (mapped, unmapped, tracked, with reasons).
- `POST /api/sessions/adopt` `{ "claudeSessionId": "..." }` → `201` with the new session record;
  `409`/`404` if the candidate is no longer adoptable.
- Both `Depends(current_identity)`; both write `AuditEvent`s (`session.adopt`, `process.terminate`).

### 4. Data model — `server/db/models.Session`

Add **nullable** `adopted_in_place: Mapped[bool | None]`. Nullable so `init_db` backfills it on the
dev SQLite DB without a migration (per the project's create_all/backfill rule). Surfaced on the
`SessionRecord` and in the wire format so the isolation exception is visible/auditable.

### 5. Frontend — dashboard

An "Adoptable sessions" panel:
- **Mapped + untracked** candidates: "Adopt (force-takeover)" button behind a confirm dialog that
  **explicitly warns it will terminate the running process** (risk of losing unsaved TUI state).
- **Unmapped / already-tracked** candidates: shown greyed with the skip reason.

## Data flow

```
dashboard ──GET /api/sessions/adoptable──▶ discover_candidates(accounts, tracked_sids)
                                                  │  (/proc scan → classify → map → resolve sid → dedup)
        ◀────────── candidates (flagged) ─────────┘
user confirms ──POST /api/sessions/adopt {sid}──▶ orchestrator.adopt_session(sid, identity)
                                                  │  terminate proc tree (audit)
                                                  │  write DB row + record (adopted_in_place, original cwd)
                                                  │  SessionDriver(resume=sid, no worktree)
        ◀──────────── 201 SessionRecord ──────────┘
session now appears in list_sessions, usage-aware, rotation-eligible
```

## Error handling

- **PID already gone:** skip termination, still resume from the transcript.
- **Transcript unresolvable:** candidate excluded from results with a reason (never adopted blindly).
- **Resume fails:** fall back to a fresh session (existing `ensure_driver` behavior) — the prior
  transcript still shows via replay.
- **Terminate races a vanishing PID:** treated as success.
- **Candidate no longer adoptable at POST time** (process gone *and* already tracked, or now
  unmapped): `409` with reason.

## Testing (TDD)

Discovery (`adopt.py`) with a **fake `/proc` lister**:
- Each bucket classified correctly: SDK-owned (excluded), TUI (candidate), helpers (excluded).
- Account mapping incl. default-`~/.claude` → `mapped=False`, correct `skip_reason`.
- `claude_session_id` resolution picks most-recently-modified transcript for the cwd's project dir.
- Dedup against `tracked_sids` sets `tracked=True`.

Takeover (`adopt_session`) with a **fake process-killer + fake SDK**:
- Resume-in-place: cwd == original, **no worktree created**, `adopted_in_place=True`.
- `process.terminate` + `session.adopt` audit rows written.
- Dead-PID path: no termination attempted, resume still happens.

API:
- `GET /adoptable` shape + flags; `401` without identity.
- `POST /adopt` happy path `201`; `409` when no longer adoptable; `401` without identity.

## Scope / non-goals

- **In scope:** discovery, force-takeover of mapped+untracked sessions, API, dashboard panel.
- **Out of scope:** adopting sessions on unmapped config dirs (reported, skipped); read-only "follow"
  mode; cross-platform discovery (Linux `/proc` only — the deploy target). These can be later
  increments if needed.

## Constitution compliance

- **I (never fork/patch the binary):** we resume via the SDK; the binary is untouched.
- **II (account = CLAUDE_CONFIG_DIR, per-process isolation):** skip-unmapped keeps "account" strictly
  a managed config dir; one driver per adopted conversation; original process is terminated before
  resume so two clients never share one token family (avoids the single-use-token / TOCTOU hazards
  the code already guards against).
- **III (verify against reality):** discovery reads the live `/proc` table and real transcripts.
- **IV (security-first):** endpoints require verified identity; terminations and adoptions are
  audit-logged.
- **V (TDD + quality gates):** tests precede implementation; the resume-in-place isolation exception
  is explicit and flagged.
- **VI (single source of truth):** reuses the existing detached-record + `resume=` machinery rather
  than a parallel path.
