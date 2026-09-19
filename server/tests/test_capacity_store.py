import asyncio
import pytest

from server.automations.capacity_store import read_plan, write_plan

pytestmark = pytest.mark.usefixtures("isolated_db")


def test_plan_round_trips_and_upserts():
    async def run():
        assert await read_plan() is None
        await write_plan({"pushLevel": "steady", "applied": False, "fleetConcurrency": 3,
                          "perProduct": {"kindash": 2}, "pending": {}, "budget": {"accounts": 4},
                          "binding": {"layer": "product", "code": "product_capacity_full", "text": "x"},
                          "reasons": ["kindash: 2"], "defects": []})
        first = await read_plan()
        assert first["fleetConcurrency"] == 3 and first["perProduct"] == {"kindash": 2}
        assert first["computedAt"] is not None
        await write_plan({**first, "fleetConcurrency": 5})
        assert (await read_plan())["fleetConcurrency"] == 5
    asyncio.run(run())
