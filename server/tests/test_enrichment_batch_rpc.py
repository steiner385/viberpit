"""The batched RPCs are registered and serialize the way their singular twins do."""
import pytest
from fastapi import FastAPI

from server.github.pr_status import PullRequestInfo
from server.ipc.orchestrator_rpc import register_orchestrator_rpc


class _Orch:
    def prs_for_many(self, session_ids):
        return {sid: [PullRequestInfo(7, "u", "open", "none", f"t-{sid}")] for sid in session_ids}

    async def latest_suggestions_many(self, session_ids):
        return {sid: [f"s-{sid}"] for sid in session_ids}

    async def latest_summary_many(self, session_ids, *, viewing):
        return {sid: f"sum-{sid}-{viewing}" for sid in session_ids}


def _app():
    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, _Orch())
    return app


@pytest.mark.asyncio
async def test_prs_for_many_serializes_via_to_dict():
    app = _app()
    out = await app.state.rpc["prs_for_many"]({"session_ids": ["a"]})
    assert out["prs"]["a"][0]["number"] == 7
    assert out["prs"]["a"][0]["title"] == "t-a"


@pytest.mark.asyncio
async def test_suggestions_and_summary_many_are_registered():
    app = _app()
    assert await app.state.rpc["latest_suggestions_many"]({"session_ids": ["a"]}) == {"a": ["s-a"]}
    assert await app.state.rpc["latest_summary_many"](
        {"session_ids": ["a"], "viewing": True}) == {"a": "sum-a-True"}
