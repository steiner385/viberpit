"""The batched client methods unwrap their envelopes and carry the deadline INWARD."""
import pytest

from server.sessions.remote import RemoteOrchestrator


class _Client:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return self._reply


def _orch(reply):
    o = RemoteOrchestrator.__new__(RemoteOrchestrator)
    o._client = _Client(reply)
    return o


@pytest.mark.asyncio
async def test_prs_for_many_unwraps_and_rehydrates():
    o = _orch({"prs": {"a": [{"number": 7, "url": "u", "state": "open",
                              "checks": "none", "title": "t"}]}})
    out = await o.prs_for_many(["a"], timeout=3.0)
    assert out["a"][0].number == 7
    method, kwargs = o._client.calls[0]
    assert method == "prs_for_many"
    assert kwargs["session_ids"] == ["a"]
    assert kwargs["timeout"] == 3.0


@pytest.mark.asyncio
async def test_summary_many_forwards_viewing():
    o = _orch({"a": "sum"})
    out = await o.latest_summary_many(["a"], viewing=True, timeout=3.0)
    assert out == {"a": "sum"}
    assert o._client.calls[0][1]["viewing"] is True


@pytest.mark.asyncio
async def test_malformed_replies_degrade_to_empty():
    assert await _orch(None).prs_for_many(["a"]) == {}
    assert await _orch("nonsense").latest_suggestions_many(["a"]) == {}
    assert await _orch(None).latest_summary_many(["a"], viewing=False) == {}
