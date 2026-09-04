"""Edge-side proxy that drives a SessionOrchestrator living in the session daemon.

Methods are async (they await RPC) even where the in-process orchestrator method
is sync; reconciling that with the sync API callers is an A2.2 concern. A2.1 wires
the vertical-slice methods only.
"""
import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ccswitch.oauth import Usage
from server.ipc.client import DaemonError, DaemonUnavailable
from server.sessions.facade import rpc_deadline
from server.sessions.orchestrator import ModelBlockedError
from server.sessions.selection import AllAccountsBusy

if TYPE_CHECKING:
    from server.github.pr_status import PipelinePr
    from server.pm.onboarding import ProjectOnboardingView
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY

log = logging.getLogger(__name__)

# Usage is served from the daemon's poller cache — a cheap read. Bound it tightly
# so a wedged/slow daemon degrades one account to "unknown" fast instead of tying
# up the edge for the 30s client default (incident 2026-07-02).
# Default only: GET /api/accounts passes its own USAGE_FETCH_TIMEOUT down through
# call_with_deadline, so there is exactly ONE deadline on that path rather than a
# nested pair that could fire in the wrong order.
USAGE_RPC_TIMEOUT = 3.0

# The opposite extreme: automation_consolidate / automation_trigger run the whole
# clone→model-pick→spawn pipeline INLINE inside the daemon RPC and legitimately
# take longer than the 30s client default (a healthy fetch + Haiku model pick +
# worktree create + claude start). Without headroom the edge times out and 503s a
# spawn the daemon actually completes — and a user retry then hits `duplicate`
# (issue #567). Bound it well above that worst case, which the origin-only fetch
# and the assess_model timeout already cap.
AUTOMATION_RPC_TIMEOUT = 90.0


class _WireEvent:
    """Wraps an already-serialized event dict so ws.py's e.to_wire() works.

    ``src_seq`` carries the daemon's per-stream source seq (from the DaemonClient's
    _SeqDict), which ws._forward_live stamps as the additive ``srcSeq`` loss
    telemetry (audit W2.3). ``None`` for a frame with no daemon seq (a GAP sentinel);
    local in-process events aren't _WireEvents at all, so their src_seq reads as None
    and the field is omitted."""
    def __init__(self, wire: dict, src_seq: int | None = None):
        self._wire = wire
        self.src_seq = src_seq

    def to_wire(self) -> dict:
        return self._wire


class RemoteDriverStream:
    """``open_stream()`` shim mirroring the local ``DriverStream`` shape
    (``turn_active`` / ``turn_started_at`` / ``events()`` / ``close()``) so
    ws.py drives both driver flavors through ONE interface — no hasattr
    branching. The flags come from the ensure_driver RPC snapshot; unlike the
    local stream there is no settle race to close on this side: the daemon
    anchors the subscription at ``resume_since`` (just before the open turn's
    user message), so the ring replay IS the snapshot."""

    def __init__(self, driver: "RemoteDriver") -> None:
        self._driver = driver
        self.turn_active = driver.turn_active
        self.turn_started_at = driver.turn_started_at

    def events(self):
        return self._driver.events()

    def close(self) -> None:
        # Best-effort no-op: the daemon subscription only starts when events()
        # is first iterated, and async-generator teardown closes it — there is
        # nothing to release before that point.
        return None


