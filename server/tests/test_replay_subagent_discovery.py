"""Sub-agent sidecar discovery must be bounded (RC2) — and must not pay the bound
on every call (#2178).

The original implementation globbed `*/<sid>/subagents/*.meta.json` across every
project directory — 1,696 of them on the live fleet, over an sshfs mount at
~54ms RTT — which hung transcript_window for 30s+ and made sessions
un-openable. Discovery must degrade to a partial result instead of hanging.

#2178: the bound alone is not enough. The fleet grew to ~3,900 project dirs, so
a full sweep costs ~2.0s against a 1.0s budget — the budget became *unmeetable*,
`complete` was permanently False, the discovery cache therefore never filled, and
every transcript window re-paid ~1.0s of sshfs probes. The census that settled it:
all 401 `subagents/` dirs on the real store sit under the SAME encoded-cwd as their
own `<sid>.jsonl`, i.e. under the `preferred` dir the fast path probes in full
before the sweep ever starts. So a fast-path hit is authoritative: stop sweeping.
"""
import logging
import os
import re
import time
from pathlib import Path

from server.sessions import replay
from server.sessions.replay import _find_subagent_meta_files


def _layout(root: Path, sid: str, *, cwd_dirs: list[str], sub_in: str) -> Path:
    """Build projects/<cwd_dirs...>/ with the sidecar under `sub_in`."""
    projects = root / "projects"
    for d in cwd_dirs:
        (projects / d).mkdir(parents=True, exist_ok=True)
    sub = projects / sub_in / sid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-1.meta.json").write_text('{"toolUseId": "toolu_task"}')
    return projects


class _ScandirStandIn:
    """`os.scandir` stand-in yielding entries in a caller-chosen order.

    `_find_subagent_meta_files` uses the scandir result as BOTH a context manager
    and an iterator, so the stand-in has to be both.
    """

    def __init__(self, entries):
        self._it = iter(entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)


def _scandir_visiting_first(monkeypatch, first: str):
    """Pin the sweep's visit order so "found before truncation" is deterministic.

    Real `os.scandir` order is the filesystem's, which makes any "the sweep found
    X before running out of budget" assertion a coin flip.
    """
    real_scandir = os.scandir

    def ordered(path):
        with real_scandir(path) as it:
            entries = list(it)
        entries.sort(key=lambda e: (e.name != first, e.name))
        return _ScandirStandIn(entries)

    monkeypatch.setattr(replay.os, "scandir", ordered)


def _counting_scandir(monkeypatch) -> dict:
    """Patch `os.scandir` with a call counter. Returns the counter dict."""
    calls = {"n": 0}
    real_scandir = os.scandir

    def counting(path):
        calls["n"] += 1
        return real_scandir(path)

    monkeypatch.setattr(replay.os, "scandir", counting)
    return calls


def _slow_is_dir(monkeypatch, seconds: float = 0.05):
    """Stand in for one sshfs round trip per directory probe."""
    real_is_dir = Path.is_dir

    def slow(self):
        time.sleep(seconds)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow)


def _scan_log(caplog) -> logging.LogRecord:
    return next(r for r in caplog.records if "subagent scan" in r.getMessage())


def test_finds_sidecar_in_the_sessions_own_project_dir(tmp_path):
    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s1", cwd_dirs=["-cwd-main", "-cwd-other"], sub_in="-cwd-main")
    metas, complete = _find_subagent_meta_files(projects, "s1", preferred=projects / "-cwd-main")
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is True


def test_finds_sidecar_under_a_different_cwd(tmp_path):
    """The cross-cwd join (main checkout vs. worktree) must keep working."""
    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s2", cwd_dirs=["-cwd-main", "-cwd-worktree"], sub_in="-cwd-worktree")
    metas, complete = _find_subagent_meta_files(projects, "s2", preferred=projects / "-cwd-main")
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is True


def test_missing_sidecar_returns_empty_not_error(tmp_path):
    replay._subagent_cache_clear()
    projects = tmp_path / "projects"
    (projects / "-cwd-main").mkdir(parents=True)
    metas, complete = _find_subagent_meta_files(projects, "nope", preferred=projects / "-cwd-main")
    assert metas == []
    assert complete is True


