"""CredentialAuthority, CredentialStore and InstanceLock.

The credential authority is the one ccswitch process that holds a ChatGPT refresh
token, so its tests are written against the *real* artefacts rather than stubs of
them: a real-shaped ``auth.json`` from a 0.153.2 install, a real (unsigned) JWT so
``decode_claims`` is exercised for real, real ``flock`` semantics, and real
``asyncio`` concurrency for the refresh-coalescing paths. The only injected seams
are the two the authority itself declares — ``login_runner`` / ``refresh_runner``
— plus the clock, because a lease is a statement about time.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import stat
import time
from pathlib import Path

import pytest

from server.codex.auth_authority import CredentialAuthority, InstanceLock
from server.codex.auth_store import AUTH_CLAIM, CredentialStore, decode_claims
from server.codex.auth_types import (
    LOGIN_AUTHORIZED,
    LOGIN_AWAITING_AUTHORIZATION,
    LOGIN_FAILED,
    LOGIN_PENDING,
    AuthorityError,
    AuthorityErrorCode,
    AuthorizationRecord,
    AuthorizationSync,
    LoginSnapshot,
)

ACCOUNT = "openai-primary"
CHATGPT_ACCOUNT_ID = "e5b1f0aa-2f2e-4a1e-9f2c-cafe00000001"
REFRESH_TOKEN = "rt_9f1c8c1a6f0e4bd4a1d2e3f4a5b6c7d8"


# ---------------------------------------------------------------------------
# Real-shaped credential cache fixtures. Imported by the RPC and redaction
# suites too, so there is one definition of "what auth.json looks like".
# ---------------------------------------------------------------------------


def _b64url(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_access_token(
    *,
    chatgpt_account_id: str = CHATGPT_ACCOUNT_ID,
    plan_type: str | None = "pro",
    exp: int | None = None,
    nonce: str = "0",
) -> str:
    """A real unsigned JWT: base64url ``header.payload.signature``.

    Not signed, and deliberately so — the store does not verify signatures (the
    API does). ``nonce`` only exists so a refresh can produce a *different* token
    string, which is what makes "the second envelope is not the first" testable.
    """
    header = _b64url({"alg": "none", "typ": "JWT"})
    claims: dict[str, object] = {
        AUTH_CLAIM: {
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_plan_type": plan_type,
        },
        "exp": int(exp if exp is not None else time.time() + 3600),
        "nonce": nonce,
    }
    return f"{header}.{_b64url(claims)}.c2lnbmF0dXJlLXBsYWNlaG9sZGVy{nonce}"


def auth_json_body(access_token: str) -> dict[str, object]:
    """The shape a real ``codex login`` writes."""
    return {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": make_access_token(nonce="id"),
            "access_token": access_token,
            "refresh_token": REFRESH_TOKEN,
            "account_id": CHATGPT_ACCOUNT_ID,
        },
        "last_refresh": "2026-09-05T12:00:00.000000000Z",
    }


def write_auth_json(home: Path, access_token: str) -> Path:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / "auth.json"
    path.write_text(json.dumps(auth_json_body(access_token)), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class FakeClock:
    """An injectable clock. Lease expiry is a statement about time, not a sleep."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(tmp_path / "authority-state")


def make_authority(store: CredentialStore, **kwargs: object) -> CredentialAuthority:
    return CredentialAuthority(store=store, **kwargs)  # type: ignore[arg-type]


def record(name: str = ACCOUNT, *, enabled: bool = True) -> AuthorizationRecord:
    return AuthorizationRecord(
        name=name, provider="openai", engine="codex", enabled=enabled
    )


async def install_sync(
    authority: CredentialAuthority,
    *records: AuthorizationRecord,
    revision: int = 1,
    lease_s: float = 900.0,
) -> None:
    await authority.authorize(
        AuthorizationSync(
            revision=revision,
            records=records or (record(),),
            lease_s=lease_s,
        )
    )


