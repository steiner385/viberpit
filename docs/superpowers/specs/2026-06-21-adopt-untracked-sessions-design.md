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
    skip_reason: str | None      # machine code: "unmapped" | "tracked" | None
    # UX MUST-FIX #2 — informed-consent risk signal. The whole danger of
    # force-takeover is unsaved/in-flight work; the user cannot judge that risk
    # without knowing how recently the session did anything. Computed from the
    # transcript .jsonl mtime at discovery time.
    last_activity: datetime      # transcript mtime (UTC)
    pid_alive: bool              # process still present at discovery (re-checked at adopt time)

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
   `skip_reason="unmapped"`.
5. **Resolve `claude_session_id`:** glob `<cfg>/projects/*/<*>.jsonl`, restrict to the project dir
   matching `cwd`, pick the **most-recently-modified** transcript. Record its **mtime as
   `last_activity`** (UX MUST-FIX #2). Unresolvable → exclude with reason.
6. **Dedup:** if the `claude_session_id` is already in `tracked_sids`, set `tracked=True`,
   `skip_reason="tracked"`.

`skip_reason` is a machine code; the UI maps it to plain-language copy (UX SHOULD #5):
`"unmapped"` → **"Not a managed account"** (subtext: running under an unmanaged config dir such
as `~/.claude`; move it to a managed account to adopt) · `"tracked"` → **"Already managed by
ccswitch"** (with a jump-to-session link).
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
   children (pty-host/spare), wait a **bounded grace period** (gives the TUI a chance to flush —
   exposed to the UI as a visible countdown, UX MUST-FIX #3), escalate to SIGKILL. A vanished PID is
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
  `409`/`404` if the candidate is no longer adoptable. The `201` body reports the **outcome** so the
  UI can be honest (UX MUST-FIX #3): `resumed: bool` (true = resumed the transcript, false = resume
  failed and a fresh session was started) and `terminatedPid: int | null` (null = process had
  already exited, nothing was killed).
- The endpoint **re-verifies liveness** before terminating, so the confirm dialog and the action
  agree on whether a kill will actually happen.
- Both `Depends(current_identity)`; both write `AuditEvent`s (`session.adopt`, `process.terminate`).

### 4. Data model — `server/db/models.Session`

Add **nullable** `adopted_in_place: Mapped[bool | None]`. Nullable so `init_db` backfills it on the
dev SQLite DB without a migration (per the project's create_all/backfill rule). Surfaced on the
`SessionRecord` and in the wire format so the isolation exception is visible/auditable.

### 5. Frontend — dashboard

An "Untracked sessions" panel (plainer than "Adoptable"). Inline list, **modal confirm**.
- **Mapped + untracked** candidates show: **name** (primary), human-friendly **repo/cwd**
  (basename + middle-elided path, full on hover), **account**, **last activity** ("3s ago" /
  "20m ago"), and **PID** (secondary). `claude_session_id` is demoted to a tooltip — a UUID
  identifies nothing to a human (UX SHOULD #4).
- The action is **risk-scaled** (UX MUST-FIX #1 + #2):
  - *Recently active* (last_activity within a small window, or `pid_alive`): button **"Stop & take
    over"** (destructive/red); confirm modal **names what dies** and requires a checkbox — *"I
    understand this will stop PID {pid} and may lose unsaved work."*
  - *Idle*: button **"Manage this session"**; a lighter named confirm (no checkbox).
- **Confirm modal copy:** *"ccswitch will stop the `claude` process running in **{repo}** (PID
  {pid}, under **{account}**) and resume the conversation **"{name}"** here. Any unsaved work in
  that terminal will be lost. This can't be undone."* Primary action red; **Cancel** focused/default.
- **Post-adoption (MUST-FIX #3):** success toast reflects the real outcome — *"Took over "{name}"
  under {account}. It's now in your sessions list."* or, on resume-fallback, *"Couldn't resume the
  previous conversation — started a fresh session instead."*
- **Unmapped / already-tracked** candidates go in a collapsed **"Not adoptable (N)"** subsection so
  greyed dead-ends don't dilute or get mis-clicked; each shows plain-language skip copy (#5), and
  tracked ones link to their existing session row.

## UX requirements (from UX review, 2026-06-21)

MUST-FIX #1–#3 are folded into the sections above (risk-scaled consent, `last_activity` signal,
honest post-adoption outcome). The remaining findings the implementation plan MUST honor:

- **Candidate legibility (#4):** lead with name + human-friendly repo/cwd + account + last-activity;
  PID secondary; `claude_session_id` in a tooltip only. *Nice:* a 1–2 line transcript-tail preview
  (last user/assistant turn) as the strongest "yes, this is the one" signal, if cheaply readable.
- **Staleness (#6):** panel shows **"scanned Ns ago"** + a **Refresh** button; auto-refresh on open.
  `409`/`404` on adopt is a **normal, expected** outcome (the PID may die between render and click),
  not a red error — re-verify liveness and adjust the dialog ("This process already exited — adopting
  will just resume the conversation, nothing will be killed").
- **Empty / error states (#7):**
  - *Empty:* "No untracked sessions found. ccswitch is managing everything that's running."
  - *Stale adopt (409/404):* "That session changed since the last scan" + auto-refresh; if the
    process died but the transcript survived, offer to resume it anyway.
  - *Discovery failure (can't read `/proc`):* "Couldn't scan running sessions. Retry." — not a blank
    panel that reads as "nothing to adopt."
- **Information architecture (#8):** collapse the panel to a quiet line when empty in steady state
  ("All running sessions are managed"); surface prominently only when candidates exist; keep
  adoptable rows separate from the collapsed "Not adoptable (N)" subsection; inline list + modal
  confirm.
- **Microcopy (#9):** panel title "Untracked sessions"; idle button "Manage this session"; active
  button "Stop & take over" (red); confirm title "Take over this session?"; primary "Stop & take
  over"; Cancel as focused/default; success toast links to the new session row.
- **Visible isolation exception (#10):** an adopted-in-place session shows a small badge in its row —
  **"running in original dir — no worktree"** — so a non-isolated session is never a later surprise.

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
- `claude_session_id` resolution picks most-recently-modified transcript for the cwd's project dir,
  and sets `last_activity` to that transcript's mtime (UX MUST-FIX #2).
- Dedup against `tracked_sids` sets `tracked=True`, `skip_reason="tracked"`.

Takeover (`adopt_session`) with a **fake process-killer + fake SDK**:
- Resume-in-place: cwd == original, **no worktree created**, `adopted_in_place=True`.
- `process.terminate` + `session.adopt` audit rows written.
- Dead-PID path: no termination attempted, resume still happens, outcome reports
  `terminatedPid=None` (UX MUST-FIX #3).
- Resume-failure path: outcome reports `resumed=False` (fresh session started), not a silent swap.

API:
- `GET /adoptable` shape + flags incl. `last_activity` / `pid_alive`; `401` without identity.
- `POST /adopt` happy path `201` with outcome (`resumed`, `terminatedPid`); `409` when no longer
  adoptable; `401` without identity.

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
