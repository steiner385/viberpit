"""The OpenAI (Codex) half of the accounts REST API.

Two things are under test here, and the second one is the important one:

1. The device-flow add routes, which mirror the Copilot ones exactly.
2. **Ordering.** Disabling or removing an OpenAI account must revoke credentials
   at the authority and fan invalidation out to every runner BEFORE the manifest
   says "disabled" — and must refuse to report success when the fan-out was only
   partial. Those are tested by recording the call ORDER, not the end state: an
   implementation that does the right things in the wrong order produces an
   identical end state and an exploitable window.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env  # noqa: E402

from server.codex import auth_client as auth_client_mod  # noqa: E402
from server.codex.auth_types import (  # noqa: E402
    AuthorityError,
    AuthorityErrorCode,
    AuthorityHealth,
    AuthorityUnavailable,
    LoginSnapshot,
)

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
}


class _FakeAuthority:
    """Stands in for the credential authority, recording the call order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.login_state = LoginSnapshot(
            state="awaiting_authorization",
            user_code="ABCD-1234",
            verification_uri="https://chatgpt.com/device",
        )
        self.revoke_error: Exception | None = None
        self.authorize_error: Exception | None = None
        self.forget_error: Exception | None = None

    async def health(self) -> AuthorityHealth:
        self.calls.append("health")
        return AuthorityHealth(reachable=True, sync_revision=7, accounts=1)

    async def authorize(self, sync) -> AuthorityHealth:
        self.calls.append(f"authorize:{sync.revision}")
        if self.authorize_error is not None:
            raise self.authorize_error
        return AuthorityHealth(reachable=True, sync_revision=sync.revision)

    async def login_start(self, account: str) -> LoginSnapshot:
        self.calls.append(f"login_start:{account}")
        return self.login_state

    async def login_status(self, account: str) -> LoginSnapshot:
        self.calls.append(f"login_status:{account}")
        return self.login_state

    async def login_cancel(self, account: str) -> LoginSnapshot:
        self.calls.append(f"login_cancel:{account}")
        return LoginSnapshot(state="failed", error="cancelled")

    async def revoke(self, account: str) -> int:
        self.calls.append(f"revoke:{account}")
        if self.revoke_error is not None:
            raise self.revoke_error
        return 2

    async def forget(self, account: str) -> None:
        self.calls.append(f"forget:{account}")
        if self.forget_error is not None:
            raise self.forget_error

    async def logout(self, account: str) -> None:
        self.calls.append(f"logout:{account}")


class _FakeSessions:
    """Sessions facade exposing only what the accounts routes touch."""

    def __init__(self, authority: _FakeAuthority) -> None:
        self._authority = authority
        self.usage = AsyncMock(return_value=None)
        self.live_session_counts = AsyncMock(return_value={})
        self.live_session_load = AsyncMock(return_value={})
        self.codex_account_status = AsyncMock(
            return_value={
                "quota": {
                    "v": 2,
                    "provider": "openai",
                    "fetched_at": "2026-09-05T12:00:00+00:00",
                    "availability": "available",
                    "extra_usage_enabled": False,
                    "windows": [
                        {
                            "key": "primary",
                            "label": "Primary",
                            "used_pct": 10.0,
                            "resets_at": None,
                            "duration_seconds": 18_000,
                            "scope": "account",
                            "applies_to_models": [],
                        }
                    ],
                },
                "activeRunners": 1,
                "degradedReason": None,
            }
        )
        self.invalidate_result: dict = {"invalidated": ["dobby"], "failed": {}}

    async def codex_invalidate_credentials(self, account: str) -> dict:
        self._authority.calls.append(f"invalidate:{account}")
        return self.invalidate_result

    def daemon_epoch(self):
        return None


def _make_client(authority: _FakeAuthority) -> tuple[TestClient, _FakeSessions]:
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(authority)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


@pytest.fixture
def authority(monkeypatch):
    fake = _FakeAuthority()
    monkeypatch.setattr(
        auth_client_mod, "edge_authority_client", lambda: fake
    )
    # The accounts router imported the symbol by value, so patch it there too.
    import server.api.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "edge_authority_client", lambda: fake)
    return fake


def _register_openai(name: str, *, enabled: bool = True) -> None:
    from ccswitch import account_registry

    account_registry.register_account(
        name, account_registry.PROVIDER_OPENAI, engine="codex"
    )
    account_registry.set_enabled(name, enabled)


# ---------------------------------------------------------------------------
# Add flow
# ---------------------------------------------------------------------------


