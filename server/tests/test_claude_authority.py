"""Primary-side envelope minting (#2629)."""
import pytest

from server.sessions import claude_authority as ca
from viberpit import credential_envelope as ce
from viberpit import paths
from viberpit.creds import write_json_secure
from tests.helpers import temp_env


def test_generation_bumps_only_when_token_changes(monkeypatch):
    with temp_env():
        monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
        tokens = {"a": "t1"}
        monkeypatch.setattr(ca.crypto, "read_oauth_token", lambda a: tokens.get(a))
        assert [(e.kind, e.generation) for e in ca.mint_envelopes(["a"])] == [(ce.SETUP_TOKEN, 1)]
        assert ca.mint_envelopes(["a"])[0].generation == 1
        tokens["a"] = "t2"
        assert ca.mint_envelopes(["a"])[0].generation == 2


def test_falls_back_to_access_token_and_skips_empty(monkeypatch):
    with temp_env():
        monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
        monkeypatch.setattr(ca.crypto, "read_oauth_token", lambda a: None)
        write_json_secure(paths.slot_path("b"), {"accessToken": "AT", "expiresAt": 5000})
        envs = ca.mint_envelopes(["b", "nothing"])
        assert [(e.account, e.kind, e.token, e.expires_at) for e in envs] == [
            ("b", ce.OAUTH_ACCESS, "AT", 5.0)]


def test_worker_may_not_mint(monkeypatch):
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    with pytest.raises(RuntimeError):
        ca.mint_envelopes(["a"])
    with pytest.raises(RuntimeError):
        ca.mint_remote_control_envelopes(["a"])


# --- Remote Control envelopes: the slot's access token + org, beside the inference one ---

RC_SCOPES = ["user:inference", "user:sessions:claude_code"]


def _rc_ready(monkeypatch, *, ready=("a",)):
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "true")
    monkeypatch.setattr(ca, "remote_control_slot_ready", lambda a: a in ready)
    monkeypatch.setattr(ca, "organization_uuid", lambda a: f"org-{a}")


def _slot(account, token, *, ttl_ms=3_600_000, scopes=RC_SCOPES):
    import time
    write_json_secure(paths.slot_path(account), {
        "accessToken": token, "refreshToken": "RT-secret", "scopes": scopes,
        "expiresAt": int(time.time() * 1000) + ttl_ms})


def test_rc_envelope_is_the_slot_access_token_and_org_never_the_refresh(monkeypatch):
    with temp_env():
        _rc_ready(monkeypatch, ready=("a",))
        _slot("a", "AT-1")
        _slot("b", "AT-b")                                   # not Remote-Control-ready
        [env] = ca.mint_remote_control_envelopes(["a", "b", "nothing"])
        assert (env.account, env.kind, env.token, env.org_uuid, env.generation) == (
            "a", ce.OAUTH_ACCESS, "AT-1", "org-a", 1)
        assert env.expires_at is not None
        assert "RT-secret" not in repr(env.__dict__)


def test_rc_generations_are_their_own_and_bump_on_rotation(monkeypatch):
    with temp_env():
        _rc_ready(monkeypatch)
        monkeypatch.setattr(ca.crypto, "read_oauth_token", lambda a: "SETUP")
        _slot("a", "AT-1")
        assert ca.mint_envelopes(["a"])[0].generation == 1           # the inference one
        assert ca.mint_remote_control_envelopes(["a"])[0].generation == 1
        assert ca.mint_remote_control_envelopes(["a"])[0].generation == 1
        _slot("a", "AT-2")                                           # a refresh rotated it
        assert ca.mint_remote_control_envelopes(["a"])[0].generation == 2
        # The inference envelope (the setup-token) did not change: its generation holds.
        assert ca.mint_envelopes(["a"])[0].generation == 1


def test_rc_envelopes_skip_unusable_slots_and_the_kill_switch(monkeypatch):
    with temp_env():
        _rc_ready(monkeypatch, ready=("a", "b"))
        _slot("a", "AT-a", ttl_ms=-1000)                             # expired
        _slot("b", "AT-b", scopes=["user:inference"])                # no sessions scope
        assert ca.mint_remote_control_envelopes(["a", "b"]) == []
        _slot("a", "AT-a")
        monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "false")
        assert ca.mint_remote_control_envelopes(["a"]) == []
