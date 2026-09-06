"""Codex background work: what the app-server tracks, and what a restart can do about it.

`thread/backgroundTerminals/list` is the Codex answer to the question
`server/sessions/background_tasks.py` asks of every engine — *is this session parked
on work of its own, or is it waiting for me?* — and it answers it better than the
Claude side does. Claude's answer is archaeology: replay the transcript, pair each
open ``Monitor``/``run_in_background`` ``tool_use`` against a later
``task-notification``, and infer. Codex's is a live register kept by the process that
owns the terminals, so there is nothing to infer and nothing to get wrong.

**There is no restore half, and this is a verified boundary rather than a gap.** A
background terminal is a grandchild of the `codex app-server` child that
`server/codex/process.py` spawns. Daemon shutdown drains that child
(`CodexRunnerManager.stop_all`), and a boot that follows a SIGKILL reaps any survivor
by process *tree* (`sweep_orphans` → `terminate_process_tree`). Either way every
background terminal is gone before the new daemon reads its snapshot, and the pinned
contract publishes no method that would put one back: the app-server exposes exactly
`list`, `terminate` and `clean` for them — three ways to observe or end a terminal,
none to start one. So ccswitch does the only honest thing with a snapshot of them:
it **names them to the session and stops there**. The alternative — replaying a
captured shell command line because it appeared in a list — is how a deploy restart
silently runs a push, a migration, or an install a second time. That refusal is what
``OpenBgTask.replayable=False`` encodes and what ``build_rearm_prompt`` acts on.

Consequences worth stating plainly, because they are behaviour and not just prose:

* A Codex session whose turn ends with a live background terminal reads
  ``waiting_external`` ("Waiting…"), exactly like a Claude session parked on a Monitor.
* After a daemon restart that same session **always** un-parks, because the work it
  was parked on provably did not survive. The orchestrator's rehydrate heal reaches
  that answer through this module's absence of a driver, not by accident.
* Codex has no cross-turn self-scheduled wakeup to lose either. Its ``clock.sleep``
  tool (``SleepThreadItem`` in the pinned contract) blocks *inside* a turn, so a
  sleeping Codex session is ``in_progress`` and was never a parked one — there is no
  Codex counterpart to ``scan_pending_wakeup`` to write.
"""
from __future__ import annotations

import logging
from typing import Final, Protocol

from server.codex import schema
from server.sessions.background_tasks import OpenBgTask

logger = logging.getLogger(__name__)

#: Page size requested per `thread/backgroundTerminals/list` call.
TERMINAL_PAGE_LIMIT: Final = 100

#: Upper bound on pages followed for one read. Mirrors the model catalog's
#: `MAX_MODEL_PAGES`: a peer that keeps handing back a cursor must not be able to
#: pin a turn-end status write open.
MAX_TERMINAL_PAGES: Final = 20

#: ``OpenBgTask.kind`` for a Codex background terminal, and the ``name`` reported
#: for it. The name is NOT a Codex tool name on purpose — there is no tool to
#: relaunch, so naming one would invite exactly the replay this module refuses.
TERMINAL_KIND: Final = "terminal"
TERMINAL_NAME: Final = "BackgroundTerminal"


class BackgroundTerminalLister(Protocol):
    """The one app-server method this module needs (`CodexAppServerClient`)."""

    async def list_background_terminals(
        self, params: schema.ThreadBackgroundTerminalsListParams
    ) -> schema.ThreadBackgroundTerminalsListResponse: ...


async def open_background_terminals(
    client: BackgroundTerminalLister,
    thread_id: str,
    *,
    max_pages: int = MAX_TERMINAL_PAGES,
) -> list[schema.ThreadBackgroundTerminal]:
    """Every background terminal the app-server currently tracks for *thread_id*.

    Follows ``nextCursor`` to completion but stops at *max_pages*. Duplicate
    ``processId`` values across pages are dropped: a terminal that exits between two
    pages shifts the window, and reporting one twice would double-count a session's
    background work in the status overlay.

    Raises whatever the client raises — the callers decide what a failed read means,
    and they do not agree: a turn-end status read treats it as "not parked", while a
    drain-time snapshot treats it as "nothing to report". Swallowing it here would
    take that choice away from both.
    """
    seen: set[str] = set()
    out: list[schema.ThreadBackgroundTerminal] = []
    cursor: str | None = None
    for _ in range(max(1, max_pages)):
        response = await client.list_background_terminals(
            schema.ThreadBackgroundTerminalsListParams(
                thread_id=thread_id, cursor=cursor, limit=TERMINAL_PAGE_LIMIT
            )
        )
        for terminal in response.data:
            if terminal.process_id in seen:
                continue
            seen.add(terminal.process_id)
            out.append(terminal)
        cursor = response.next_cursor
        if not cursor:
            return out
    logger.warning(
        "codex.background_terminals.page_cap thread=%s pages=%d terminals=%d",
        thread_id,
        max_pages,
        len(out),
    )
    return out


def to_open_task(terminal: schema.ThreadBackgroundTerminal) -> OpenBgTask:
    """One live terminal in the engine-neutral ``OpenBgTask`` vocabulary.

    ``tool_use_id`` carries the app-server's ``itemId`` — the id of the
    ``commandExecution`` item already visible in the transcript — so a report about a
    terminal names the same thing the user is looking at. ``input`` holds the command
    and cwd because that is what makes the report legible ("`npm run dev` in
    /srv/app"), never because anything is going to run it: ``replayable=False`` is the
    contract that says so.
    """
    return OpenBgTask(
        tool_use_id=terminal.item_id,
        kind=TERMINAL_KIND,
        name=TERMINAL_NAME,
        input={"command": terminal.command, "cwd": terminal.cwd},
        description=terminal.command,
        replayable=False,
    )
