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