async def finish_login(authority: CredentialAuthority, account: str) -> LoginSnapshot:
    """Await the in-flight login task and return the settled snapshot."""
    state = authority._logins.get(account)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await state.task
    return authority.login_status(account)


# ---------------------------------------------------------------------------
# CredentialStore — the one path that reads auth.json
# ---------------------------------------------------------------------------


def test_read_envelope_parses_a_real_shaped_auth_json(tmp_path):
    store = make_store(tmp_path)
    home = store.ensure_home(ACCOUNT)
    token = make_access_token(exp=1_800_000_000)
    write_auth_json(home, token)

    envelope = store.read_envelope(ACCOUNT, generation=7)

    assert envelope.account == ACCOUNT
    assert envelope.generation == 7
    assert envelope.access_token.reveal() == token
    # Taken from the token's OWN claims, not from a sidecar we maintain.
    assert envelope.chatgpt_account_id.reveal() == CHATGPT_ACCOUNT_ID
    assert envelope.chatgpt_plan_type == "pro"
    assert envelope.expires_at == 1_800_000_000.0


def test_decode_claims_reads_the_namespaced_openai_claim():
    claims = decode_claims(make_access_token(plan_type="plus", exp=1_750_000_000))
    assert claims.chatgpt_account_id == CHATGPT_ACCOUNT_ID
    assert claims.chatgpt_plan_type == "plus"
    assert claims.expires_at == 1_750_000_000.0


def test_decode_claims_never_raises_on_junk():
    # A token whose metadata we cannot read is still a token the API may accept;
    # refusing to issue it would turn a cosmetic gap into an outage.
    assert decode_claims("not-a-jwt") == decode_claims("")
    assert decode_claims("a.!!!.c").chatgpt_account_id is None


def _no_file(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)


