"""Codex thread rollout export/import for the fleet move (D-17 follow-up, #3347).

Pure file logic over two throwaway ``CODEX_HOME`` trees: the rollout is located by
thread id, carried base64-inline with its ViberPit sidecars, never overwrites a
copy the target already holds, and is retired (renamed, not deleted) on the
source. No codex binary is involved.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from server.codex import item_times, thread_move
from server.codex.item_times import safe_thread_id, thread_sidecar_path
from server.codex.thread_move import (
    HISTORY_MAX_BYTES,
    Sidecar,
    ThreadBundle,
    ThreadMoveError,
    export_thread,
    find_rollout,
    import_thread,
    inspect,
    remove_imported,
    retire_rollout,
    verify_thread,
)

THREAD = "019a5c3e-7b1a-7d2e-8f00-0123456789ab"
OTHER = "019a5c3e-7b1a-7d2e-8f00-ffffffffffff"
REL = f"sessions/2026/11/06/rollout-2026-11-06T14-22-33-{THREAD}.jsonl"


def _rollout_bytes(thread_id: str = THREAD, pad: int = 0) -> bytes:
    lines = [
        {"timestamp": "2026-11-06T14:22:33.120Z", "type": "session_meta",
         "payload": {"id": thread_id, "timestamp": "2026-11-06T14:22:33.120Z",
                     "cwd": "/home/tony/GitHub/viberpit", "originator": "codex_cli_rs",
                     "cli_version": "0.153.2", "instructions": None, "source": "vscode"}},
        {"timestamp": "2026-11-06T14:22:40.004Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello" + ("x" * pad)}]}},
        {"timestamp": "2026-11-06T14:22:40.010Z", "type": "turn_context",
         "payload": {"cwd": "/home/tony/GitHub/viberpit", "approval_policy": "on-request",
                     "sandbox_policy": {"type": "workspace-write"}, "model": "gpt-5-codex",
                     "effort": "medium", "summary": "auto"}},
    ]
    return "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines).encode()


def _seed(home: Path, rel: str = REL, body: bytes | None = None) -> Path:
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_rollout_bytes() if body is None else body)
    return path


def _sidecar(root: Path, suffix: str, body: bytes) -> Path:
    path = thread_sidecar_path(root, THREAD, suffix)
    assert path is not None
    root.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


@pytest.fixture
def src(tmp_path: Path) -> Path:
    return tmp_path / "src"


@pytest.fixture
def dst(tmp_path: Path) -> Path:
    return tmp_path / "dst"


# --- the id rule -------------------------------------------------------------


def test_safe_thread_id_is_the_single_id_rule() -> None:
    assert safe_thread_id(THREAD)
    for bad in ("", "*", "../x", "a/b", "a.b", ".hidden", "x" * 129, "rollout-*"):
        assert not safe_thread_id(bad), bad
    # thread_move validates through the same function, never a regex of its own.
    assert thread_move.safe_thread_id is item_times.safe_thread_id
    assert thread_sidecar_path(Path("/r"), "a/b", ".jsonl") is None
    assert thread_sidecar_path(Path("/r"), THREAD, ".jsonl") == Path("/r") / f"{THREAD}.jsonl"


def test_an_unsafe_thread_id_never_touches_the_filesystem(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "never-created"

    def boom(*a, **k):  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("glob was called with an unsafe thread id")

    monkeypatch.setattr(Path, "glob", boom)
    monkeypatch.setattr(Path, "rglob", boom)
    for bad in ("*", "../x", "a/b", ""):
        assert find_rollout(home, bad) is None
        assert inspect(home, tmp_path / "side", bad) == 0
        with pytest.raises(ThreadMoveError) as ei:
            export_thread(home, None, bad)
        assert ei.value.code == "not_found"
        assert retire_rollout(home, bad, "rocky") is None
        bundle = ThreadBundle(thread_id=bad, rel_path=REL, size=1, sha256="0" * 64,
                              data=base64.b64encode(b"x").decode("ascii"), sidecars=())
        with pytest.raises(ThreadMoveError) as ei:
            import_thread(home, None, bundle)
        assert ei.value.code == "bad_bundle"
    assert not home.exists()


# --- find_rollout ------------------------------------------------------------


def test_find_rollout_returns_the_one_regular_file(src: Path) -> None:
    assert find_rollout(src, THREAD) is None            # no sessions tree at all
    path = _seed(src)
    assert find_rollout(src, THREAD) == path
    assert find_rollout(src, OTHER) is None


def test_find_rollout_skips_symlinks_and_directories(src: Path) -> None:
    real = _seed(src, rel=f"sessions/2026/11/06/rollout-2026-11-06T14-22-33-{OTHER}.jsonl")
    link = src / "sessions/2026/11/05" / f"rollout-2026-11-05T00-00-00-{THREAD}.jsonl"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    (src / "sessions/2026/11/04" / f"rollout-2026-11-04T00-00-00-{THREAD}.jsonl").mkdir(parents=True)
    assert find_rollout(src, THREAD) is None
    # a real file alongside the decoys is still found (the decoys are not "ambiguous")
    path = _seed(src)
    assert find_rollout(src, THREAD) == path


def test_find_rollout_ignores_a_layout_outside_the_date_tree(src: Path) -> None:
    _seed(src, rel=f"sessions/a/b/c/rollout-ts-{THREAD}.jsonl")
    _seed(src, rel=f"sessions/2026/11/06/rollout-bad slug-{THREAD}.jsonl")
    assert find_rollout(src, THREAD) is None


def test_two_rollouts_for_one_thread_is_ambiguous(src: Path) -> None:
    _seed(src)
    _seed(src, rel=f"sessions/2026/11/07/rollout-2026-11-07T01-02-03-{THREAD}.jsonl")
    with pytest.raises(ThreadMoveError) as ei:
        find_rollout(src, THREAD)
    assert ei.value.code == "ambiguous"
    assert str(ei.value).startswith("ambiguous: ")


# --- inspect / export --------------------------------------------------------


def test_inspect_counts_rollout_and_existing_sidecars(src: Path, tmp_path: Path) -> None:
    side = tmp_path / "side"
    assert inspect(src, side, THREAD) == 0
    rollout = _seed(src)
    assert inspect(src, side, THREAD) == rollout.stat().st_size
    assert inspect(src, None, THREAD) == rollout.stat().st_size
    a = _sidecar(side, ".jsonl", b'{"i":"exec-1","s":1}\n')
    b = _sidecar(side, ".turns.jsonl", b'{"turn":"t1"}\n')
    assert inspect(src, side, THREAD) == sum(p.stat().st_size for p in (rollout, a, b))
    assert inspect(src, None, THREAD) == rollout.stat().st_size


def test_export_carries_the_rollout_and_sidecars(src: Path, tmp_path: Path) -> None:
    side = tmp_path / "side"
    rollout = _seed(src)
    _sidecar(side, ".jsonl", b'{"i":"exec-1","s":1}\n')
    bundle = export_thread(src, side, THREAD)
    assert bundle.thread_id == THREAD
    assert bundle.rel_path == REL
    assert bundle.size == rollout.stat().st_size
    assert bundle.sha256 == hashlib.sha256(_rollout_bytes()).hexdigest()
    assert bundle.data is not None
    assert base64.b64decode(bundle.data) == _rollout_bytes()
    assert [s.name for s in bundle.sidecars] == [f"{THREAD}.jsonl"]
    (sc,) = bundle.sidecars
    assert sc.size == len(b'{"i":"exec-1","s":1}\n')
    assert sc.sha256 == hashlib.sha256(b'{"i":"exec-1","s":1}\n').hexdigest()
    assert sc.data is not None and base64.b64decode(sc.data) == b'{"i":"exec-1","s":1}\n'
    assert export_thread(src, None, THREAD).sidecars == ()


def test_export_without_a_rollout_is_not_found(src: Path) -> None:
    with pytest.raises(ThreadMoveError) as ei:
        export_thread(src, None, THREAD)
    assert ei.value.code == "not_found"


def test_export_over_the_cap_is_refused_and_inspect_reports_it(src: Path, tmp_path: Path) -> None:
    assert HISTORY_MAX_BYTES == 128 * 1024 * 1024
    side = tmp_path / "side"
    rollout = _seed(src)
    cap = rollout.stat().st_size
    assert export_thread(src, side, THREAD, max_bytes=cap).size == cap      # at the cap: fine
    with pytest.raises(ThreadMoveError) as ei:
        export_thread(src, side, THREAD, max_bytes=cap - 1)
    assert ei.value.code == "too_large"
    # sidecar bytes count against the same cap
    _sidecar(side, ".turns.jsonl", b"x" * 10)
    assert inspect(src, side, THREAD) == cap + 10
    with pytest.raises(ThreadMoveError) as ei:
        export_thread(src, side, THREAD, max_bytes=cap)
    assert ei.value.code == "too_large"
    assert export_thread(src, None, THREAD, max_bytes=cap).size == cap


# --- import ------------------------------------------------------------------


def test_round_trip_between_two_homes(src: Path, dst: Path, tmp_path: Path) -> None:
    side_src, side_dst = tmp_path / "side-src", tmp_path / "side-dst"
    _seed(src)
    _sidecar(side_src, ".jsonl", b'{"i":"exec-1","s":1}\n')
    _sidecar(side_src, ".turns.jsonl", b'{"turn":"t1"}\n')
    bundle = export_thread(src, side_src, THREAD)

    written = import_thread(dst, side_dst, bundle)

    assert written[0] == dst / REL
    assert set(written) == {dst / REL, side_dst / f"{THREAD}.jsonl", side_dst / f"{THREAD}.turns.jsonl"}
    assert (dst / REL).read_bytes() == _rollout_bytes()
    assert (side_dst / f"{THREAD}.jsonl").read_bytes() == b'{"i":"exec-1","s":1}\n'
    assert (side_dst / f"{THREAD}.turns.jsonl").read_bytes() == b'{"turn":"t1"}\n'
    for p in written:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
        assert not p.is_symlink()
    for d in (dst / "sessions", dst / "sessions/2026", dst / "sessions/2026/11", dst / "sessions/2026/11/06", side_dst):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    assert find_rollout(dst, THREAD) == dst / REL
    assert verify_thread(dst, bundle)
    assert verify_thread(dst, bundle.stripped())
    # no temp file left behind
    assert _tree(dst) == {"sessions", "sessions/2026", "sessions/2026/11", "sessions/2026/11/06", REL}


def test_import_writes_only_the_rollout_without_a_sidecar_root(src: Path, dst: Path, tmp_path: Path) -> None:
    _seed(src)
    _sidecar(tmp_path / "side", ".jsonl", b"{}\n")
    bundle = export_thread(src, tmp_path / "side", THREAD)
    assert import_thread(dst, None, bundle) == [dst / REL]


def test_sidecars_are_written_when_absent_and_left_alone_when_present(src: Path, dst: Path, tmp_path: Path) -> None:
    side_src, side_dst = tmp_path / "side-src", tmp_path / "side-dst"
    _seed(src)
    _sidecar(side_src, ".jsonl", b"from-source\n")
    _sidecar(side_src, ".turns.jsonl", b"turns-from-source\n")
    existing = _sidecar(side_dst, ".jsonl", b"already-here\n")
    bundle = export_thread(src, side_src, THREAD)

    written = import_thread(dst, side_dst, bundle)

    assert written == [dst / REL, side_dst / f"{THREAD}.turns.jsonl"]
    assert existing.read_bytes() == b"already-here\n"
    assert (side_dst / f"{THREAD}.turns.jsonl").read_bytes() == b"turns-from-source\n"


def test_import_refuses_an_existing_rollout_before_writing_anything(src: Path, dst: Path, tmp_path: Path) -> None:
    _seed(src)
    _sidecar(tmp_path / "side-src", ".jsonl", b"{}\n")
    bundle = export_thread(src, tmp_path / "side-src", THREAD)
    # same thread id at a different date path on the target
    _seed(dst, rel=f"sessions/2025/01/01/rollout-2025-01-01T00-00-00-{THREAD}.jsonl", body=b"theirs\n")
    before = _tree(dst)
    with pytest.raises(ThreadMoveError) as ei:
        import_thread(dst, tmp_path / "side-dst", bundle)
    assert ei.value.code == "exists"
    assert _tree(dst) == before
    assert not (tmp_path / "side-dst").exists()
    # anything already occupying rel_path itself (even a symlink find_rollout would skip) also refuses
    (dst / "sessions/2025/01/01" / f"rollout-2025-01-01T00-00-00-{THREAD}.jsonl").unlink()
    (dst / REL).parent.mkdir(parents=True)
    (dst / REL).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ThreadMoveError) as ei:
        import_thread(dst, None, bundle)
    assert ei.value.code == "exists"
    assert (dst / REL).is_symlink()


@pytest.mark.parametrize("rel", [
    f"../../escape/rollout-ts-{THREAD}.jsonl",
    f"/abs/sessions/2026/11/06/rollout-ts-{THREAD}.jsonl",
    f"sessions/2026/11/06/../../../rollout-ts-{THREAD}.jsonl",
    f"sessions/2026/11/06/rollout-ts-{OTHER}.jsonl",          # another thread's name
    f"sessions/a/b/c/rollout-ts-{THREAD}.jsonl",
    f"sessions/2026/11/06/rollout-ts-{THREAD}.jsonl.moved-to-rocky",
    f"sessions/2026/11/06/rollout-{THREAD}.jsonl",            # no timestamp segment
    f"sessions/2026/11/06/rollout-a/b-{THREAD}.jsonl",
    "",
])
def test_import_refuses_a_bad_rel_path_without_writing(dst: Path, rel: str) -> None:
    body = _rollout_bytes()
    bundle = ThreadBundle(thread_id=THREAD, rel_path=rel, size=len(body),
                          sha256=hashlib.sha256(body).hexdigest(),
                          data=base64.b64encode(body).decode("ascii"), sidecars=())
    with pytest.raises(ThreadMoveError) as ei:
        import_thread(dst, None, bundle)
    assert ei.value.code == "bad_bundle"
    assert not dst.exists()


def test_import_refuses_a_stripped_or_corrupt_payload(dst: Path, tmp_path: Path) -> None:
    body = _rollout_bytes()
    good = ThreadBundle(thread_id=THREAD, rel_path=REL, size=len(body),
                        sha256=hashlib.sha256(body).hexdigest(),
                        data=base64.b64encode(body).decode("ascii"), sidecars=())
    bad = [
        good.stripped(),
        ThreadBundle(**{**good.__dict__, "size": len(body) + 1}),
        ThreadBundle(**{**good.__dict__, "sha256": "0" * 64}),
        ThreadBundle(**{**good.__dict__, "data": "not*base64!"}),
        ThreadBundle(**{**good.__dict__, "sidecars": (
            Sidecar(name="../evil", size=1, sha256=hashlib.sha256(b"x").hexdigest(),
                    data=base64.b64encode(b"x").decode("ascii")),)}),
        ThreadBundle(**{**good.__dict__, "sidecars": (
            Sidecar(name=f"{OTHER}.jsonl", size=1, sha256=hashlib.sha256(b"x").hexdigest(),
                    data=base64.b64encode(b"x").decode("ascii")),)}),
        ThreadBundle(**{**good.__dict__, "sidecars": (
            Sidecar(name=f"{THREAD}.jsonl", size=1, sha256=hashlib.sha256(b"x").hexdigest(), data=None),)}),
        ThreadBundle(**{**good.__dict__, "sidecars": (
            Sidecar(name=f"{THREAD}.jsonl", size=2, sha256=hashlib.sha256(b"x").hexdigest(),
                    data=base64.b64encode(b"x").decode("ascii")),)}),
    ]
    for bundle in bad:
        with pytest.raises(ThreadMoveError) as ei:
            import_thread(dst, tmp_path / "side", bundle)
        assert ei.value.code == "bad_bundle", bundle
    assert not dst.exists()
    assert not (tmp_path / "side").exists()


def test_a_failure_midway_removes_what_was_written_and_is_io(src: Path, dst: Path, tmp_path: Path) -> None:
    _seed(src)
    _sidecar(tmp_path / "side-src", ".jsonl", b"{}\n")
    bundle = export_thread(src, tmp_path / "side-src", THREAD)
    blocked = tmp_path / "side-dst-is-a-file"
    blocked.write_text("not a directory")
    with pytest.raises(ThreadMoveError) as ei:
        import_thread(dst, blocked, bundle)
    assert ei.value.code == "io"
    assert not (dst / REL).exists()
    assert find_rollout(dst, THREAD) is None
    assert blocked.read_text() == "not a directory"


# --- verify ------------------------------------------------------------------


def test_verify_is_false_on_a_truncated_or_missing_copy(src: Path, dst: Path) -> None:
    _seed(src)
    bundle = export_thread(src, None, THREAD)
    assert verify_thread(dst, bundle) is False                   # missing
    target = dst / REL
    target.parent.mkdir(parents=True)
    target.write_bytes(_rollout_bytes()[:-7])
    assert verify_thread(dst, bundle) is False                   # truncated
    target.write_bytes(_rollout_bytes()[:-7] + b"ZZZZZZZ")
    assert verify_thread(dst, bundle) is False                   # same size, other bytes
    target.write_bytes(_rollout_bytes())
    assert verify_thread(dst, bundle) is True
    assert verify_thread(dst, bundle.stripped()) is True


def test_verify_never_follows_a_symlink_or_a_bad_rel_path(src: Path, dst: Path, tmp_path: Path) -> None:
    _seed(src)
    bundle = export_thread(src, None, THREAD)
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_bytes(_rollout_bytes())
    (dst / REL).parent.mkdir(parents=True)
    (dst / REL).symlink_to(elsewhere)
    assert verify_thread(dst, bundle) is False
    assert verify_thread(tmp_path, ThreadBundle(**{**bundle.__dict__, "rel_path": "../elsewhere.jsonl"})) is False


# --- retire / remove ---------------------------------------------------------


def test_retire_renames_and_a_second_find_is_none(src: Path) -> None:
    assert retire_rollout(src, THREAD, "rocky") is None
    rollout = _seed(src)
    moved = retire_rollout(src, THREAD, "rocky")
    assert moved == rollout.with_name(rollout.name + ".moved-to-rocky")
    assert moved.read_bytes() == _rollout_bytes()
    assert not rollout.exists()
    assert find_rollout(src, THREAD) is None
    assert retire_rollout(src, THREAD, "rocky") is None


def test_retire_sanitises_the_owner_and_never_clobbers_an_earlier_retirement(src: Path) -> None:
    rollout = _seed(src)
    moved = retire_rollout(src, THREAD, "rocky host/1:x")
    assert moved is not None and moved.name == rollout.name + ".moved-to-rocky_host_1_x"
    second = _seed(src, body=b"second\n")
    moved2 = retire_rollout(src, THREAD, "rocky host/1:x")
    assert moved2 is not None and moved2 != moved and moved2.parent == second.parent
    assert moved.read_bytes() == _rollout_bytes()
    assert moved2.read_bytes() == b"second\n"
    assert find_rollout(src, THREAD) is None


def test_remove_imported_removes_only_what_was_written(src: Path, dst: Path, tmp_path: Path) -> None:
    _seed(src)
    _sidecar(tmp_path / "side-src", ".jsonl", b"{}\n")
    bundle = export_thread(src, tmp_path / "side-src", THREAD)
    written = import_thread(dst, tmp_path / "side-dst", bundle)
    bystander = dst / "sessions/2026/11/06" / f"rollout-2026-11-06T00-00-00-{OTHER}.jsonl"
    bystander.write_bytes(b"other\n")
    side_bystander = _sidecar(tmp_path / "side-dst", ".turns.jsonl", b"mine\n")
    remove_imported(written)
    for p in written:
        assert not p.exists(), p
    assert bystander.read_bytes() == b"other\n"
    assert side_bystander.read_bytes() == b"mine\n"
    remove_imported(written)                        # already gone: never raises
    remove_imported([dst / "sessions"])             # a directory: never raises


# --- bundle shape ------------------------------------------------------------


def test_bundle_dict_round_trip_and_stripped(src: Path, tmp_path: Path) -> None:
    _seed(src)
    _sidecar(tmp_path / "side", ".jsonl", b"{}\n")
    bundle = export_thread(src, tmp_path / "side", THREAD)
    d = bundle.to_dict()
    assert d["threadId"] == THREAD and d["relPath"] == REL
    assert d["size"] == bundle.size and d["sha256"] == bundle.sha256 and d["data"] == bundle.data
    assert d["sidecars"] == [{"name": f"{THREAD}.jsonl", "size": 3,
                              "sha256": hashlib.sha256(b"{}\n").hexdigest(),
                              "data": base64.b64encode(b"{}\n").decode("ascii")}]
    json.dumps(d)
    assert ThreadBundle.from_dict(d) == bundle
    stripped = bundle.stripped()
    assert stripped.data is None and all(s.data is None for s in stripped.sidecars)
    assert (stripped.thread_id, stripped.rel_path, stripped.size, stripped.sha256) == \
        (bundle.thread_id, bundle.rel_path, bundle.size, bundle.sha256)
    assert ThreadBundle.from_dict(stripped.to_dict()) == stripped
    assert stripped.to_dict()["data"] is None
    assert stripped.stripped() == stripped


@pytest.mark.parametrize("bad", [
    None,
    [],
    {},
    {"threadId": THREAD},
    {"threadId": 7, "relPath": REL, "size": 1, "sha256": "a" * 64, "data": None, "sidecars": []},
    {"threadId": THREAD, "relPath": REL, "size": "1", "sha256": "a" * 64, "data": None, "sidecars": []},
    {"threadId": THREAD, "relPath": REL, "size": True, "sha256": "a" * 64, "data": None, "sidecars": []},
    {"threadId": THREAD, "relPath": REL, "size": -1, "sha256": "a" * 64, "data": None, "sidecars": []},
    {"threadId": THREAD, "relPath": REL, "size": 1, "sha256": "zz", "data": None, "sidecars": []},
    {"threadId": THREAD, "relPath": REL, "size": 1, "sha256": "a" * 64, "data": 5, "sidecars": []},
    {"threadId": THREAD, "relPath": REL, "size": 1, "sha256": "a" * 64, "data": None, "sidecars": {}},
    {"threadId": THREAD, "relPath": REL, "size": 1, "sha256": "a" * 64, "data": None, "sidecars": ["x"]},
    {"threadId": THREAD, "relPath": REL, "size": 1, "sha256": "a" * 64, "data": None,
     "sidecars": [{"name": "n", "size": 1, "sha256": "a" * 64}]},
    {"threadId": THREAD, "relPath": REL, "size": 1, "sha256": "a" * 64, "data": None,
     "sidecars": [{"name": 1, "size": 1, "sha256": "a" * 64, "data": None}]},
])
def test_from_dict_rejects_a_bad_shape(bad) -> None:
    with pytest.raises(ThreadMoveError) as ei:
        ThreadBundle.from_dict(bad)
    assert ei.value.code == "bad_bundle"


def test_thread_move_error_carries_code_and_detail() -> None:
    err = ThreadMoveError("too_large", "140 MB over the 128 MB cap")
    assert err.code == "too_large"
    assert err.detail == "140 MB over the 128 MB cap"
    assert str(err) == "too_large: 140 MB over the 128 MB cap"
    assert isinstance(err, Exception)


def test_module_is_pure() -> None:
    import ast
    import inspect as _inspect
    tree = ast.parse(_inspect.getsource(thread_move))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for banned in ("asyncio", "server.db", "server.sessions", "sqlalchemy"):
        assert not any(m == banned or m.startswith(banned + ".") for m in imported), banned
    assert os.sep == "/"
