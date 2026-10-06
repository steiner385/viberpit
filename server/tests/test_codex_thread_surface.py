"""`server/codex/thread_surface.py` — the start-time surface fingerprint (#2714)."""
from __future__ import annotations

from server.codex import schema
from server.codex.thread_surface import (SURFACE_SUFFIX, ThreadSurfaceStore,
                                         surface_fingerprint)


def _tool(name: str, description: str = "d") -> schema.FunctionDynamicToolSpec:
    return schema.FunctionDynamicToolSpec(name=name, description=description,
                                          input_schema={"type": "object"})


def _params(**kw) -> schema.ThreadStartParams:
    base = {"cwd": "/n", "base_instructions": "be helpful", "dynamic_tools": (_tool("a"),)}
    return schema.ThreadStartParams(**{**base, **kw})


def test_the_fingerprint_covers_instructions_and_tools() -> None:
    fp = surface_fingerprint(_params())
    assert fp == surface_fingerprint(_params())
    assert fp != surface_fingerprint(_params(base_instructions="be terse"))
    assert fp != surface_fingerprint(_params(dynamic_tools=(_tool("a"), _tool("b"))))
    assert fp != surface_fingerprint(_params(dynamic_tools=(_tool("a", "changed"),)))


def test_the_fingerprint_ignores_what_a_resume_re_sends() -> None:
    """cwd, model, sandbox and approval policy ride `thread/resume` itself."""
    fp = surface_fingerprint(_params())
    assert fp == surface_fingerprint(_params(cwd="/elsewhere", model="m",
                                             sandbox="read-only",
                                             approval_policy="untrusted"))


def test_the_store_round_trips_and_overwrites(tmp_path) -> None:
    store = ThreadSurfaceStore(tmp_path / "root")
    assert not store.matches("t-1", "x")          # no record: unknown, never a match
    store.record("t-1", "x")
    assert store.matches("t-1", "x") and not store.matches("t-1", "y")
    store.record("t-1", "y")
    assert store.matches("t-1", "y")
    assert (tmp_path / "root" / f"t-1{SURFACE_SUFFIX}").read_text() == "y"
    assert not list((tmp_path / "root").glob("*.tmp"))


def test_an_unsafe_thread_id_is_never_a_file(tmp_path) -> None:
    store = ThreadSurfaceStore(tmp_path)
    store.record("../escape", "x")
    assert not store.matches("../escape", "x")
    assert not (tmp_path.parent / "escape.surface").exists()


def test_an_unwritable_root_costs_only_the_resume(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    store = ThreadSurfaceStore(blocker / "root")
    store.record("t-1", "x")                      # logged, never raised
    assert not store.matches("t-1", "x")