def _not_json(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A truncated document — the shape a crash mid-write leaves behind, and the
    # shape whose parser message would quote the token if it were echoed.
    (home / "auth.json").write_text(
        json.dumps(auth_json_body(token))[:-12], encoding="utf-8"
    )


def _no_tokens_object(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": None, "last_refresh": "x"}), encoding="utf-8"
    )


def _empty_access_token(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = auth_json_body(token)
    body["tokens"]["access_token"] = ""  # type: ignore[index]
    (home / "auth.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.mark.parametrize(
    "prepare",
    [_no_file, _not_json, _no_tokens_object, _empty_access_token],
    ids=["missing-file", "not-json", "no-tokens-object", "empty-access-token"],
)
def test_every_unusable_cache_shape_is_login_required(tmp_path, prepare):
    store = make_store(tmp_path)
    token = make_access_token()
    prepare(store.home(ACCOUNT), token)

    with pytest.raises(AuthorityError) as excinfo:
        store.read_envelope(ACCOUNT, generation=1)

    assert excinfo.value.code is AuthorityErrorCode.LOGIN_REQUIRED


@pytest.mark.parametrize(
    "prepare",
    [_not_json, _no_tokens_object, _empty_access_token],
    ids=["not-json", "no-tokens-object", "empty-access-token"],
)
def test_a_refusal_never_quotes_the_credential(tmp_path, prepare):
    # The `not-json` case is the one that matters most: a stdlib JSON error
    # quotes the document, and this document is a refresh token.
    store = make_store(tmp_path)
    token = make_access_token()
    prepare(store.home(ACCOUNT), token)

    with pytest.raises(AuthorityError) as excinfo:
        store.read_envelope(ACCOUNT, generation=1)

    message = str(excinfo.value)
    assert token not in message
    assert token[:24] not in message
    assert REFRESH_TOKEN not in message


def test_read_envelope_refuses_to_publish_at_generation_zero(tmp_path):
    store = make_store(tmp_path)
    write_auth_json(store.ensure_home(ACCOUNT), make_access_token())

    with pytest.raises(AuthorityError) as excinfo:
        store.read_envelope(ACCOUNT, generation=0)

    assert excinfo.value.code is AuthorityErrorCode.INTERNAL


def test_generations_are_monotonic_and_survive_a_new_store(tmp_path):
    store = make_store(tmp_path)
    assert store.generation(ACCOUNT) == 0
    assert [store.bump_generation(ACCOUNT) for _ in range(3)] == [1, 2, 3]

    # A restart must not re-mint a number a runner has already rejected, which is
    # why the counter is on disk rather than in memory.
    restarted = make_store(tmp_path)
    assert restarted.generation(ACCOUNT) == 3
    assert restarted.bump_generation(ACCOUNT) == 4
    assert make_store(tmp_path).generation(ACCOUNT) == 4


def test_forget_drops_the_home_and_the_generation(tmp_path):
    store = make_store(tmp_path)
    write_auth_json(store.ensure_home(ACCOUNT), make_access_token())
    store.bump_generation(ACCOUNT)

    store.forget(ACCOUNT)

    assert not store.home(ACCOUNT).exists()
    assert store.generation(ACCOUNT) == 0


def test_clear_credentials_keeps_the_generation(tmp_path):
    store = make_store(tmp_path)
    write_auth_json(store.ensure_home(ACCOUNT), make_access_token())
    store.bump_generation(ACCOUNT)

    store.clear_credentials(ACCOUNT)

    assert not store.has_credentials(ACCOUNT)
    assert store.generation(ACCOUNT) == 1


def test_assert_private_refuses_a_group_readable_home(tmp_path):
    store = make_store(tmp_path)
    home = store.ensure_home(ACCOUNT)
    os.chmod(home, 0o755)

    with pytest.raises(AuthorityError) as excinfo:
        store.assert_private(ACCOUNT)

    assert excinfo.value.code is AuthorityErrorCode.INTERNAL
    assert "0700" in str(excinfo.value)


def test_assert_private_tightens_a_world_readable_cache_then_still_refuses(tmp_path):
    store = make_store(tmp_path)
    home = store.ensure_home(ACCOUNT)
    auth = write_auth_json(home, make_access_token())
    os.chmod(auth, 0o644)

    with pytest.raises(AuthorityError) as excinfo:
        store.assert_private(ACCOUNT)

    # Tightened first — but still a refusal, because the permissive window
    # already happened and the operator must rotate rather than be silently fixed.
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert excinfo.value.code is AuthorityErrorCode.INTERNAL
    assert "rotated" in str(excinfo.value)


def test_assert_private_is_quiet_when_the_modes_are_right(tmp_path):
    store = make_store(tmp_path)
    write_auth_json(store.ensure_home(ACCOUNT), make_access_token())
    store.assert_private(ACCOUNT)  # no raise


# ---------------------------------------------------------------------------
# The authorization projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_accepts_an_equal_or_higher_revision(tmp_path):
    authority = make_authority(make_store(tmp_path))
    await install_sync(authority, record(), revision=4)
    assert authority.health().sync_revision == 4

    await install_sync(authority, record(), revision=4)
    assert authority.health().sync_revision == 4

    await install_sync(authority, record(), revision=5)
    assert authority.health().sync_revision == 5


@pytest.mark.asyncio
async def test_authorize_refuses_a_lower_revision(tmp_path):
    authority = make_authority(make_store(tmp_path))
    await install_sync(authority, record(), revision=9)

    with pytest.raises(AuthorityError) as excinfo:
        await install_sync(authority, record(), revision=8)

    assert excinfo.value.code is AuthorityErrorCode.REVISION_ROLLBACK
    # The rollback must not have been applied.
    assert authority.health().sync_revision == 9


@pytest.mark.asyncio
async def test_an_unknown_account_is_refused_by_name(tmp_path):
    authority = make_authority(make_store(tmp_path))
    await install_sync(authority, record())

    with pytest.raises(AuthorityError) as excinfo:
        await authority.get_envelope("someone-else")

    assert excinfo.value.code is AuthorityErrorCode.UNKNOWN_ACCOUNT


@pytest.mark.asyncio
async def test_no_sync_at_all_fails_closed(tmp_path):
    authority = make_authority(make_store(tmp_path))

    with pytest.raises(AuthorityError) as excinfo:
        await authority.get_envelope(ACCOUNT)

    assert excinfo.value.code is AuthorityErrorCode.SYNC_EXPIRED


@pytest.mark.asyncio
async def test_a_disabled_account_cannot_be_issued_but_can_still_be_administered(
    tmp_path,
):
    clock = FakeClock()
    store = make_store(tmp_path)

    async def login_runner(account: str, home: Path) -> None:
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600)))

    authority = make_authority(store, clock=clock, login_runner=login_runner)
    await install_sync(authority, record(enabled=False))

    with pytest.raises(AuthorityError) as excinfo:
        await authority.get_envelope(ACCOUNT)
    assert excinfo.value.code is AuthorityErrorCode.ACCOUNT_DISABLED

    # revoke-before-disable must remain possible, so administration is allowed on
    # a disabled account. login too: re-authing a disabled account is how you fix it.
    assert await authority.revoke(ACCOUNT) == 1
    snapshot = await authority.login_start(ACCOUNT)
    assert snapshot.state == LOGIN_PENDING
    assert (await finish_login(authority, ACCOUNT)).state == LOGIN_AUTHORIZED


