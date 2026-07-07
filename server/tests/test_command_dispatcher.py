# server/tests/test_command_dispatcher.py
import pytest
from server.sessions.commands.catalog import Command
from server.sessions.commands.dispatcher import dispatch


class FakeDriver:
    def __init__(self):
        self.model = "unset"
        self.reconnected = False
        self.prompts: list[str] = []
        self._usage = {"percentage": 33.0}
    async def set_model(self, m): self.model = m
    async def reconnect(self): self.reconnected = True
    async def context_usage(self): return self._usage
    async def send_prompt(self, text, images=None): self.prompts.append(text)


def _cmd(name, kind):
    return Command(name=name, description="", argument_hint=None, kind=kind)


@pytest.mark.anyio
async def test_help_returns_render_only():
    d = FakeDriver()
    res = await dispatch(d, _cmd("help", "client"), "")
    assert res == {"name": "help", "render": "help", "data": None}


@pytest.mark.anyio
async def test_context_fetches_usage():
    d = FakeDriver()
    res = await dispatch(d, _cmd("context", "client"), "")
    assert res["render"] == "context"
    assert res["data"] == {"percentage": 33.0}


@pytest.mark.anyio
async def test_model_is_not_dispatchers_to_own():
    """/model is intercepted by run_command BEFORE dispatch (orchestrator-level:
    record + DB persistence + busy stop/switch/restart, works driverless). The
    dispatcher must not grow a parallel /model implementation — reaching it is
    a routing bug and must fail loudly."""
    d = FakeDriver()
    with pytest.raises(ValueError, match="unhandled control command"):
        await dispatch(d, _cmd("model", "control"), "sonnet")
    assert d.model == "unset"


@pytest.mark.anyio
async def test_clear_reconnects():
    d = FakeDriver()
    res = await dispatch(d, _cmd("clear", "control"), "")
    assert d.reconnected is True
    assert res == {"name": "clear", "render": "clear", "data": {"text": "Conversation cleared"}}


@pytest.mark.anyio
async def test_passthrough_sends_prompt_and_returns_none():
    d = FakeDriver()
    res = await dispatch(d, _cmd("review", "passthrough"), "64")
    assert res is None
    assert d.prompts == ["/review 64"]


@pytest.mark.anyio
async def test_passthrough_no_args():
    d = FakeDriver()
    await dispatch(d, _cmd("compact", "passthrough"), "")
    assert d.prompts == ["/compact"]


@pytest.mark.anyio
async def test_unknown_control_raises():
    """A control-kind command with no matching branch must raise, not silently passthrough."""
    d = FakeDriver()
    with pytest.raises(ValueError, match="unhandled control command"):
        await dispatch(d, _cmd("future-control", "control"), "")
    # Must NOT have sent any prompt.
    assert d.prompts == []
