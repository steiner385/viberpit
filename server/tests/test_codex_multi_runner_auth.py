"""Two runners, one authority: the regression form of the #1116 spike.

PR #1116 proved this end-to-end against the real ``codex-cli 0.153.2`` with
``scripts/spike_codex_external_auth.py``: two distinct runner hosts, both firing
``account/chatgptAuthTokens/refresh``, coalescing to **one** authority refresh and
**one** credential generation, with a clean home scan on both runners. That was a
scratch implementation and it is not run in CI.

This file re-asserts the same invariants against the *production* modules —
:class:`~server.codex.external_auth.ExternalAuthProvider` and
:class:`~server.codex.auth_authority.CredentialAuthority` — wired to each other
exactly as the deployed system wires them, with only the vendor subprocess faked.
The counters asserted here (``refresh_calls == 1``, one generation advance, no
``auth.json`` on either runner) are the spike's release-gate fields, so a
regression fails the same way the spike would have.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import pytest

from server.codex.auth_authority import CredentialAuthority
from server.codex.auth_store import CredentialStore
from server.codex.auth_types import (
    AuthorityErrorCode,
    AuthorizationRecord,
    AuthorizationSync,
)
from server.codex.external_auth import (
    ExternalAuthProvider,
    ExternalAuthUnavailable,
    assert_no_runner_credentials,
)
from server.codex.redaction import Redacted

ACCOUNT = "chatgpt-pro"
CHATGPT_ACCOUNT_ID = "ec35f85c-8981-4d2f-9f0e-000000000000"


def _jwt(payload: dict) -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.signature"


def _write_auth_json(home: Path, *, nonce: str) -> str:
    """Write a real-shaped ``auth.json`` and return its access token.

    The shape is the one read off a live 0.153.2 install: the token's own claims
    carry the ChatGPT account id, plan type and expiry, which is why the store
    needs no sidecar of its own.
    """
    home.mkdir(parents=True, exist_ok=True)
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": CHATGPT_ACCOUNT_ID,
                "chatgpt_plan_type": "pro",
            },
            "exp": int(time.time()) + 3600,
            "nonce": nonce,
        }
    )
    path = home / "auth.json"
    path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _jwt({"email": "operator@example.com"}),
                    "access_token": token,
                    "refresh_token": f"rt.1.{nonce}",
                    "account_id": CHATGPT_ACCOUNT_ID,
                },
                "last_refresh": "2026-09-05T08:00:00.000Z",
            }
        )
    )
    path.chmod(0o600)
    return token


class _AuthorityOverRpc:
    """Client shim: what a runner sees, with the runner-role ACL applied.

    The transport itself is covered by ``test_codex_auth_rpc.py``; what matters
    here is that a runner reaches **only** ``envelope`` and ``refresh``, so this
    shim exposes exactly those two and nothing else. A test that called
    ``authority.revoke`` through it would not compile, which is the point.
    """

    def __init__(self, authority: CredentialAuthority) -> None:
        self._authority = authority
        self.envelope_calls = 0
        self.refresh_calls = 0

    async def envelope(self, account: str, *, minimum_generation: int = 0):
        self.envelope_calls += 1
        return await self._authority.get_envelope(
            account, minimum_generation=minimum_generation
        )

    async def refresh(self, account: str, *, rejected_generation: int = 0):
        self.refresh_calls += 1
        return await self._authority.refresh(
            account, rejected_generation=rejected_generation
        )


@pytest.fixture
def fleet(tmp_path):
    """One authority, its managed home seeded, and two runner providers."""
    store = CredentialStore(tmp_path / "authority")
    home = store.ensure_home(ACCOUNT)
    _write_auth_json(home, nonce="gen1")
    store.bump_generation(ACCOUNT)

    refreshes: list[int] = []

    async def _refresh_runner(account: str, managed_home: Path) -> None:
        # Stands in for the short-lived `codex app-server` doing
        # `account/read {refreshToken: true}`: it rotates the vendor cache.
        refreshes.append(1)
        _write_auth_json(managed_home, nonce=f"gen{len(refreshes) + 1}")

    authority = CredentialAuthority(store=store, refresh_runner=_refresh_runner)
    dobby_home = tmp_path / "runners" / "dobby" / ACCOUNT
    rocky_home = tmp_path / "runners" / "rocky" / ACCOUNT
    dobby_home.mkdir(parents=True)
    rocky_home.mkdir(parents=True)

    dobby_client = _AuthorityOverRpc(authority)
    rocky_client = _AuthorityOverRpc(authority)
    return {
        "store": store,
        "authority": authority,
        "refreshes": refreshes,
        "dobby": ExternalAuthProvider(dobby_client),
        "rocky": ExternalAuthProvider(rocky_client),
        "dobby_client": dobby_client,
        "rocky_client": rocky_client,
        "dobby_home": dobby_home,
        "rocky_home": rocky_home,
    }


def _authorize(authority: CredentialAuthority, *, enabled: bool = True):
    return authority.authorize(
        AuthorizationSync(
            revision=1,
            records=(
                AuthorizationRecord(
                    name=ACCOUNT, provider="openai", engine="codex", enabled=enabled
                ),
            ),
        )
    )


def test_both_runners_log_in_on_the_same_generation(fleet):
    async def go():
        await _authorize(fleet["authority"])
        a = await fleet["dobby"].login_params(ACCOUNT)
        b = await fleet["rocky"].login_params(ACCOUNT)
        return a, b

    a, b = asyncio.run(go())
    assert a.access_token.reveal() == b.access_token.reveal()
    assert a.chatgpt_account_id.reveal() == CHATGPT_ACCOUNT_ID
    assert fleet["dobby"].generation_for(ACCOUNT) == 1
    assert fleet["rocky"].generation_for(ACCOUNT) == 1
    # No rotation happened just because two runners asked.
    assert fleet["refreshes"] == []


def test_two_runners_refreshing_at_once_coalesce_to_one_generation(fleet):
    """The spike's headline gate: 2 callbacks -> 1 refresh -> 1 new generation."""
    from server.codex import schema

    async def go():
        await _authorize(fleet["authority"])
        await fleet["dobby"].login_params(ACCOUNT)
        await fleet["rocky"].login_params(ACCOUNT)
        params = schema.ChatgptAuthTokensRefreshParams(
            reason=schema.REFRESH_REASON_UNAUTHORIZED,
            previous_account_id=Redacted(CHATGPT_ACCOUNT_ID),
        )
        return await asyncio.gather(
            fleet["dobby"].refresh_handler_for(ACCOUNT)(params),
            fleet["rocky"].refresh_handler_for(ACCOUNT)(params),
        )

    first, second = asyncio.run(go())

    # Exactly one rotation of the vendor cache, and one generation advance.
    assert fleet["refreshes"] == [1]
    assert fleet["store"].generation(ACCOUNT) == 2
    # Both runners got the SAME new credential, and it is not the old one.
    assert first.access_token.reveal() == second.access_token.reveal()
    assert fleet["dobby"].generation_for(ACCOUNT) == 2
    assert fleet["rocky"].generation_for(ACCOUNT) == 2
    # Two runner-side callbacks reached the authority; it coalesced them.
    assert fleet["dobby_client"].refresh_calls == 1
    assert fleet["rocky_client"].refresh_calls == 1


