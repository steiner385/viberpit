"""Attribution for async generators that fail while being FINALIZED (#1869, #1870).

An async generator abandoned mid-run (its consumer cancelled, its `async for`
broken out of) is not closed by anyone: it is closed by the garbage collector,
through the hook asyncio installs for exactly that purpose::

    def _asyncgen_finalizer_hook(self, agen):
        self._asyncgens.discard(agen)
        if not self.is_closed():
            self.call_soon_threadsafe(self.create_task, agen.aclose())

Nobody holds that task. If the close raises — and closing a generator IS allowed
to raise — the failure surfaces from ``Task.__del__`` as::

    ERROR asyncio: Task exception was never retrieved
    future: <Task finished coro=<<async_generator_athrow without __name__>()>
            exception=ProcessError(...)>

which names neither the generator nor the code that abandoned it. Two shapes of
it were filed against the daemon from the `claude-agent-sdk` stream:

* ``ProcessError('Command failed with exit code 1')`` (#1869) — the SDK's
  ``subprocess_cli._read_messages_impl`` *catches* ``GeneratorExit``, then awaits
  ``process.wait()`` and re-raises the CLI's non-zero exit. Closing that generator
  after the CLI died therefore raises BY DESIGN, and the close is always the GC's.
* ``RuntimeError('aclose(): asynchronous generator is already running')`` (#1870)
  — the generator's frame was still executing an abandoned ``__anext__`` when the
  finalizer tried to close it.

This module replaces the default report with an attributed one. It does NOT
swallow anything: it logs the same exception, with the generator's own frame
(file and line, via the shared ``server/aio.py`` await-chain walker) naming which
generator failed, and delegates every other loop error to the previous handler
untouched.

**Why INFO and not ERROR.** Finalization only happens once the generator has been
abandoned — by definition no consumer is left to be affected by the failure, and
the condition that caused it (a dead CLI) is already surfaced on the session by
the driver's own error path. The abandonment itself, when it is ours, is reported
by ``bounded_wait``/``_watch_abandoned``. What was left was an unattributed ERROR
per GC pass, which is triage noise (WARNING+ is captured by ``server/obs/errsink``)
standing in for a diagnosis. Keeping the record at INFO keeps it in the journal
for forensics without filing it as a new failure every time a CLI exits non-zero.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

#: Marks a handler this module installed, so a second install is a no-op rather
#: than another layer of delegation (lifespans re-run in tests).
_INSTALLED = "_viberpit_asyncgen_finalize_reporter"

#: The C-level type of the awaitable ``agen.aclose()`` / ``agen.athrow()`` return.
#: It has no ``__name__``, which is why the default report cannot name the culprit.
_ATHROW = "async_generator_athrow"


def _coro_of(context: dict[str, Any]) -> Any:
    """The coroutine-ish object behind a loop-error context, if it has one."""
    fut = context.get("task") or context.get("future")
    get_coro = getattr(fut, "get_coro", None)
    if not callable(get_coro):
        return None
    try:
        return get_coro()
    except Exception:  # pragma: no cover — defensive; diagnostics must not raise
        return None


def is_asyncgen_finalization(context: dict[str, Any]) -> bool:
    """True when *context* reports a failure while CLOSING an async generator.

    Two shapes reach the loop handler: ``Task.__del__`` on the unowned task the
    asyncgen finalizer hook created (its coro is an ``async_generator_athrow``),
    and ``loop.shutdown_asyncgens`` failing for one generator (it passes the
    generator itself under ``asyncgen``).
    """
    if context.get("asyncgen") is not None:
        return True
    return type(_coro_of(context)).__name__ == _ATHROW


def describe_asyncgen(context: dict[str, Any]) -> str:
    """Name the generator being closed — ``file:line in name``, or a fallback.

    The exception's own traceback is the reliable source: by the time
    ``Task.__del__`` reports the failure the task is finished and its coroutine
    (and with it the generator) is already gone, so there is no await chain left
    to walk. The deepest traceback frame is the generator's own code — for #1869
    that is ``subprocess_cli.py:<line> in _read_messages_impl``, which is the
    whole point: the default report names nothing at all. ``shutdown_asyncgens``
    hands the generator over directly instead, so its frame is read when present.
    """
    exc = context.get("exception")
    tb = getattr(exc, "__traceback__", None)
    if tb is not None:
        import traceback

        frames = traceback.extract_tb(tb)
        if frames:
            f = frames[-1]
            return f"{f.filename}:{f.lineno} in {f.name}"
    agen = context.get("asyncgen")
    frame = getattr(agen, "ag_frame", None)
    if frame is not None:
        return (f"{frame.f_code.co_filename}:{frame.f_lineno} "
                f"in {frame.f_code.co_name}")
    name = getattr(agen, "__qualname__", None)
    return str(name or "<unnamed async generator>")


def install_asyncgen_finalize_reporter(
    loop: asyncio.AbstractEventLoop | None = None,
) -> bool:
    """Route unowned asyncgen-close failures on *loop* through this module.

    Idempotent (a second call on the same loop returns False). Must run INSIDE
    the running loop: ``BaseEventLoop.run_forever`` installs its own asyncgen
    hooks at start, so anything set before the loop runs is replaced.
    """
    loop = loop or asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    if getattr(previous, _INSTALLED, False):
        return False

    def handler(lp: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if is_asyncgen_finalization(context):
            exc = context.get("exception")
            log.info(
                "asyncgen finalize: %s failed while being closed by the garbage "
                "collector (%s) — no consumer was left to receive it (#1869/#1870)",
                describe_asyncgen(context),
                type(exc).__name__ if exc is not None else context.get("message"),
                exc_info=exc if isinstance(exc, BaseException) else None,
            )
            return
        if previous is not None:
            previous(lp, context)
        else:
            lp.default_exception_handler(context)

    setattr(handler, _INSTALLED, True)
    loop.set_exception_handler(handler)
    return True
