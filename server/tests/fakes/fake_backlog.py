"""Fake harness for Backlog Runner tests — zero-network, zero-LLM fixtures.

Three fakes that cover every I/O seam the selection layer touches:

- ``FakeGh``    — stand-in for ``gitrepos._run_gh(args)``.  Records every argv
                  and returns a scripted ``subprocess.CompletedProcess`` keyed on
                  argv-prefix, or a default-success proc.
- ``fake_rank`` — factory that returns a canned ranking function (the injected
                  LLM ranking seam).
- ``FakeSpawn`` — async callable standing in for
                  ``AutomationContext.spawn_session``.  Writes a ``Session`` row
                  with ``origin="backlog-runner"`` and de-dups by
                  ``(origin, dedupe_key)``.
"""
from __future__ import annotations

import subprocess
from typing import Any


# --------------------------------------------------------------------------- #
# FakeGh                                                                       #
# --------------------------------------------------------------------------- #

def _default_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class FakeGh:
    """Callable recorder standing in for ``gitrepos._run_gh(args, token=...)``.

    Parameters
    ----------
    scripts:
        Mapping of ``tuple[str, ...]`` argv-*prefix* → ``CompletedProcess`` to
        return when that prefix matches the incoming argv.  Checked in insertion
        order; the longest matching prefix wins.  Missing prefixes fall back to
        a success proc (returncode=0, empty stdout/stderr).

    Usage::

        fake = FakeGh({("api", "graphql"): proc(stdout='{"data": {}}')})
        proc_out = fake(["api", "graphql", "-f", "query=..."])
        assert fake.calls[0] == ["api", "graphql", "-f", "query=..."]
    """

    def __init__(self, scripts: dict[tuple, subprocess.CompletedProcess] | None = None) -> None:
        self._scripts: dict[tuple, subprocess.CompletedProcess] = scripts or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], token: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        argv = tuple(args)
        # Longest matching prefix wins.
        best: subprocess.CompletedProcess | None = None
        best_len = -1
        for prefix, result in self._scripts.items():
            plen = len(prefix)
            if argv[:plen] == prefix and plen > best_len:
                best = result
                best_len = plen
        return best if best is not None else _default_proc()


# --------------------------------------------------------------------------- #
# fake_rank                                                                     #
# --------------------------------------------------------------------------- #

def fake_rank(order: list[str]):
    """Return a ranking function that always returns the canned ``order``.

    The returned function mirrors the ``ranking(candidates) -> list[str]`` seam
    consumed by ``rank_and_batch``.
    """

    def _rank(candidates: list) -> list[str]:  # noqa: ARG001
        return list(order)

    return _rank


# --------------------------------------------------------------------------- #
# FakeSpawn                                                                     #
# --------------------------------------------------------------------------- #

class FakeSpawn:
    """Async callable standing in for ``AutomationContext.spawn_session``.

    Writes a ``Session`` row with ``origin="backlog-runner"`` and the given
    ``origin_ref`` via ``db_session()``.  De-dups by ``(origin, dedupe_key)``:
    if a non-terminal session already exists for the key, skips the insert and
    returns the existing row.

    Exposes ``.spawns`` — list of ``origin_ref`` values for which a *new* row
    was actually inserted.
    """

    ORIGIN = "backlog-runner"

    def __init__(self) -> None:
        self.spawns: list[str] = []

    async def __call__(self, *, origin_ref: str,
                       dedupe_key: str | None = None, **_kwargs: Any) -> Any:
        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import Session

        key = dedupe_key or origin_ref
        async with db_session() as db:
            existing = (await db.execute(
                select(Session).where(
                    Session.origin == self.ORIGIN,
                    Session.origin_ref == key,
                    Session.status != "ended",
                )
            )).scalars().first()
            if existing is not None:
                return existing
            row = Session(
                name=f"backlog:{origin_ref}",
                account_name="test",
                cwd="/tmp",
                status="idle",
                origin=self.ORIGIN,
                origin_ref=key,
            )
            db.add(row)
            await db.flush()
            self.spawns.append(origin_ref)
            return row