def test_a_second_runner_rejecting_a_superseded_generation_gets_the_current_one(fleet):
    """rocky 401s on generation 1 after dobby already rotated to 2.

    The stale rejection must NOT trigger a second rotation — the credential rocky
    is complaining about has already been replaced.
    """
    from server.codex import schema

    params = schema.ChatgptAuthTokensRefreshParams(
        reason=schema.REFRESH_REASON_UNAUTHORIZED,
        previous_account_id=Redacted(CHATGPT_ACCOUNT_ID),
    )

    async def go():
        await _authorize(fleet["authority"])
        await fleet["dobby"].login_params(ACCOUNT)
        await fleet["rocky"].login_params(ACCOUNT)
        await fleet["dobby"].refresh_handler_for(ACCOUNT)(params)
        # rocky is still fenced to generation 1 and only now notices.
        return await fleet["rocky"].refresh_handler_for(ACCOUNT)(params)

    response = asyncio.run(go())
    assert fleet["refreshes"] == [1]
    assert fleet["store"].generation(ACCOUNT) == 2
    assert response.access_token.reveal().endswith("signature")
    assert fleet["rocky"].generation_for(ACCOUNT) == 2


def test_neither_runner_home_ever_holds_a_credential(fleet):
    """The design's central claim, checked on both hosts after a full cycle."""
    from server.codex import schema

    async def go():
        await _authorize(fleet["authority"])
        await fleet["dobby"].login_params(ACCOUNT)
        await fleet["rocky"].login_params(ACCOUNT)
        params = schema.ChatgptAuthTokensRefreshParams(
            reason=schema.REFRESH_REASON_UNAUTHORIZED,
            previous_account_id=Redacted(CHATGPT_ACCOUNT_ID),
        )
        await fleet["dobby"].refresh_handler_for(ACCOUNT)(params)

    asyncio.run(go())
    assert_no_runner_credentials(fleet["dobby_home"])
    assert_no_runner_credentials(fleet["rocky_home"])
    # And the authority's own home is NOT inside either runner's tree.
    authority_home = fleet["store"].home(ACCOUNT).resolve()
    for runner_home in (fleet["dobby_home"], fleet["rocky_home"]):
        assert not str(authority_home).startswith(str(runner_home.resolve()))


