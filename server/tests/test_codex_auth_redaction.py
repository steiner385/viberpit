"""The secrecy contract: a ChatGPT credential must not leak from the authority.

``server/codex/auth_types.py`` names exactly three sanctioned reveals
(``to_login_params``, ``to_refresh_response``, ``to_wire``). Everything else —
``repr``, ``str``, f-strings, ``logging`` ``%s``, ``public_summary()``, every
``AuthorityError.to_wire()``, every log record emitted across a full
login/issue/refresh/revoke cycle, and a traceback with captured locals — must
render ``[REDACTED]``.

These tests assert the *absence* of a string, which is a weak assertion unless the
string is genuinely reachable. So every one of them drives the real objects
carrying a real-shaped token, and each first asserts that the sanctioned reveal
DOES produce the token — otherwise "the token is absent" would pass against an
envelope that never held it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from server.codex import redaction
from server.codex.auth_authority import CredentialAuthority
from server.codex.auth_store import CredentialStore
from server.codex.auth_types import (
    AuthorityError,
    AuthorityErrorCode,
    AuthorityHealth,
    AuthorizationSync,
    CredentialEnvelope,
    LoginSnapshot,
)
from server.codex.external_auth import (
    ExternalAuthProvider,
    ExternalAuthUnavailable,
    assert_no_runner_credentials,
)
from server.codex.redaction import Redacted
from server.tests.test_codex_auth_authority import (
    ACCOUNT,
    CHATGPT_ACCOUNT_ID,
    REFRESH_TOKEN,
    FakeClock,
    make_access_token,
    make_store,
    record,
    write_auth_json,
)

# A FIXED expiry, so the token string is byte-identical in every process. It is
# used as a parametrize value below, and an id that differs per worker makes
# `pytest -n auto` fail collection with "different tests were collected".
TOKEN = make_access_token(nonce="secrecy", exp=1_800_000_000)
#: A synthetic OpenAI API key. Shaped like the real thing, valid nowhere.
API_KEY = "sk-proj-T3stK3yMaterialNeverIssuedAnywhere0000"

SECRETS = (TOKEN, CHATGPT_ACCOUNT_ID, REFRESH_TOKEN)


def envelope(generation: int = 3) -> CredentialEnvelope:
    return CredentialEnvelope(
        account=ACCOUNT,
        generation=generation,
        access_token=Redacted(TOKEN),
        chatgpt_account_id=Redacted(CHATGPT_ACCOUNT_ID),
        chatgpt_plan_type="pro",
        expires_at=1_800_000_000.0,
        issued_at=1_700_000_000.0,
    )


def assert_no_secrets(rendered: str, *, extra: tuple[str, ...] = ()) -> None:
    for secret in SECRETS + extra:
        assert secret not in rendered, f"a credential leaked into: {rendered!r}"
        # Guard against a partial leak too: half a JWT is still a finding.
        assert secret[:24] not in rendered


def revealed_strings(value: Any, *, seen: set[int] | None = None) -> list[str]:
    """Every string reachable from *value*, with ``Redacted`` UNWRAPPED.

    Unwrapping is the point: an attribute that quietly parked the token inside a
    ``Redacted`` would still be a retained credential, and ``str()`` would hide it.
    """
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, Redacted):
        return [value.reveal()]
    if isinstance(value, CredentialEnvelope):
        return [
            value.access_token.reveal(),
            value.chatgpt_account_id.reveal(),
            value.account,
        ]
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        out: list[str] = []
        for key, item in value.items():
            out += revealed_strings(key, seen=seen) + revealed_strings(item, seen=seen)
        return out
    if isinstance(value, (list, tuple, set, frozenset)) or (
        isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray))
    ):
        out = []
        for item in value:
            out += revealed_strings(item, seen=seen)
        return out
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return revealed_strings(vars(value), seen=seen)
    return [str(value)]


# ---------------------------------------------------------------------------
# CredentialEnvelope rendering
# ---------------------------------------------------------------------------


def test_the_sanctioned_reveals_really_do_reveal():
    # The control for every other test in this file: the envelope genuinely holds
    # the token, so "the token is absent" below is a finding and not a tautology.
    env = envelope()
    assert env.to_wire()["accessToken"] == TOKEN
    assert env.to_wire()["chatgptAccountId"] == CHATGPT_ACCOUNT_ID
    assert env.to_login_params().access_token.reveal() == TOKEN
    assert env.to_refresh_response().access_token.reveal() == TOKEN


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda e: f"{e}",
        lambda e: f"{e!r}",
        lambda e: "{}".format(e),  # noqa: UP032 -- exercising str.format explicitly
        lambda e: f"token={e.access_token} id={e.chatgpt_account_id}",
        lambda e: json.dumps(e.public_summary()),
    ],
    ids=[
        "repr",
        "str",
        "f-string",
        "f-string-repr",
        "format",
        "field-interpolation",
        "public-summary",
    ],
)
def test_an_envelope_never_renders_its_credentials(render):
    rendered = render(envelope())
    assert_no_secrets(rendered)
    assert "REDACTED" in rendered or "access_token" not in rendered


def test_logging_percent_s_of_an_envelope_renders_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("test.codex.auth").info("issuing %s", envelope())

    message = caplog.records[-1].getMessage()
    assert_no_secrets(message)
    assert "[REDACTED]" in message


def test_public_summary_is_the_projection_the_rest_of_ccswitch_gets():
    summary = envelope(generation=9).public_summary()
    assert summary == {
        "account": ACCOUNT,
        "generation": 9,
        "planType": "pro",
        "expiresAt": 1_800_000_000.0,
        "issuedAt": 1_700_000_000.0,
    }
    assert_no_secrets(json.dumps(summary))


def test_login_snapshot_and_health_carry_no_credential():
    snapshot = LoginSnapshot(
        state="failed", error=f"device exchange failed for {TOKEN}"
    )
    assert_no_secrets(json.dumps(snapshot.to_wire()))
    assert_no_secrets(json.dumps(AuthorityHealth.unreachable(TOKEN).to_wire()))


# ---------------------------------------------------------------------------
# AuthorityError.detail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [API_KEY, TOKEN, f"refused key {API_KEY} for token {TOKEN}"],
    ids=["openai-api-key", "access-token", "both"],
)
def test_an_error_detail_is_scrubbed_at_construction(secret):
    error = AuthorityError(AuthorityErrorCode.REFRESH_FAILED, detail=secret)

    assert_no_secrets(error.detail, extra=(API_KEY,))
    assert_no_secrets(str(error), extra=(API_KEY,))
    assert_no_secrets(json.dumps(error.to_wire()), extra=(API_KEY,))
    assert redaction.REDACTED in error.detail


@pytest.mark.asyncio
async def test_every_failure_path_produces_a_credential_free_wire_error(tmp_path):
    """Collect a real AuthorityError from each code path and check its wire form."""
    clock = FakeClock()
    store = make_store(tmp_path)
    collected: dict[AuthorityErrorCode, AuthorityError] = {}

    def note(exc: AuthorityError) -> None:
        collected.setdefault(exc.code, exc)

    async def failing_refresh(account: str, home: Path) -> None:
        raise RuntimeError(f"upstream refused: token={TOKEN} key={API_KEY}")

    blocking = asyncio.Event()

    async def blocking_login(account: str, home: Path) -> None:
        await blocking.wait()

    authority = CredentialAuthority(
        store=store,
        clock=clock,
        login_runner=blocking_login,
        refresh_runner=failing_refresh,
    )

    # SYNC_EXPIRED — nothing synced yet.
    with pytest.raises(AuthorityError) as exc:
        await authority.get_envelope(ACCOUNT)
    note(exc.value)

    await authority.authorize(
        AuthorizationSync(revision=5, records=(record(),), lease_s=900.0)
    )

    # UNKNOWN_ACCOUNT
    with pytest.raises(AuthorityError) as exc:
        await authority.get_envelope("not-in-the-manifest")
    note(exc.value)

    # REVISION_ROLLBACK
    with pytest.raises(AuthorityError) as exc:
        await authority.authorize(
            AuthorizationSync(revision=4, records=(record(),), lease_s=900.0)
        )
    note(exc.value)

    # LOGIN_REQUIRED — never logged in.
    with pytest.raises(AuthorityError) as exc:
        await authority.get_envelope(ACCOUNT)
    note(exc.value)

    # LOGIN_BUSY
    await authority.login_start(ACCOUNT)
    with pytest.raises(AuthorityError) as exc:
        await authority.login_start(ACCOUNT)
    note(exc.value)
    blocking.set()
    await authority.login_cancel(ACCOUNT)

    # REFRESH_FAILED — the runner raised with the credential in its message.
    write_auth_json(store.ensure_home(ACCOUNT), TOKEN)
    store.bump_generation(ACCOUNT)
    with pytest.raises(AuthorityError) as exc:
        await authority.refresh(ACCOUNT)
    note(exc.value)

    # INTERNAL — a mode drift on the managed cache.
    (store.auth_path(ACCOUNT)).chmod(0o644)
    with pytest.raises(AuthorityError) as exc:
        store.assert_private(ACCOUNT)
    note(exc.value)

    # ACCOUNT_DISABLED
    await authority.authorize(
        AuthorizationSync(revision=6, records=(record(enabled=False),), lease_s=900.0)
    )
    with pytest.raises(AuthorityError) as exc:
        await authority.get_envelope(ACCOUNT)
    note(exc.value)

    # The transport-layer codes have no authority-side path; construct them.
    for code in (
        AuthorityErrorCode.FORBIDDEN,
        AuthorityErrorCode.BAD_REQUEST,
        AuthorityErrorCode.NOT_AUTHORITY,
        AuthorityErrorCode.REVOKED,
    ):
        note(AuthorityError(code, f"denied while holding {TOKEN}"))

    assert set(collected) == set(AuthorityErrorCode), (
        "a failure path was added without a redaction assertion: "
        f"{sorted(set(AuthorityErrorCode) - set(collected))}"
    )
    for code, error in collected.items():
        wire = json.dumps(error.to_wire())
        assert_no_secrets(wire, extra=(API_KEY,))
        assert_no_secrets(str(error), extra=(API_KEY,))
        assert wire.count(code.value) >= 1

    await authority.aclose()


# ---------------------------------------------------------------------------
# A full cycle, with every log record inspected
# ---------------------------------------------------------------------------


def _record_strings(record_: logging.LogRecord) -> list[str]:
    out = [record_.getMessage()]
    for key, value in record_.__dict__.items():
        out += revealed_strings(key)
        out += revealed_strings(value)
    return out


@pytest.mark.asyncio
async def test_a_full_login_issue_refresh_revoke_cycle_logs_no_credential(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    store = make_store(tmp_path)
    tokens = iter(
        [TOKEN, make_access_token(nonce="rotated-1"), make_access_token(nonce="rotated-2")]
    )

    async def writer(account: str, home: Path) -> None:
        write_auth_json(home, next(tokens))

    authority = CredentialAuthority(
        store=store, clock=clock, login_runner=writer, refresh_runner=writer
    )
    await authority.authorize(
        AuthorizationSync(revision=1, records=(record(),), lease_s=900.0)
    )

    await authority.login_start(ACCOUNT)
    login = authority._logins[ACCOUNT]
    assert login.task is not None
    await login.task
    assert authority.login_status(ACCOUNT).state == "authorized"

    issued = await authority.get_envelope(ACCOUNT)
    assert issued.access_token.reveal() == TOKEN  # control: it really is in play
    rotated = await authority.refresh(ACCOUNT, rejected_generation=issued.generation)
    assert rotated.generation == 2
    assert await authority.revoke(ACCOUNT) == 3
    await authority.aclose()

    assert caplog.records, "the cycle emitted no log records at all"
    for entry in caplog.records:
        for rendered in _record_strings(entry):
            assert_no_secrets(rendered, extra=(API_KEY,))

    # The alias IS logged — that is the audit trail, and it is public in the manifest.
    assert any(ACCOUNT in s for e in caplog.records for s in _record_strings(e))


# ---------------------------------------------------------------------------
# Tracebacks
# ---------------------------------------------------------------------------


def _handler_that_raises(env: CredentialEnvelope) -> None:
    login_params = env.to_login_params()  # noqa: F841 -- deliberately a live local
    raise RuntimeError("the app-server rejected account/login/start")


def test_a_credential_never_reaches_a_traceback():
    env = envelope()
    try:
        _handler_that_raises(env)
    except RuntimeError as exc:
        formatted = traceback.format_exc()
        # The real risk is a locals-capturing formatter (support bundles, rich
        # tracebacks, faulthandler dumps): it reprs every frame local, and the
        # frames here hold both the envelope and its login params.
        with_locals = "".join(
            traceback.TracebackException.from_exception(
                exc, capture_locals=True
            ).format()
        )
    else:  # pragma: no cover -- the helper always raises
        pytest.fail("the handler did not raise")

    assert "_handler_that_raises" in formatted
    assert "login_params" in with_locals  # control: locals really were captured
    assert_no_secrets(formatted)
    assert_no_secrets(with_locals)
    assert "[REDACTED]" in with_locals


def test_scrub_exception_reduces_a_failure_to_a_fixed_category():
    """The authority surfaces exception CATEGORIES, never messages.

    `redaction.scrub_exception` is owned by that module and tested there; what is
    asserted here is only the property this package depends on — that a message
    carrying a credential cannot reach an authority error, a login snapshot, or a
    log line through it.
    """
    rendered = redaction.scrub_exception(
        ValueError(f"line one {TOKEN}\nline two {API_KEY}\r")
    )

    assert rendered == "validation_error"
    assert "\n" not in rendered and "\r" not in rendered
    assert_no_secrets(rendered, extra=(API_KEY,))


# ---------------------------------------------------------------------------
# The runner side holds nothing
# ---------------------------------------------------------------------------


def test_assert_no_runner_credentials_passes_on_a_clean_home(tmp_path):
    home = tmp_path / "runner-home"
    home.mkdir()
    (home / "config.toml").write_text("model = 'gpt-5'\n", encoding="utf-8")
    assert_no_runner_credentials(home)  # no raise


def test_assert_no_runner_credentials_refuses_a_home_that_acquired_a_cache(tmp_path):
    home = tmp_path / "runner-home"
    write_auth_json(home, TOKEN)

    with pytest.raises(ExternalAuthUnavailable) as excinfo:
        assert_no_runner_credentials(home)

    assert "must never hold a credential cache" in str(excinfo.value)
    assert_no_secrets(str(excinfo.value))


class RecordingAuthorityClient:
    """The two calls ``ExternalAuthProvider`` makes. No transport, no I/O."""

    def __init__(self) -> None:
        self.envelope_calls: list[int] = []
        self.refresh_calls: list[int] = []

    async def envelope(
        self, account: str, *, minimum_generation: int = 0
    ) -> CredentialEnvelope:
        self.envelope_calls.append(minimum_generation)
        return envelope(generation=3)

    async def refresh(
        self, account: str, *, rejected_generation: int = 0
    ) -> CredentialEnvelope:
        self.refresh_calls.append(rejected_generation)
        return envelope(generation=4)


@pytest.mark.asyncio
async def test_the_runner_provider_retains_no_envelope_after_a_login(caplog):
    caplog.set_level(logging.DEBUG)
    client = RecordingAuthorityClient()
    provider = ExternalAuthProvider(client)  # type: ignore[arg-type]

    params = await provider.login_params(ACCOUNT)

    # Control: the caller really did receive the credential.
    assert params.access_token.reveal() == TOKEN
    assert params.chatgpt_account_id.reveal() == CHATGPT_ACCOUNT_ID

    # ...and the provider kept exactly one thing: an integer.
    assert provider.generation_for(ACCOUNT) == 3
    for value in revealed_strings(vars(provider)):
        assert value not in SECRETS
        assert TOKEN[:24] not in value

    handler = provider.refresh_handler_for(ACCOUNT)
    from server.codex import schema

    response = await handler(
        schema.ChatgptAuthTokensRefreshParams(reason="unauthorized")
    )
    assert response.access_token.reveal() == TOKEN
    assert client.refresh_calls == [3]
    assert provider.generation_for(ACCOUNT) == 4
    for value in revealed_strings(vars(provider)):
        assert value not in SECRETS
        assert TOKEN[:24] not in value

    for entry in caplog.records:
        for rendered in _record_strings(entry):
            assert_no_secrets(rendered)


def test_the_provider_repr_names_accounts_and_nothing_else():
    provider = ExternalAuthProvider(RecordingAuthorityClient())  # type: ignore[arg-type]
    asyncio.run(provider.login_params(ACCOUNT))
    rendered = repr(provider)

    assert ACCOUNT in rendered
    assert_no_secrets(rendered)


def test_a_runner_home_and_the_authority_home_are_not_the_same_place(tmp_path):
    # The claim `assert_no_runner_credentials` protects: the authority's managed
    # home holds auth.json, and it is not inside any runner's CODEX_HOME.
    store = CredentialStore(tmp_path / "authority-state")
    authority_home = store.ensure_home(ACCOUNT)
    write_auth_json(authority_home, TOKEN)
    runner_home = tmp_path / "runner-homes" / ACCOUNT
    runner_home.mkdir(parents=True)

    assert_no_runner_credentials(runner_home)
    assert store.has_credentials(ACCOUNT)
    assert authority_home not in runner_home.parents
