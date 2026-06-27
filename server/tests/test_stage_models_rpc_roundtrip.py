"""Task 6: all_repos_stage_models() wired across daemon-edge (RPC handler + registration)."""
import asyncio
from server.pipeline.track import StageModel, StageNode


class _Orch:
    def all_repos_stage_models(self):
        return {"o/r": StageModel("o/r", (StageNode("ci", "CI", "ci", 0), StageNode("merged", "Merged", "merged", 1)))}


def test_rpc_handler_serializes_models():
    from server.ipc import orchestrator_rpc as mod
    captured = {}

    class _App:
        class state:  # noqa: N801
            pass

    # Monkeypatch register_rpc to capture handlers.
    orig = mod.register_rpc
    mod.register_rpc = lambda app, name, fn: captured.__setitem__(name, fn)
    try:
        mod.register_orchestrator_rpc(_App(), _Orch())
    finally:
        mod.register_rpc = orig

    handler = captured["all_repos_stage_models"]
    # asyncio.run() spins a fresh loop — robust to full-suite event-loop
    # contamination (get_event_loop() raises "no current event loop" in some orderings).
    out = asyncio.run(handler({}))
    assert out["models"]["o/r"]["nodes"][0]["id"] == "ci"
    # Edge side rebuilds:
    assert StageModel.from_dict(out["models"]["o/r"]).repo == "o/r"