def test_slow_filesystem_degrades_instead_of_hanging(tmp_path, monkeypatch):
    """THE regression test for RC2.

    Simulate the sshfs round-trip cost. With a 0.2s budget and 50ms per
    directory probe, discovery must return in well under the time a full
    traversal would take, and report itself incomplete.

    The sidecar deliberately sits OUTSIDE the preferred dir: a fast-path hit
    short-circuits the sweep entirely (#2178), so only a fast-path MISS can
    exercise the budget at all.
    """
    replay._subagent_cache_clear()
    projects = _layout(
        tmp_path, "s3",
        cwd_dirs=[f"-cwd-{i}" for i in range(60)] + ["-cwd-main"],
        sub_in="-cwd-59",
    )

    _slow_is_dir(monkeypatch)

    started = time.monotonic()
    metas, complete = _find_subagent_meta_files(
        projects, "s3", preferred=projects / "-cwd-main", budget_s=0.2)
    elapsed = time.monotonic() - started

    # A full 61-directory traversal would cost ~3s. The budget must cut it short.
    assert elapsed < 1.0, f"discovery took {elapsed:.2f}s — budget not enforced"
    assert complete is False, "a truncated scan must report itself incomplete"


def test_fast_path_hit_skips_the_cross_cwd_sweep(tmp_path, monkeypatch):
    """#2178, the core fix.

    Every `subagents/` dir on the real store (401 of 401) sits under the same
    encoded-cwd as its own transcript — the `preferred` dir, probed in full by the
    fast path. When that probe answers, sweeping the other ~3,900 dirs costs ~1s of
    sshfs round trips and has never once contributed a file.
    """
    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s7", cwd_dirs=["-cwd-main", "-cwd-other"], sub_in="-cwd-main")
    calls = _counting_scandir(monkeypatch)

    metas, complete = _find_subagent_meta_files(projects, "s7", preferred=projects / "-cwd-main")

    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is True, "the fast path answered in full — the result is authoritative"
    assert calls["n"] == 0, "the preferred dir already answered; sweeping every cwd is pure cost"


def test_fast_path_result_is_cached(tmp_path, monkeypatch):
    """#2178: a fast-path answer must fill the cache.

    Only a `complete` full sweep was cached, and the tree here is big enough and slow
    enough that the sweep cannot finish inside the budget — exactly the live fleet's
    ~3,900 dirs against 1.0s. So the old code re-paid the whole scan on every window.
    """
    replay._subagent_cache_clear()
    projects = _layout(
        tmp_path, "s9",
        cwd_dirs=[f"-cwd-{i}" for i in range(60)] + ["-cwd-main"],
        sub_in="-cwd-main",
    )
    _slow_is_dir(monkeypatch)

    first, _ = _find_subagent_meta_files(
        projects, "s9", preferred=projects / "-cwd-main", budget_s=0.2)
    assert [p.name for p in first] == ["agent-1.meta.json"]
    monkeypatch.undo()

    probes = {"n": 0}
    real_is_dir = Path.is_dir

    def counting_is_dir(self):
        probes["n"] += 1
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", counting_is_dir)
    second, complete = _find_subagent_meta_files(
        projects, "s9", preferred=projects / "-cwd-main", budget_s=0.2)

    assert second == first
    assert complete is True
    assert probes["n"] == 0, "the second lookup must be served from cache, not re-probed"


