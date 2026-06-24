import asyncio, time
from server.sessions.permissions import PermissionRegistry

# ---------------------------------------------------------------------------
# Regression tests for issue #5 — "Ask each time" mode must prompt for
# Write/Edit/Bash even after an "always" grant, and must not auto-allow them
# in interactive mode.  These tests are written to FAIL before the fix.
# ---------------------------------------------------------------------------

def test_interactive_write_always_prompts():
    """interactive mode must prompt for Write even after a prior 'always' grant."""
    reg = PermissionRegistry()
    async def run():
        # First call: approve with "always" to seed the grant cache.
        pid1, fut1 = await reg.request("s1", "t1", "Write", {"file_path": "/tmp/out.txt"},
                                       mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                       allowlist=True, identity="me")
        assert pid1 is not None, "first Write should prompt in interactive mode"
        reg.resolve(pid1, "always", "me")
        await fut1

        # Second call with same fingerprint: interactive mode must still prompt.
        pid2, fut2 = await reg.request("s1", "t2", "Write", {"file_path": "/tmp/out.txt"},
                                       mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                       allowlist=True, identity="me")
        assert pid2 is not None, "Write must prompt again in interactive mode even after 'always'"
        reg.resolve(pid2, "allow", "me")
        return await fut2
    assert asyncio.run(run()) is True


def test_interactive_bash_always_prompts():
    """interactive mode must prompt for Bash even after a prior 'always' grant."""
    reg = PermissionRegistry()
    async def run():
        cmd = "echo hello"
        # Seed "always" grant.
        pid1, fut1 = await reg.request("s1", "t1", "Bash", {"command": cmd, "cwd": "/tmp"},
                                       mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                       allowlist=True, identity="me")
        assert pid1 is not None
        reg.resolve(pid1, "always", "me")
        await fut1

        # Must still prompt on the next call.
        pid2, fut2 = await reg.request("s1", "t2", "Bash", {"command": cmd, "cwd": "/tmp"},
                                       mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                       allowlist=True, identity="me")
        assert pid2 is not None, "Bash must prompt again in interactive mode even after 'always'"
        reg.resolve(pid2, "allow", "me")
        return await fut2
    assert asyncio.run(run()) is True



def test_allowlist_mode_prompts_for_todoswrite():
    """allowlist mode ('Allowlist read-only, prompt the rest') must prompt for TodoWrite."""
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "TodoWrite", {"todos": []},
                                     mode="allowlist", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        if pid is not None:
            reg.resolve(pid, "allow", "me")
            await fut
        return pid
    pid = asyncio.run(run())
    assert pid is not None, "TodoWrite must prompt in allowlist mode (not read-only)"


def test_allowlist_mode_auto_allows_read_only():
    """allowlist mode auto-allows strictly read-only tools (Read, Grep, Glob, LS)."""
    reg = PermissionRegistry()
    async def run():
        results = {}
        for tool, inp in [("Read", {"file_path": "/a"}),
                          ("Grep", {"pattern": "x", "path": "/"}),
                          ("Glob", {"pattern": "*.py"}),
                          ("LS", {"path": "/"})]:
            pid, fut = await reg.request("s1", "t1", tool, inp,
                                         mode="allowlist", denylist=PermissionRegistry.DANGEROUS,
                                         allowlist=True, identity="me")
            results[tool] = (pid, await fut)
        return results
    results = asyncio.run(run())
    for tool, (pid, allowed) in results.items():
        assert pid is None, f"{tool} should be auto-allowed in allowlist mode"
        assert allowed is True


def test_interactive_mode_round_trips_then_allow():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "ls", "cwd": "/x"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        assert pid is not None
        reg.resolve(pid, "allow", "me")
        return await fut
    assert asyncio.run(run()) is True

def test_auto_mode_denylist_still_round_trips_bash():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "ls"},
                                     mode="auto", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        assert pid is not None
        reg.resolve(pid, "deny", "me")
        await fut
        return pid
    assert asyncio.run(run()) is not None

def test_full_auto_allows_bash_without_prompt():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "ls"},
                                     mode="full-auto", denylist=frozenset(),
                                     allowlist=True, identity="me")
        return pid, (await fut if fut else True)
    pid, allowed = asyncio.run(run())
    assert pid is None and allowed is True

def test_always_grant_is_fingerprinted_and_expires():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "npm test", "cwd": "/r"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        reg.resolve(pid, "always", "me")
        await fut
    asyncio.run(run())
    now = time.time()
    assert reg.is_granted("s1", "Bash", {"command": "npm test", "cwd": "/r"}, now) is True
    assert reg.is_granted("s1", "Bash", {"command": "rm -rf /", "cwd": "/r"}, now) is False
    assert reg.is_granted("s1", "Bash", {"command": "npm test", "cwd": "/r"}, now + 4000) is False

def test_resolve_rejects_wrong_identity_and_unknown():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Edit", {"file_path": "/a"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="owner")
        assert reg.resolve(pid, "allow", "attacker") is False
        assert reg.resolve("nope", "allow", "owner") is False
        reg.resolve(pid, "deny", "owner")
        return await fut
    assert asyncio.run(run()) is False

def test_deny_all_resolves_pending_false():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Edit", {"file_path": "/a"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="o")
        reg.deny_all("s1")
        return await fut
    assert asyncio.run(run()) is False