def test_revocation_stops_both_runners_at_the_authority(fleet):
    """After revoke + disable, neither runner can obtain a credential again."""
    from server.codex import schema

    params = schema.ChatgptAuthTokensRefreshParams(
        reason=schema.REFRESH_REASON_UNAUTHORIZED,
        previous_account_id=Redacted(CHATGPT_ACCOUNT_ID),
    )

    async def go():
        await _authorize(fleet["authority"])
        await fleet["dobby"].login_params(ACCOUNT)
        await fleet["rocky"].login_params(ACCOUNT)

        await fleet["authority"].revoke(ACCOUNT)
        # The edge's next sync marks it disabled; issuance now fails closed.
        await fleet["authority"].authorize(
            AuthorizationSync(
                revision=2,
                records=(
                    AuthorizationRecord(
                        name=ACCOUNT, provider="openai", engine="codex", enabled=False
                    ),
                ),
            )
        )
        # And the fleet fan-out invalidates the runner-side providers.
        fleet["dobby"].invalidate(ACCOUNT)
        fleet["rocky"].invalidate(ACCOUNT)

        for runner in ("dobby", "rocky"):
            with pytest.raises(ExternalAuthUnavailable) as exc:
                await fleet[runner].login_params(ACCOUNT)
            assert AuthorityErrorCode.ACCOUNT_DISABLED.value in str(exc.value)

        # A callback issued BEFORE the invalidation must also refuse, without
        # even reaching the authority.
        stale = fleet["dobby"].refresh_handler_for(ACCOUNT)
        fleet["dobby"].invalidate(ACCOUNT)
        with pytest.raises(ExternalAuthUnavailable) as exc:
            await stale(params)
        assert "superseded" in str(exc.value)

    asyncio.run(go())


def test_an_expired_authorization_lease_fails_both_runners_closed(fleet):
    """A silent edge must not keep an account alive on either runner."""
    clock = {"now": 1_000_000.0}
    authority = CredentialAuthority(
        store=fleet["store"],
        refresh_runner=lambda a, h: asyncio.sleep(0),
        clock=lambda: clock["now"],
    )
    dobby = ExternalAuthProvider(_AuthorityOverRpc(authority))
    rocky = ExternalAuthProvider(_AuthorityOverRpc(authority))

    async def go():
        await authority.authorize(
            AuthorizationSync(
                revision=1,
                records=(
                    AuthorizationRecord(
                        name=ACCOUNT, provider="openai", engine="codex"
                    ),
                ),
                lease_s=60.0,
            )
        )
        await dobby.login_params(ACCOUNT)
        clock["now"] += 61.0
        for runner in (dobby, rocky):
            with pytest.raises(ExternalAuthUnavailable) as exc:
                await runner.login_params(ACCOUNT)
            assert AuthorityErrorCode.SYNC_EXPIRED.value in str(exc.value)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Authority restart
# ---------------------------------------------------------------------------


def test_an_authority_restart_keeps_issuing_without_waiting_for_a_resync(fleet, tmp_path):
    """The projection is persisted, so a restart is not a multi-minute Codex outage.

    The authority holds no sessions and should be restartable freely; before this
    it forgot every authorization on restart and failed closed until the edge's
    next periodic sync (up to 300 s).
    """
    clock = {"now": 1_000_000.0}

    async def go():
        first = CredentialAuthority(
            store=fleet["store"], clock=lambda: clock["now"]
        )
        await first.authorize(
            AuthorizationSync(
                revision=4,
                records=(
                    AuthorizationRecord(
                        name=ACCOUNT, provider="openai", engine="codex"
                    ),
                ),
                lease_s=900.0,
            )
        )
        # A brand-new instance over the SAME state directory: a restart.
        clock["now"] += 30.0
        restarted = CredentialAuthority(
            store=CredentialStore(fleet["store"].root), clock=lambda: clock["now"]
        )
        assert restarted.health().sync_revision == 4
        return await restarted.get_envelope(ACCOUNT)

    envelope = asyncio.run(go())
    assert envelope.generation == 1


def test_a_restart_cannot_extend_a_lease_the_edge_stopped_renewing(fleet):
    """The lease is measured from the ORIGINAL install, not from process start."""
    clock = {"now": 1_000_000.0}

    async def go():
        first = CredentialAuthority(
            store=fleet["store"], clock=lambda: clock["now"]
        )
        await first.authorize(
            AuthorizationSync(
                revision=4,
                records=(
                    AuthorizationRecord(
                        name=ACCOUNT, provider="openai", engine="codex"
                    ),
                ),
                lease_s=60.0,
            )
        )
        clock["now"] += 61.0
        restarted = CredentialAuthority(
            store=CredentialStore(fleet["store"].root), clock=lambda: clock["now"]
        )
        runner = ExternalAuthProvider(_AuthorityOverRpc(restarted))
        with pytest.raises(ExternalAuthUnavailable) as exc:
            await runner.login_params(ACCOUNT)
        assert AuthorityErrorCode.SYNC_EXPIRED.value in str(exc.value)

    asyncio.run(go())