def test_partial_read_of_the_preferred_dir_does_not_short_circuit(tmp_path, monkeypatch):
    """The fast-path short-circuit keys off `found`, but `_collect` extends `found`
    from a lazy `iterdir()` generator — an OSError partway through leaves SOME entries
    behind. Short-circuiting on that would return a silently-short list AND cache it
    for the whole TTL, which is precisely the "never cache a partial result" guard the
    sweep has always honoured."""
    replay._subagent_cache_clear()
    projects = tmp_path / "projects"
    sub = projects / "-cwd-main" / "s11" / "subagents"
    sub.mkdir(parents=True)
    for name in ("agent-1.meta.json", "agent-2.meta.json"):
        (sub / name).write_text('{"toolUseId": "toolu_task"}')

    real_iterdir = Path.iterdir

    def flaky_iterdir(self):
        if self.name == "subagents":
            yield next(iter(real_iterdir(self)))
            raise OSError("stale sshfs handle mid-read")
        yield from real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", flaky_iterdir)

    metas, complete = _find_subagent_meta_files(projects, "s11", preferred=projects / "-cwd-main")

    assert len(metas) == 1, "the fixture must actually produce a short read"
    assert complete is False, "a half-read directory is not an authoritative answer"

    monkeypatch.undo()
    again, complete2 = _find_subagent_meta_files(projects, "s11", preferred=projects / "-cwd-main")
    assert [p.name for p in again] == ["agent-1.meta.json", "agent-2.meta.json"], \
        "the short read must not have been cached"
    assert complete2 is True


def test_cache_is_keyed_by_the_preferred_dir(tmp_path):
    """#2178 invariant update.

    Caching a fast-path result makes the entry preferred-DEPENDENT: it reports what
    one particular dir holds, not what the whole tree holds. `preferred` must
    therefore be part of the cache key, or the first caller's answer is served to
    every other caller.
    """
    replay._subagent_cache_clear()
    projects = tmp_path / "projects"
    for d, name in (("-cwd-a", "agent-a.meta.json"), ("-cwd-b", "agent-b.meta.json")):
        sub = projects / d / "s8" / "subagents"
        sub.mkdir(parents=True)
        (sub / name).write_text('{"toolUseId": "toolu_task"}')

    a, _ = _find_subagent_meta_files(projects, "s8", preferred=projects / "-cwd-a")
    b, _ = _find_subagent_meta_files(projects, "s8", preferred=projects / "-cwd-b")

    assert [p.name for p in a] == ["agent-a.meta.json"]
    assert [p.name for p in b] == ["agent-b.meta.json"], \
        "the -cwd-a entry must not be served to a caller whose preferred dir is -cwd-b"


def test_flaky_entry_during_scan_does_not_abort_and_reports_incomplete(tmp_path, monkeypatch):
    """A per-entry OSError mid-scan (a stale/flaky sshfs handle on exactly one of
    the ~3,900 project dirs is the plausible real-world trigger) must not abort the
    sweep, and the scan must NOT be reported as complete=True — a falsely-True
    complete flag would defeat the memoization guard (never cache a partial result)."""
    replay._subagent_cache_clear()
    projects = _layout(
        tmp_path, "s4",
        cwd_dirs=["-cwd-flaky", "-cwd-main"],
        sub_in="-cwd-other",
    )

    real_is_dir = os.DirEntry.is_dir

    def flaky_is_dir(self):
        if self.name == "-cwd-flaky":
            raise OSError("stale sshfs handle")
        return real_is_dir(self)

    monkeypatch.setattr(os.DirEntry, "is_dir", flaky_is_dir)

    metas, complete = _find_subagent_meta_files(projects, "s4", preferred=projects / "-cwd-main")

    # The sweep still reached the cross-cwd sidecar.
    assert [p.name for p in metas] == ["agent-1.meta.json"]
    # But it hit an error on one entry, so it cannot be trusted as exhaustive.
    assert complete is False


def test_collect_oserror_on_preferred_dir_marks_incomplete(tmp_path, monkeypatch):
    """A per-directory OSError raised inside `_collect` itself (e.g. `sub.is_dir()`
    failing on a stale sshfs handle for the probed `<cwd>/<sid>/subagents` dir) must
    mark the overall result incomplete — not be silently swallowed as if that
    directory had been fully scanned. A falsely-True `complete` here would let a
    wrong answer live for the whole cache TTL."""
    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s6", cwd_dirs=["-cwd-main"], sub_in="-cwd-main")

    real_is_dir = Path.is_dir

    def raising_is_dir(self):
        if self.name == "subagents":
            raise OSError("stale sshfs handle")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", raising_is_dir)

    metas, complete = _find_subagent_meta_files(projects, "s6", preferred=projects / "-cwd-main")

    assert metas == []
    assert complete is False


