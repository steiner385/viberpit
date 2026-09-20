"""An unowned async-generator close failure must NAME the generator (#1869, #1870).

asyncio finalizes an abandoned async generator by creating a task for
``agen.aclose()`` that nobody holds; when the close raises, ``Task.__del__``
reports "Task exception was never retrieved" against a coroutine with no
``__name__``. These tests pin the replacement report: same exception, attributed
to the generator's own frame, and every other loop error untouched.
"""
import asyncio
import gc
import logging

from server.obs.asyncgen_finalize import (
    describe_asyncgen,
    install_asyncgen_finalize_reporter,
    is_asyncgen_finalization,
)


async def _failing_on_close():
    """A generator whose CLOSE raises — the shape of the SDK's transport reader,
    which catches GeneratorExit and then re-raises the CLI's non-zero exit."""
    try:
        yield 1
    except GeneratorExit:
        pass
    raise RuntimeError("Command failed with exit code 1")


def _finalize_like_asyncio(agen) -> asyncio.Task:
    """What ``BaseEventLoop._asyncgen_finalizer_hook`` does: an UNOWNED task."""
    return asyncio.get_running_loop().create_task(agen.aclose())


def test_unowned_asyncgen_close_failure_is_reported_with_its_frame(caplog):
    caplog.set_level(logging.DEBUG)

    async def main():
        assert install_asyncgen_finalize_reporter() is True
        agen = _failing_on_close()
        assert await agen.__anext__() == 1
        task = _finalize_like_asyncio(agen)
        await asyncio.wait({task})
        assert task.done() and not task.cancelled()
        # Deliberately NOT task.exception(): retrieving it is what the real
        # finalizer never does, and it is the absence of that call that makes
        # Task.__del__ report. Drop the only reference instead — the report goes
        # through the loop's exception handler, which is now ours.
        del task
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(main())

    ours = [r for r in caplog.records if r.name == "server.obs.asyncgen_finalize"]
    assert ours, "the asyncgen finalization failure was not reported"
    assert ours[0].levelno == logging.INFO
    # Attributed: the message names THIS file and the generator's own function,
    # which is exactly what the default asyncio report cannot do.
    assert "_failing_on_close" in ours[0].getMessage()
    assert __file__.split("/")[-1] in ours[0].getMessage()
    # ...and the original exception is still carried, not swallowed.
    assert ours[0].exc_info is not None
    assert "exit code 1" in str(ours[0].exc_info[1])
    # The unattributed asyncio ERROR it replaces is gone.
    assert not [r for r in caplog.records
                if r.name == "asyncio" and r.levelno >= logging.ERROR]


def test_other_loop_errors_are_delegated_untouched():
    seen: list[dict] = []

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda lp, ctx: seen.append(ctx))
        install_asyncgen_finalize_reporter()
        loop.call_exception_handler({"message": "something else"})

    asyncio.run(main())
    assert [c["message"] for c in seen] == ["something else"]


def test_install_is_idempotent():
    async def main():
        assert install_asyncgen_finalize_reporter() is True
        assert install_asyncgen_finalize_reporter() is False

    asyncio.run(main())


def test_shutdown_asyncgens_shape_is_recognised_and_named():
    """``loop.shutdown_asyncgens`` passes the generator itself, not a task."""

    async def main():
        agen = _failing_on_close()
        await agen.__anext__()
        ctx = {"message": "an error occurred during closing of asynchronous generator",
               "asyncgen": agen}
        assert is_asyncgen_finalization(ctx) is True
        assert "_failing_on_close" in describe_asyncgen(ctx)
        # Close it here so this test leaves no abandoned generator for a later
        # GC pass to finalize (and report) inside an unrelated test.
        try:
            await agen.aclose()
        except RuntimeError:
            pass

    asyncio.run(main())


def test_plain_task_failure_is_not_mistaken_for_a_finalization():
    async def main():
        async def boom():
            raise ValueError("ordinary")

        task = asyncio.ensure_future(boom())
        await asyncio.wait({task})
        assert is_asyncgen_finalization({"task": task, "exception": task.exception()}) is False

    asyncio.run(main())