@pytest.mark.asyncio
async def test_get_envelope_without_a_login_is_login_required(tmp_path):
    authority = make_authority(make_store(tmp_path))
    await install_sync(authority, record())

    with pytest.raises(AuthorityError) as excinfo:
        await authority.get_envelope(ACCOUNT)

    assert excinfo.value.code is AuthorityErrorCode.LOGIN_REQUIRED


# ---------------------------------------------------------------------------
# Lease expiry: a silent edge must fail CLOSED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_issuance_stops_once_the_sync_lease_lapses(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path)
    refreshes = 0

    async def refresh_runner(account: str, home: Path) -> None:
        nonlocal refreshes
        refreshes += 1
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600)))

    authority = make_authority(store, clock=clock, refresh_runner=refresh_runner)
    await install_sync(authority, record(), lease_s=120.0)
    write_auth_json(store.ensure_home(ACCOUNT), make_access_token(exp=int(clock.now + 3600)))
    store.bump_generation(ACCOUNT)

    # In force: issuance works.
    assert (await authority.get_envelope(ACCOUNT)).generation == 1

    clock.advance(121.0)

    with pytest.raises(AuthorityError) as envelope_exc:
        await authority.get_envelope(ACCOUNT)
    assert envelope_exc.value.code is AuthorityErrorCode.SYNC_EXPIRED

    with pytest.raises(AuthorityError) as refresh_exc:
        await authority.refresh(ACCOUNT)
    assert refresh_exc.value.code is AuthorityErrorCode.SYNC_EXPIRED

    # And nothing was rotated behind the refusal.
    assert refreshes == 0
    assert authority.health().sync_expires_in_s is not None
    assert authority.health().sync_expires_in_s < 0


# ---------------------------------------------------------------------------
# Refresh: coalescing, stale rejection, detaching waiters
# ---------------------------------------------------------------------------


def _refresh_authority(tmp_path, clock, runner):
    store = make_store(tmp_path)
    authority = make_authority(store, clock=clock, refresh_runner=runner)
    write_auth_json(store.ensure_home(ACCOUNT), make_access_token(exp=int(clock.now + 3600)))
    store.bump_generation(ACCOUNT)
    return store, authority


@pytest.mark.asyncio
async def test_a_refresh_storm_runs_exactly_one_refresh(tmp_path):
    clock = FakeClock()
    calls = 0
    release = asyncio.Event()

    async def refresh_runner(account: str, home: Path) -> None:
        nonlocal calls
        calls += 1
        await release.wait()
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600), nonce="2"))

    store, authority = _refresh_authority(tmp_path, clock, refresh_runner)
    await install_sync(authority, record())

    waiters = [asyncio.create_task(authority.refresh(ACCOUNT)) for _ in range(5)]
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    envelopes = await asyncio.gather(*waiters)

    assert calls == 1
    # One rotation, one generation, and every waiter got the SAME one.
    assert {e.generation for e in envelopes} == {2}
    assert store.generation(ACCOUNT) == 2