def test_second_lookup_does_not_rescan(tmp_path, monkeypatch):
    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s5", cwd_dirs=["-cwd-main", "-cwd-b"], sub_in="-cwd-b")

    calls = _counting_scandir(monkeypatch)

    a, _ = _find_subagent_meta_files(projects, "s5", preferred=projects / "-cwd-main")
    after_first = calls["n"]
    b, _ = _find_subagent_meta_files(projects, "s5", preferred=projects / "-cwd-main")

    assert a == b
    assert after_first >= 1, "the first lookup should have swept (the fast path found nothing)"
    assert calls["n"] == after_first, "the second lookup must be served from cache"


def test_partial_results_are_not_cached(tmp_path, monkeypatch):
    """A budget-truncated scan must not become permanent."""
    replay._subagent_cache_clear()
    projects = _layout(
        tmp_path, "s10",
        cwd_dirs=[f"-cwd-{i}" for i in range(60)] + ["-cwd-main"],
        sub_in="-cwd-59",
    )

    _slow_is_dir(monkeypatch)
    _, complete = _find_subagent_meta_files(
        projects, "s10", preferred=projects / "-cwd-main", budget_s=0.2)
    assert complete is False

    monkeypatch.undo()
    calls = _counting_scandir(monkeypatch)
    _, complete2 = _find_subagent_meta_files(projects, "s10", preferred=projects / "-cwd-main")

    assert calls["n"] >= 1, "a partial result must not have been cached"
    assert complete2 is True


# ---------------------------------------------------------------------------
# Truncation logging
#
# Severity keys off EVIDENCE that sub-agents exist, not off the fast path coming
# up empty: on the live fleet most sessions have no `subagents/` dir at all, so
# warning-on-empty floods the log on every transcript request.
#
# #2178 adds truthfulness: the line interpolated the budget CONSTANT as if it were
# an elapsed time, and said "budget exhausted" for every incompleteness cause —
# including filesystem errors that happen in milliseconds and have nothing to do
# with a budget.
# ---------------------------------------------------------------------------


def _truncating_layout(tmp_path, monkeypatch, sid, *, sidecar_in: str | None):
    """Force a budget-truncated sweep, with or without a discoverable sidecar.

    The sidecar never goes in the preferred dir: a fast-path hit short-circuits the
    sweep, so it could not truncate.
    """
    replay._subagent_cache_clear()
    projects = tmp_path / "projects"
    for i in range(60):
        (projects / f"-cwd-{i}").mkdir(parents=True, exist_ok=True)
    (projects / "-cwd-main").mkdir(parents=True, exist_ok=True)
    if sidecar_in is not None:
        sub = projects / sidecar_in / sid / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "agent-1.meta.json").write_text('{"toolUseId": "toolu_task"}')
        _scandir_visiting_first(monkeypatch, sidecar_in)

    _slow_is_dir(monkeypatch)
    return projects


def test_truncated_scan_with_no_sidecars_does_not_warn(tmp_path, monkeypatch, caplog):
    """The steady state on the live fleet: nothing found, sweep truncated.

    No evidence sub-agents exist at all → debug, NOT warning. A warning here
    fires on every session open and every scrollback page — a pure log flood.
    """
    projects = _truncating_layout(tmp_path, monkeypatch, "s-quiet", sidecar_in=None)
    caplog.set_level(logging.DEBUG, logger="server.sessions.replay")

    metas, complete = _find_subagent_meta_files(
        projects, "s-quiet", preferred=projects / "-cwd-main", budget_s=0.2)

    assert metas == []
    assert complete is False, "the scan must actually have truncated for this test to mean anything"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"truncation with no sidecars must not warn, got: {[r.getMessage() for r in warnings]}"
    assert _scan_log(caplog).levelno == logging.DEBUG, "it should still be visible at debug"


