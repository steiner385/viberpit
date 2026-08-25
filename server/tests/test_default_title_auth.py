"""Tests for the auth gate in server.sessions.orchestrator._default_title_fn.

Covers issue #751: `_default_title_fn` was a verbatim copy of the pre-#749 suggestions gate —
it called `acquire_valid_token`, caught `AuthError`/`FileNotFoundError`, and returned `None` with
no fallback to the account's setup-token. On an account whose browser-OAuth refresh is dead but
whose setup-token still launches sessions fine (the same case #749 fixed for suggestions), this
meant auto-generated session titles silently never appeared.

Mirrors the shape of test_detached_suggestion_auth.py's gate tests, without the caching/backoff
machinery that `_generate_detached_suggestions` has (there is no such state here — the title fn is
called directly and returns a value, not spawned into a background cache).
"""
import asyncio

import pytest

from ccswitch.oauth import AuthError
import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


@pytest.fixture(autouse=True)
def _stub_compose(monkeypatch):
    # generate_title is monkeypatched to a fake in every test below, so the real
    # config_dir value never matters — just avoid touching real on-disk credentials.
    monkeypatch.setattr(orch_mod, "compose", lambda account: "/cfg/" + account, raising=False)


def test_falls_back_to_setup_token_when_refresh_dead(monkeypatch):
    """AuthError from acquire_valid_token + a live setup-token -> title generation proceeds."""
    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-tok"})

        called_with = {}

        async def fake_generate_title(prompts, *, config_dir, cwd):
            called_with["prompts"] = prompts
            called_with["config_dir"] = config_dir
            called_with["cwd"] = cwd
            return "A generated title"

        monkeypatch.setattr(orch_mod, "generate_title", fake_generate_title)

        title = await orch._default_title_fn(
            ["do the thing"], account="tonystein85_gmail.com", cwd="/x",
        )
        assert title == "A generated title"
        assert called_with["prompts"] == ["do the thing"]
        assert called_with["cwd"] == "/x"
    asyncio.run(run())


def test_gives_up_when_no_setup_token_either(monkeypatch, caplog):
    """AuthError + no setup-token -> give up, log a warning naming the account, never the token."""
    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        async def fail_if_called(*a, **k):
            raise AssertionError("generate_title must not be called with no usable auth")

        monkeypatch.setattr(orch_mod, "generate_title", fail_if_called)

        with caplog.at_level("WARNING"):
            title = await orch._default_title_fn(
                ["do the thing"], account="tonystein85_gmail.com", cwd="/x",
            )

        assert title is None
        assert "tonystein85_gmail.com" in caplog.text
    asyncio.run(run())


def test_no_token_value_ever_logged(monkeypatch, caplog):
    """The setup-token string must never appear in any log record, success or failure path."""
    token = "sk-ant-oat01-supersecret-token-value"

    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        # Success path: token is present and used.
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": token})

        async def fake_generate_title(prompts, *, config_dir, cwd):
            return "A generated title"

        monkeypatch.setattr(orch_mod, "generate_title", fake_generate_title)

        with caplog.at_level("DEBUG"):
            await orch._default_title_fn(["do the thing"], account="tonystein85_gmail.com", cwd="/x")

            # Failure path: no setup-token, gives up and logs a warning.
            monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})
            await orch._default_title_fn(["do the thing"], account="tonystein85_gmail.com", cwd="/x")

        for record in caplog.records:
            assert token not in record.getMessage()
    asyncio.run(run())


def test_healthy_token_behaviour_unchanged(monkeypatch):
    """A healthy browser-OAuth token never even reaches oauth_env_for — behaviour unchanged."""
    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(orch_mod.ccswitch_accounts, "acquire_valid_token", lambda name: "tok")

        def fail_if_called(account):
            raise AssertionError("oauth_env_for must not be consulted when acquire_valid_token succeeds")

        monkeypatch.setattr(orch_mod, "oauth_env_for", fail_if_called)

        async def fake_generate_title(prompts, *, config_dir, cwd):
            return "A generated title"

        monkeypatch.setattr(orch_mod, "generate_title", fake_generate_title)

        title = await orch._default_title_fn(["do the thing"], account="tonystein85_gmail.com", cwd="/x")
        assert title == "A generated title"
    asyncio.run(run())


def test_unexpected_exception_still_degrades_to_none(monkeypatch, caplog):
    """Fail-open: any lookup failure degrades to today's behaviour (None), never raises."""
    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )

        def boom(account):
            raise RuntimeError("ledger corrupt")

        monkeypatch.setattr(orch_mod, "oauth_env_for", boom)

        with caplog.at_level("WARNING"):
            title = await orch._default_title_fn(["do the thing"], account="tonystein85_gmail.com", cwd="/x")
        assert title is None
    asyncio.run(run())


def _giveup_warning_count(caplog, account: str) -> int:
    return sum(
        1 for r in caplog.records
        if r.levelname == "WARNING" and "title gen: giving up" in r.getMessage() and account in r.getMessage()
    )


def test_giveup_warning_rate_limited_within_window(monkeypatch, caplog):
    """Finding 1: _titling_sweep re-invokes _default_title_fn every _titling_interval (15s
    default) for every still-auto-named session, so an unconditional WARNING on give-up is a
    log storm (~240/hr per session). Repeated calls within the backoff window must log the
    WARNING only once."""
    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        clock = {"t": 1000.0}
        monkeypatch.setattr(orch_mod.time, "monotonic", lambda: clock["t"])

        account = "tonystein85_gmail.com"
        with caplog.at_level("WARNING"):
            for _ in range(5):
                title = await orch._default_title_fn(["do the thing"], account=account, cwd="/x")
                assert title is None
                clock["t"] += 1.0  # 5 calls, 1s apart — well inside the 60s window

        assert _giveup_warning_count(caplog, account) == 1
    asyncio.run(run())


def test_giveup_warning_fires_again_after_window(monkeypatch, caplog):
    """Finding 1: a give-up call after the backoff window has elapsed must warn again — the
    rate limit suppresses the storm, it does not silence the account forever."""
    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        clock = {"t": 1000.0}
        monkeypatch.setattr(orch_mod.time, "monotonic", lambda: clock["t"])

        account = "tonystein85_gmail.com"
        with caplog.at_level("WARNING"):
            title = await orch._default_title_fn(["do the thing"], account=account, cwd="/x")
            assert title is None
            assert _giveup_warning_count(caplog, account) == 1

            # Still inside the window: no second warning.
            clock["t"] += orch_mod._TITLE_AUTH_WARN_BACKOFF_S - 1
            title = await orch._default_title_fn(["do the thing"], account=account, cwd="/x")
            assert title is None
            assert _giveup_warning_count(caplog, account) == 1

            # Window elapsed: warns again.
            clock["t"] += 2
            title = await orch._default_title_fn(["do the thing"], account=account, cwd="/x")
            assert title is None
            assert _giveup_warning_count(caplog, account) == 2
    asyncio.run(run())


def test_giveup_warning_backoff_keyed_per_account(monkeypatch, caplog):
    """The backoff must not cross-suppress a different account's give-up warning."""
    async def run():
        orch = SessionOrchestrator()

        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})
        monkeypatch.setattr(orch_mod.time, "monotonic", lambda: 1000.0)

        with caplog.at_level("WARNING"):
            await orch._default_title_fn(["hi"], account="account-a", cwd="/x")
            await orch._default_title_fn(["hi"], account="account-b", cwd="/x")

        assert _giveup_warning_count(caplog, "account-a") == 1
        assert _giveup_warning_count(caplog, "account-b") == 1
    asyncio.run(run())
