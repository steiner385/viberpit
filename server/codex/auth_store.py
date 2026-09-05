"""The authority's private view of Codex's managed credential cache.

One rule, and this module exists to enforce it: **the credential cache is read
here or nowhere.** ``codex login`` / ``codex logout`` write ``auth.json`` inside a
managed ``CODEX_HOME``; the authority reads it back, validates it, and forms an
in-memory :class:`~server.codex.auth_types.CredentialEnvelope`. Nothing else in
ccswitch opens that file, and the envelope is never written anywhere.

Layout. Each OpenAI account gets ``<state_dir>/accounts/<name>/`` — an
authority-only ``CODEX_HOME`` (0700, files 0600) created through
``server.codex.process.ensure_codex_home``, which already refuses network
filesystems and symlinked path components. It is deliberately NOT under the
Claude account store, not exported to any runner mount, and not readable by the
session-daemon user: a runner's whole point is that it never holds a refresh
token.

Alongside it, ``<state_dir>/state/<name>.json`` holds the non-secret credential
generation and durable revocation bit. It lives outside the managed home so that
``codex logout`` (which deletes the home's contents) cannot rewind either fence,
and so that its own contents can never be confused for credential material.

Format note. ``auth.json``'s shape was read from a real 0.153.2 install:
``{"OPENAI_API_KEY": null, "tokens": {"id_token", "access_token",
"refresh_token", "account_id"}, "last_refresh": "<iso>"}``. Plan type and expiry
are taken from the access token's own claims rather than from a sidecar we
maintain, so they cannot drift from the token they describe. The JWT is
**not** signature-verified and must not be trusted as an authorization decision:
it is our own local cache, the claims are read only for display and for an
expiry hint, and the API re-validates the token on every call.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from server.codex import redaction
from server.codex.auth_types import (
    AuthorityError,
    AuthorityErrorCode,
    CredentialEnvelope,
)
from server.codex.process import CodexHomeRefused, ensure_codex_home
from server.codex.redaction import Redacted
from server.obs.context import log_event

logger = logging.getLogger(__name__)

AUTH_FILENAME: Final = "auth.json"
#: The namespaced claim OpenAI puts ChatGPT identity under, in both tokens.
AUTH_CLAIM: Final = "https://api.openai.com/auth"


class CredentialCacheError(RuntimeError):
    """The managed cache is missing, unreadable, or does not hold a usable token."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The non-secret claims we read out of an access token."""

    chatgpt_account_id: str | None = None
    chatgpt_plan_type: str | None = None
    expires_at: float | None = None


