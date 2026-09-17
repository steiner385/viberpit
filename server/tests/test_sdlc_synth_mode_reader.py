"""The API and the loop read the synthesizer's mode through ONE function, so the
guidance a page shows and the tick that acts cannot fold a legacy mode differently."""
from __future__ import annotations

import asyncio
import tempfile


def _read(seed) -> tuple[str, dict]:
    from server.automations.sdlc_synthesizer import read_synth_mode
    from server.db import db_session, init_db

    async def _run(db_url: str):
        await init_db(db_url)
        async with db_session() as db:
            for row in seed():
                db.add(row)
        return await read_synth_mode()

    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(_run(f"sqlite+aiosqlite:///{tmp}/test.db"))


def test_absent_row_is_off():
    mode, config = _read(lambda: [])
    assert mode == "off"
    assert isinstance(config, dict)


def test_legacy_propose_folds_to_on():
    from server.db.models import Automation
    mode, _ = _read(lambda: [Automation(id="sdlc-synthesizer", enabled=True, mode="propose", config={})])
    assert mode == "on"


def test_disabled_row_is_off_even_with_a_mode():
    from server.db.models import Automation
    mode, _ = _read(lambda: [Automation(id="sdlc-synthesizer", enabled=False, mode="on", config={})])
    assert mode == "off"


def test_tick_constant_is_the_loop_interval():
    from server.automations.sdlc_synthesizer import TICK_INTERVAL_S
    assert TICK_INTERVAL_S == 60.0
