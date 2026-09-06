---
slug: transcript-replay-archive
title: Transcript, Replay & Archive
state: live
links:
  code:
    - server/sessions/replay.py
    - server/sessions/transcript_repair.py
    - server/sessions/archive_move.py
    - server/sessions/export.py
    - server/sessions/attachments.py
    - server/sessions/adopt.py
    - server/api/attachments.py
    - server/ipc/daemon.py
    - web/src/components/session/TranscriptView.tsx
    - web/src/session/events.ts
  prs: [482, 496, 787, 609, 305, 621, 223, 40]
---

# Transcript, Replay & Archive

**Problem** — A session's transcript is the record of everything it did, but the `claude` binary
owns the JSONL on disk and `ccswitch` may not patch it. That creates hard edges: when a user
navigates away and back, or the daemon restarts, the browser must rebuild a complete,
correctly-ordered transcript — without duplicating turns, blanking the pane, truncating a
completed turn, or amputating an in-progress one. Turns interrupted mid tool-use leave orphaned
`tool_use` blocks that dead-end every resumed turn. And finished sessions pile up: archiving used
to move worktrees on disk, which failed under disk/inode pressure, stale SSHFS handles, and
cross-filesystem copies, wedging the whole loop.

**Target users** — Anyone reopening or refreshing a session (especially on mobile, where
reconnects are frequent) who expects the full history to be intact and to keep streaming; operators
who archive/unarchive sessions in bulk to keep the dashboard clean and to reclaim disk; users who
export a session to Markdown or attach images to a turn.

**Desired outcome** — Returning to a session shows a complete, totally-ordered transcript that
continues live, healing gaps from the replay ring when it can and falling back to disk when it
can't; interrupted turns resume cleanly with orphaned tool-uses repaired. Archiving is a near-
instant DB status flip (files stay in place; disk is reclaimed only at a cap), unarchive restores
safely with byte+inode pre-checks and cross-device fallback, and auto-archive/auto-unarchive learn
from human reversals. Untracked live `claude` sessions can be discovered and adopted.

**Success metrics** — (unknown — needs owner input) for a headline target. In-code signals the
team tracks: replay complete and totally ordered across navigation with no duplicate/blank/tail-
only transcripts (#482, #507, #487, #122); heals catch-up from the ring instead of full replay
(#496); archiving is O(ms) with no disk move and cap-time reclaim (#787); unarchive detects
inode/space exhaustion before a cross-device copy and surfaces 507 (#621); no resumed turn
dead-ends on an orphaned tool_use (#223).

**Non-goals** — Not a rewrite of the `claude`-owned JSONL format — `ccswitch` reads it, dedupes
recovery-resend duplicates at read time, and appends only byte-compatible synthetic repairs; it
does not fork the binary. Archive is not a backup product: purge-at-cap permanently deletes the
oldest archived sessions once a configurable ceiling is exceeded.

**Notes**
- **Replay.** `server/sessions/replay.py` rebuilds transcript events from the JSONL, discovers
  sub-agent sidecar threads, collapses recovery-resend duplicate user turns, and drops an
  in-progress turn using a `turn_started_at` timestamp guard (so a completed-but-late-flushed turn
  is not amputated). The daemon (`server/ipc/daemon.py`) keeps a per-stream replay ring with
  monotonic `seq`; on attach the edge probes ring coverage (`ring_covers`/`ring_tail`), catches up
  from the ring when covered, and falls back to full disk replay on a seq gap or a daemon epoch
  change (#482, #496, #510, #410).
- **Repair.** `transcript_repair.py` heals trailing orphaned `tool_use` blocks left by mid-turn
  interrupts (appends a synthetic, byte-compatible `tool_result`) and truncates half-written JSONL
  tails from a hard kill, so resumes don't fail with `stop_reason=tool_use` (#223).
- **Archive / unarchive.** Archiving is a pure DB status flip (`status="archived"`,
  `archive_root=NULL`, files left at `cwd`) that frees a capacity slot immediately and cannot
  half-fail (#787). A configurable `max_archived_sessions` cap purges the oldest archived sessions
  in the background (#609). Unarchive is a status flip for new-style archives; legacy on-disk
  archives are restored via `archive_move.py` (atomic `os.rename`, then a guarded `copytree` on
  `EXDEV`) with free-byte + inode pre-checks so a restore never drives disk to zero (#305, #621,
  #624). Auto-archive is provenance-tracked (`autopilot`/`backlog-runner`/`completion`) and a manual
  unarchive pins the session opt-out so it is never auto-archived again and is fed a "why you're
  back" seed prompt (#655, #658, #469, #651). A global setting can disable auto-archival (#783);
  it gates only the archive step — a self-declared `complete` is still verified against the
  goal (open-question hold, completion bar, classifier salvage, corrective nudge) with the
  toggle off. A session's own `<!-- ccswitch:no-pr -->` on a repo-attached code goal is a claim,
  not a verdict: the bar refuses it until the PR-intent classifier confirms the goal genuinely
  needed no code change, otherwise the session is nudged to finish or ask the user.
- **Export & attachments.** `export.py` renders a session to downloadable Markdown (#180).
  `attachments.py` / `server/api/attachments.py` store per-turn file/image uploads content-addressed
  by sha256, dedup, reference-count deletion, sweep orphans, and enforce byte/count caps with
  magic-byte sniffing (#123, #842).
- **Adoption.** `adopt.py` discovers untracked live `claude` processes via `/proc`, classifies
  them, resolves their transcript and account, and can force-terminate to take over — gated behind
  `CCSWITCH_ENABLE_ADOPTION` (#40, #424, #143).

**Evidence** — Replay complete and totally ordered across navigation (#482); heals catch-up from
the ring instead of full replay (#496); archiving as a pure status flip + reclaim at cap (#787);
quick-purge shortcuts + max-archived auto-cap (#609); cross-device-safe archive/unarchive (#305)
and inode-exhaustion 507 (#621); heal orphaned-tool_use transcripts (#223); adopt untracked live
`claude` sessions (#40). Code: `server/sessions/replay.py`, `transcript_repair.py`,
`archive_move.py`, `export.py`, `attachments.py`, `adopt.py`; `server/api/attachments.py`;
`server/ipc/daemon.py`; `web/src/components/session/TranscriptView.tsx`, `web/src/session/events.ts`.
