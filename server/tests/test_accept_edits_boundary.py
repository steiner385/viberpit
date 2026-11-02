"""acceptEdits auto-approves file writes only inside the session worktree (#3217).

Before the fix, ``acceptEdits`` auto-approved Edit/Write/MultiEdit/NotebookEdit to ANY
path (``~/.bashrc``, another repo, ``.git/hooks``). A write outside the worktree, or into
its ``.git``, must now fall through to the standing-rule check and then a card.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

from server.sessions.permissions import PermissionRegistry, write_target_inside_worktree


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    (root / "sub").mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    return root


def _inside(path, cwd, key="file_path", tool="Write"):
    return write_target_inside_worktree(tool, {key: str(path)}, None if cwd is None else str(cwd))


# ------------------------------------------------------------------ helper: inside


def test_existing_file_inside_is_inside(repo):
    assert _inside(repo / "a.py", repo) is True


def test_new_file_in_new_subdirectory_is_inside(repo):
    assert _inside(repo / "new" / "dir" / "f.py", repo) is True


def test_relative_target_resolves_against_cwd(repo):
    assert _inside("a.py", repo) is True
    assert _inside("new/f.py", repo) is True


def test_notebook_path_key_is_read(repo):
    assert _inside(repo / "nb.ipynb", repo, key="notebook_path", tool="NotebookEdit") is True
    assert _inside("/etc/nb.ipynb", repo, key="notebook_path", tool="NotebookEdit") is False


def test_path_key_is_chosen_by_tool(repo):
    # notebook_path is only NotebookEdit's key; file_path is every other tool's.
    assert _inside(repo / "nb.ipynb", repo, key="notebook_path", tool="Write") is False
    assert _inside(repo / "a.py", repo, key="file_path", tool="NotebookEdit") is False


def test_ambiguous_path_keys_are_outside(repo):
    evil = {"file_path": "a.py", "notebook_path": "/etc/evil"}
    assert write_target_inside_worktree("NotebookEdit", evil, str(repo)) is False
    assert write_target_inside_worktree("Write", evil, str(repo)) is False
    same = {"file_path": str(repo / "n.ipynb"), "notebook_path": str(repo / "n.ipynb")}
    assert write_target_inside_worktree("NotebookEdit", same, str(repo)) is True


# ------------------------------------------------------------------ helper: outside


def test_home_dotfile_is_outside(repo):
    assert _inside(os.path.expanduser("~/.bashrc"), repo) is False
    # An unexpanded tilde is never trusted either.
    assert _inside("~/.bashrc", repo) is False


def test_parent_escape_is_outside(repo):
    assert _inside(f"{repo}/../x", repo) is False


def test_nested_parent_escape_is_outside(repo):
    assert _inside(f"{repo}/sub/../../x", repo) is False


def test_any_dotdot_component_is_outside(repo):
    # The CLI may normalise `..` lexically while realpath resolves symlinks first, so
    # the two readings can disagree about the side of the boundary: never trusted.
    assert _inside(f"{repo}/missing/../a.py", repo) is False
    assert _inside(f"{repo}/sub/../a.py", repo) is False
    assert _inside("missing/../../x", repo) is False


def test_dotdot_that_realpath_keeps_inside_but_lexically_escapes(tmp_path, repo):
    # realpath(<repo>/deep/../../outside) = <repo>/a/outside (inside); a lexical
    # normalisation gives <tmp>/outside (outside). Must not be auto-approved.
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "deep").symlink_to(repo / "a" / "b")
    assert _inside(f"{repo}/deep/../../outside", repo) is False


def test_sibling_with_shared_prefix_is_outside(tmp_path, repo):
    evil = tmp_path / "repo-evil"
    evil.mkdir()
    assert _inside(evil / "f.py", repo) is False
    assert _inside(tmp_path / "repo-evil-new" / "f.py", repo) is False


def test_absolute_path_elsewhere_is_outside(repo):
    assert _inside("/etc/passwd", repo) is False


def test_missing_or_non_string_target_is_outside(repo):
    assert write_target_inside_worktree("Write", {}, str(repo)) is False
    assert write_target_inside_worktree("Write", {"file_path": ""}, str(repo)) is False
    assert write_target_inside_worktree("Write", {"file_path": 3}, str(repo)) is False
    assert write_target_inside_worktree("Write", {"file_path": "a\x00b"}, str(repo)) is False


def test_worktree_root_itself_is_not_inside(repo):
    assert _inside(repo, repo) is False


# ------------------------------------------------------------------ helper: symlinks


def test_symlink_inside_pointing_outside_is_outside(tmp_path, repo):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / "link").symlink_to(elsewhere)
    assert _inside(repo / "link" / "f", repo) is False
    assert _inside(repo / "link" / "new" / "f", repo) is False
    assert _inside("link/f", repo) is False


def test_dangling_symlink_pointing_outside_is_outside(tmp_path, repo):
    # Writing through a dangling link creates its target — outside the worktree.
    (repo / "dangling").symlink_to(tmp_path / "nowhere" / "f")
    assert _inside(repo / "dangling", repo) is False
    (repo / "dangling_dir").symlink_to(tmp_path / "nowhere-dir")
    assert _inside(repo / "dangling_dir" / "f", repo) is False


def test_symlinked_cwd_with_target_under_realpath_is_inside(tmp_path, repo):
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    assert _inside(repo / "a.py", alias) is True
    assert _inside(alias / "new.py", alias) is True
    assert _inside(alias / "a.py", repo) is True


def test_symlink_inside_pointing_inside_is_inside(repo):
    (repo / "inner").symlink_to(repo / "sub")
    assert _inside(repo / "inner" / "f.py", repo) is True


# ------------------------------------------------------------------ helper: .git


def test_git_directory_is_excluded(repo):
    assert _inside(repo / ".git" / "config", repo) is False
    assert _inside(repo / ".git" / "hooks" / "pre-commit", repo) is False
    assert _inside(".git/config", repo) is False


def test_git_file_of_linked_worktree_is_excluded(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    assert _inside(wt / ".git", wt) is False


def test_nested_git_component_is_excluded(repo):
    assert _inside(repo / "sub" / ".git" / "hooks" / "post-checkout", repo) is False


def test_symlink_into_git_is_excluded(repo):
    (repo / "hooks").symlink_to(repo / ".git")
    assert _inside(repo / "hooks" / "config", repo) is False


@pytest.mark.parametrize("rel", [
    ".claude/settings.json", ".claude/settings.local.json", ".claude", ".mcp.json", ".envrc",
    ".husky/pre-commit", ".vscode/settings.json", ".gitconfig", ".gitmodules", ".bashrc",
    ".zshrc", ".zshenv", ".profile", ".bash_profile", ".yarnrc", ".yarnrc.yml", ".pnp.cjs",
    ".ripgreprc", ".claude.json", ".npmrc", ".env", "sub/.envrc", "sub/.claude/x.md",
    ".devcontainer/devcontainer.json", ".pre-commit-config.yaml", ".MCP.JSON",
])
def test_protected_paths_inside_cwd_are_outside(repo, rel):
    # The CLI escalates these in-cwd writes even under acceptEdits; approving them
    # here would undo its safety check.
    assert _inside(repo / rel, repo) is False


def test_git_lookalike_names_are_inside(repo):
    assert _inside(repo / ".gitignore", repo) is True
    assert _inside(repo / ".github" / "workflows" / "ci.yml", repo) is True


# ------------------------------------------------------------------ helper: no cwd


def test_no_cwd_is_never_inside(repo):
    assert _inside(repo / "a.py", None) is False
    assert _inside(repo / "a.py", "") is False


# ------------------------------------------------------------------ registry


def _req(reg, tool, inp, *, mode="acceptEdits", cwd=None):
    async def run():
        pid, fut, _ = await reg.request("s1", "t1", tool, inp, mode=mode,
                                        denylist=PermissionRegistry.DANGEROUS,
                                        identity="me", cwd=cwd)
        verdict = fut.result() if fut.done() else None
        if pid is not None:
            reg.resolve(pid, "deny", "me")
            await fut
        return pid, verdict
    return asyncio.run(run())


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_accept_edits_inside_is_auto_approved(repo, tool, key):
    pid, verdict = _req(PermissionRegistry(), tool, {key: str(repo / "x")}, cwd=str(repo))
    assert pid is None and verdict is True


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"),
                                      ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_accept_edits_outside_pends_a_card(tmp_path, repo, tool, key):
    pid, verdict = _req(PermissionRegistry(), tool, {key: str(tmp_path / "outside")},
                        cwd=str(repo))
    assert pid is not None and verdict is None


def test_accept_edits_git_pends_a_card(repo):
    pid, verdict = _req(PermissionRegistry(), "Write",
                        {"file_path": str(repo / ".git" / "config")}, cwd=str(repo))
    assert pid is not None and verdict is None


@pytest.mark.parametrize("rel", [".claude/settings.json", ".mcp.json", ".envrc",
                                 ".husky/pre-commit"])
def test_accept_edits_protected_in_cwd_pends_a_card(repo, rel):
    pid, verdict = _req(PermissionRegistry(), "Write", {"file_path": str(repo / rel)},
                        cwd=str(repo))
    assert pid is not None and verdict is None


def test_accept_edits_ambiguous_notebook_pends_a_card(repo):
    pid, verdict = _req(PermissionRegistry(), "NotebookEdit",
                        {"file_path": "a.py", "notebook_path": "/etc/evil"}, cwd=str(repo))
    assert pid is not None and verdict is None


def test_account_memory_auto_allow_keys_path_by_tool(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    mem = str(tmp_path / "home" / "cfg" / "acct" / "projects" / "-enc" / "memory" / "n.md")
    # A memory path smuggled in the key the tool does not honour must not auto-allow.
    for tool, inp in [("NotebookEdit", {"file_path": mem, "notebook_path": "/etc/evil"}),
                      ("NotebookEdit", {"file_path": mem}),
                      ("Write", {"notebook_path": mem, "file_path": "/etc/evil"})]:
        pid, verdict = _req(PermissionRegistry(), tool, inp, mode="interactive", cwd=str(repo))
        assert pid is not None and verdict is None, (tool, inp)
    pid, verdict = _req(PermissionRegistry(), "NotebookEdit", {"notebook_path": mem},
                        mode="interactive", cwd=str(repo))
    assert pid is None and verdict is True


def test_accept_edits_without_cwd_pends_a_card(repo):
    pid, verdict = _req(PermissionRegistry(), "Write", {"file_path": str(repo / "a.py")})
    assert pid is not None and verdict is None


def test_accept_edits_outside_logs_info_without_path(tmp_path, repo, caplog):
    secret = str(tmp_path / "secret-name.txt")
    with caplog.at_level(logging.DEBUG, logger="server.sessions.permissions"):
        _req(PermissionRegistry(), "Write", {"file_path": secret}, cwd=str(repo))
    hits = [r for r in caplog.records if "perm.accept_edits.outside_worktree" in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno == logging.INFO
    assert hits[0].getMessage() == "perm.accept_edits.outside_worktree session=s1 tool=Write"
    assert all(r.levelno < logging.WARNING for r in caplog.records)


def test_accept_edits_inside_does_not_log_boundary(repo, caplog):
    with caplog.at_level(logging.DEBUG, logger="server.sessions.permissions"):
        _req(PermissionRegistry(), "Write", {"file_path": str(repo / "a.py")}, cwd=str(repo))
    assert not [r for r in caplog.records
                if "perm.accept_edits.outside_worktree" in r.getMessage()]


class _GateStub:
    """A standing-rule gate that covers exactly one (tool, path) pair."""

    def __init__(self, tool, path):
        self.tool, self.path, self.calls = tool, path, []

    async def match(self, session_id, tool, input):
        self.calls.append((session_id, tool, input))
        return object() if (tool, input.get("file_path")) == (self.tool, self.path) else None

    def suggestions(self, session_id, tool, input):
        return []


def test_accept_edits_outside_honours_standing_rule(tmp_path, repo):
    target = str(tmp_path / "shared" / "notes.md")
    reg = PermissionRegistry()
    reg.rule_gate = _GateStub("Write", target)  # type: ignore[assignment]
    pid, verdict = _req(reg, "Write", {"file_path": target}, cwd=str(repo))
    assert pid is None and verdict is True
    assert reg.rule_gate.calls == [("s1", "Write", {"file_path": target})]


def test_accept_edits_inside_does_not_consult_rules(repo):
    reg = PermissionRegistry()
    reg.rule_gate = _GateStub("Write", "/never")  # type: ignore[assignment]
    pid, verdict = _req(reg, "Write", {"file_path": str(repo / "a.py")}, cwd=str(repo))
    assert pid is None and verdict is True
    assert reg.rule_gate.calls == []


def test_account_memory_write_outside_cwd_still_auto_approved(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    mem = str(tmp_path / "home" / "cfg" / "acct" / "projects" / "-enc" / "memory" / "n.md")
    assert write_target_inside_worktree("Write", {"file_path": mem}, str(repo)) is False
    pid, verdict = _req(PermissionRegistry(), "Write", {"file_path": mem}, cwd=str(repo))
    assert pid is None and verdict is True


def test_accept_edits_bash_still_prompts(repo):
    pid, verdict = _req(PermissionRegistry(), "Bash", {"command": "ls"}, cwd=str(repo))
    assert pid is not None and verdict is None


@pytest.mark.parametrize("mode", ["full-auto", "bypassPermissions"])
@pytest.mark.parametrize("cwd_kind", ["repo", "none"])
def test_full_auto_and_bypass_unchanged(tmp_path, repo, mode, cwd_kind):
    cwd = str(repo) if cwd_kind == "repo" else None
    for inp in ({"file_path": str(tmp_path / "outside")},
                {"file_path": str(repo / ".git" / "config")},
                {"file_path": os.path.expanduser("~/.bashrc")}):
        pid, verdict = _req(PermissionRegistry(), "Write", inp, mode=mode, cwd=cwd)
        assert pid is None and verdict is True


def test_interactive_inside_still_prompts(repo):
    pid, verdict = _req(PermissionRegistry(), "Write", {"file_path": str(repo / "a.py")},
                        mode="interactive", cwd=str(repo))
    assert pid is not None and verdict is None


# ------------------------------------------------------------------ driver plumbing


class _RecordingRegistry(PermissionRegistry):
    def __init__(self):
        super().__init__()
        self.kwargs: list[dict] = []

    async def request(self, *args, **kwargs):
        self.kwargs.append(kwargs)
        return await super().request(*args, **kwargs)


def test_claude_driver_passes_cwd_to_registry(tmp_path, repo):
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.tests.fakes.fake_sdk import FakeSDKClient

    reg = _RecordingRegistry()

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([])

    async def run():
        d = ClaudeAgentDriver("pcwd", "primary", str(repo), str(tmp_path / "cfg"),
                              mode="acceptEdits", sdk_factory=factory, registry=reg,
                              identity="me")
        inside = await d._can_use_tool("Write", {"file_path": str(repo / "a.py")})
        task = asyncio.create_task(
            d._can_use_tool("Write", {"file_path": str(tmp_path / "outside.txt")}))
        for _ in range(200):
            if reg.pending(d.session_id):
                break
            await asyncio.sleep(0.005)
        pending = reg.pending(d.session_id)
        reg.deny_all(d.session_id)
        outside = await asyncio.wait_for(task, timeout=2.0)
        await d.aclose()
        return inside, pending, outside

    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
    inside, pending, outside = asyncio.run(run())
    assert [k.get("cwd") for k in reg.kwargs] == [str(repo), str(repo)]
    assert isinstance(inside, PermissionResultAllow)
    assert len(pending) == 1
    assert isinstance(outside, PermissionResultDeny)


# ------------------------------------------------------------------ shared resolution


def test_rule_matcher_uses_the_same_tool_keyed_resolution(repo):
    from server.permission_rules.patterns import matches, parse

    p = parse("Edit(**)")
    cwd = str(repo)
    assert matches(p, "Write", {"file_path": str(repo / "a.py")}, cwd) is True
    assert matches(p, "NotebookEdit", {"file_path": "a.py", "notebook_path": "/etc/evil"},
                   cwd) is False
    assert matches(p, "NotebookEdit", {"file_path": "a.py"}, cwd) is False
    assert matches(p, "Write", {"file_path": "~/notes.txt"}, cwd) is False
    assert matches(p, "Write", {"file_path": str(repo / ".gitmodules")}, cwd) is False