@pytest.mark.asyncio
async def test_a_stale_rejection_is_answered_without_rotating(tmp_path):
    clock = FakeClock()
    calls = 0

    async def refresh_runner(account: str, home: Path) -> None:
        nonlocal calls
        calls += 1

    store = make_store(tmp_path)

    async def login_runner(account: str, home: Path) -> None:
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600)))

    authority = make_authority(
        store, clock=clock, login_runner=login_runner, refresh_runner=refresh_runner
    )
    await install_sync(authority, record())
    for _ in range(4):
        store.bump_generation(ACCOUNT)
    await authority.login_start(ACCOUNT)
    assert (await finish_login(authority, ACCOUNT)).generation == 5

    envelope = await authority.refresh(ACCOUNT, rejected_generation=3)

    # Three runners rejecting generation 4 in sequence must not each rotate: the
    # third would publish a token the first two had been told to stop using.
    assert envelope.generation == 5
    assert calls == 0


@pytest.mark.asyncio
async def test_a_waiter_that_gives_up_does_not_cancel_the_shared_refresh(tmp_path):
    clock = FakeClock()
    release = asyncio.Event()
    calls = 0

    async def refresh_runner(account: str, home: Path) -> None:
        nonlocal calls
        calls += 1
        await release.wait()
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600), nonce="9"))

    store, authority = _refresh_authority(tmp_path, clock, refresh_runner)
    await install_sync(authority, record())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(authority.refresh(ACCOUNT), 0.05)

    # The rotation the impatient waiter detached from is still running...
    second = asyncio.create_task(authority.refresh(ACCOUNT))
    for _ in range(3):
        await asyncio.sleep(0)
    release.set()
    envelope = await second

    assert calls == 1
    assert envelope.generation == 2
    assert envelope.access_token.reveal().endswith("9")


@pytest.mark.asyncio
async def test_a_failing_refresh_runner_becomes_a_typed_refresh_failure(tmp_path):
    clock = FakeClock()

    async def refresh_runner(account: str, home: Path) -> None:
        raise RuntimeError("app-server exited before account/read")

    store, authority = _refresh_authority(tmp_path, clock, refresh_runner)
    await install_sync(authority, record())

    with pytest.raises(AuthorityError) as excinfo:
        await authority.refresh(ACCOUNT)

    assert excinfo.value.code is AuthorityErrorCode.REFRESH_FAILED
    assert excinfo.value.retryable
    # The generation must not move on a failed rotation.
    assert store.generation(ACCOUNT) == 1


@pytest.mark.asyncio
async def test_refreshing_an_account_with_no_cache_is_login_required(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path)
    authority = make_authority(store, clock=clock)
    await install_sync(authority, record())
    store.ensure_home(ACCOUNT)

    with pytest.raises(AuthorityError) as excinfo:
        await authority.refresh(ACCOUNT)

    assert excinfo.value.code is AuthorityErrorCode.LOGIN_REQUIRED


