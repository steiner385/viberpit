# server/tests/test_mcp_auth_manager.py
import asyncio
from pathlib import Path
import pytest
from server import mcp_auth
from server.mcp_auth import McpAuthManager, McpAuthError

class FakeSession:
    def __init__(self, account, cfg_dir, *, url="https://auth/x?state=s", fail=None):
        self.account, self.cfg_dir = account, cfg_dir
        self._url, self._fail = url, fail
        self.terminated = False
        self.submitted = None
    async def read_auth_url(self, timeout):
        return self._url
    async def complete(self, redirect_url, timeout):
        self.submitted = redirect_url
        if self._fail:
            raise McpAuthError(self._fail)
    def terminate(self):
        self.terminated = True

def make_manager(monkeypatch, tmp_path, *, fail=None, calls=None):
    cfg = tmp_path / "cfg-alice"; cfg.mkdir()
    sess = FakeSession("alice", cfg, fail=fail)
    monkeypatch.setattr(mcp_auth, "merge_mcp_oauth_from_cfg", lambda d: (calls.append(("merge", d)) or 1) if calls is not None else 1)
    monkeypatch.setattr(mcp_auth, "clear_needs_auth_cache", lambda s: (calls.append(("clear", s)) or ["alice"]) if calls is not None else ["alice"])
    mgr = McpAuthManager(
        session_factory=lambda server, acct, d: sess,
        account_picker=lambda server: ("alice", cfg),
    )
    return mgr, sess

@pytest.mark.asyncio
async def test_start_surfaces_auth_url(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    st = await mgr.start("notion")
    # start returns quickly as 'starting'; wait for URL read.
    for _ in range(50):
        if mgr.status("notion").state == "awaiting_redirect":
            break
        await asyncio.sleep(0.01)
    s = mgr.status("notion")
    assert s.state == "awaiting_redirect"
    assert s.auth_url == "https://auth/x?state=s"
    assert s.server == "notion"

@pytest.mark.asyncio
async def test_submit_redirect_success_merges_and_clears(monkeypatch, tmp_path):
    calls = []
    mgr, sess = make_manager(monkeypatch, tmp_path, calls=calls)
    await mgr.start("notion")
    for _ in range(50):
        if mgr.status("notion").state == "awaiting_redirect": break
        await asyncio.sleep(0.01)
    st = await mgr.submit_redirect("notion", "http://localhost:1/callback?code=abc")
    assert st.state == "success"
    assert sess.submitted == "http://localhost:1/callback?code=abc"
    assert ("merge", sess.cfg_dir) in calls
    assert ("clear", "notion") in calls
    assert st.accounts == ["alice"]

@pytest.mark.asyncio
async def test_submit_redirect_failure_sets_error(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path, fail="server rejected code")
    await mgr.start("notion")
    for _ in range(50):
        if mgr.status("notion").state == "awaiting_redirect": break
        await asyncio.sleep(0.01)
    st = await mgr.submit_redirect("notion", "http://localhost/x")
    assert st.state == "error"
    assert "rejected" in (st.message or "")

@pytest.mark.asyncio
async def test_second_start_while_running_raises_busy(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    await mgr.start("notion")
    with pytest.raises(mcp_auth.McpAuthBusy):
        await mgr.start("railway")

@pytest.mark.asyncio
async def test_cancel_terminates_and_idles(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    await mgr.start("notion")
    st = await mgr.cancel("notion")
    assert st.state in ("cancelled", "idle")
    assert sess.terminated is True

@pytest.mark.asyncio
async def test_status_scoped_to_active_server(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    await mgr.start("notion")
    assert mgr.status("railway").state == "idle"  # different server → fresh idle

@pytest.mark.asyncio
async def test_cancel_does_not_clobber_terminal_success(monkeypatch, tmp_path):
    """Finding 1 regression: cancel() must not overwrite an already-terminal state."""
    mgr, sess = make_manager(monkeypatch, tmp_path)
    await mgr.start("notion")
    for _ in range(50):
        if mgr.status("notion").state == "awaiting_redirect":
            break
        await asyncio.sleep(0.01)
    st = await mgr.submit_redirect("notion", "http://localhost/cb?code=ok")
    assert st.state == "success"
    # Now cancel races in after the exchange already succeeded.
    st2 = await mgr.cancel("notion")
    assert st2.state == "success", (
        f"cancel() clobbered terminal success → got {st2.state!r}"
    )


@pytest.mark.asyncio
async def test_read_url_error_surfaces_cli_message(monkeypatch, tmp_path):
    # stdio graceful-degradation: read_auth_url raises McpAuthError with the
    # CLI's verbatim message; the manager lands in error carrying that message.
    cfg = tmp_path / "cfg-alice"; cfg.mkdir()
    class ErrSession:
        account, cfg_dir = "alice", cfg
        async def read_auth_url(self, timeout):
            raise mcp_auth.McpAuthError('"aws-mcp" doesn\'t support OAuth login')
        async def complete(self, u, t): ...
        def terminate(self): pass
    mgr = McpAuthManager(session_factory=lambda s, a, d: ErrSession(),
                         account_picker=lambda s: ("alice", cfg))
    await mgr.start("aws-mcp")
    for _ in range(50):
        if mgr.status("aws-mcp").state in mcp_auth.TERMINAL: break
        await asyncio.sleep(0.01)
    s = mgr.status("aws-mcp")
    assert s.state == "error"
    assert "doesn't support OAuth login" in (s.message or "")
