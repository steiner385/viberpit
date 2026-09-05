"""Runner-side external-token auth: fetch, refresh, dedupe, degrade, fail closed.

The property under test throughout is that a runner holds *nothing*. It fetches an
envelope, hands the token straight to the app-server, and keeps only a generation
integer. Several tests therefore assert on what is ABSENT — no retained token, no
``auth.json``, no fallback path — because those absences are the design.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from server.codex import schema
from server.codex.auth_types import (
    AuthorityError,
    AuthorityErrorCode,
    AuthorityUnavailable,
    CredentialEnvelope,
)
from server.codex.external_auth import (
    ExternalAuthProvider,
    ExternalAuthUnavailable,
    assert_no_runner_credentials,
    build_external_auth_provider,
)
from server.codex.redaction import Redacted

TOKEN = "eyJhbGciOiJIUzI1NiJ9.chatgpt-access-token-value.signature"
ACCOUNT_ID = "acct_0123456789"


def _envelope(generation: int, token: str = TOKEN) -> CredentialEnvelope:
    return CredentialEnvelope(
        account="chatgpt-pro",
        generation=generation,
        access_token=Redacted(token),
        chatgpt_account_id=Redacted(ACCOUNT_ID),
        chatgpt_plan_type="pro",
    )


class FakeAuthorityClient:
    """Counts calls, so 'coalesced to one' is provable rather than plausible."""

    def __init__(self) -> None:
        self.generation = 1
        self.envelope_calls = 0
        self.refresh_calls = 0
        self.envelope_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.gate: asyncio.Event | None = None
        #: When set, refresh answers with this generation instead of advancing.
        self.refresh_generation: int | None = None

    async def envelope(self, account: str, *, minimum_generation: int = 0):
        self.envelope_calls += 1
        if self.envelope_error is not None:
            raise self.envelope_error
        return _envelope(self.generation)

    async def refresh(self, account: str, *, rejected_generation: int = 0):
        self.refresh_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refresh_generation is not None:
            return _envelope(self.refresh_generation)
        self.generation += 1
        return _envelope(self.generation, token=f"{TOKEN}.{self.generation}")


def _refresh_params(
    reason: str = schema.REFRESH_REASON_UNAUTHORIZED,
    previous_account_id: str | None = ACCOUNT_ID,
):
    return schema.ChatgptAuthTokensRefreshParams(
        reason=reason,
        previous_account_id=(
            None if previous_account_id is None else Redacted(previous_account_id)
        ),
    )


# ---------------------------------------------------------------------------
# login_params
# ---------------------------------------------------------------------------


def test_login_params_carry_the_envelope_and_nothing_is_retained():
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)

    async def go():
        return await provider.login_params("chatgpt-pro")

    params = asyncio.run(go())
    assert isinstance(params, schema.ExternalAuthLoginParams)
    assert params.access_token.reveal() == TOKEN
    assert params.chatgpt_plan_type == "pro"
    assert provider.generation_for("chatgpt-pro") == 1

    # The whole point: the provider kept the FENCE, not the credential.
    for value in vars(provider).values():
        assert TOKEN not in repr(value)
        assert ACCOUNT_ID not in repr(value)


def test_login_params_asks_for_at_least_the_generation_we_already_had():
    """A runner that was told 'this is stale' must not be handed the same one back."""
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)
    seen: list[int] = []
    real = client.envelope

    async def _spy(account, *, minimum_generation=0):
        seen.append(minimum_generation)
        return await real(account, minimum_generation=minimum_generation)

    client.envelope = _spy  # type: ignore[method-assign]

    async def go():
        await provider.login_params("chatgpt-pro")
        await provider._refresh("chatgpt-pro", reason="unauthorized")
        await provider.login_params("chatgpt-pro")

    asyncio.run(go())
    assert seen == [0, 2]


def test_login_params_rejects_an_envelope_below_the_requested_minimum():
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)

    async def go():
        await provider.login_params("chatgpt-pro")
        await provider._refresh("chatgpt-pro", reason="unauthorized")
        client.generation = 1
        await provider.login_params("chatgpt-pro")

    with pytest.raises(ExternalAuthUnavailable, match="older generation"):
        asyncio.run(go())


def test_login_params_fails_closed_on_a_typed_refusal():
    client = FakeAuthorityClient()
    client.envelope_error = AuthorityError(
        AuthorityErrorCode.ACCOUNT_DISABLED, "disabled"
    )
    provider = ExternalAuthProvider(client)

    async def go():
        await provider.login_params("chatgpt-pro")

    with pytest.raises(ExternalAuthUnavailable) as exc:
        asyncio.run(go())
    assert "account_disabled" in str(exc.value)


def test_login_params_fails_closed_when_the_authority_is_unreachable():
    client = FakeAuthorityClient()
    client.envelope_error = AuthorityUnavailable("connection refused")
    provider = ExternalAuthProvider(client)

    async def go():
        await provider.login_params("chatgpt-pro")

    with pytest.raises(ExternalAuthUnavailable):
        asyncio.run(go())


# ---------------------------------------------------------------------------
# refresh callback
# ---------------------------------------------------------------------------


def test_refresh_handler_answers_with_a_newer_generation():
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        return await handler(_refresh_params())

    response = asyncio.run(go())
    assert isinstance(response, schema.ChatgptAuthTokensRefreshResponse)
    assert response.access_token.reveal().endswith(".2")
    assert provider.generation_for("chatgpt-pro") == 2


@pytest.mark.parametrize("previous", [None, "different-native-account"])
def test_refresh_handler_refuses_a_missing_or_mismatched_previous_account(previous):
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        await handler(_refresh_params(previous_account_id=previous))

    with pytest.raises(ExternalAuthUnavailable):
        asyncio.run(go())
    assert client.refresh_calls == 0


def test_login_refuses_an_envelope_for_a_different_account_alias():
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)

    async def wrong_alias(account: str, *, minimum_generation: int = 0):
        return CredentialEnvelope(
            account="other-alias",
            generation=1,
            access_token=Redacted(TOKEN),
            chatgpt_account_id=Redacted(ACCOUNT_ID),
        )

    client.envelope = wrong_alias  # type: ignore[method-assign]
    with pytest.raises(ExternalAuthUnavailable):
        asyncio.run(provider.login_params("chatgpt-pro"))


def test_concurrent_refreshes_coalesce_to_one_authority_call():
    """Three threads on one app-server 401 at once; the authority sees one refresh."""
    client = FakeAuthorityClient()
    client.gate = None
    provider = ExternalAuthProvider(client)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        client.gate = asyncio.Event()
        tasks = [asyncio.create_task(handler(_refresh_params())) for _ in range(3)]
        await asyncio.sleep(0)
        client.gate.set()
        return await asyncio.gather(*tasks)

    responses = asyncio.run(go())
    assert client.refresh_calls == 1
    tokens = {r.access_token.reveal() for r in responses}
    assert len(tokens) == 1


def test_a_waiter_that_times_out_does_not_cancel_the_shared_refresh():
    """One app-server request expiring must not abort the rotation others await."""
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        client.gate = asyncio.Event()
        impatient = asyncio.create_task(handler(_refresh_params()))
        patient = asyncio.create_task(handler(_refresh_params()))
        await asyncio.sleep(0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(impatient), 0.01)
        client.gate.set()
        return await patient

    response = asyncio.run(go())
    assert response.access_token.reveal().endswith(".2")
    assert client.refresh_calls == 1


def test_a_refresh_that_returns_an_already_rejected_generation_is_a_failure():
    """Answering with the credential that just 401'd would loop the app-server."""
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        client.refresh_generation = 1  # same generation we already had
        await handler(_refresh_params())

    with pytest.raises(ExternalAuthUnavailable) as exc:
        asyncio.run(go())
    assert "already rejected" in str(exc.value)