def test_truncated_scan_with_sidecars_found_warns(tmp_path, monkeypatch, caplog):
    """Sidecars were found by the SWEEP and the sweep was then cut short — the
    cross-cwd directories never reached may hold more. That IS actionable, so warn.

    Note this is now the only path to the warning: when the sidecar sits in the
    preferred dir (401 of 401 real ones do) the fast path short-circuits and there
    is no truncation to report.
    """
    projects = _truncating_layout(tmp_path, monkeypatch, "s-loud", sidecar_in="-cwd-0")
    caplog.set_level(logging.DEBUG, logger="server.sessions.replay")

    metas, complete = _find_subagent_meta_files(
        projects, "s-loud", preferred=projects / "-cwd-main", budget_s=0.2)

    assert [p.name for p in metas] == ["agent-1.meta.json"]
    assert complete is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "a truncated sweep that DID find sidecars must warn exactly once"
    assert "may be incomplete" in warnings[0].getMessage()


def test_truncation_log_reports_measured_elapsed_not_the_budget_constant(
        tmp_path, monkeypatch, caplog):
    """#2178 secondary defect: the line interpolated `budget`, the constant.

    "after 1.00s" was a fixed string that happened to look like a measurement, so
    the log could never distinguish a slow mount from an instant failure.
    """
    replay._subagent_cache_clear()
    projects = tmp_path / "projects"
    for d in ("-cwd-main", "-cwd-a", "-cwd-b"):
        (projects / d).mkdir(parents=True)
    _slow_is_dir(monkeypatch, seconds=0.10)
    caplog.set_level(logging.DEBUG, logger="server.sessions.replay")

    _, complete = _find_subagent_meta_files(
        projects, "s-timed", preferred=projects / "-cwd-main", budget_s=0.02)

    assert complete is False
    message = _scan_log(caplog).getMessage()
    reported = float(re.search(r"after ([0-9.]+)s", message).group(1))
    assert reported >= 0.08, (
        f"the log must report measured elapsed time, not the 0.02s budget constant "
        f"(reported {reported}s, message: {message})")


def test_filesystem_error_is_not_reported_as_budget_exhaustion(tmp_path, monkeypatch, caplog):
    """#2178 secondary defect: every incompleteness cause claimed "budget exhausted".

    A stale handle fails in milliseconds; reporting it as a 1.00s budget overrun
    sends whoever reads the log looking for a slow mount that isn't there.
    """
    replay._subagent_cache_clear()
    projects = _layout(tmp_path, "s-oserr", cwd_dirs=["-cwd-flaky", "-cwd-main"], sub_in="-cwd-main")

    real_is_dir = os.DirEntry.is_dir

    def flaky_is_dir(self):
        if self.name == "-cwd-flaky":
            raise OSError("stale sshfs handle")
        return real_is_dir(self)

    monkeypatch.setattr(os.DirEntry, "is_dir", flaky_is_dir)
    caplog.set_level(logging.DEBUG, logger="server.sessions.replay")

    # preferred is empty, so the sweep runs and trips over the flaky entry.
    _, complete = _find_subagent_meta_files(projects, "s-oserr-miss",
                                            preferred=projects / "-cwd-main")

    assert complete is False
    message = _scan_log(caplog).getMessage()
    assert "filesystem error" in message
    assert "deadline" not in message, f"no deadline was hit: {message}"


def test_unreadable_projects_root_is_logged_as_such(tmp_path, monkeypatch, caplog):
    """An account dir that was renamed or removed used to read as "budget exhausted
    after 1.00s" — the one report that named a `projects_root` which no longer
    existed on disk sent triage chasing a slow mount."""
    replay._subagent_cache_clear()
    projects = tmp_path / "projects"
    (projects / "-cwd-main").mkdir(parents=True)

    def gone(path):
        raise OSError("no such directory")

    monkeypatch.setattr(replay.os, "scandir", gone)
    caplog.set_level(logging.DEBUG, logger="server.sessions.replay")

    metas, complete = _find_subagent_meta_files(projects, "s-gone",
                                                preferred=projects / "-cwd-main")

    assert metas == []
    assert complete is False
    assert "projects_root unreadable" in _scan_log(caplog).getMessage()
