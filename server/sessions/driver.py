from __future__ import annotations
import asyncio, logging
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from . import events as ev
from .translator import Translator
from .permissions import PermissionRegistry

log = logging.getLogger(__name__)

_STREAM_DONE = object()


class SessionDriver:
    """Drives one Claude session via an SDK client, exposing normalized events.

    The orchestrator composes the per-account config dir and passes it as `config_dir`;
    `sdk_factory(cwd, config_dir, can_use_tool, mode, resume) -> SDKClientProtocol` builds
    the client (production = real ClaudeSDKClient; tests = FakeSDKClient).
    """

    def __init__(self, session_id, account, cwd, config_dir, *, mode, sdk_factory, registry,
                 identity, denylist=PermissionRegistry.DANGEROUS, resume=None):
        self.session_id = session_id
        self.account = account
        self.cwd = cwd
        self.config_dir = config_dir
        self.mode = mode
        self.identity = identity
        self.denylist = denylist
        self.resume = resume
        self.claude_session_id: str | None = None
        self._factory = sdk_factory
        self._reg = registry
        self._tr = Translator()
        self._client = None
        self._out: asyncio.Queue = asyncio.Queue()
        self._pump: asyncio.Task | None = None

    def _turn(self):
        return getattr(self._tr, "_turn_id", None) or "turn"

    async def _can_use_tool(self, tool_name, tool_input, ctx=None):
        turn_id = self._turn()
        perm_id, fut = await self._reg.request(
            self.session_id, turn_id, tool_name, tool_input,
            mode=self.mode, denylist=self.denylist, allowlist=True, identity=self.identity)
        if perm_id is not None:
            await self._out.put(ev.PermissionRequest(
                perm_id, turn_id, tool_name, ev.tool_kind(tool_name),
                tool_input, plan=(tool_name == "ExitPlanMode")))
        allowed = await fut
        return PermissionResultAllow() if allowed else PermissionResultDeny(message="denied by user")

    async def start(self):
        self._client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, self.resume)
        await self._client.__aenter__()

    async def send_prompt(self, text: str):
        if self._client is None:
            raise RuntimeError("SessionDriver.send_prompt called before start()")
        await self._out.put(ev.Status(state="working"))
        await self._client.query(text)
        self._pump = asyncio.create_task(self._drain())

    async def _drain(self):
        try:
            async for msg in self._client.receive_response():
                for e in self._tr.feed(msg):
                    await self._out.put(e)
                if self.claude_session_id is None and self._tr.session_id:
                    self.claude_session_id = self._tr.session_id
        except Exception:
            log.exception("driver.drain session=%s", self.session_id)
            await self._out.put(ev.ErrorEvent("driver_error", "session stream failed"))
        finally:
            await self._out.put(ev.Status(state="idle"))

    async def events(self):
        while True:
            item = await self._out.get()
            if item is _STREAM_DONE:
                return
            yield item

    async def interrupt(self):
        self._reg.deny_all(self.session_id)
        if self._client:
            await self._client.interrupt()

    async def set_permission_mode(self, mode: str):
        self.mode = mode
        if self._client:
            await self._client.set_permission_mode("plan" if mode == "plan" else "default")

    def resolve_permission(self, perm_id, decision, identity) -> bool:
        return self._reg.resolve(perm_id, decision, identity)

    async def aclose(self):
        self._reg.deny_all(self.session_id)
        if self._pump:
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.__aexit__(None, None, None)
        await self._out.put(_STREAM_DONE)
