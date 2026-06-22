"""Expose a real SessionOrchestrator over the A1 daemon RPC/event transport.

Keeps orchestrator knowledge out of the generic transport (daemon.py). A2.1 wires
the vertical-slice methods; A2.2 adds the rest.
"""
import asyncio
from server.ipc.daemon import register_rpc


def register_orchestrator_rpc(app, orch) -> None:
    app.state.pumped = set()

    async def _pump(session_id: str, driver) -> None:
        async for e in driver.events():
            await app.state.publish(session_id, e.to_wire())

    async def ensure_driver(params: dict) -> dict:
        session_id = params["session_id"]
        driver = await orch.ensure_driver(session_id, params.get("identity", "system"))
        if driver is not None and session_id not in app.state.pumped:
            app.state.pumped.add(session_id)
            asyncio.create_task(_pump(session_id, driver), name=f"pump-{session_id}")
        return {"found": driver is not None}

    async def list_sessions(params: dict) -> dict:
        return {"sessions": [r.to_dict() for r in orch.list_sessions()]}

    async def get_session(params: dict) -> dict:
        rec = orch.get_session(params["session_id"])
        return {"session": rec.to_dict() if rec is not None else None}

    async def create_session(params: dict) -> dict:
        rec = await orch.create_session(**params)
        return {"session": rec.to_dict()}

    async def send_prompt(params: dict) -> dict:
        await orch.send_prompt(params["session_id"], params["text"], params.get("images"))
        return {"ok": True}

    register_rpc(app, "ensure_driver", ensure_driver)
    register_rpc(app, "list_sessions", list_sessions)
    register_rpc(app, "get_session", get_session)
    register_rpc(app, "create_session", create_session)
    register_rpc(app, "send_prompt", send_prompt)
