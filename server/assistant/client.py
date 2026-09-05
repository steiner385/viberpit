"""Loopback HTTP client the assistant's tools use to call ccswitch's own API as the user
(spec §4.3). Every request carries the assistant bearer token and attribution headers."""
from __future__ import annotations

from typing import Any

import httpx


class AssistantApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class AssistantApi:
    def __init__(self, *, base_url: str, token: str, session_id: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout_s,
            headers={"Authorization": f"Bearer {token}",
                     "X-CCSwitch-Via": "assistant",
                     "X-CCSwitch-Assistant-Session": session_id})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
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
