"""Boot-time reap of `claude` CLIs a previous daemon incarnation left behind (#1379).

The other half of the duplicate-owner fix. The per-session lease stops a second
daemon from STARTING a twin, but an orphan that outlived its unit holds no lease
and answers to no daemon — the journal records one on every deploy::

    systemd: ccswitch-daemon.service: Unit process 4041937 (claude) remains
             running after unit stopped.

It keeps editing files and can push, invisibly to the UI and to the daemon that
resumes the same conversation moments later.

Discovery is pure given an injected process lister, so these tests exercise the
real classification with no ``/proc`` and nothing is ever signalled — the reaping
tests inject a recording ``terminate``.
"""
from __future__ import annotations

from server.sessions import orphan_cli
from server.sessions.adopt import ProcInfo

CFG_A = "/cfg/tony_cairnea.com"
CFG_B = "/cfg/tony.claude_steinonline.net"
KNOWN = ["acct-a", "acct-b"]
CONV = "f9eaab62-4465-4651-8425-f42645b92ef7"

DAEMON_PID = 2756056
EDGE_PID = 991000


def _daemon(pid: int = DAEMON_PID, ppid: int = 1) -> ProcInfo:
    """A live ccswitch process that owns CLIs — this daemon, or a co-located edge."""
    return ProcInfo(pid=pid, cmdline=["python", "-m", "server.ipc"], environ={},
                    cwd="/srv", ppid=ppid)


def _account_cfg(name: str) -> str:
    return {"acct-a": CFG_A, "acct-b": CFG_B}[name]


def _sdk(pid: int, ppid: int, *, cfg: str = CFG_A, resume: str | None = CONV,
         cwd: str = "/wt/79dc2220") -> ProcInfo:
    """An SDK-protocol claude — the shape ccswitch's own driver runs as."""
    argv = ["claude", "--input-format", "stream-json", "--output-format", "stream-json"]
    if resume:
        argv += ["--resume", resume]
    return ProcInfo(pid=pid, cmdline=argv, environ={"CLAUDE_CONFIG_DIR": cfg},
                    cwd=cwd, ppid=ppid)


def _find(procs, **kw):
    return orphan_cli.discover_orphans(
        KNOWN, self_pid=DAEMON_PID, proc_lister=lambda: procs,
        account_cfg=_account_cfg, **kw)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_survivor_of_the_previous_daemon_is_an_orphan():
    """Reparented to init after its unit stopped — the exact journal shape."""
    found = _find([_sdk(4041937, ppid=1)])
    assert [(o.pid, o.account, o.resume) for o in found] == [(4041937, "acct-a", CONV)]


def test_our_own_children_are_never_orphans():
    """Direct child and grandchild (a CLI the CLI itself spawned) of this daemon."""
    procs = [_daemon(), _sdk(3026491, ppid=DAEMON_PID), _sdk(3026492, ppid=3026491)]
    assert _find(procs) == []


def test_a_co_located_live_process_keeps_its_own_clis():
    """The reap predicate is "nothing alive owns it", NOT "not descended from me".
    An edge running its own in-process orchestrator on this box owns CLIs too, and
    a daemon restart must not machine-gun them."""
    procs = [
        _daemon(),                                   # us
        _daemon(pid=EDGE_PID),                       # a co-located ccswitch process
        _sdk(991001, ppid=EDGE_PID),                 # its live CLI — hands off
        _sdk(4041937, ppid=1),                       # a genuine orphan
    ]
    assert [o.pid for o in _find(procs)] == [4041937]


def test_a_cli_whose_parent_exited_between_snapshot_and_walk_is_an_orphan():
    """A /proc snapshot is not atomic: the parent can be gone by the time we look."""
    assert [o.pid for o in _find([_sdk(4041937, ppid=777777)])] == [4041937]


def test_a_humans_interactive_claude_is_left_alone():
    """`classify` calls an interactive CLI a 'session', not an 'sdk' driver — it is
    a person's terminal, not a leftover of ours, and must never be signalled."""
    procs = [ProcInfo(pid=900, cmdline=["claude", "agents"],
                      environ={"CLAUDE_CONFIG_DIR": CFG_A}, cwd="/home/tony", ppid=1)]
    assert _find(procs) == []


def test_internal_helpers_are_left_alone():
    helpers = [
        ProcInfo(pid=901, cmdline=["claude", "--bg-pty-host"],
                 environ={"CLAUDE_CONFIG_DIR": CFG_A}, cwd="/x", ppid=1),
        ProcInfo(pid=902, cmdline=["claude", "--bg-spare", "/s"],
                 environ={"CLAUDE_CONFIG_DIR": CFG_A}, cwd="/x", ppid=1),
        ProcInfo(pid=903, cmdline=["claude", "auth", "login"],
                 environ={"CLAUDE_CONFIG_DIR": CFG_A}, cwd="/x", ppid=1),
    ]
    assert _find(helpers) == []