def test_add_openai_returns_202_with_device_state(authority):
    with temp_env():
        client, _ = _make_client(authority)
        with client:
            resp = client.post(
                "/api/accounts/add/openai",
                json={"name": "chatgpt-pro"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "awaiting_authorization"
    assert body["userCode"] == "ABCD-1234"
    assert body["verificationUri"].startswith("https://")


def test_add_openai_registers_disabled_and_syncs_before_login(authority):
    """The entry must exist (disabled) and be synced BEFORE login is started.

    The authority refuses to log in an account it has never been told about, so
    the registration has to come first; `enabled=False` is what keeps that
    ordering from creating a usable account before any credential exists.
    """
    from ccswitch import account_registry

    with temp_env():
        client, _ = _make_client(authority)
        with client:
            client.post(
                "/api/accounts/add/openai",
                json={"name": "chatgpt-pro"},
                headers=IDENTITY_HEADERS,
            )
            entry = account_registry.entry("chatgpt-pro")
            assert entry.provider == "openai"
            assert entry.engine == "codex"
            assert entry.enabled is False

    authorize_at = next(
        i for i, c in enumerate(authority.calls) if c.startswith("authorize:")
    )
    login_at = authority.calls.index("login_start:chatgpt-pro")
    assert authorize_at < login_at


def test_add_openai_409_when_name_taken_by_another_provider(authority):
    from ccswitch import account_registry

    with temp_env():
        account_registry.register_account("dup", account_registry.PROVIDER_ANTHROPIC)
        client, _ = _make_client(authority)
        with client:
            resp = client.post(
                "/api/accounts/add/openai",
                json={"name": "dup"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "account_exists"


def test_add_openai_400_invalid_name(authority):
    with temp_env():
        client, _ = _make_client(authority)
        with client:
            resp = client.post(
                "/api/accounts/add/openai",
                json={"name": "not a name"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_name"


def test_add_openai_status_enables_on_authorized(authority):
    """The edge learns the login succeeded only by polling, so that is where the
    account becomes enabled — and it must re-sync so the authority may issue."""
    from ccswitch import account_registry

    authority.login_state = LoginSnapshot(state="authorized", generation=1)
    with temp_env():
        _register_openai("chatgpt-pro", enabled=False)
        client, _ = _make_client(authority)
        with client:
            resp = client.get(
                "/api/accounts/add/openai?name=chatgpt-pro", headers=IDENTITY_HEADERS
            )
            assert resp.status_code == 200
            assert resp.json()["state"] == "authorized"
            assert account_registry.entry("chatgpt-pro").enabled is True
    assert any(c.startswith("authorize:") for c in authority.calls)


def test_add_openai_cancel_drops_the_placeholder_entry(authority):
    from ccswitch import account_registry

    with temp_env():
        _register_openai("chatgpt-pro", enabled=False)
        client, _ = _make_client(authority)
        with client:
            resp = client.post(
                "/api/accounts/add/openai/cancel",
                json={"name": "chatgpt-pro"},
                headers=IDENTITY_HEADERS,
            )
            assert resp.status_code == 200
            assert not account_registry.has_account("chatgpt-pro")
    assert "forget:chatgpt-pro" in authority.calls


def test_openai_routes_503_without_an_authority(monkeypatch):
    """No authority configured is a fixable operator state, not a 500."""
    import server.api.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "edge_authority_client", lambda: None)
    with temp_env():
        client, _ = _make_client(_FakeAuthority())
        with client:
            resp = client.post(
                "/api/accounts/add/openai",
                json={"name": "chatgpt-pro"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "codex_authority_unconfigured"


# ---------------------------------------------------------------------------
# Ordering: revoke -> fan out -> manifest
# ---------------------------------------------------------------------------


def test_disable_revokes_and_invalidates_before_writing_the_flag(authority):
    """The ORDER is the safety property, so the order is what is asserted.

    Writing "disabled" first and revoking second leaves a window in which
    ccswitch reports the account off while runners still hold live envelopes.
    """
    from ccswitch import account_registry

    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)

        seen_at_revoke: dict = {}
        real_revoke = authority.revoke

        async def _spy_revoke(account: str) -> int:
            seen_at_revoke["enabled"] = account_registry.entry(account).enabled
            return await real_revoke(account)

        authority.revoke = _spy_revoke  # type: ignore[method-assign]
        with client:
            resp = client.patch(
                "/api/accounts/chatgpt-pro/enabled",
                json={"enabled": False},
                headers=IDENTITY_HEADERS,
            )
            assert resp.status_code == 200
            assert account_registry.entry("chatgpt-pro").enabled is False

    # Still enabled at the moment of revocation => the manifest write came after.
    assert seen_at_revoke["enabled"] is True
    assert authority.calls.index("revoke:chatgpt-pro") < authority.calls.index(
        "invalidate:chatgpt-pro"
    )


def test_disable_reports_degraded_when_a_host_could_not_be_invalidated(authority):
    """A partial fan-out is 502 + degraded, never a silent success.

    Some host may still be running an app-server with a live envelope. Reporting
    that as "disabled" is the difference the plan calls out by name.
    """
    from ccswitch import account_registry

    with temp_env():
        _register_openai("chatgpt-pro")
        client, sessions = _make_client(authority)
        sessions.invalidate_result = {
            "invalidated": ["dobby"],
            "failed": {"rocky": "unreachable"},
        }
        with client:
            resp = client.patch(
                "/api/accounts/chatgpt-pro/enabled",
                json={"enabled": False},
                headers=IDENTITY_HEADERS,
            )
            assert resp.status_code == 502
            detail = resp.json()["detail"]
            assert detail["error"] == "invalidation_partial"
            assert detail["degraded"] is True
            assert detail["retryable"] is True
            assert detail["hosts"] == ["rocky"]
            # And the flag must NOT have flipped: an operator who sees an error
            # must not also find the account marked disabled.
            assert account_registry.entry("chatgpt-pro").enabled is True


def test_enable_writes_the_manifest_before_syncing(authority):
    """Enable is the opposite order: a failed sync leaves it un-issuable, not live."""
    from ccswitch import account_registry

    with temp_env():
        _register_openai("chatgpt-pro", enabled=False)
        client, _ = _make_client(authority)

        seen: dict = {}

        async def _spy_authorize(sync):
            seen["enabled_at_sync"] = account_registry.entry("chatgpt-pro").enabled
            seen["record_enabled"] = sync.get("chatgpt-pro").enabled
            return AuthorityHealth(reachable=True, sync_revision=sync.revision)

        authority.authorize = _spy_authorize  # type: ignore[method-assign]
        with client:
            resp = client.patch(
                "/api/accounts/chatgpt-pro/enabled",
                json={"enabled": True},
                headers=IDENTITY_HEADERS,
            )
            assert resp.status_code == 200

    assert seen["enabled_at_sync"] is True
    assert seen["record_enabled"] is True
    assert "revoke:chatgpt-pro" not in authority.calls


def test_authorized_status_retries_sync_after_manifest_was_already_enabled(authority):
    """A failed first sync must not turn the next status poll into false success."""
    from ccswitch import account_registry

    authority.login_state = LoginSnapshot(state="authorized")
    authority.authorize_error = AuthorityUnavailable("connection lost")
    with temp_env():
        _register_openai("chatgpt-pro", enabled=False)
        client, _ = _make_client(authority)
        with client:
            syncs_before = len(
                [call for call in authority.calls if call.startswith("authorize:")]
            )
            first = client.get(
                "/api/accounts/add/openai",
                params={"name": "chatgpt-pro"},
                headers=IDENTITY_HEADERS,
            )
            assert first.status_code == 503
            assert account_registry.entry("chatgpt-pro").enabled is True
            assert len(
                [call for call in authority.calls if call.startswith("authorize:")]
            ) == syncs_before + 1

            authority.authorize_error = None
            second = client.get(
                "/api/accounts/add/openai",
                params={"name": "chatgpt-pro"},
                headers=IDENTITY_HEADERS,
            )

    assert second.status_code == 200
    assert len(
        [call for call in authority.calls if call.startswith("authorize:")]
    ) == syncs_before + 2


def test_delete_revokes_and_forgets_before_removing(authority):
    from ccswitch import account_registry

    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.delete(
                "/api/accounts/chatgpt-pro", headers=IDENTITY_HEADERS
            )
            assert resp.status_code == 204
            assert not account_registry.has_account("chatgpt-pro")
    assert authority.calls.index("revoke:chatgpt-pro") < authority.calls.index(
        "invalidate:chatgpt-pro"
    )
    assert "forget:chatgpt-pro" in authority.calls


def test_delete_never_reports_success_when_authority_forget_fails(authority):
    from ccswitch import account_registry

    authority.forget_error = AuthorityUnavailable("connection lost")
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.delete(
                "/api/accounts/chatgpt-pro", headers=IDENTITY_HEADERS
            )
            assert resp.status_code == 503
            assert account_registry.has_account("chatgpt-pro")


def test_disable_reports_sync_failure_instead_of_success(authority):
    from ccswitch import account_registry

    authority.authorize_error = AuthorityUnavailable("connection lost")
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.patch(
                "/api/accounts/chatgpt-pro/enabled",
                json={"enabled": False},
                headers=IDENTITY_HEADERS,
            )
            assert resp.status_code == 503
            # Local state is safely disabled and the durable authority revoke already
            # ran, but the response remains retryable until sync is confirmed.
            assert account_registry.entry("chatgpt-pro").enabled is False


def test_disable_503s_when_the_authority_is_unreachable(authority):
    """We could not revoke, so we must not claim the account is disabled."""
    from ccswitch import account_registry

    authority.revoke_error = AuthorityUnavailable("connection refused")
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.patch(
                "/api/accounts/chatgpt-pro/enabled",
                json={"enabled": False},
                headers=IDENTITY_HEADERS,
            )
            assert resp.status_code == 503
            assert account_registry.entry("chatgpt-pro").enabled is True


def test_disable_tolerates_an_account_the_authority_never_knew(authority):
    """UNKNOWN_ACCOUNT is not a failure to revoke — there was nothing to revoke."""
    from ccswitch import account_registry

    authority.revoke_error = AuthorityError(
        AuthorityErrorCode.UNKNOWN_ACCOUNT, "never synced"
    )
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.patch(
                "/api/accounts/chatgpt-pro/enabled",
                json={"enabled": False},
                headers=IDENTITY_HEADERS,
            )
            assert resp.status_code == 200
            assert account_registry.entry("chatgpt-pro").enabled is False


# ---------------------------------------------------------------------------
# Reauth dispatch + the account wire shape
# ---------------------------------------------------------------------------


def test_reauth_dispatches_openai_to_the_device_flow(authority):
    """An OpenAI account has no local slot and no browser OAuth to re-run."""
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.post(
                "/api/accounts/chatgpt-pro/reauth", headers=IDENTITY_HEADERS
            )
    assert resp.status_code == 202
    assert resp.json()["userCode"] == "ABCD-1234"
    assert "login_start:chatgpt-pro" in authority.calls


def test_openai_account_is_addressable_without_a_local_credential_slot(authority):
    """An OpenAI account exists by manifest entry alone — its credential is not here.

    Before this, ``_check_name_and_slot`` 404'd anything with no slot file or
    copilot token dir, which is every OpenAI account by design.
    """
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.patch(
                "/api/accounts/chatgpt-pro",
                json={"nickname": "Work ChatGPT"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200


def test_accounts_list_carries_identity_and_codex_blocks_but_no_secrets(authority):
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    entry = next(a for a in resp.json() if a["name"] == "chatgpt-pro")
    assert entry["provider"] == "openai"
    assert entry["engine"] == "codex"
    assert entry["userAuthMode"] == "chatgpt_device"
    assert entry["runnerAuthMode"] == "chatgpt_external_tokens"
    assert entry["credentialAuthority"] == "codex_auth_service"
    assert entry["codex"]["authority"]["reachable"] is True
    assert entry["codex"]["quota"]["availability"] == "available"
    assert entry["usage"]["provider"] == "openai"
    assert entry["codex"]["activeRunners"] == 1
    # The native ChatGPT account id and any token must never appear.
    blob = resp.text.lower()
    assert "accesstoken" not in blob
    assert "chatgptaccountid" not in blob


def test_accounts_list_still_renders_when_the_authority_is_down(authority):
    """The accounts page is where an operator FIXES a broken authority."""
    async def _boom():
        raise AuthorityUnavailable("connection refused")

    authority.health = _boom  # type: ignore[method-assign]
    with temp_env():
        _register_openai("chatgpt-pro")
        client, _ = _make_client(authority)
        with client:
            resp = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    entry = next(a for a in resp.json() if a["name"] == "chatgpt-pro")
    assert entry["codex"]["authority"] is None


def test_codex_status_outage_overrides_prior_available_usage(authority):
    """A dead status RPC must not leave stale AVAILABLE quota selectable."""
    from datetime import datetime, timezone

    from ccswitch.quota import QuotaSnapshot, QuotaWindow

    prior = QuotaSnapshot(
        provider="openai",
        fetched_at=datetime.now(timezone.utc),
        windows=(QuotaWindow(key="primary", label="Primary", used_pct=10),),
    )
    with temp_env():
        _register_openai("chatgpt-pro")
        client, sessions = _make_client(authority)
        sessions.usage.return_value = prior
        sessions.codex_account_status.side_effect = AuthorityUnavailable(
            "primary daemon unavailable"
        )
        with client:
            resp = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    entry = next(a for a in resp.json() if a["name"] == "chatgpt-pro")
    assert entry["usage"]["availability"] == "unknown"
    assert entry["codex"]["quota"]["availability"] == "unknown"
    assert entry["rank"] is None


@pytest.mark.parametrize(
    "malformed",
    [
        {"not": "canonical"},
        {
            "v": 2,
            "provider": "openai",
            "fetched_at": "2026-09-05T12:00:00+00:00",
            "availability": "available",
            "extra_usage_enabled": False,
            "windows": [],
        },
        *[
            {
                "v": version,
                "provider": "openai",
                "fetched_at": "2026-09-05T12:00:00+00:00",
                "availability": "available",
                "extra_usage_enabled": False,
                "windows": [
                    {
                        "key": "primary",
                        "label": "Primary",
                        "used_pct": pct,
                        "scope": "account",
                        "applies_to_models": [],
                    }
                ],
            }
            for version, pct in ((2.0, 10.0), (2, 10**400))
        ],
        *[
            {
                "v": 2,
                "provider": "openai",
                "fetched_at": "2026-09-05T12:00:00+00:00",
                "availability": "available",
                "extra_usage_enabled": False,
                "windows": [
                    {
                        "key": "primary",
                        "label": "Primary",
                        "used_pct": pct,
                        "scope": "account",
                        "applies_to_models": [],
                    }
                ],
            }
            for pct in (float("nan"), float("inf"), -1.0, 101.0)
        ],
    ],
)
def test_malformed_codex_quota_is_unknown_and_never_ranked(authority, malformed):
    with temp_env():
        _register_openai("chatgpt-pro")
        client, sessions = _make_client(authority)
        sessions.codex_account_status.return_value = {
            "quota": malformed,
            "activeRunners": 1,
            "degradedReason": None,
        }
        with client:
            resp = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    entry = next(a for a in resp.json() if a["name"] == "chatgpt-pro")
    assert entry["usage"]["provider"] == "openai"
    assert entry["usage"]["availability"] == "unknown"
    assert entry["codex"]["quota"]["availability"] == "unknown"
    assert entry["rank"] is None


def test_accounts_list_costs_nothing_when_no_openai_account_exists(authority):
    """A deployment with no OpenAI account must pay no per-request Codex cost.

    Note what is NOT asserted: that the authority is untouched entirely. The
    lifespan's periodic authorization sync pushes an (empty) projection on
    startup, and it should — an empty projection is what tells the authority that
    nothing is authorized, which is strictly more restrictive than letting a stale
    one ride out its lease.
    """
    from ccswitch import account_registry

    with temp_env():
        account_registry.register_account(
            "anth", account_registry.PROVIDER_ANTHROPIC
        )
        client, sessions = _make_client(authority)
        with client:
            authority.calls.clear()
            resp = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    assert "health" not in authority.calls
    sessions.codex_account_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# Lifespan wiring
# ---------------------------------------------------------------------------


def test_no_authority_means_no_background_sync_task():
    """A deployment without a credential authority must not gain a shutdown phase.

    The edge's authorization sync is a background task cancelled during shutdown.
    Most deployments have no authority at all, and one that starts an idle task
    anyway would add a cancel-and-reap phase to every shutdown — including the CI
    lifespan-budget tests — for a feature that is switched off.
    """
    from server.app import create_app
    from server.codex.auth_client import reset_edge_authority_client
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with temp_env():
        # "Without a credential authority" is this test's PREMISE, so establish it
        # rather than assume it. `temp_env()` manages four variables and none of
        # them is `CCSWITCH_CODEX_AUTHORITY_*`, so on any host whose shell
        # configures a real authority — which is every Codex operator, and the
        # deployed fleet — the edge would correctly start the task and this would
        # read as a regression in whatever change happened to be in flight.
        #
        # The scope matters too: the task is created in the LIFESPAN, not in
        # `create_app()`, so the patch has to cover the `TestClient` block. That
        # also puts `_ENV`'s in-memory DB URL in force while `init_db` runs, which
        # is what the fixture was always for.
        with patch.dict(os.environ, _ENV):
            for _var in [v for v in os.environ if v.startswith("CCSWITCH_CODEX_AUTHORITY_")]:
                os.environ.pop(_var, None)
            # The edge's authority client is a process-wide cache, so a neighbouring
            # test that built one would otherwise decide this assertion for us.
            reset_edge_authority_client()
            app = create_app(
                orchestrator=SessionOrchestrator(
                    sdk_factory=fake_sdk_factory(), poll_interval=9999.0
                )
            )
            with TestClient(app):
                assert app.state.codex_auth_sync_task is None
    # Leave no client built from the temp environment behind for the next test.
    reset_edge_authority_client()