def test_a_settled_refusal_degrades_immediately_and_stops_asking():
    """`revoked` will not become false by asking again; stop, and say why."""
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        client.refresh_error = AuthorityError(AuthorityErrorCode.REVOKED, "revoked")
        for _ in range(3):
            with pytest.raises(ExternalAuthUnavailable):
                await handler(_refresh_params())

    asyncio.run(go())
    assert provider.degraded_reason("chatgpt-pro") == "revoked"
    # One attempt, then refused locally: a revocation must not become a request loop.
    assert client.refresh_calls == 1


def test_a_retryable_failure_degrades_only_after_the_cap():
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client, max_consecutive_failures=3)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        client.refresh_error = AuthorityError(
            AuthorityErrorCode.REFRESH_FAILED, "upstream said no"
        )
        for _ in range(5):
            with pytest.raises(ExternalAuthUnavailable):
                await handler(_refresh_params())

    asyncio.run(go())
    assert provider.degraded_reason("chatgpt-pro") == "refresh_failed"
    assert client.refresh_calls == 3


def test_a_successful_refresh_clears_a_previous_failure_count():
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client, max_consecutive_failures=3)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        client.refresh_error = AuthorityError(
            AuthorityErrorCode.REFRESH_FAILED, "transient"
        )
        for _ in range(2):
            with pytest.raises(ExternalAuthUnavailable):
                await handler(_refresh_params())
        client.refresh_error = None
        await handler(_refresh_params())
        client.refresh_error = AuthorityError(
            AuthorityErrorCode.REFRESH_FAILED, "transient"
        )
        for _ in range(2):
            with pytest.raises(ExternalAuthUnavailable):
                await handler(_refresh_params())

    asyncio.run(go())
    # Two failures, a success (reset), two more failures — still under the cap of 3.
    assert provider.degraded_reason("chatgpt-pro") is None