@pytest.mark.asyncio
async def test_get_envelope_refreshes_a_token_that_is_inside_the_expiry_skew(tmp_path):
    clock = FakeClock()
    calls = 0

    async def refresh_runner(account: str, home: Path) -> None:
        nonlocal calls
        calls += 1
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600), nonce="fresh"))

    store = make_store(tmp_path)
    authority = make_authority(
        store, clock=clock, refresh_runner=refresh_runner, expiry_skew_s=60.0
    )
    await install_sync(authority, record())
    # Ten seconds of life left: inside the 60 s skew, so the cache is not reused.
    write_auth_json(store.ensure_home(ACCOUNT), make_access_token(exp=int(clock.now + 10)))
    store.bump_generation(ACCOUNT)

    envelope = await authority.get_envelope(ACCOUNT)

    assert calls == 1
    assert envelope.generation == 2
    assert envelope.expires_at == clock.now + 3600


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_bumps_the_fence_drops_the_cache_and_cancels_a_live_refresh(
    tmp_path,
):
    clock = FakeClock()
    release = asyncio.Event()

    async def refresh_runner(account: str, home: Path) -> None:
        await release.wait()

    store, authority = _refresh_authority(tmp_path, clock, refresh_runner)
    await install_sync(authority, record())
    assert (await authority.get_envelope(ACCOUNT)).generation == 1

    pending = asyncio.create_task(authority.refresh(ACCOUNT, rejected_generation=1))
    for _ in range(3):
        await asyncio.sleep(0)

    generation = await authority.revoke(ACCOUNT)

    assert generation == 2
    assert store.generation(ACCOUNT) == 2
    assert authority._cache.get(ACCOUNT) is None
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()


@pytest.mark.asyncio
async def test_forget_revokes_and_then_destroys_every_trace(tmp_path):
    clock = FakeClock()
    store, authority = _refresh_authority(tmp_path, clock, None)
    await install_sync(authority, record())

    await authority.forget(ACCOUNT)

    assert not store.home(ACCOUNT).exists()
    assert store.generation(ACCOUNT) == 0


@pytest.mark.asyncio
async def test_logout_clears_the_cache_but_keeps_the_generation(tmp_path):
    clock = FakeClock()
    store, authority = _refresh_authority(tmp_path, clock, None)
    await install_sync(authority, record())
    # No vendor binary in the test environment: the vendor `codex logout` call is
    # best-effort by design, and the file removal is what has to happen.
    authority._binary = "/nonexistent/codex"

    await authority.logout(ACCOUNT)

    assert not store.has_credentials(ACCOUNT)
    assert store.generation(ACCOUNT) == 1


# ---------------------------------------------------------------------------
# Device login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_device_login_that_writes_a_cache_becomes_authorized(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path)
    seen: list[Path] = []

    async def login_runner(account: str, home: Path) -> None:
        seen.append(home)
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600)))

    authority = make_authority(store, clock=clock, login_runner=login_runner)
    await install_sync(authority, record())

    assert (await authority.login_start(ACCOUNT)).state == LOGIN_PENDING
    snapshot = await finish_login(authority, ACCOUNT)

    assert snapshot.state == LOGIN_AUTHORIZED
    assert snapshot.generation == 1
    assert snapshot.error is None
    assert seen == [store.home(ACCOUNT)]
    # The envelope is cached in memory and issuable immediately.
    assert (await authority.get_envelope(ACCOUNT)).generation == 1


@pytest.mark.asyncio
async def test_a_second_login_while_one_is_running_is_login_busy(tmp_path):
    clock = FakeClock()
    release = asyncio.Event()

    async def login_runner(account: str, home: Path) -> None:
        await release.wait()
        write_auth_json(home, make_access_token(exp=int(clock.now + 3600)))

    authority = make_authority(make_store(tmp_path), clock=clock, login_runner=login_runner)
    await install_sync(authority, record())
    await authority.login_start(ACCOUNT)

    with pytest.raises(AuthorityError) as excinfo:
        await authority.login_start(ACCOUNT)
    assert excinfo.value.code is AuthorityErrorCode.LOGIN_BUSY

    release.set()
    assert (await finish_login(authority, ACCOUNT)).state == LOGIN_AUTHORIZED
    # Terminal, so a re-login is allowed again.
    await authority.login_start(ACCOUNT)
    release.set()
    assert (await finish_login(authority, ACCOUNT)).state == LOGIN_AUTHORIZED


