import asyncio, time
from server.sessions.permissions import PermissionRegistry

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