# ---------------------------------------------------------------------------
# Epoch fencing: a handler from a superseded manager must refuse
# ---------------------------------------------------------------------------


def test_a_handler_from_a_superseded_manager_refuses():
    """The manager cannot un-register a callback inside a running app-server.

    So the callback has to notice it is stale. Without this, an app-server that
    401s during a revocation teardown would be handed a fresh credential for the
    account we are in the middle of stopping.
    """
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)
    handler = provider.refresh_handler_for("chatgpt-pro")

    async def go():
        await provider.login_params("chatgpt-pro")
        provider.invalidate("chatgpt-pro")
        await handler(_refresh_params())

    with pytest.raises(ExternalAuthUnavailable) as exc:
        asyncio.run(go())
    assert "superseded" in str(exc.value)
    assert client.refresh_calls == 0


def test_invalidate_clears_generation_degraded_state_and_in_flight_refresh():
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)

    async def go():
        await provider.login_params("chatgpt-pro")
        assert provider.generation_for("chatgpt-pro") == 1
        provider.invalidate("chatgpt-pro")

    asyncio.run(go())
    assert provider.generation_for("chatgpt-pro") == 0
    assert provider.degraded_reason("chatgpt-pro") is None


def test_a_handler_built_after_invalidation_works_again():
    """Invalidation orphans OLD handlers; it does not disable the account forever."""
    client = FakeAuthorityClient()
    provider = ExternalAuthProvider(client)

    async def go():
        await provider.login_params("chatgpt-pro")
        provider.invalidate("chatgpt-pro")
        fresh = provider.refresh_handler_for("chatgpt-pro")
        await provider.login_params("chatgpt-pro")
        return await fresh(_refresh_params())

    response = asyncio.run(go())
    assert response.access_token.reveal().endswith(".2")


# ---------------------------------------------------------------------------
# No credential on a runner, ever
# ---------------------------------------------------------------------------


def test_assert_no_runner_credentials_passes_on_a_clean_home(tmp_path):
    assert_no_runner_credentials(tmp_path)


def test_assert_no_runner_credentials_refuses_an_auth_json(tmp_path):
    (tmp_path / "auth.json").write_text("{}")
    with pytest.raises(ExternalAuthUnavailable) as exc:
        assert_no_runner_credentials(tmp_path)
    assert "must never hold a credential cache" in str(exc.value)