@pytest.mark.asyncio
async def test_a_login_runner_that_raises_fails_with_a_scrubbed_error(tmp_path):
    token = make_access_token()

    async def login_runner(account: str, home: Path) -> None:
        raise RuntimeError(f"device exchange rejected token {token}")

    authority = make_authority(make_store(tmp_path), login_runner=login_runner)
    await install_sync(authority, record())
    await authority.login_start(ACCOUNT)

    snapshot = await finish_login(authority, ACCOUNT)

    assert snapshot.state == LOGIN_FAILED
    assert snapshot.error is not None
    assert token not in snapshot.error
    # Since #1172 the guarantee is stronger than "redacted": the failure reduces to
    # a fixed category, so no part of the upstream message survives at all.
    assert snapshot.error == "runtime_error"


@pytest.mark.asyncio
async def test_a_login_that_writes_no_cache_is_a_failure_not_an_authorization(tmp_path):
    async def login_runner(account: str, home: Path) -> None:
        return None  # "succeeded" without writing auth.json

    store = make_store(tmp_path)
    authority = make_authority(store, login_runner=login_runner)
    await install_sync(authority, record())
    await authority.login_start(ACCOUNT)

    snapshot = await finish_login(authority, ACCOUNT)

    # Success is the credential cache being READABLE, never the subprocess exiting 0.
    assert snapshot.state == LOGIN_FAILED
    assert snapshot.generation is None
    assert authority._cache.get(ACCOUNT) is None


@pytest.mark.asyncio
async def test_login_cancel_settles_a_running_login(tmp_path):
    release = asyncio.Event()

    async def login_runner(account: str, home: Path) -> None:
        await release.wait()

    authority = make_authority(make_store(tmp_path), login_runner=login_runner)
    await install_sync(authority, record())
    await authority.login_start(ACCOUNT)

    snapshot = await authority.login_cancel(ACCOUNT)

    assert snapshot.state == LOGIN_FAILED
    assert snapshot.error == "cancelled"
    release.set()


def test_note_login_prompt_lifts_the_uri_and_code_out_of_vendor_output(tmp_path):
    authority = make_authority(make_store(tmp_path))
    authority._set_login(ACCOUNT, LoginSnapshot(state=LOGIN_PENDING))

    authority._note_login_prompt(
        ACCOUNT,
        "  Open https://auth.openai.com/device and enter the code ABCD-1234 ",
    )

    snapshot = authority.login_status(ACCOUNT)
    assert snapshot.state == LOGIN_AWAITING_AUTHORIZATION
    assert snapshot.user_code == "ABCD-1234"
    assert snapshot.verification_uri == "https://auth.openai.com/device"


def test_note_login_prompt_ignores_chatter_and_terminal_logins(tmp_path):
    authority = make_authority(make_store(tmp_path))
    authority._set_login(ACCOUNT, LoginSnapshot(state=LOGIN_PENDING))
    authority._note_login_prompt(ACCOUNT, "Starting device authorization flow...")
    assert authority.login_status(ACCOUNT).state == LOGIN_PENDING

    authority._set_login(ACCOUNT, LoginSnapshot(state=LOGIN_FAILED, error="cancelled"))
    authority._note_login_prompt(ACCOUNT, "https://auth.openai.com/device ABCD-1234")
    assert authority.login_status(ACCOUNT).state == LOGIN_FAILED


# ---------------------------------------------------------------------------
# InstanceLock
# ---------------------------------------------------------------------------


def test_a_second_instance_lock_refuses_rather_than_becoming_a_warm_spare(tmp_path):
    path = tmp_path / "state" / "authority.lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        assert first.held()
        with pytest.raises(AuthorityError) as excinfo:
            InstanceLock(path).acquire()
        assert excinfo.value.code is AuthorityErrorCode.NOT_AUTHORITY
        assert "no authority failover" in str(excinfo.value)
    finally:
        first.release()

    # After release the lock is takeable again, and the pid file is rewritten.
    second = InstanceLock(path)
    second.acquire()
    try:
        assert second.held()
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        second.release()
    assert not second.held()


def test_releasing_a_lock_that_was_never_taken_is_a_no_op(tmp_path):
    lock = InstanceLock(tmp_path / "state" / "authority.lock")
    lock.release()
    assert not lock.held()