class RemoteDriver:
    """Driver-shaped shim so ws.py can consume a remote session like a local one.

    ws.py needs ``.open_stream()`` (returning a stream with ``turn_active`` /
    ``turn_started_at`` / ``events()`` / ``close()``), ``.current_status()``
    (sync, returns str), and ``.queued()`` (sync, returns list).
    """
    def __init__(self, client, session_id: str, status="idle", queued=None, head_seq=None,
                 resume_since=None, turn_active=False,
                 turn_started_at: float | None = None):
        self._client = client
        self._session_id = session_id
        self._status = status
        self._queued = queued or []
        self._head_seq = head_seq
        # Where to resume the daemon topic: while a turn is open this is just before its
        # user message, so the whole in-progress turn replays from the ring; else from-now.
        self._resume_since = resume_since if resume_since is not None else head_seq
        self.turn_active = bool(turn_active)
        # Epoch seconds of the open turn's start (None when idle), from the daemon's
        # ensure_driver snapshot. ws.py hands it to replay_window so the incomplete-
        # turn drop only trims the turn that actually IS in progress — mirrors the
        # local SessionDriver.turn_started_at property.
        self.turn_started_at = turn_started_at
        self._bg_tasks: set[asyncio.Task] = set()
        # Wave-3 cursor catch-up (audit W2.3), FINDING-1 atomic commitment. When
        # ``_catch_up_snapshot`` is pinned (via join_live + pin_catch_up, driven by ws.py
        # BEFORE resumeOk is enqueued), events() replays that already-fetched ring_tail
        # snapshot then continues on the shared live fan-out joined at ``_live``, deduped by
        # seq (src <= head). The snapshot is fetched and pinned while the fan-out is already
        # joined, so a not-covered result takes the full-replay path and resumeOk is only
        # ever sent with the snapshot in hand — no silent-gap window between probe and fetch.
        # ``_live`` is the fan-out iterator joined synchronously by join_live(); events()
        # reuses it on BOTH paths (catch-up replay, or a not-covered full-replay forward).
        # None everywhere ⇒ ordinary non-catch-up attach: events() subscribes for itself.
        self._live = None
        self._catch_up_snapshot: list[dict] | None = None
        self._catch_up_head: int | None = None

    def current_status(self) -> str:
        return self._status

    def queued(self) -> list:
        return self._queued

    def queue_wire(self) -> list[dict]:
        """Structured queue snapshot, mirroring local SessionDriver.queue_wire so
        ws.py can re-emit queued-prompt chips on reattach in remote mode too.

        Without this, ws.py's `driver.queue_wire()` raised AttributeError on every
        remote attach (caught, but it meant queued prompts were invisible — a prompt
        typed while a turn was busy vanished with no feedback). The remote
        ensure_driver snapshot carries queued prompt *text* only (driver.queued()),
        not image refs, so images are omitted here; the live QueueUpdate events that
        stream over .events() carry the full wire shape with images."""
        return [{"text": t, "images": []} for t in self._queued]

    def seed_idle_suggestions(self, user_text: str, assistant_text: str) -> None:
        """Seed idle-session suggestions on the daemon-side driver (best-effort).

        Mirrors the local Driver.seed_idle_suggestions sync, fire-and-forget shape
        so the WS attach path (suggestions.kick_idle_suggestions) drives a remote
        session identically to a local one — without this, daemon mode raises
        AttributeError and idle sessions never get seeded suggestions. Schedules
        the RPC and returns immediately; any failure is logged, never raised.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop to schedule on — skip (best-effort)
        task = loop.create_task(self._client.call(
            "seed_idle_suggestions",
            session_id=self._session_id,
            user_text=user_text,
            assistant_text=assistant_text,
        ))
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.warning("RemoteDriver.seed_idle_suggestions RPC failed for %s: %s",
                            self._session_id, t.exception())

        task.add_done_callback(_done)

    def join_live(self) -> None:
        """Join the shared live fan-out synchronously (FINDING-1 atomic commitment step 1).

        From this instant every newly-published event is captured in our subscriber queue,
        so anything the ring_tail snapshot fetched NEXT does not cover (seq > its head) is
        guaranteed to already be queued here. ws.py calls this BEFORE fetching the snapshot,
        which is BEFORE resumeOk — the ordering that makes the catch-up gapless. Idempotent:
        a second call is a no-op. Per-attach and safe: ensure_driver mints a fresh
        RemoteDriver every attach, so a joined fan-out never leaks across attaches."""
        if self._live is None:
            self._live = self._client.subscribe(self._session_id, since=self._resume_since)

    def pin_catch_up(self, snapshot: list[dict], head: int) -> None:
        """Pin the ring_tail snapshot (FINDING-1 atomic commitment step 2).

        ws.py fetches the one-shot ring_tail snapshot AFTER join_live and, only if it comes
        back covered, pins it here and THEN enqueues resumeOk — so resumeOk is never sent
        without the snapshot already in hand (closes the silent-gap window between the
        ring_covers probe and the snapshot fetch). events() replays this snapshot then
        continues on the already-joined fan-out, deduping the overlap by daemon seq
        (src <= head).

        Raises RuntimeError when join_live() has not run first: a snapshot fetched
        without a joined fan-out has a silent-gap window (events between its head and
        the eventual join are captured by NOBODY), and joining late here cannot close
        it. Both callers (ws._maybe_catch_up, prepare_open_turn_resupply) satisfy the
        order today; this makes the invariant fail-fast under refactor instead of
        comment-enforced. The raise fires BEFORE the caller enqueues resumeOk, so an
        ordering regression fails the attach loudly (→ full replay on retry) rather
        than committing a catch-up the client would render with a hole in it."""
        if self._live is None:
            raise RuntimeError(
                "pin_catch_up before join_live (FINDING-1 ordering violated): "
                "refusing to commit a catch-up snapshot that may have a silent gap")
        self._catch_up_snapshot = snapshot
        self._catch_up_head = head

    async def prepare_open_turn_resupply(self) -> bool:
        """Pin a one-shot ring snapshot of the OPEN turn so events() re-supplies it to
        this attach. Returns False when the ring no longer covers the turn anchor (or
        the fetch failed) — the caller must then KEEP the turn in the disk replay.

        Why this exists (incident 2026-07-07, session fced5538): the shared DaemonClient
        keeps ONE daemon-side subscription per stream — only the FIRST local subscriber
        triggers the daemon's replay-on-subscribe, and nothing ever unsubscribes. So a
        RE-attach's ``subscribe(since=resume_since)`` is silently ignored: the new queue
        joins the fan-out at "now" and the open turn the disk replay just dropped
        (``drop_incomplete_turn``) is re-supplied by NOBODY — the view resets to only
        post-attach events until the turn settles to disk. Exactly the trap the
        ``ring_tail`` docstring (server/ipc/daemon.py) warns about for the catch-up
        path; this applies its snapshot-then-dedup answer to the ordinary live attach.

        Same FINDING-1 atomic ordering as the wave-3 catch-up: join the fan-out FIRST
        (everything published after the snapshot head is then already queued), fetch
        ``ring_tail`` at the turn anchor, and pin only a covered snapshot. Coverage is
        re-verified AT FETCH TIME by the daemon, so an eviction between the
        ensure_driver snapshot and this call degrades safely to False."""
        self.join_live()
        try:
            snap = await self._client.call(
                "ring_tail", session_id=self._session_id, since=self._resume_since)
        except Exception:
            log.warning("RemoteDriver.prepare_open_turn_resupply: ring_tail failed for %s",
                        self._session_id, exc_info=True)
            return False
        if not snap.get("covered"):
            return False
        self.pin_catch_up(snap["events"], snap["head"])
        return True

    def open_stream(self) -> RemoteDriverStream:
        """Uniform attach interface with the local SessionDriver.open_stream()."""
        return RemoteDriverStream(self)

    async def events(self):
        # Reuse the fan-out already joined at commit time (join_live) when a catch-up was
        # prepared — that SAME subscription captured everything published after the snapshot
        # head, so nothing is lost. A not-covered catch-up also left a joined fan-out (used
        # here for the full-replay live forward). An ordinary attach never joined, so join
        # now, anchored at resume_since.
        live = self._live if self._live is not None else self._client.subscribe(
            self._session_id, since=self._resume_since)
        if self._catch_up_snapshot is None:
            async for wire in live:
                # The DaemonClient delivers a _SeqDict carrying the daemon source seq on
                # .src_seq; a plain dict (or GAP sentinel) reads as None. Surface it so
                # ws._forward_live can stamp srcSeq telemetry.
                yield _WireEvent(wire, getattr(wire, "src_seq", None))
            return

        # Cursor catch-up: replay the pinned ring_tail snapshot (fetched by ws.py while the
        # fan-out was already joined and BEFORE resumeOk), then continue live. Because the
        # snapshot supplies cursor+1..head and the fan-out captured everything > head, the
        # join has no gap.
        head = self._catch_up_head
        for item in self._catch_up_snapshot:
            yield _WireEvent(item["event"], item["seq"])
        # Continue live, dropping the overlap already delivered by the snapshot (seq<=head).
        # A GAP sentinel (src_seq None) is never an overlap — always forward it.
        async for wire in live:
            src = getattr(wire, "src_seq", None)
            if src is not None and src <= head:
                continue
            yield _WireEvent(wire, src)


class RemoteOrchestrator:
    def __init__(self, client):
        self._client = client
        self._global_subs: set[asyncio.Queue] = set()
        self._global_pump: asyncio.Task | None = None

    async def list_sessions(self) -> list[SessionRecord]:
        r = await self._client.call("list_sessions")
        return [SessionRecord.from_dict(d) for d in r["sessions"]]

    async def pr_for(self, repo, branch) -> "PullRequestInfo | None":
        """Resolve a session's PR via the daemon's poller cache (None if absent)."""
        if not repo or not branch:
            return None
        r = await self._client.call("pr_for", repo=repo, branch=branch)
        info = r.get("pr")
        return PullRequestInfo.from_dict(info) if info else None

    @rpc_deadline
    async def prs_for(self, session_id: str, *, timeout: float | None = None) -> "list[PullRequestInfo]":
        """Resolve a session's full PR list via the daemon (empty if none).

        ``timeout`` is the caller's deadline pushed INTO the RPC (see
        server.sessions.facade.call_with_deadline) — never race this from outside."""
        r = await self._client.call("prs_for", session_id=session_id, timeout=timeout)
        return [PullRequestInfo.from_dict(d) for d in (r.get("prs") or [])]

    @rpc_deadline
    async def prs_for_many(self, session_ids: list[str], *,
                           timeout: float | None = None) -> "dict[str, list[PullRequestInfo]]":
        """Batched :meth:`prs_for`. One RPC for the whole page instead of one per session.

        ``timeout`` is the caller's deadline pushed INTO the RPC — never race this from
        outside (see server.sessions.facade.call_with_deadline)."""
        r = await self._client.call("prs_for_many", session_ids=list(session_ids),
                                    timeout=timeout)
        by_sid = (r or {}).get("prs") if isinstance(r, dict) else None
        return {sid: [PullRequestInfo.from_dict(d) for d in (v or [])]
                for sid, v in (by_sid or {}).items()}

    async def all_prs(self) -> "list[PipelinePr]":
        """Resolve the full PR population (joined to sessions) via the daemon."""
        from server.github.pr_status import PipelinePr
        r = await self._client.call("all_prs")
        return [PipelinePr.from_dict(d) for d in (r.get("prs") or [])]

    async def all_repos_stage_models(self) -> "dict":
        """Resolve per-repo StageModels from the daemon."""
        from server.pipeline.track import StageModel
        r = await self._client.call("all_repos_stage_models")
        return {k: StageModel.from_dict(v) for k, v in (r.get("models") or {}).items()}

    async def all_merge_trains(self) -> "dict":
        """Resolve per-repo MergeTrainState (the merge-train widget) from the daemon."""
        from server.pipeline.merge_train import MergeTrainState
        r = await self._client.call("all_merge_trains")
        return {k: MergeTrainState.from_dict(v) for k, v in (r.get("trains") or {}).items()}

    async def latest_queue_entries(self) -> "dict":
        """Resolve the newest merge-queue observation per repo from the daemon.

        QueueEntry is a flat frozen dataclass with no to_dict/from_dict, so the RPC
        wires each entry as a plain field dict (dataclasses.asdict) and we rebuild it.
        """
        from server.github.merge_queue import QueueEntry
        r = await self._client.call("latest_queue_entries")
        return {
            repo: [QueueEntry(**e) for e in entries]
            for repo, entries in (r.get("queues") or {}).items()
        }

    async def check_insights(self, repo: str, window: str) -> dict:
        """Resolve per-repo Check Health insights from the daemon (already JSON-safe)."""
        return await self._client.call("check_insights", repo=repo, window=window)

    async def ready_merge(self, repo: str, number: int) -> dict:
        """Arm ready + auto-merge on a PR via the daemon (already JSON-safe)."""
        return await self._client.call("ready_merge", repo=repo, number=number)

    async def deploy_config_list(self) -> dict:
        """List all deploy-env configs via the daemon."""
        r = await self._client.call("deploy_config_list")
        return r.get("configs") or {}

    async def deploy_config_put(
        self, repo: str, environments: list[dict], enabled: bool = True
    ) -> None:
        """Upsert one repo's deploy-env config via the daemon."""
        await self._client.call(
            "deploy_config_put", repo=repo, environments=environments, enabled=enabled
        )

    async def deploy_status(self) -> dict:
        """Watcher's per-repo env health cache via the daemon."""
        r = await self._client.call("deploy_status")
        return r.get("status") or {}

    async def git_credential_alerts(self) -> dict:
        """Stale git-credential alerts (#670) via the daemon — the process that
        runs ``ensure_clone`` and owns the registry."""
        return await self._client.call("git_credential_alerts")

    async def dismiss_git_credential_alert(self, repo: str) -> dict:
        """Clear one repo's stale-credential alert (#670) via the daemon."""
        return await self._client.call("dismiss_git_credential_alert", repo=repo)

    async def list_adoptable_candidates(self) -> list[dict]:
        """Adoption candidates via the daemon — /proc discovery runs on the daemon
        host, not this edge process (#709 finding 4)."""
        r = await self._client.call("list_adoptable_candidates")
        return r if isinstance(r, list) else []

    async def migrate_archive_home(
        self, old_home: str | None, new_home: str | None, archived_ids: list[str],
    ) -> dict:
        """Move archived session dirs on the daemon's filesystem (#709 finding 5)."""
        return await self._client.call(
            "migrate_archive_home", old_home=old_home, new_home=new_home,
            archived_ids=archived_ids,
        )

    async def cloned_repos(self, names: list[str]) -> list[str]:
        """Which repos have a clone present on the daemon host (#709 finding 6)."""
        r = await self._client.call("cloned_repos", names=names)
        return r if isinstance(r, list) else []

    async def request_pr_fix(self, session_id: str, pr_number: int) -> str:
        """Inject the branch-aware fix prompt for PR #pr_number via the daemon.

        Proxies the local orchestrator's request_pr_fix so the manual 'Fix PR #N'
        endpoint works in daemon mode. Returns the status string ("sent" /
        "no_session" / "no_pr" / "not_actionable").
        """
        return await self._client.call("request_pr_fix", session_id=session_id, pr_number=pr_number)

    async def automation_statuses(self) -> "dict[str, dict]":
        """Rendered live status per automation id (badge text/level), via the daemon."""
        return await self._client.call("automation_statuses")

    async def resume_recovery_status(self) -> "dict":
        """Live deploy-resume failure set ({failed, gaveUp}) for the banner, via the
        daemon — the single source of truth for which sessions are still not live."""
        return await self._client.call("resume_recovery_status")

    async def startup_report(self) -> "dict":
        """The daemon's boot outcomes (rehydrate/rearm counts + step errors) for
        /api/daemon/health — a failed rehydrate must be observable from the edge."""
        r = await self._client.call("startup_report")
        return r if isinstance(r, dict) else {}

    async def automation_actionable(self, automation_id: str) -> "list[dict]":
        """Actionable items for *automation_id*, via the daemon."""
        r = await self._client.call("automation_actionable", automation_id=automation_id)
        return r.get("items") or []

    async def automation_trigger(self, automation_id: str, item_id: str, kind: str) -> dict:
        """Trigger a manual action for an item on *automation_id*, via the daemon."""
        return await self._client.call("automation_trigger", automation_id=automation_id,
                                       item_id=item_id, kind=kind,
                                       timeout=AUTOMATION_RPC_TIMEOUT)

    async def pr_doctor_job(self, job_id: str) -> dict | None:
        """Current state of an async pr-doctor trigger job, via the daemon (issue
        #668). A quick read — the default RPC timeout is fine (unlike the trigger,
        which needed AUTOMATION_RPC_TIMEOUT while it blocked on clone+spawn)."""
        return await self._client.call("pr_doctor_job", job_id=job_id)

    async def consolidation_candidates(self, repo: str | None = None) -> dict:
        """Open PRs + suggested consolidation clusters, via the daemon."""
        return await self._client.call("consolidation_candidates", repo=repo)

    async def automation_consolidate(self, repo: str, numbers: list[int]) -> dict:
        """Spawn a consolidation session for the selected PRs, via the daemon."""
        return await self._client.call("automation_consolidate", repo=repo, numbers=numbers,
                                       timeout=AUTOMATION_RPC_TIMEOUT)

    async def backlog_candidates(self) -> "list[dict]":
        """Current eligible issue candidates for manual dispatch, via the daemon."""
        r = await self._client.call("backlog_candidates")
        return r if isinstance(r, list) else []

    async def backlog_runner_dispatch(self, refs: "list[str]") -> dict:
        """Manually claim + spawn a batch for *refs*, via the daemon."""
        return await self._client.call("backlog_runner_dispatch", refs=refs,
                                       timeout=AUTOMATION_RPC_TIMEOUT)

    async def get_session(self, session_id: str) -> SessionRecord | None:
        r = await self._client.call("get_session", session_id=session_id)
        return SessionRecord.from_dict(r["session"]) if r["session"] is not None else None

    async def create_session(self, **kwargs) -> SessionRecord:
        # Faithfully reconstruct the in-process error contract across the RPC
        # boundary: the edge's create handler renders a clear "all accounts at
        # limit, resets <date>" message only if it catches AllAccountsBusy by
        # type. Without this re-raise it sees a generic DaemonError → opaque 500 →
        # "Something went wrong." (mirrors run_command's KeyError re-raise.)
        #
        # gitrepos.CloneError / GhError get the same treatment: the daemon now
        # owns repo→clone resolution (ensure_clone), so a clone failure raised
        # there must still reach the edge's existing
        # ``except (gitrepos.CloneError, gitrepos.GhError)`` → HTTP 502
        # clone_failed handling, not degrade to a generic 500. Unlike
        # AllAccountsBusy these carry no structured ``error_data`` payload —
        # the message alone is the whole contract — so reconstruction only
        # needs the type name.
        try:
            r = await self._client.call("create_session", **kwargs)
        except DaemonError as exc:
            if exc.err_type == "AllAccountsBusy":
                data = exc.data or {}
                reset = data.get("earliest_reset")
                earliest = datetime.fromisoformat(reset) if reset else None
                raise AllAccountsBusy(earliest, data.get("kind", "at_ceiling")) from exc
            if exc.err_type in ("CloneError", "GhError"):
                from ccswitch import gitrepos
                exc_cls = gitrepos.CloneError if exc.err_type == "CloneError" else gitrepos.GhError
                raise exc_cls(exc.message) from exc
            raise
        return SessionRecord.from_dict(r["session"])

    async def send_prompt(self, session_id: str, text: str, images=None, *,
                          interrupt: bool = False) -> str | None:
        r = await self._client.call("send_prompt", session_id=session_id, text=text,
                                    images=images, interrupt=interrupt)
        return (r or {}).get("status")

    def daemon_sha(self) -> str | None:
        """The daemon's running git sha, or None before first /info contact.

        Sync cached-field read (no RPC), mirroring daemon_epoch. /healthz reports it
        so the deploy gate can reject an edge/daemon version skew."""
        return self._client.sha

    def daemon_pool_stats(self) -> dict | None:
        """The RPC connection pool's occupancy, or None if the client cannot report it.

        Sync cached-field read (no RPC), mirroring daemon_sha/daemon_epoch — deliberately,
        because the condition it reports is precisely the one in which an RPC cannot
        complete. ``/api/daemon/health`` renders it so a wedged pool is visible in the
        journal and the dashboard instead of needing `ss -tn state close-wait` on the box
        (issue #734)."""
        stats = getattr(self._client, "pool_stats", None)
        return stats() if stats is not None else None

    def daemon_epoch(self) -> str | None:
        """The daemon's current process epoch, or ``None`` if the edge hasn't reached
        ``/info`` yet. Sync (a cached-field read via DaemonClient.epoch — no RPC). ws.py's
        cursor catch-up reads it to reject a stale cross-epoch ``sinceEpoch`` after a daemon
        restart (#497). The epoch gate is deliberately an EDGE concern: the edge already
        tracks the daemon epoch cheaply here, so it is NOT threaded through the
        ring_covers/ring_tail RPCs."""
        return self._client.epoch

    async def ring_covers(self, session_id: str, since: int | None) -> tuple[bool, str | None]:
        """Ask the daemon whether its replay ring can resume ``session_id`` from ``since``
        (the browser's contiguous srcSeq cursor) with no GAP — the wave-3 cursor-catchup
        coverage probe (audit W2.3). The edge decides resumeOk vs full replay off this
        BEFORE enqueuing anything, so a not-covered cursor never half-sends resumeOk. A
        daemon/RPC error surfaces to the caller, which treats it as not-covered.

        Returns ``(covered, epoch)``: the daemon's CURRENT process epoch rides the same
        response so the edge's epoch gate is authoritative at gate time (#497 stale-epoch RPC
        race). The cached ``daemon_epoch()`` can lag a restart (events-reader in backoff);
        this epoch is what the daemon reports RIGHT NOW over the RPC ``_http`` channel, so a
        cursor minted under the old epoch is rejected even before the reader has reconnected.
        ``epoch`` is None only against an old daemon that predates the field."""
        r = await self._client.call("ring_covers", session_id=session_id, since=since)
        return bool(r.get("covered")), r.get("epoch")

    async def ring_tail(self, session_id: str, since: int | None) -> dict:
        """One-shot ring snapshot after ``since`` (the wave-3 catch-up tail): the commit-time
        coverage re-check. ws.py's attach handler fetches this AFTER driver.join_live() and
        BEFORE resumeOk — a covered result pins the snapshot (driver.pin_catch_up) and lets
        resumeOk go out; a not-covered result (ring evicted between the ring_covers probe and
        here) falls back to the full disk replay with no resumeOk (FINDING-1 atomicity).
        Returns ``{covered, events:[{seq,event}], head}``; the driver replays ``events`` then
        continues live, deduping the overlap by ``head`` (see daemon.ring_tail)."""
        return await self._client.call("ring_tail", session_id=session_id, since=since)

    @rpc_deadline
    async def ensure_driver(self, session_id: str, identity: str = SYSTEM_IDENTITY,
                            *, timeout: float | None = None) -> "RemoteDriver | None":
        """Resume (or fetch) a session's driver in the daemon.

        ``timeout`` is the caller's deadline pushed INTO the RPC; expiry raises
        DaemonTimeout, which ws.py maps to SESSION_RESUME_TIMEOUT."""
        r = await self._client.call("ensure_driver", session_id=session_id, identity=identity,
                                    timeout=timeout)
        if not r["found"]:
            return None
        return RemoteDriver(self._client, session_id, status=r["status"], queued=r["queued"],
                            head_seq=r.get("head_seq"), resume_since=r.get("resume_since"),
                            turn_active=r.get("turn_active", False),
                            turn_started_at=r.get("turn_started_at"))

    async def archive_session(self, session_id):
        return await self._client.call("archive_session", session_id=session_id)

    async def resume_interrupted(self, session_id: str, identity: str = SYSTEM_IDENTITY) -> str:
        """Proxy the owning daemon's resume_interrupted (re-attach + nudge a
        restart-interrupted session). Returns "resumed" / "skipped:<reason>" / "no_driver"."""
        return await self._client.call(
            "resume_interrupted", session_id=session_id, identity=identity)

    async def permanent_delete(self, session_id):
        return await self._client.call("permanent_delete", session_id=session_id)

    async def rename_session(self, session_id, name):
        return await self._client.call("rename_session", session_id=session_id, name=name)

    async def note_user_interaction(self, session_id):
        return await self._client.call("note_user_interaction", session_id=session_id)

    async def set_autopilot_paused(self, session_id, paused):
        return await self._client.call("set_autopilot_paused", session_id=session_id, paused=paused)

    async def session_queue_move(self, session_id: str, op: str, position: int | None = None) -> bool:
        r = await self._client.call("session_queue_move", session_id=session_id, op=op, position=position)
        return r

    async def session_queue_cancel(self, session_id: str) -> bool:
        r = await self._client.call("session_queue_cancel", session_id=session_id)
        return r

    async def session_queue_positions(self) -> dict[str, int]:
        r = await self._client.call("session_queue_positions")
        return r if isinstance(r, dict) else {}

    async def maybe_start_queued(self) -> int:
        r = await self._client.call("maybe_start_queued")
        return r if isinstance(r, int) else 0

    async def interrupt(self, session_id):
        return await self._client.call("interrupt", session_id=session_id)

    async def set_permission_mode(self, session_id, mode):
        return await self._client.call("set_permission_mode", session_id=session_id, mode=mode)

    async def cancel_queued(self, session_id, index):
        return await self._client.call("cancel_queued", session_id=session_id, index=index)

    async def resolve_permission(self, session_id, perm_id, decision, identity):
        return await self._client.call("resolve_permission", session_id=session_id, perm_id=perm_id,
                                        decision=decision, identity=identity)

    async def answer_question(self, session_id, question_id, selected, identity, mode="option"):
        return await self._client.call("answer_question", session_id=session_id, question_id=question_id,
                                        selected=selected, identity=identity, mode=mode)

    async def transcript_path(self, session_id):
        return await self._client.call("transcript_path", session_id=session_id)

    async def pending_questions(self, session_id):
        return await self._client.call("pending_questions", session_id=session_id)

    async def orphaned_interaction(self, session_id):
        return await self._client.call("orphaned_interaction", session_id=session_id)

    async def pop_pending_initial_prompt(self, session_id):
        return await self._client.call("pop_pending_initial_prompt", session_id=session_id)

    async def pop_pending_initial_attachment_ids(self, session_id):
        return await self._client.call("pop_pending_initial_attachment_ids", session_id=session_id)

    async def command_catalog(self, session_id):
        return await self._client.call("command_catalog", session_id=session_id)

    async def run_command(self, session_id, name, args, identity=SYSTEM_IDENTITY):
        """Proxy run_command, faithfully reconstituting the in-process error contract.

        The daemon-side orchestrator.run_command raises ``KeyError(name)`` for an
        unknown command, and ws.py distinguishes that ("unknown command" chip) from a
        generic failure ("command failed") solely by the exception *type*. But an
        exception's type doesn't survive the RPC boundary — it collapses to a
        DaemonError whose ``err_type`` carries the original class name. Re-raise a
        KeyError so the remote path renders identically to the local one; any other
        DaemonError propagates unchanged (still the generic-failure branch)."""
        try:
            return await self._client.call("run_command", session_id=session_id, name=name, args=args,
                                           identity=identity)
        except DaemonError as exc:
            if exc.err_type == "KeyError":
                raise KeyError(exc.message) from exc
            raise

    async def resolve_model_approval(self, session_id: str, decision: str, *,
                                     model: str | None = None,
                                     identity: str) -> "SessionRecord":
        """Proxy resolve_model_approval, reconstituting the in-process error contract.

        The orchestrator raises ``KeyError`` (unknown session), ``ValueError``
        (nothing pending / bad decision), or ``ModelBlockedError`` (the counter-offered
        model is blocked by policy). All three collapse to a DaemonError at the RPC
        boundary; re-raise the original types so the API endpoint sees the same
        exceptions on both code paths."""
        try:
            r = await self._client.call("resolve_model_approval", session_id=session_id,
                                        decision=decision, model=model, identity=identity)
        except DaemonError as exc:
            if exc.err_type == "KeyError":
                raise KeyError(exc.message) from exc
            if exc.err_type == "ValueError":
                raise ValueError(exc.message) from exc
            if exc.err_type == "ModelBlockedError":
                raise ModelBlockedError(exc.message) from exc
            raise
        return SessionRecord.from_dict(r["session"])

    @rpc_deadline
    async def latest_suggestions(self, session_id: str, *,
                                 timeout: float | None = None) -> list[str]:
        r = await self._client.call("latest_suggestions", session_id=session_id,
                                    timeout=timeout)
        return r if isinstance(r, list) else []

    async def generate_suggestions_now(self, session_id: str) -> list[str]:
        r = await self._client.call("generate_suggestions_now", session_id=session_id)
        return r if isinstance(r, list) else []

    @rpc_deadline
    async def latest_summary(self, session_id: str, *, viewing: bool = False,
                             timeout: float | None = None) -> str:
        r = await self._client.call("latest_summary", session_id=session_id, viewing=viewing,
                                    timeout=timeout)
        return r if isinstance(r, str) else ""

    @rpc_deadline
    async def latest_suggestions_many(self, session_ids: list[str], *,
                                      timeout: float | None = None) -> dict[str, list[str]]:
        """Batched :meth:`latest_suggestions` (deadline pushed in, never raced)."""
        r = await self._client.call("latest_suggestions_many",
                                    session_ids=list(session_ids), timeout=timeout)
        return r if isinstance(r, dict) else {}

    @rpc_deadline
    async def latest_summary_many(self, session_ids: list[str], *, viewing: bool = False,
                                  timeout: float | None = None) -> dict[str, str]:
        """Batched :meth:`latest_summary` (deadline pushed in, never raced)."""
        r = await self._client.call("latest_summary_many", session_ids=list(session_ids),
                                    viewing=viewing, timeout=timeout)
        return r if isinstance(r, dict) else {}

    async def list_archived(self):
        return await self._client.call("list_archived")

    async def unarchive(self, session_id):
        r = await self._client.call("unarchive", session_id=session_id)
        return SessionRecord.from_dict(r["session"]) if r["session"] is not None else None

    async def adopt_session(self, claude_session_id, identity):
        r = await self._client.call("adopt_session", claude_session_id=claude_session_id, identity=identity)
        return SessionRecord.from_dict(r["record"]), r["metadata"]

    async def transcript_events(self, session_id, *, max_turns: int | None = 20):
        r = await self._client.call("transcript_events", session_id=session_id,
                                    max_turns=max_turns)
        return r["events"]

    async def resolve_transcript_path(self, session_id):
        """Resolve a transcript path via the daemon — unlike transcript_path (in-memory
        driver state only), this includes the DB-glob fallback, so archived sessions
        resolve too. Search and transcript replay both depend on that."""
        return await self._client.call("resolve_transcript_path", session_id=session_id)

    async def transcript_window(self, session_id, *, before=None, limit=None, timeout_s=None):
        """Windowed transcript replay via the daemon's transcript_window RPC.

        The daemon resolves the path AND replays it in-process, sharing the
        response shape (events / earliestTurn / totalTurns) that the transcript_window
        RPC handler builds — the edge never reads the transcript file itself, so this
        is correct even when the daemon lives on a different host than the edge.
        limit=None → full transcript. Returns None when no transcript is found.

        ``timeout_s`` (the export path's larger replay ceiling) is forwarded to the
        daemon ONLY when set, so the RPC shape is unchanged for every other caller.
        The client-side RPC deadline is widened past it — otherwise the edge would
        give up before the daemon's own ceiling ever fires.
        """
        from server.sessions.replay import ReplayWindow
        extra = {} if timeout_s is None else {"timeout_s": timeout_s,
                                              "timeout": timeout_s + 10.0}
        r = await self._client.call("transcript_window", session_id=session_id,
                                    before=before, limit=limit, **extra)
        if r is None:
            return None
        return ReplayWindow(events=r["events"], earliest_turn=r["earliestTurn"],
                            total_turns=r["totalTurns"])

    async def transcript_search_text(self, session_id: str) -> str:
        """Return transcript text for search matching, via the daemon RPC — the
        edge never reads the transcript file itself (cross-host safe)."""
        r = await self._client.call("transcript_search_text", session_id=session_id)
        return r if isinstance(r, str) else ""

    @rpc_deadline
    async def usage(self, name, *, timeout: float | None = None):
        try:
            r = await self._client.call("usage", name=name,
                                        timeout=USAGE_RPC_TIMEOUT if timeout is None else timeout)
        except DaemonUnavailable:
            # Wedged/slow daemon → treat as unknown usage rather than 500 the caller.
            log.warning("usage RPC for %s unavailable — returning None", name)
            return None
        return Usage.from_dict(r["usage"]) if r["usage"] is not None else None

    async def pending_permissions(self, session_id):
        return await self._client.call("pending_permissions", session_id=session_id)

    async def active_session_count(self):
        return await self._client.call("active_session_count")

    async def live_session_load(self):
        return await self._client.call("live_session_load")

    async def live_session_counts(self):
        return await self._client.call("live_session_counts")

    async def copilot_add_start(self, account: str) -> dict:
        """Start the GitHub device-flow login for a copilot account, via the daemon."""
        return await self._client.call("copilot_add_start", account=account)

    async def copilot_add_status(self, account: str) -> dict:
        """Return the current device-flow auth state for a copilot account, via the daemon."""
        return await self._client.call("copilot_add_status", account=account)

    async def copilot_add_cancel(self, account: str) -> dict:
        """Cancel an in-flight device-flow login for a copilot account, via the daemon."""
        return await self._client.call("copilot_add_cancel", account=account)

    async def copilot_usage(self, account: str) -> dict | None:
        """Return the proxy /usage payload for a copilot account, via the daemon."""
        return await self._client.call("copilot_usage", account=account)

    async def subscribe_events(self) -> asyncio.Queue:
        """Return a queue that receives global lifecycle events.

        A single daemon subscription is shared; each caller gets their own
        unbounded queue and events are fanned out to all active subscribers.
        Call unsubscribe_events(q) to stop receiving events on a queue.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._global_subs.add(q)
        if self._global_pump is None or self._global_pump.done():
            async def _pump():
                async for ev in self._client.subscribe("_global"):
                    for sq in list(self._global_subs):
                        sq.put_nowait(ev)
            self._global_pump = asyncio.create_task(_pump(), name="global-pump")
        return q

    async def unsubscribe_events(self, q: asyncio.Queue) -> None:
        """Remove q from the fan-out set; the shared pump continues for remaining subscribers."""
        self._global_subs.discard(q)

    def subscribe(self, session_id: str):
        return self._client.subscribe(session_id)

    async def x11_metrics(self) -> dict:
        return await self._client.call("x11_metrics")

    async def x11_open_browser(self, session_id: str, url: str | None = None) -> dict:
        return await self._client.call("x11_open_browser", session_id=session_id, url=url)

    # --- pm onboarding seams (phase 2, 2026-08-31) ---
    async def pm_onboarding_counts(self, project_id: str) -> "ProjectOnboardingView":
        """Build the onboarding engine's view for a project, via the daemon.

        The daemon returns a flat dict matching ``ProjectOnboardingView``'s fields;
        reconstruct the dataclass here so all callers (API routes, tests) receive the
        same type regardless of whether the orchestrator is in-process or remote."""
        from server.pm.onboarding import ProjectOnboardingView
        result = await self._client.call("pm_onboarding_counts", project_id=project_id)
        if result is None:
            # Daemon error: return a safe zero-state view rather than raising an
            # AttributeError later — consistent with other remote seams that return
            # empty collections on None (e.g. backlog_candidates, automation_statuses).
            return ProjectOnboardingView(
                state="registered",
                repo_count=0,
                backfill_done_count=0,
                feature_count=0,
                open_unclassified_count=0,
                pending_proposal_count=0,
                map_pr_open=False,
                map_pr_merged=False,
            )
        return ProjectOnboardingView(**result)

    async def pm_onboard_spawn_map(self, slug: str, **kwargs) -> dict | None:
        """Spawn the archaeology map-synthesis session, via the daemon."""
        return await self._client.call("pm_onboard_spawn_map", slug=slug, **kwargs,
                                       timeout=AUTOMATION_RPC_TIMEOUT)

    async def pm_classify_run(self, slug: str, **kwargs) -> int:
        """Spawn one backlog-classification session, via the daemon."""
        r = await self._client.call("pm_classify_run", slug=slug, **kwargs,
                                    timeout=AUTOMATION_RPC_TIMEOUT)
        return int(r) if r is not None else 0

    async def pm_advance_map_merged(self, slug: str) -> None:
        """Advance map_proposed → map_merged, via the daemon."""
        await self._client.call("pm_advance_map_merged", slug=slug)

    async def aclose(self) -> None:
        if self._global_pump is not None and not self._global_pump.done():
            self._global_pump.cancel()
        self._global_subs.clear()
        await self._client.aclose()