def test_pending_lists_open_requests_for_session():
    reg = PermissionRegistry()
    async def run():
        await reg.request("s1", "t1", "Edit", {"file_path": "/a"}, mode="interactive",
                          denylist=PermissionRegistry.DANGEROUS, allowlist=True, identity="o")
        return reg.pending("s1")
    p = asyncio.run(run())
    assert len(p) == 1 and p[0]["tool"] == "Edit" and "permId" in p[0]

def test_bash_always_grant_does_not_cover_other_commands():
    reg = PermissionRegistry()
    async def run():
        pid, fut = await reg.request("s1", "t1", "Bash", {"command": "npm test", "cwd": "/r"},
                                     mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        reg.resolve(pid, "always", "me"); await fut
    asyncio.run(run())
    now = time.time()
    assert reg.is_granted("s1", "Bash", {"command": "npm test", "cwd": "/r"}, now) is True
    assert reg.is_granted("s1", "Bash", {"command": "npm install evil", "cwd": "/r"}, now) is False


# ---------------------------------------------------------------------------
# Auto-allow writes to a managed-account auto-memory directory
# (<cfg_root>/<account>/projects/<encoded-cwd>/memory/**).  These are the
# agent's own bookkeeping writes; prompting for them is pure friction.
# Anchored on cfg_root() so it can never auto-approve a write into a real repo.
# ---------------------------------------------------------------------------

def _mem_path(tmp_path, *, account="secondary", enc="-enc-cwd", name="note.md"):
    return str(tmp_path / "cfg" / account / "projects" / enc / "memory" / name)


def _request(reg, tool, file_path, mode):
    async def run():
        pid, fut = await reg.request("s1", "t1", tool, {"file_path": file_path},
                                     mode=mode, denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        if pid is not None:
            reg.resolve(pid, "allow", "me")
            await fut
        return pid
    return asyncio.run(run())


def test_account_memory_write_auto_allowed_in_allowlist_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    reg = PermissionRegistry()
    pid = _request(reg, "Write", _mem_path(tmp_path), "allowlist")
    assert pid is None, "Write to account memory dir must auto-allow (no prompt) in allowlist mode"


def test_account_memory_edit_auto_allowed_in_auto_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    reg = PermissionRegistry()
    pid = _request(reg, "Edit", _mem_path(tmp_path, name="ccswitch-x.md"), "auto")
    assert pid is None, "Edit to account memory dir must auto-allow in auto mode"


def test_account_memory_write_auto_allowed_in_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    reg = PermissionRegistry()
    # The agent's own auto-memory writes are sandboxed to cfg_root() and can never
    # reach a real repo, so they are pure bookkeeping noise — auto-allowed even in
    # interactive ("ask each time") mode.  Real, user-facing tools still prompt.
    pid = _request(reg, "Write", _mem_path(tmp_path), "interactive")
    assert pid is None, "memory writes must auto-allow even in interactive mode"


def test_non_memory_write_under_cfg_still_prompts(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    reg = PermissionRegistry()
    # Under the account projects dir but NOT in a memory/ subdir → must prompt.
    other = str(tmp_path / "cfg" / "secondary" / "projects" / "-enc-cwd" / "settings.json")
    pid = _request(reg, "Write", other, "allowlist")
    assert pid is not None, "non-memory writes inside the config dir must still prompt"


def test_repo_memory_dir_not_auto_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    reg = PermissionRegistry()
    # A real repo that happens to have projects/x/memory/ must NOT be auto-allowed:
    # the rule is anchored on cfg_root(), which this path is outside of.
    repo = str(tmp_path / "repo" / "projects" / "x" / "memory" / "note.md")
    pid = _request(reg, "Write", repo, "allowlist")
    assert pid is not None, "memory dirs outside cfg_root must still prompt"


# ---------------------------------------------------------------------------
# MCP tools (mcp__<server>__<tool>) must never prompt — the user opted into
# trusting MCP unconditionally, so they are auto-allowed in EVERY mode, mirroring
# the account-memory-write auto-allow.  These FAIL before the fix: in interactive
# and allowlist modes an MCP tool would otherwise create a pending permission.
# ---------------------------------------------------------------------------

def _request_tool(reg, tool, mode, input=None):
    async def run():
        pid, fut = await reg.request("s1", "t1", tool, input or {},
                                     mode=mode, denylist=PermissionRegistry.DANGEROUS,
                                     allowlist=True, identity="me")
        if pid is not None:
            reg.resolve(pid, "allow", "me")
            await fut
        return pid
    return asyncio.run(run())


def test_mcp_tool_auto_allowed_in_interactive():
    reg = PermissionRegistry()
    pid = _request_tool(reg, "mcp__railway__deploy", "interactive", {"serviceId": "x"})
    assert pid is None, "MCP tools must auto-allow (no prompt) even in interactive mode"


def test_mcp_tool_auto_allowed_in_allowlist():
    reg = PermissionRegistry()
    pid = _request_tool(reg, "mcp__github__create_pull_request", "allowlist")
    assert pid is None, "MCP tools must auto-allow in allowlist mode"


def test_mcp_tool_auto_allowed_in_auto():
    reg = PermissionRegistry()
    pid = _request_tool(reg, "mcp__chrome-devtools__navigate_page", "auto")
    assert pid is None, "MCP tools must auto-allow in auto mode"


def test_non_mcp_unknown_tool_still_prompts_in_interactive():
    # Guard: the mcp__ prefix is what triggers the auto-allow, not "any unknown tool".
    reg = PermissionRegistry()
    pid = _request_tool(reg, "SomeRandomTool", "interactive")
    assert pid is not None, "non-MCP tools must still prompt in interactive mode"