def test_a_claude_on_an_unmanaged_config_dir_is_left_alone():
    """A developer's own SDK agent on the same box is not ours to kill."""
    procs = [_sdk(910, ppid=1, cfg="/home/someone/.claude")]
    assert _find(procs) == []


def test_a_cycle_in_the_parent_chain_terminates():
    """A /proc snapshot is not atomic: a pid can be reparented mid-walk. The walk
    must terminate rather than spin, and the orphan is still reported."""
    procs = [_sdk(10, ppid=11), _sdk(11, ppid=10)]
    assert {o.pid for o in _find(procs)} == {10, 11}


def test_resume_id_is_extracted_in_both_argv_forms():
    """The conversation id is the evidence that makes an orphan diagnosable —
    the incident was only readable because someone ran pgrep by hand."""
    joined = ProcInfo(pid=20, cmdline=["claude", "--input-format", "stream-json",
                                       f"--resume={CONV}"],
                      environ={"CLAUDE_CONFIG_DIR": CFG_A}, cwd="/wt", ppid=1)
    assert _find([joined])[0].resume == CONV
    assert _find([_sdk(21, ppid=1, resume=None)])[0].resume is None


def test_enumeration_failure_never_breaks_the_boot_path():
    def boom():
        raise OSError("/proc unreadable")
    assert orphan_cli.discover_orphans(KNOWN, self_pid=DAEMON_PID, proc_lister=boom,
                                       account_cfg=_account_cfg) == []


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------


def test_reap_terminates_only_the_orphans(monkeypatch):
    monkeypatch.delenv("CCSWITCH_REAP_ORPHAN_CLIS", raising=False)
    killed: list[int] = []
    procs = [
        _daemon(),
        _sdk(4041937, ppid=1),                     # orphan
        _sdk(3026491, ppid=DAEMON_PID),            # ours
        _sdk(910, ppid=1, cfg="/home/someone/.claude"),  # not ccswitch's
    ]
    reaped = orphan_cli.reap_orphans(
        KNOWN, self_pid=DAEMON_PID, proc_lister=lambda: procs,
        account_cfg=_account_cfg,
        terminate=lambda pid: (killed.append(pid), True)[1])
    assert killed == [4041937]
    assert [o.pid for o in reaped] == [4041937]


def test_a_process_that_already_exited_is_not_reported_as_reaped(monkeypatch):
    """terminate_process_tree returns False for a pid that is already gone."""
    monkeypatch.delenv("CCSWITCH_REAP_ORPHAN_CLIS", raising=False)
    reaped = orphan_cli.reap_orphans(
        KNOWN, self_pid=DAEMON_PID, proc_lister=lambda: [_sdk(4041937, ppid=1)],
        account_cfg=_account_cfg, terminate=lambda pid: False)
    assert reaped == []


def test_one_failed_kill_does_not_stop_the_sweep(monkeypatch):
    monkeypatch.delenv("CCSWITCH_REAP_ORPHAN_CLIS", raising=False)
    killed: list[int] = []

    def flaky(pid):
        if pid == 1000:
            raise PermissionError("not yours")
        killed.append(pid)
        return True

    procs = [_sdk(1000, ppid=1), _sdk(1001, ppid=1)]
    reaped = orphan_cli.reap_orphans(
        KNOWN, self_pid=DAEMON_PID, proc_lister=lambda: procs,
        account_cfg=_account_cfg, terminate=flaky)
    assert killed == [1001] and [o.pid for o in reaped] == [1001]


def test_kill_switch_disables_the_reap(monkeypatch):
    monkeypatch.setenv("CCSWITCH_REAP_ORPHAN_CLIS", "false")
    killed: list[int] = []
    reaped = orphan_cli.reap_orphans(
        KNOWN, self_pid=DAEMON_PID, proc_lister=lambda: [_sdk(4041937, ppid=1)],
        account_cfg=_account_cfg,
        terminate=lambda pid: (killed.append(pid), True)[1])
    assert killed == [] and reaped == []


def test_ppid_is_read_from_a_real_proc_stat():
    """`_read_ppid` parses /proc/<pid>/stat, whose comm field can contain spaces and
    ')' — pinned against this process rather than a fixture so a kernel format
    assumption cannot silently rot."""
    import os

    from server.sessions.adopt import _read_ppid
    assert _read_ppid(f"/proc/{os.getpid()}") == os.getppid()
    assert _read_ppid("/proc/does-not-exist") == 0
