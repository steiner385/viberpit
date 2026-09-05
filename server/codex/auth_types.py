"""The vocabulary shared by the ChatGPT credential authority and its clients.

The authority (:mod:`server.codex.auth_authority`), its wire server
(:mod:`server.codex.auth_server`), its client (:mod:`server.codex.auth_client`)
and the runner-side supplier (:mod:`server.codex.external_auth`) are four
processes' worth of code that must agree on exactly three things: what a
credential envelope is, what an authorization record is, and how a failure is
named. All three are defined **here, once** — a second copy of the error
vocabulary in the client would be the parallel-implementation shape the
constitution forbids, and it is the shape that hurts most here, because a client
that does not recognise ``account_disabled`` degrades it to "some error" and
retries a revocation forever.

Secrecy contract
----------------
:class:`CredentialEnvelope` is the *only* type in ccswitch that carries a live
ChatGPT access token, and it carries it as
:class:`~server.codex.redaction.Redacted`, so the dataclass ``repr`` — and any
traceback frame dump, log record, or f-string that formats it — renders
``[REDACTED]``. Two methods deliberately break that seal, and only two:

* :meth:`CredentialEnvelope.to_login_params` / :meth:`to_refresh_response`,
  which hand the value to ``server/codex/schema.py`` DTOs that reveal it exactly
  once, at the JSON-RPC serialization boundary into the app-server's stdin; and
* :meth:`CredentialEnvelope.to_wire`, which reveals it for the authority→runner
  mTLS hop.

There is no third. An envelope is never persisted, never put in the DB, the
ledger, the event ring, an audit row, or a metric — :meth:`public_summary` is
what those surfaces get.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from server.codex import redaction, schema
from server.codex.redaction import Redacted

#: Protocol version of the authority RPC surface. Bumped when a method's params
#: or result change shape incompatibly; the server refuses a mismatched client
#: rather than guessing, because a guess here is a guess about credentials.
AUTHORITY_PROTOCOL_VERSION: Final = 1

#: Device-login state vocabulary. Deliberately the SAME four words
#: ``server/sessions/copilot_auth.py`` already uses (and that
#: ``web/src/components/AddAccountDialog.tsx`` already renders), so the browser
#: has one device-flow state machine rather than one per provider.
LOGIN_PENDING: Final = "pending"
LOGIN_AWAITING_AUTHORIZATION: Final = "awaiting_authorization"
LOGIN_AUTHORIZED: Final = "authorized"
LOGIN_FAILED: Final = "failed"
LOGIN_STATES: Final[tuple[str, ...]] = (
    LOGIN_PENDING,
    LOGIN_AWAITING_AUTHORIZATION,
    LOGIN_AUTHORIZED,
    LOGIN_FAILED,
)
#: The two states a poller may stop on. Mirrors ``web/src/api/hooks.ts``.
LOGIN_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {LOGIN_AUTHORIZED, LOGIN_FAILED}
)


def safe_detail(text: str) -> str:
    """Guard an **authored** diagnostic string on its way to a log or the browser.

    Deliberately NOT :func:`server.codex.redaction.scrub_text`. That function
    treats its input as *native, peer-controlled* text and replaces it wholesale,
    which is right for an app-server stderr dump and wrong here: every string that
    reaches this module is a fixed phrase ccswitch wrote ("account is disabled in
    the account manifest"), and collapsing those to ``[REDACTED]`` would leave the
    typed-error surface with a code and no explanation — the browser gates its
    recovery UI on exactly that explanation.

    ``redaction``'s own doctrine names this split: native strings are opaque, and
    "diagnostic call sites retain usefulness by logging only fixed categories,
    reason codes, [and] supported method names". This is that second category,
    with a belt-and-braces check so the convention is enforced rather than
    trusted: a detail that ever *does* carry credential-shaped material is
    replaced entirely rather than trimmed, because a partially-redacted secret is
    still a leak and a caller that produced one has a bug worth noticing.
    """
    if not text:
        return ""
    if redaction.contains_secret_shaped_value(text):
        return redaction.REDACTED
    return text


class AuthorityErrorCode(StrEnum):
    """Why an authority call failed, in words a client can branch on.

    Every one of these is a *typed refusal*, not a transport fault: the authority
    answered, and the answer was no. Transport faults surface as
    :class:`AuthorityUnavailable` instead, because the two demand opposite
    responses — a refusal must not be retried, an outage should be.
    """

    #: The account is not in the authorization projection at all.
    UNKNOWN_ACCOUNT = "unknown_account"
    #: Known, but the manifest says ``enabled: false``.
    ACCOUNT_DISABLED = "account_disabled"
    #: The edge's authorization sync has not been renewed inside its lease. Every
    #: issuance fails closed until it is: a stale projection cannot prove that an
    #: account the operator disabled ten minutes ago is still enabled.
    SYNC_EXPIRED = "sync_expired"
    #: The edge tried to install a projection older than the one in force.
    REVISION_ROLLBACK = "revision_rollback"
    #: Credentials were explicitly revoked; the generation moved past the caller's.
    REVOKED = "revoked"
    #: No usable credential cache for the account — it has never logged in, or
    #: ``codex logout`` cleared it.
    LOGIN_REQUIRED = "login_required"
    #: A login is already running for this account.
    LOGIN_BUSY = "login_busy"
    #: The refresh attempt itself failed (upstream refused, cache never updated).
    REFRESH_FAILED = "refresh_failed"
    #: The caller's certificate is not permitted to call this method.
    FORBIDDEN = "forbidden"
    #: Malformed request: unknown method, bad params, wrong protocol version.
    BAD_REQUEST = "bad_request"
    #: The authority could not take (or lost) its exclusive instance lock.
    NOT_AUTHORITY = "not_authority"
    #: Anything else. Carries a scrubbed detail; never a credential.
    INTERNAL = "internal"


#: Codes a caller may usefully retry. Everything else is a settled "no", and
#: retrying it is how a runner turns one revocation into an authority DoS.
RETRYABLE_ERROR_CODES: Final[frozenset[AuthorityErrorCode]] = frozenset(
    {
        AuthorityErrorCode.SYNC_EXPIRED,
        AuthorityErrorCode.REFRESH_FAILED,
        AuthorityErrorCode.INTERNAL,
    }
)


class AuthorityError(RuntimeError):
    """A typed refusal from the authority. Safe to log: ``detail`` is scrubbed."""

    def __init__(
        self,
        code: AuthorityErrorCode,
        detail: str = "",
        *,
        account: str | None = None,
    ) -> None:
        self.code = code
        self.account = account
        self.detail = safe_detail(detail)
        super().__init__(f"{code.value}: {self.detail}" if self.detail else code.value)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_ERROR_CODES

    def to_wire(self) -> dict[str, Any]:
        return {"code": self.code.value, "detail": self.detail}

    @classmethod
    def from_wire(cls, payload: Any, *, account: str | None = None) -> AuthorityError:
        """Rebuild a refusal on the client side.

        An unrecognised code becomes :data:`AuthorityErrorCode.INTERNAL` rather
        than raising: a newer authority must be able to add a code without every
        older runner crashing, and ``INTERNAL`` is the conservative reading
        (retryable, but never treated as success).
        """
        body = payload if isinstance(payload, Mapping) else {}
        raw = str(body.get("code") or "")
        try:
            code = AuthorityErrorCode(raw)
        except ValueError:
            code = AuthorityErrorCode.INTERNAL
        return cls(code, str(body.get("detail") or ""), account=account)


class AuthorityUnavailable(RuntimeError):
    """The authority could not be reached, or did not answer in time.

    Distinct from :class:`AuthorityError` on purpose: this is "we do not know",
    and the only safe reading of "we do not know" for a credential is to keep the
    last known-good envelope until it expires and then fail the session — never
    to invent one, and never to conclude the account was revoked.
    """


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    """One account's short-lived ChatGPT credentials, plus the fence around them.

    ``generation`` is the authority's monotonic per-account counter. It is the
    whole anti-rollback mechanism: a runner that had generation 4 rejected asks
    for "something newer than 4", and a refresh that races another refresh can
    only ever publish a *higher* number. A stale answer therefore cannot
    overwrite a fresh one, which is the failure the plan calls out by name.
    """

    account: str
    generation: int
    access_token: Redacted
    chatgpt_account_id: Redacted
    chatgpt_plan_type: str | None = None
    #: Epoch seconds, from the token's own expiry claim. Non-secret — it says
    #: *when*, never *what*. ``None`` when the cache did not record one.
    expires_at: float | None = None
    #: Epoch seconds at which the authority formed this envelope.
    issued_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.account:
            raise ValueError("a credential envelope requires an account name")
        if self.generation < 1:
            raise ValueError("credential generations start at 1")
        if not self.access_token or not self.chatgpt_account_id:
            raise ValueError("a credential envelope requires non-empty credentials")

    # -- the three sanctioned reveals ------------------------------------

    def to_login_params(self) -> schema.ExternalAuthLoginParams:
        """The `account/login/start` payload. Revealed at JSON-RPC serialization."""
        return schema.ExternalAuthLoginParams(
            access_token=self.access_token,
            chatgpt_account_id=self.chatgpt_account_id,
            chatgpt_plan_type=self.chatgpt_plan_type,
        )

    def to_refresh_response(self) -> schema.ChatgptAuthTokensRefreshResponse:
        """The `account/chatgptAuthTokens/refresh` reply the app-server asked for."""
        return schema.ChatgptAuthTokensRefreshResponse(
            access_token=self.access_token,
            chatgpt_account_id=self.chatgpt_account_id,
            chatgpt_plan_type=self.chatgpt_plan_type,
        )

    def to_wire(self) -> dict[str, Any]:
        """Serialize for the authority→runner mTLS hop. **Reveals the token.**

        The only caller is :mod:`server.codex.auth_server`, writing onto a socket
        whose peer presented a certificate this authority verified against its own
        client CA and its own per-route ACL. Do not call this from anywhere that
        writes to a log, a database, an audit row, or an HTTP response.
        """
        return {
            "account": self.account,
            "generation": self.generation,
            "accessToken": self.access_token.reveal(),
            "chatgptAccountId": self.chatgpt_account_id.reveal(),
            "chatgptPlanType": self.chatgpt_plan_type,
            "expiresAt": self.expires_at,
            "issuedAt": self.issued_at,
        }

    @classmethod
    def from_wire(cls, payload: Any) -> CredentialEnvelope:
        if not isinstance(payload, Mapping):
            raise AuthorityError(
                AuthorityErrorCode.BAD_REQUEST, "envelope must be an object"
            )
        try:
            return cls(
                account=str(payload["account"]),
                generation=int(payload["generation"]),
                access_token=Redacted(str(payload["accessToken"])),
                chatgpt_account_id=Redacted(str(payload["chatgptAccountId"])),
                chatgpt_plan_type=(
                    None
                    if payload.get("chatgptPlanType") is None
                    else str(payload["chatgptPlanType"])
                ),
                expires_at=(
                    None
                    if payload.get("expiresAt") is None
                    else float(payload["expiresAt"])
                ),
                issued_at=float(payload.get("issuedAt") or time.time()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # str(exc) can only name a KEY here, never a value, so it is safe —
            # but scrub anyway: this is the boundary, not a place to be clever.
            raise AuthorityError(
                AuthorityErrorCode.BAD_REQUEST,
                f"malformed credential envelope: {exc}",
            ) from None

    # -- the non-secret projection ---------------------------------------

    def public_summary(self) -> dict[str, Any]:
        """Everything about this envelope that is safe anywhere. No credentials."""
        return {
            "account": self.account,
            "generation": self.generation,
            "planType": self.chatgpt_plan_type,
            "expiresAt": self.expires_at,
            "issuedAt": self.issued_at,
        }

    def expired(self, *, now: float | None = None, skew_s: float = 0.0) -> bool:
        """True when the token is past (or within *skew_s* of) its own expiry.

        An envelope with no recorded expiry is never *known* to be expired, so it
        is reported live: the app-server's 401 → refresh callback is the
        authoritative signal, and guessing "expired" here would restart every
        runner's login on every fetch.
        """
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) + skew_s >= self.expires_at


@dataclass(frozen=True, slots=True)
class AuthorizationRecord:
    """One account as the edge's manifest sees it. Non-secret by construction.

    This is the *whole* projection: name, provider, engine, enabled. The
    authority is deliberately given no read access to the Claude account store and
    no way to enumerate accounts itself, so this record is the only thing that can
    make it issue a credential.
    """

    name: str
    provider: str
    engine: str | None = None
    enabled: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "engine": self.engine,
            "enabled": self.enabled,
        }

    @classmethod
    def from_wire(cls, payload: Any) -> AuthorizationRecord:
        if not isinstance(payload, Mapping) or not payload.get("name"):
            raise AuthorityError(
                AuthorityErrorCode.BAD_REQUEST, "authorization record requires a name"
            )
        return cls(
            name=str(payload["name"]),
            provider=str(payload.get("provider") or ""),
            engine=(
                None if payload.get("engine") is None else str(payload["engine"])
            ),
            enabled=payload.get("enabled", True) is not False,
        )


@dataclass(frozen=True, slots=True)
class AuthorizationSync:
    """A full-sync snapshot of who may be issued credentials, and until when.

    ``revision`` is the account manifest's own monotonic revision, so "is this
    older than what I already have?" is answerable without trusting a clock.
    ``lease_s`` is what makes a *silent* edge fail closed rather than open: the
    authority stops issuing when the lease lapses, so an edge that dies right
    after an operator disables an account cannot leave that account usable.
    """

    revision: int
    records: tuple[AuthorizationRecord, ...] = ()
    lease_s: float = 900.0

    def to_wire(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "leaseS": self.lease_s,
            "records": [record.to_wire() for record in self.records],
        }

    @classmethod
    def from_wire(cls, payload: Any) -> AuthorizationSync:
        if not isinstance(payload, Mapping):
            raise AuthorityError(
                AuthorityErrorCode.BAD_REQUEST, "authorization sync must be an object"
            )
        raw_records = payload.get("records")
        if not isinstance(raw_records, (list, tuple)):
            raise AuthorityError(
                AuthorityErrorCode.BAD_REQUEST, "authorization sync requires records"
            )
        try:
            revision = int(payload["revision"])
            lease_s = float(payload.get("leaseS") or 900.0)
        except (KeyError, TypeError, ValueError):
            raise AuthorityError(
                AuthorityErrorCode.BAD_REQUEST, "authorization sync requires a revision"
            ) from None
        return cls(
            revision=revision,
            records=tuple(AuthorizationRecord.from_wire(r) for r in raw_records),
            lease_s=lease_s,
        )

    def get(self, account: str) -> AuthorizationRecord | None:
        for record in self.records:
            if record.name == account:
                return record
        return None


@dataclass(frozen=True, slots=True)
class LoginSnapshot:
    """The browser-facing view of a device login. Never carries a token.

    ``userCode`` and ``verificationUri`` ARE shown to the operator — that is the
    point of a device flow — so they are the one class of login detail that is
    allowed out of the authority. They are single-use, short-lived, and useless
    without the operator's own ChatGPT session.
    """

    state: str = LOGIN_PENDING
    user_code: str | None = None
    verification_uri: str | None = None
    error: str | None = None
    generation: int | None = None

    def __post_init__(self) -> None:
        if self.state not in LOGIN_STATES:
            raise ValueError(f"unknown login state {self.state!r}")
        if self.error is not None:
            object.__setattr__(self, "error", safe_detail(self.error))

    @property
    def terminal(self) -> bool:
        return self.state in LOGIN_TERMINAL_STATES

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "userCode": self.user_code,
            "verificationUri": self.verification_uri,
            "error": self.error,
            "generation": self.generation,
        }

    @classmethod
    def from_wire(cls, payload: Any) -> LoginSnapshot:
        body = payload if isinstance(payload, Mapping) else {}
        raw_state = str(body.get("state") or LOGIN_PENDING)
        state = raw_state if raw_state in LOGIN_STATES else LOGIN_FAILED
        generation = body.get("generation")
        return cls(
            state=state,
            user_code=(
                None if body.get("userCode") is None else str(body["userCode"])
            ),
            verification_uri=(
                None
                if body.get("verificationUri") is None
                else str(body["verificationUri"])
            ),
            error=None if body.get("error") is None else str(body["error"]),
            generation=None if generation is None else int(generation),
        )


@dataclass(frozen=True, slots=True)
class AuthorityHealth:
    """What the edge may show an operator about the authority itself."""

    reachable: bool
    protocol_version: int = AUTHORITY_PROTOCOL_VERSION
    #: Revision of the authorization projection currently in force, if any.
    sync_revision: int | None = None
    #: Seconds until the projection lease lapses. Negative means already lapsed.
    sync_expires_in_s: float | None = None
    accounts: int = 0
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "protocolVersion": self.protocol_version,
            "syncRevision": self.sync_revision,
            "syncExpiresInS": self.sync_expires_in_s,
            "accounts": self.accounts,
            "detail": self.detail,
        }

    @classmethod
    def from_wire(cls, payload: Any) -> AuthorityHealth:
        body = payload if isinstance(payload, Mapping) else {}
        revision = body.get("syncRevision")
        expires = body.get("syncExpiresInS")
        return cls(
            reachable=bool(body.get("reachable", True)),
            protocol_version=int(
                body.get("protocolVersion") or AUTHORITY_PROTOCOL_VERSION
            ),
            sync_revision=None if revision is None else int(revision),
            sync_expires_in_s=None if expires is None else float(expires),
            accounts=int(body.get("accounts") or 0),
            detail=safe_detail(str(body.get("detail") or "")),
        )

    @classmethod
    def unreachable(cls, detail: str) -> AuthorityHealth:
        return cls(reachable=False, detail=safe_detail(detail))
