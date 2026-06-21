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
