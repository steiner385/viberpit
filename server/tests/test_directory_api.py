"""``directory_api.fetch_listing`` against a mock transport: never a real network call."""
from __future__ import annotations

import asyncio

import httpx

from server.library import directory_api


def _fetch(listing_id: str, handler) -> dict | None:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    result = asyncio.run(directory_api.fetch_listing(
        listing_id, transport=httpx.MockTransport(wrapped)))
    _fetch.calls = calls  # type: ignore[attr-defined]
    return result


def test_a_200_dict_is_returned_and_the_request_is_well_formed() -> None:
    body = {"id": "plugin_x", "checks": {"review": {"state": "done"}}}
    assert _fetch("plugin_x", lambda r: httpx.Response(200, json=body)) == body
    req = _fetch.calls[0]  # type: ignore[attr-defined]
    assert str(req.url) == f"{directory_api.DIRECTORY_URL}/plugin_x"
    assert req.headers["user-agent"] == "viberpit"


def test_any_other_status_is_none() -> None:
    for status in (404, 500, 301):
        assert _fetch("plugin_x", lambda r, s=status: httpx.Response(s, json={"id": "x"})) is None


def test_a_non_dict_body_is_none() -> None:
    assert _fetch("plugin_x", lambda r: httpx.Response(200, json=["x"])) is None
    assert _fetch("plugin_x", lambda r: httpx.Response(200, content=b"not json")) is None


def test_transport_errors_and_timeouts_are_none() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    assert _fetch("plugin_x", boom) is None
    assert _fetch("plugin_x", slow) is None


def test_a_bad_id_is_none_without_a_request() -> None:
    for bad in ("", "a/b", "a b", "x\n", "../x", "a" * 121, "x?y=1"):
        assert _fetch(bad, lambda r: httpx.Response(200, json={"id": "x"})) is None
        assert _fetch.calls == []  # type: ignore[attr-defined]