def test_the_manager_refuses_a_runner_that_acquired_a_credential(tmp_path, monkeypatch):
    """The check runs where it matters: right after the app-server was given a token."""
    from server.codex import manager as mgr_mod
    from server.codex.capabilities import CodexAvailability
    from server.tests.fakes.fake_codex_app_server import FakeCodexAppServer

    home = tmp_path / "chatgpt-pro"
    home.mkdir()
    (home / "auth.json").write_text('{"tokens": {}}')

    class _Process:
        def __init__(self) -> None:
            self.server = FakeCodexAppServer()
            self.server.rule(
                "initialize", result=FakeCodexAppServer.default_initialize_response()
            )
            self.server.rule(
                "account/login/start", result={"type": "chatgptAuthTokens"}
            )
            self.pid = 4242

        @property
        def transport(self):
            return self.server.transport

        async def start(self) -> None:
            self.server.start_autopilot()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

        async def stop(self, timeout: float = 5.0) -> None:
            await self.server.stop_autopilot()
            self.server.eof()

        def stderr_tail(self) -> str:
            return ""

    async def _credentials(account: str):
        return schema.ExternalAuthLoginParams(
            access_token=Redacted(TOKEN), chatgpt_account_id=Redacted(ACCOUNT_ID)
        )

    manager = mgr_mod.CodexRunnerManager(
        availability=CodexAvailability.ready(
            binary_path="/usr/bin/codex", cli_version=schema.PINNED_CLI_VERSION
        ),
        codex_home_base=tmp_path,
        credential_supplier=_credentials,
        process_factory=lambda **kw: _Process(),
        home_factory=lambda base, account: Path(base) / account,
    )

    async def go():
        await manager.ensure("chatgpt-pro")

    with pytest.raises(mgr_mod.CodexStartError) as exc:
        asyncio.run(go())
    assert "credential cache" in str(exc.value)


# ---------------------------------------------------------------------------
# Startup: fail closed, never fall back
# ---------------------------------------------------------------------------


def _settings(**kw):
    return types.SimpleNamespace(**kw)


def test_build_refuses_when_the_experimental_surface_is_disabled():
    with pytest.raises(ExternalAuthUnavailable) as exc:
        build_external_auth_provider(_settings(codex_external_auth_enabled=False))
    assert "EXTERNAL_AUTH_ENABLED" in str(exc.value)


def test_build_refuses_when_no_authority_endpoint_is_configured(monkeypatch):
    monkeypatch.delenv("CCSWITCH_CODEX_AUTHORITY_ENDPOINT", raising=False)
    with pytest.raises(ExternalAuthUnavailable) as exc:
        build_external_auth_provider(_settings(codex_external_auth_enabled=True))
    assert "AUTHORITY_ENDPOINT" in str(exc.value)


def test_build_refuses_when_the_mtls_material_is_incomplete(monkeypatch):
    monkeypatch.setenv("CCSWITCH_CODEX_AUTHORITY_ENDPOINT", "https://dobby:8443")
    monkeypatch.delenv("CCSWITCH_CODEX_AUTHORITY_CLIENT_CERT", raising=False)
    with pytest.raises(ExternalAuthUnavailable):
        build_external_auth_provider(_settings(codex_external_auth_enabled=True))


def test_build_codex_manager_returns_none_rather_than_managed_auth(monkeypatch):
    """No authority => no Codex. There is deliberately no degraded mode.

    A manager built with managed runner credentials would silently put a refresh
    token back on a session host — the exact property Phase 3 removes.
    """
    from server.codex import manager as mgr_mod
    from server.codex import process as proc
    from server.codex.capabilities import CodexAvailability

    async def _available(**kw):
        return CodexAvailability.ready(
            binary_path="/usr/bin/codex", cli_version=schema.PINNED_CLI_VERSION
        )

    monkeypatch.setattr(proc, "probe_availability", _available)
    monkeypatch.delenv("CCSWITCH_CODEX_AUTHORITY_ENDPOINT", raising=False)
    settings = _settings(
        codex_enabled=True,
        codex_external_auth_enabled=True,
        codex_bin="codex",
        codex_supported_versions="",
        codex_idle_grace_s=300.0,
    )
    assert asyncio.run(mgr_mod.build_codex_manager(settings)) is None