def decode_claims(token: str) -> TokenClaims:
    """Best-effort, signature-free read of a JWT payload.

    Every failure mode returns empty claims rather than raising. A token whose
    payload we cannot parse is still a token the API may accept, and refusing to
    issue it because our metadata extraction failed would turn a cosmetic problem
    ("plan type unknown in the UI") into an outage.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return TokenClaims()
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        body = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, TypeError):
        return TokenClaims()
    if not isinstance(body, dict):
        return TokenClaims()
    auth = body.get(AUTH_CLAIM)
    auth = auth if isinstance(auth, dict) else {}
    expires = body.get("exp")
    return TokenClaims(
        chatgpt_account_id=_optional_str(auth.get("chatgpt_account_id")),
        chatgpt_plan_type=_optional_str(auth.get("chatgpt_plan_type")),
        expires_at=float(expires) if isinstance(expires, (int, float)) else None,
    )


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


class CredentialStore:
    """Managed homes, generations, and the one path that reads ``auth.json``."""

    def __init__(self, state_dir: str | Path) -> None:
        self._root = Path(state_dir)
        self._accounts = self._root / "accounts"
        self._state = self._root / "state"

    def __repr__(self) -> str:
        return f"<CredentialStore root={self._root}>"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def accounts_base(self) -> Path:
        return self._accounts

    # -- homes -------------------------------------------------------------

    def ensure_home(self, account: str) -> Path:
        """The authority-only ``CODEX_HOME`` for *account*, created 0700.

        Delegates the hard parts — network-filesystem refusal, symlink refusal,
        mode tightening — to ``process.ensure_codex_home``, which the runner side
        already uses for its own homes. One implementation of "a private local
        Codex home", two callers with different roots.
        """
        try:
            return ensure_codex_home(self._accounts, account)
        except CodexHomeRefused as exc:
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                f"managed credential home refused: {exc}",
                account=account,
            ) from None

    def home(self, account: str) -> Path:
        """The managed home path without creating it."""
        return self._accounts / account

    def auth_path(self, account: str) -> Path:
        return self.home(account) / AUTH_FILENAME

    def has_credentials(self, account: str) -> bool:
        return self.auth_path(account).is_file()

    def assert_private(self, account: str) -> None:
        """Fail loudly if the managed home or its ``auth.json`` is group/world readable.

        Called after every login. The session-daemon user must not be able to
        traverse this tree; a mode drift (an operator's ``chmod -R``, a umask
        change, a restore from a backup) would make the whole external-token
        design pointless while everything still appeared to work.
        """
        home = self.home(account)
        if home.exists() and stat.S_IMODE(home.stat().st_mode) & 0o077:
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                f"managed credential home {home} is not private (expected 0700)",
                account=account,
            )
        auth = self.auth_path(account)
        if auth.exists():
            mode = stat.S_IMODE(auth.stat().st_mode)
            if mode & 0o077:
                # Tighten first, then still refuse: a permissive window already
                # happened, and the operator needs to know the token may have been
                # readable rather than have it silently fixed.
                os.chmod(auth, 0o600)
                raise AuthorityError(
                    AuthorityErrorCode.INTERNAL,
                    f"credential cache {auth} was mode {mode:o}; tightened to 0600, "
                    f"but the credential must be rotated",
                    account=account,
                )

    # -- generations -------------------------------------------------------

    def generation(self, account: str) -> int:
        """The account's current credential generation. Absent state means 0.

        Zero means "no credential has ever been published for this account", which
        is distinguishable from generation 1 — and has to be, because
        :meth:`read_envelope` refuses to build an envelope at generation 0.
        """
        body = self._generation_state(account)
        generation = body.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                "durable credential generation state is invalid",
                account=account,
            )
        return generation

    def revoked(self, account: str) -> bool:
        """Whether explicit revocation currently fences this account.

        This bit is stored beside the generation so a service restart cannot
        turn the still-present vendor cache back into an issuable credential.
        Only a successfully validated device login clears it.
        """
        revoked = self._generation_state(account).get("revoked", False)
        if not isinstance(revoked, bool):
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                "durable credential revocation state is invalid",
                account=account,
            )
        return revoked

    def _generation_state(self, account: str) -> dict[str, Any]:
        path = self._state / f"{account}.json"
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                "durable credential state is malformed",
                account=account,
            ) from exc
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "codex.auth.generation_unreadable",
                account=account,
                error=redaction.scrub_exception(exc),
            )
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                "durable credential state is unreadable",
                account=account,
            ) from exc
        if not isinstance(body, dict):
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                "durable credential state is malformed",
                account=account,
            )
        return body

    def bump_generation(self, account: str, *, revoked: bool | None = None) -> int:
        """Advance and persist the fence. Monotonic across authority restarts.

        Persisted (rather than kept in memory) precisely because a restart must
        not re-mint a number a runner already had rejected: a runner that asked
        for "newer than 4" and got a fresh in-memory "1" would treat a stale
        credential as new.
        """
        self._state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self._state.stat().st_mode) != 0o700:
            self._state.chmod(0o700)
        nxt = self.generation(account) + 1
        path = self._state / f"{account}.json"
        tmp = path.with_name(f".{account}.json.tmp")
        state = self._generation_state(account)
        next_revoked = bool(state.get("revoked")) if revoked is None else revoked
        tmp.write_text(
            json.dumps({"generation": nxt, "revoked": next_revoked}),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return nxt

    def mark_revoked(self, account: str) -> int:
        """Advance the generation and durably refuse issuance."""
        return self.bump_generation(account, revoked=True)

    def mark_authenticated(self, account: str) -> int:
        """Advance the generation and clear revoke after a validated login."""
        return self.bump_generation(account, revoked=False)

    def forget(self, account: str) -> None:
        """Drop every trace of *account* — managed home and generation state.

        Used by account removal, never by logout: a logged-out account keeps its
        generation so that a later re-login cannot reuse a number an in-flight
        runner is still fencing against.
        """
        import shutil

        try:
            shutil.rmtree(self.home(account))
        except FileNotFoundError:
            pass
        try:
            (self._state / f"{account}.json").unlink()
        except FileNotFoundError:
            pass

    def clear_credentials(self, account: str) -> None:
        """Remove the cached credential without touching the generation."""
        try:
            self.auth_path(account).unlink()
        except FileNotFoundError:
            pass

    # -- the authorization projection --------------------------------------
    #
    # Persisted, and deliberately alongside the generations rather than inside a
    # managed home: it is the edge's non-secret statement of who may be issued a
    # credential, and `codex logout` must not be able to delete it.

    def _sync_path(self) -> Path:
        return self._state / "authorization.json"

    def save_sync(self, payload: dict[str, Any], *, at: float) -> None:
        """Persist the projection with the wall-clock time it was installed.

        Without this, an authority restart forgets who is authorized and every
        account fails closed until the edge's next periodic sync — safe, but a
        multi-minute Codex outage on every restart of a service that holds no
        sessions and should be restartable freely.
        """
        self._state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self._state.stat().st_mode) != 0o700:
            self._state.chmod(0o700)
        path = self._sync_path()
        tmp = path.with_name(".authorization.json.tmp")
        tmp.write_text(
            json.dumps({"installedAt": at, "sync": payload}), encoding="utf-8"
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def load_sync(self) -> tuple[dict[str, Any], float] | None:
        """The persisted projection and when it was installed, or ``None``.

        The *lease* is still enforced by the caller against ``installedAt``, so a
        projection restored after its lease lapsed grants nothing — restart cannot
        be used to extend an authorization the edge stopped renewing.
        """
        try:
            body = json.loads(self._sync_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "codex.auth.sync_unreadable",
                error=redaction.scrub_exception(exc),
            )
            return None
        payload = body.get("sync")
        if not isinstance(payload, dict):
            return None
        try:
            return payload, float(body.get("installedAt") or 0.0)
        except (TypeError, ValueError):
            return None

    # -- the one read path -------------------------------------------------

    def read_envelope(self, account: str, *, generation: int) -> CredentialEnvelope:
        """Validate the managed cache and form an in-memory envelope.

        Raises :class:`AuthorityError` with :data:`AuthorityErrorCode.LOGIN_REQUIRED`
        for every "there is no usable credential here" shape — absent file,
        truncated JSON, missing ``tokens``, empty ``access_token`` — because they
        all have the same remedy and the caller must not have to enumerate them.
        """
        path = self.auth_path(account)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise AuthorityError(
                AuthorityErrorCode.LOGIN_REQUIRED,
                "no credential cache for this account",
                account=account,
            ) from None
        except OSError as exc:
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                f"credential cache unreadable: {redaction.scrub_exception(exc)}",
                account=account,
            ) from None

        try:
            body = json.loads(raw)
        except ValueError:
            # Never echo `raw` or the parser's message: a truncated JSON error
            # quotes the document, and this document is a token.
            raise AuthorityError(
                AuthorityErrorCode.LOGIN_REQUIRED,
                "credential cache is not valid JSON",
                account=account,
            ) from None

        tokens = body.get("tokens") if isinstance(body, dict) else None
        if not isinstance(tokens, dict):
            raise AuthorityError(
                AuthorityErrorCode.LOGIN_REQUIRED,
                "credential cache has no tokens object",
                account=account,
            )
        access_token = _optional_str(tokens.get("access_token"))
        if access_token is None:
            raise AuthorityError(
                AuthorityErrorCode.LOGIN_REQUIRED,
                "credential cache has no access token",
                account=account,
            )

        claims = decode_claims(access_token)
        chatgpt_account_id = claims.chatgpt_account_id or _optional_str(
            tokens.get("account_id")
        )
        if chatgpt_account_id is None:
            raise AuthorityError(
                AuthorityErrorCode.LOGIN_REQUIRED,
                "credential cache names no ChatGPT account id",
                account=account,
            )
        if generation < 1:
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                "refusing to publish an envelope at generation 0",
                account=account,
            )
        return CredentialEnvelope(
            account=account,
            generation=generation,
            access_token=Redacted(access_token),
            chatgpt_account_id=Redacted(chatgpt_account_id),
            chatgpt_plan_type=claims.chatgpt_plan_type,
            expires_at=claims.expires_at,
            issued_at=time.time(),
        )
