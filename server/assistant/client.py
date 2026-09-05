"""Loopback HTTP client the assistant's tools use to call ccswitch's own API as the user
(spec §4.3). Every request carries the assistant bearer token and attribution headers."""
from __future__ import annotations

from typing import Any

import httpx

from server.api.ratelimit import TokenBucketRateLimiter
from server.sessions.types import SYSTEM_IDENTITY

# Tool-call ceiling for ONE assistant session (spec §4.10): a burst of 30 calls,
# refilling at 1/s — so a runaway loop settles at ~60 calls/min and a 31st call in
# a cold minute is refused. Reuses the API layer's limiter (server/api/ratelimit.py),
# which is deliberately framework-free so non-FastAPI callers like this one can use it.
TOOL_CALL_CAPACITY = 30.0
TOOL_CALL_REFILL_PER_SEC = 1.0


class AssistantApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class AssistantApi:
    def __init__(self, *, base_url: str, token: str, session_id: str,
                 identity: str = SYSTEM_IDENTITY,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout_s,
            headers={"Authorization": f"Bearer {token}",
                     "X-CCSwitch-Via": "assistant",
                     "X-CCSwitch-Assistant-Session": session_id})
        self.session_id = session_id
        # The identity the token is bound to (server/assistant/token.py mints it over
        # {identity, session_id}). Carried here so the `assistant.tool_call` audit is
        # attributed to the human who owns the drawer, not to "system".
        self.identity = identity
        # One bucket per AssistantApi = one per assistant session: the orchestrator
        # mints exactly one client per session (``_setup_assistant_overlay``). Keyed by
        # session id anyway so the key is meaningful in a shared-limiter future.
        self._limiter = TokenBucketRateLimiter(TOOL_CALL_CAPACITY, TOOL_CALL_REFILL_PER_SEC)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        # Checked BEFORE the request so a rate-limited call costs nothing downstream.
        # Surfaced as a normal AssistantApiError, so `guarded()` in tools.py turns it
        # into an is_error tool result the model can read and back off from.
        if self._limiter.check(self.session_id) is not None:
            raise AssistantApiError(429, "rate limited — slow down and batch your work")
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise AssistantApiError(0, f"ccswitch API unreachable: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                msg = f"{err.get('code', r.status_code)}: {err.get('message', '')}".strip()
            except ValueError:
                msg = r.text[:200]
            raise AssistantApiError(r.status_code, msg)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._call("GET", path, params=params)

    async def post(self, path: str, json: Any = None) -> Any:
        return await self._call("POST", path, json=json)

    async def patch(self, path: str, json: Any = None) -> Any:
        return await self._call("PATCH", path, json=json)

    async def delete(self, path: str) -> Any:
        return await self._call("DELETE", path)
