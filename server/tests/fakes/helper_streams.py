"""Canned SDK streams for one-shot helper-call sites.

The SDK surfaces an HTTP API error as a stream that yields the CLI's own words
(an API-error assistant message + an ``is_error`` result) and THEN raises the
opaque ``RuntimeError("Claude Code returned an error result: success")`` — the
result subtype leaking into the exception text (#1820, #1841). A site test that
fakes only the exception cannot tell whether the site logs the cause or the
opaque sentence; these fakes carry both halves.
"""
from __future__ import annotations

from types import SimpleNamespace

OPAQUE_SDK_ERROR = "Claude Code returned an error result: success"

ORG_DENIED = ("Your organization has disabled Claude subscription access for Claude "
              "Code · Use an Anthropic API key instead, or ask your admin to enable "
              "access")


def org_denied_messages(status: int | None = 403) -> list:
    """What the SDK yields before raising on a 403 organization denial."""
    return [
        SimpleNamespace(content=[SimpleNamespace(text=ORG_DENIED)],
                        error="oauth_org_not_allowed"),
        SimpleNamespace(api_error_status=status, is_error=True, subtype="success"),
    ]


def org_denied_query(calls: list | None = None):
    """A ``query_fn(prompt=, options=)`` seam answering every call with an org
    denial. *calls* (if given) collects each prompt, so a test can prove a
    blocked account is never called again."""
    def query(*, prompt, options):  # noqa: ARG001
        if calls is not None:
            calls.append(prompt)

        async def gen():
            for m in org_denied_messages():
                yield m
            raise RuntimeError(OPAQUE_SDK_ERROR)
        return gen()
    return query
