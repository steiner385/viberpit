import asyncio
import pytest
from server.deploy import drain
from server.ipc.client import DaemonUnavailable


class FakeClient:
    def __init__(self, statuses, begin_exc=None):
        self._statuses = list(statuses)
        self._begin_exc = begin_exc
        self.began = False
    async def call(self, method, **params):
        if method == "begin_drain":
            if self._begin_exc:
                raise self._begin_exc
            self.began = True
            return {}
        if method == "drain_status":
            return self._statuses.pop(0) if self._statuses else {"state": "working", "condensing": 0}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_ready_when_daemon_reports_ready():
    c = FakeClient([{"state": "working", "condensing": 0}, {"state": "ready", "condensing": 0}])
    res = await drain.drain_and_wait(c, turn_cap_s=100, poll_s=0, sleep=lambda s: asyncio.sleep(0))
    assert res == "ready" and c.began is True


@pytest.mark.asyncio
async def test_unavailable_when_begin_drain_fails():
    c = FakeClient([], begin_exc=DaemonUnavailable("no sock"))
    assert await drain.drain_and_wait(c) == "unavailable"


@pytest.mark.asyncio
async def test_cap_when_never_ready():
    ticks = iter([0, 0, 1, 999])   # now() jumps past cap
    c = FakeClient([{"state": "working", "condensing": 0}] * 10)
    res = await drain.drain_and_wait(c, turn_cap_s=5, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "cap"


@pytest.mark.asyncio
async def test_condensing_session_is_not_capped_at_turn_cap():
    # Past the turn cap but still condensing → keep waiting (no "cap"); then it finishes.
    ticks = iter([0, 0, 200, 200, 201])
    c = FakeClient([{"state": "draining", "condensing": 1},
                    {"state": "draining", "condensing": 1},
                    {"state": "ready", "condensing": 0}])
    res = await drain.drain_and_wait(c, turn_cap_s=180, condense_cap_s=600, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "ready"


@pytest.mark.asyncio
async def test_condensing_still_capped_at_condense_ceiling():
    ticks = iter([0, 0, 1, 999])   # now() jumps past the condense ceiling
    c = FakeClient([{"state": "draining", "condensing": 1}] * 10)
    res = await drain.drain_and_wait(c, turn_cap_s=180, condense_cap_s=600, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "cap"


@pytest.mark.asyncio
async def test_plain_turn_still_capped_at_turn_cap():
    ticks = iter([0, 0, 1, 200])   # past turn cap, nothing condensing
    c = FakeClient([{"state": "draining", "condensing": 0}] * 10)
    res = await drain.drain_and_wait(c, turn_cap_s=180, condense_cap_s=600, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "cap"
