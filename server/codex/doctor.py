"""Startup diagnostics for the Codex boundary: one report, no second opinions.

An operator bringing Codex up on a runner has six independent ways to be wrong —
the binary, the runtime home, the authority, the client certificate, the account
manifest, and the launch flags — and every one of them currently surfaces as the
same symptom: "Codex isn't offered". This module turns that single symptom into
six named, machine-readable verdicts, which is what `deploy/README.md`'s
"Codex isn't offered" triage section can then point at.

**It invents no verdicts of its own.** That is the load-bearing rule here:

* Whether the binary may be used is :func:`server.codex.process.probe_availability`'s
  answer, already computed once per daemon and published through ``/info``. The
  doctor *reports* the :class:`~server.codex.capabilities.CodexAvailability` it is
  handed; it never re-resolves ``$PATH``, re-compares a version, or re-hashes the
  schema contract. A second availability opinion is exactly the failure this
  repo's "one source per concept" rule exists to prevent.
* Whether a path is local enough for Codex SQLite/WAL is
  :func:`server.codex.process.assert_local_filesystem`'s answer (the #1005 sshfs
  incident is why it fails closed on an *undeterminable* mount too). The doctor
  calls it for the verdict and :func:`~server.codex.process.filesystem_type_for`
  only for the fstype *fact* it puts in the report.
* Whether the TLS material is usable and belongs to a different trust domain than
  the session daemon is :func:`server.codex.auth_endpoint.authority_client_tls_files`'s
  answer (it calls ``assert_distinct_from_sessiond`` itself). The doctor adds only
  what that loader does not look at: key file mode, the certificate validity
  window, and the client CN.

**Nothing credential-shaped may reach the journal.** ``redaction.scrub_text``
replaces arbitrary native text *wholesale*, so it cannot be used on the doctor's
own prose without erasing it. The contract here is instead the one the rest of
``server/codex/`` already follows: typed, ccswitch-authored fields are carried
raw, free text is an authored constant or a fixed
:func:`~server.codex.redaction.scrub_exception` category, and
:class:`DoctorResult` runs every string it is given past
:func:`~server.codex.redaction.contains_secret_shaped_value` as a belt — so an
operator who managed to put a token in a filesystem path still cannot log one.

Run it by hand with ``python -m server.codex.doctor`` (the runbook uses this);
the daemon runs it once at startup and logs the result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from server.codex import process as codex_process
from server.codex import redaction, schema
from server.codex.auth_endpoint import AuthorityClientTLSFiles
from server.codex.auth_types import AuthorityHealth
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.obs.context import log_event

logger = logging.getLogger(__name__)

#: How long before ``notAfter`` a client certificate starts warning. Two weeks is
#: comfortably longer than the manual cert-minting procedure in
#: `docs/runbooks/codex-credential-authority.md` takes to schedule.
CERT_EXPIRY_WARN_DAYS: Final = 14

#: Runtime-home directory mode. Codex keeps thread state, history and a refresh-free
#: token cache under it, so group/other must have nothing.
REQUIRED_HOME_MODE: Final = 0o700

#: Whole-report budget for :func:`run_and_log`. Comfortably above the authority's
#: 20 s admin deadline would defeat the purpose: this is a *diagnostic*, and a
#: hung authority must not keep the daemon's startup task alive indefinitely.
DOCTOR_TIMEOUT_S: Final = 25.0


class DoctorOutcome(StrEnum):
    """One check's verdict, ordered by severity in :data:`_SEVERITY`."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


#: SKIPPED sorts *below* OK so that a report of nothing but skips (Codex disabled)
#: reads as "skipped", not as a clean bill of health.
_SEVERITY: Final[dict[DoctorOutcome, int]] = {
    DoctorOutcome.SKIPPED: 0,
    DoctorOutcome.OK: 1,
    DoctorOutcome.WARN: 2,
    DoctorOutcome.FAIL: 3,
}


class DoctorCheck(StrEnum):
    """The checks, in the order :func:`run_doctor` runs and reports them.

    The order is causal: a flag that is off explains an unprobed binary, an
    unconfigured authority explains unusable TLS material, so reading the report
    top-down lands on the first real cause.
    """

    EXTERNAL_AUTH = "external_auth"
    BINARY = "binary"
    RUNTIME_HOME = "runtime_home"
    AUTHORITY = "authority"
    MTLS_IDENTITY = "mtls_identity"
    MANIFEST = "manifest"


def _safe_scalar(value: Any) -> Any:
    """A fact value, with anything credential-shaped removed.

    Only strings can carry a secret shape; everything else is passed through so
    the report keeps real booleans and numbers (``json.dumps`` of the wire form is
    asserted in the tests, so a stray enum or ``Path`` fails loudly rather than
    stringifying itself into the journal).
    """
    if isinstance(value, str):
        return redaction.REDACTED if redaction.contains_secret_shaped_value(value) else value
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class DoctorResult:
    """One check's outcome: a stable code, authored prose, and typed facts."""

    check: DoctorCheck
    outcome: DoctorOutcome
    #: Machine-readable and stable — this is what a runbook and an alert match on.
    code: str
    #: Authored text or a ``scrub_exception`` category. Never native output.
    detail: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _safe_scalar(self.detail))
        object.__setattr__(
            self, "facts", {key: _safe_scalar(value) for key, value in self.facts.items()}
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "check": self.check.value,
            "outcome": self.outcome.value,
            "code": self.code,
            "detail": self.detail,
            "facts": dict(self.facts),
        }


@dataclass(frozen=True, slots=True)
class CodexDoctorReport:
    """Every check's result plus the worst outcome among them."""

    results: tuple[DoctorResult, ...]

    @property
    def outcome(self) -> DoctorOutcome:
        worst = DoctorOutcome.SKIPPED
        for result in self.results:
            if _SEVERITY[result.outcome] > _SEVERITY[worst]:
                worst = result.outcome
        return worst

    @property
    def ok(self) -> bool:
        """True when nothing failed. A WARN is still a usable Codex boundary."""
        return self.outcome is not DoctorOutcome.FAIL

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "checks": [result.to_wire() for result in self.results],
        }


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_external_auth(*, enabled: bool, external_auth_enabled: bool) -> DoctorResult:
    """The two launch flags, and why "half on" is worse than off.

    Runners hold no ChatGPT credential by design, so without the external-token
    API there is no way for an app-server to authenticate at all. Codex enabled
    *without* it therefore advertises a session that can never start — a FAIL, not
    a warning.
    """
    if not enabled:
        return DoctorResult(
            DoctorCheck.EXTERNAL_AUTH, DoctorOutcome.SKIPPED, "codex_disabled",
            detail="CCSWITCH_CODEX_ENABLED is false",
        )
    if not external_auth_enabled:
        return DoctorResult(
            DoctorCheck.EXTERNAL_AUTH, DoctorOutcome.FAIL, "external_auth_disabled",
            detail=(
                "CCSWITCH_CODEX_ENABLED is true but "
                "CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED is false; runners carry no "
                "credential, so no Codex session can authenticate"
            ),
        )
    return DoctorResult(
        DoctorCheck.EXTERNAL_AUTH, DoctorOutcome.OK, "enabled",
        detail="both Codex launch flags are on",
    )


#: The two unavailability reasons that mean "a flag is off", not "something broke".
#: They are reported as skips so the external-auth check owns that FAIL exactly once.
_FLAG_REASONS: Final[frozenset[CodexUnavailableReason]] = frozenset(
    {CodexUnavailableReason.DISABLED, CodexUnavailableReason.EXTERNAL_AUTH_DISABLED}
)


def check_binary(availability: CodexAvailability | None) -> DoctorResult:
    """Report the runner's existing availability verdict; never recompute it."""
    if availability is None:
        return DoctorResult(
            DoctorCheck.BINARY, DoctorOutcome.SKIPPED, "not_probed",
            detail="no Codex availability was computed on this host",
        )

    facts: dict[str, Any] = {
        "cliVersion": availability.cli_version,
        "binaryPath": availability.binary_path,
        "contractSha256": availability.contract_sha256,
        "pinnedCliVersion": schema.PINNED_CLI_VERSION,
        "pinnedContractSha256": schema.PINNED_CONTRACT_SHA256,
    }

    if not availability.available:
        assert availability.reason is not None  # CodexAvailability enforces this
        outcome = (
            DoctorOutcome.SKIPPED
            if availability.reason in _FLAG_REASONS
            else DoctorOutcome.FAIL
        )
        return DoctorResult(
            DoctorCheck.BINARY, outcome, availability.reason.value,
            # `availability.detail` is scrubbed wholesale at construction, so it
            # carries no information; the typed reason is the message.
            detail=_UNAVAILABLE_DETAIL.get(availability.reason, "Codex is unavailable"),
            facts=facts,
        )

    if availability.accepted_drift is not None:
        return DoctorResult(
            DoctorCheck.BINARY, DoctorOutcome.WARN, "version_drift_accepted",
            detail=(
                "this binary does not match the pinned Codex version and is running "
                "only because CCSWITCH_CODEX_SUPPORTED_VERSIONS admits it"
            ),
            facts={**facts, "acceptedDrift": list(availability.accepted_drift.reason_codes())},
        )

    return DoctorResult(
        DoctorCheck.BINARY, DoctorOutcome.OK, "ready",
        detail="the Codex binary matches the pinned version and schema contract",
        facts=facts,
    )


_UNAVAILABLE_DETAIL: Final[dict[CodexUnavailableReason, str]] = {
    CodexUnavailableReason.DISABLED: "CCSWITCH_CODEX_ENABLED is false",
    CodexUnavailableReason.EXTERNAL_AUTH_DISABLED:
        "CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED is false",
    CodexUnavailableReason.BINARY_NOT_FOUND:
        "the configured Codex binary (CCSWITCH_CODEX_BIN) is not installed or not "
        "executable; see scripts/fetch-codex.sh",
    CodexUnavailableReason.VERSION_PROBE_FAILED:
        "`codex --version` could not be executed",
    CodexUnavailableReason.UNSUPPORTED_VERSION:
        "the installed Codex app-server schema does not match the pinned contract",
    CodexUnavailableReason.RUNTIME_HOME_REFUSED:
        "the runner's CODEX_HOME failed its locality/privacy preconditions",
}


def check_runtime_home(
    base: str | Path,
    *,
    mounts: codex_process.Mounts | None = None,
    uid: int | None = None,
    enabled: bool = True,
) -> DoctorResult:
    """The runner-local Codex state root: local, private, and ours.

    Absence is not a fault — ``ensure_codex_home`` creates the per-account home at
    first use. What must be true *now* is that the path it would create under is on
    a provably local filesystem, is not redirected through a symlink, and is not
    owned by another user (notably ``ccswitch-auth``, whose managed home a link
    here could otherwise aim at).
    """
    if not enabled:
        return DoctorResult(
            DoctorCheck.RUNTIME_HOME, DoctorOutcome.SKIPPED, "codex_disabled",
            detail="CCSWITCH_CODEX_ENABLED is false",
        )

    path = Path(os.path.expanduser(str(base)))
    facts: dict[str, Any] = {"path": str(path)}

    linked = codex_process.first_symlinked_component(path)
    if linked is not None:
        return DoctorResult(
            DoctorCheck.RUNTIME_HOME, DoctorOutcome.FAIL, "symlinked_path",
            detail=(
                "a component of the Codex runtime home is a symlink; the home and "
                "every component of its path must be real local directories"
            ),
            facts={**facts, "symlinkedComponent": str(linked)},
        )

    # The fstype is only a FACT for the report. The verdict below is
    # assert_local_filesystem's, which fails closed on an undeterminable mount too.
    fstype = codex_process.filesystem_type_for(path, mounts=mounts)
    facts["filesystemType"] = fstype
    try:
        codex_process.assert_local_filesystem(path, mounts=mounts)
    except codex_process.CodexHomeRefused:
        if fstype is None:
            return DoctorResult(
                DoctorCheck.RUNTIME_HOME, DoctorOutcome.FAIL, "filesystem_undeterminable",
                detail=(
                    "the mount table does not say which filesystem holds the Codex "
                    "runtime home, so it cannot be proven local"
                ),
                facts=facts,
            )
        return DoctorResult(
            DoctorCheck.RUNTIME_HOME, DoctorOutcome.FAIL, "network_filesystem",
            detail=(
                "the Codex runtime home is on a network filesystem; Codex keeps "
                "SQLite/WAL, history and thread state there"
            ),
            facts=facts,
        )

    if not path.exists():
        return DoctorResult(
            DoctorCheck.RUNTIME_HOME, DoctorOutcome.OK, "ready",
            detail="the Codex runtime home is local and will be created on first use",
            facts={**facts, "exists": False},
        )

    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    facts.update({"exists": True, "mode": f"{mode:04o}", "ownerUid": info.st_uid})

    expected_uid = os.getuid() if uid is None else uid
    if info.st_uid != expected_uid:
        return DoctorResult(
            DoctorCheck.RUNTIME_HOME, DoctorOutcome.FAIL, "foreign_owner",
            detail="the Codex runtime home is owned by another user",
            facts=facts,
        )
    if not os.access(path, os.W_OK):
        return DoctorResult(
            DoctorCheck.RUNTIME_HOME, DoctorOutcome.FAIL, "not_writable",
            detail="the Codex runtime home is not writable by this daemon",
            facts=facts,
        )
    if mode != REQUIRED_HOME_MODE:
        return DoctorResult(
            DoctorCheck.RUNTIME_HOME, DoctorOutcome.WARN, "permissive_mode",
            detail="the Codex runtime home is wider than 0700",
            facts=facts,
        )
    return DoctorResult(
        DoctorCheck.RUNTIME_HOME, DoctorOutcome.OK, "ready",
        detail="the Codex runtime home is local, private and writable",
        facts=facts,
    )


AuthorityHealthProbe = Callable[[], Awaitable[AuthorityHealth]]


async def check_authority(
    health: AuthorityHealthProbe | None,
    *,
    configured: bool,
    enabled: bool,
) -> DoctorResult:
    """Can this host reach the credential authority, and is issuance armed?

    ``AuthorityClient.health()`` is the only prober — it already turns an
    unreachable authority into a value rather than an exception, so the failure
    modes worth separating here are *unconfigured* (nothing to reach) and
    *unreachable* (something to reach that did not answer).
    """
    if not enabled:
        return DoctorResult(
            DoctorCheck.AUTHORITY, DoctorOutcome.SKIPPED, "codex_disabled",
            detail="CCSWITCH_CODEX_ENABLED is false",
        )
    if not configured or health is None:
        return DoctorResult(
            DoctorCheck.AUTHORITY, DoctorOutcome.FAIL, "unconfigured",
            detail=(
                "CCSWITCH_CODEX_AUTHORITY_ENDPOINT is unset; Codex is enabled but "
                "this host has no credential authority to obtain tokens from"
            ),
        )

    try:
        snapshot = await health()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a probe fault is a health answer
        return DoctorResult(
            DoctorCheck.AUTHORITY, DoctorOutcome.FAIL, "probe_failed",
            detail=redaction.scrub_exception(exc),
        )

    facts: dict[str, Any] = {
        "reachable": snapshot.reachable,
        "protocolVersion": snapshot.protocol_version,
        "syncRevision": snapshot.sync_revision,
        "syncExpiresInS": snapshot.sync_expires_in_s,
        "accounts": snapshot.accounts,
    }
    if not snapshot.reachable:
        return DoctorResult(
            DoctorCheck.AUTHORITY, DoctorOutcome.FAIL, "unreachable",
            detail="the credential authority did not answer",
            facts=facts,
        )
    if snapshot.sync_revision is None:
        return DoctorResult(
            DoctorCheck.AUTHORITY, DoctorOutcome.WARN, "no_authorization_sync",
            detail=(
                "the authority is reachable but the edge has not pushed an "
                "authorization projection yet, so it will not issue envelopes"
            ),
            facts=facts,
        )
    if snapshot.sync_expires_in_s is not None and snapshot.sync_expires_in_s <= 0:
        return DoctorResult(
            DoctorCheck.AUTHORITY, DoctorOutcome.WARN, "sync_lease_lapsed",
            detail=(
                "the authorization projection lease has lapsed; the authority stops "
                "issuing until the edge renews it "
                "(CCSWITCH_CODEX_AUTH_SYNC_INTERVAL_S must be below the lease)"
            ),
            facts=facts,
        )
    return DoctorResult(
        DoctorCheck.AUTHORITY, DoctorOutcome.OK, "reachable",
        detail="the credential authority is reachable with a live authorization lease",
        facts=facts,
    )


TLSFilesLoader = Callable[[], AuthorityClientTLSFiles]


def check_mtls_identity(
    load_files: TLSFilesLoader | None,
    *,
    configured: bool,
    enabled: bool,
    now: datetime | None = None,
) -> DoctorResult:
    """This host's client certificate for the authority's separate trust domain.

    ``authority_client_tls_files()`` already refuses missing/unreadable material
    and a key shared with the session daemon (``assert_distinct_from_sessiond``).
    The doctor adds the three things it does not look at: the key's file mode, the
    certificate's validity window, and the subject CN — which is the identity the
    authority matches against ``CCSWITCH_CODEX_AUTHORITY_CLIENT_ALLOWLIST``, and is
    by far the most common thing to get wrong when onboarding a runner.
    """
    if not enabled:
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.SKIPPED, "codex_disabled",
            detail="CCSWITCH_CODEX_ENABLED is false",
        )
    if not configured or load_files is None:
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.SKIPPED, "authority_unconfigured",
            detail="no authority endpoint is configured, so no client identity is used",
        )

    try:
        files = load_files()
    except Exception as exc:  # noqa: BLE001 -- a config fault is this check's answer
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.FAIL, "tls_material_unusable",
            detail=(
                "the authority client certificate/key/CA is missing, unreadable, or "
                "shares a private key with the session daemon"
            ),
            facts={"error": redaction.scrub_exception(exc)},
        )

    facts: dict[str, Any] = {"certPath": files.cert, "caPath": files.ca}

    try:
        key_mode = stat.S_IMODE(os.stat(files.key).st_mode)
    except OSError as exc:
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.FAIL, "tls_material_unusable",
            detail="the authority client private key could not be stat'd",
            facts={**facts, "error": redaction.scrub_exception(exc)},
        )
    facts["keyMode"] = f"{key_mode:04o}"
    if key_mode & 0o077:
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.FAIL, "key_permissions_too_open",
            detail=(
                "the authority client private key is readable beyond its owner; it "
                "authenticates this host to the service that hands out ChatGPT tokens"
            ),
            facts=facts,
        )

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        certificate = x509.load_pem_x509_certificate(Path(files.cert).read_bytes())
        not_before = certificate.not_valid_before_utc
        not_after = certificate.not_valid_after_utc
        common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    except Exception as exc:  # noqa: BLE001 -- an unparseable cert is this check's answer
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.FAIL, "certificate_unreadable",
            detail="the authority client certificate could not be parsed",
            facts={**facts, "error": redaction.scrub_exception(exc)},
        )

    moment = now or datetime.now(timezone.utc)
    facts.update(
        {
            "subjectCommonName": str(common_names[0].value) if common_names else None,
            "notBefore": not_before.isoformat(),
            "notAfter": not_after.isoformat(),
            "daysRemaining": (not_after - moment).days,
        }
    )

    if moment < not_before:
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.FAIL, "certificate_not_yet_valid",
            detail="the authority client certificate is not valid yet",
            facts=facts,
        )
    if moment >= not_after:
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.FAIL, "certificate_expired",
            detail="the authority client certificate has expired",
            facts=facts,
        )
    if moment + timedelta(days=CERT_EXPIRY_WARN_DAYS) >= not_after:
        return DoctorResult(
            DoctorCheck.MTLS_IDENTITY, DoctorOutcome.WARN, "certificate_expiring",
            detail=(
                "the authority client certificate expires soon; mint a replacement "
                "(docs/runbooks/codex-credential-authority.md)"
            ),
            facts=facts,
        )
    return DoctorResult(
        DoctorCheck.MTLS_IDENTITY, DoctorOutcome.OK, "valid",
        detail="the authority client certificate is valid and its key is private",
        facts=facts,
    )


ManifestLoader = Callable[[], Sequence[Any]]


def check_manifest(load_entries: ManifestLoader, *, enabled: bool) -> DoctorResult:
    """Are the OpenAI accounts in the account manifest routed at the authority?

    The manifest is the declared identity record — for OpenAI it is the *only*
    proof an account exists, because its credential lives in the authority's
    private state and no host may read it. An OpenAI entry left on
    ``config_dir``/``local`` would send a runner looking for a local credential
    file, which is precisely the "no ``auth.json`` on a runner" invariant.
    """
    if not enabled:
        return DoctorResult(
            DoctorCheck.MANIFEST, DoctorOutcome.SKIPPED, "codex_disabled",
            detail="CCSWITCH_CODEX_ENABLED is false",
        )

    try:
        from ccswitch import account_registry

        entries = list(load_entries())
    except Exception as exc:  # noqa: BLE001 -- an unreadable manifest is not fatal
        return DoctorResult(
            DoctorCheck.MANIFEST, DoctorOutcome.WARN, "manifest_unreadable",
            detail="the account manifest could not be read",
            facts={"error": redaction.scrub_exception(exc)},
        )

    openai = [
        entry for entry in entries
        if getattr(entry, "provider", None) == account_registry.PROVIDER_OPENAI
    ]
    if not openai:
        return DoctorResult(
            DoctorCheck.MANIFEST, DoctorOutcome.WARN, "no_openai_accounts",
            detail=(
                "Codex is enabled but the account manifest registers no OpenAI "
                "account, so no Codex session can be launched"
            ),
            facts={"openaiAccounts": 0},
        )

    misrouted = [
        entry.name for entry in openai
        if entry.runner_auth_mode != account_registry.RUNNER_AUTH_CHATGPT_EXTERNAL_TOKENS
        or entry.credential_authority != account_registry.AUTHORITY_CODEX_AUTH
    ]
    facts: dict[str, Any] = {
        "openaiAccounts": len(openai),
        "enabledOpenaiAccounts": sum(1 for entry in openai if entry.enabled),
    }
    if misrouted:
        return DoctorResult(
            DoctorCheck.MANIFEST, DoctorOutcome.FAIL, "openai_account_misrouted",
            detail=(
                "an OpenAI account is not routed at the credential authority; a "
                "runner would look for a local credential file for it"
            ),
            # Account names are public in the manifest and already appear in the
            # audit trail; the operator cannot fix this without knowing which one.
            facts={**facts, "misroutedAccounts": sorted(misrouted)},
        )
    return DoctorResult(
        DoctorCheck.MANIFEST, DoctorOutcome.OK, "routed",
        detail="every OpenAI account is routed at the credential authority",
        facts=facts,
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _guard(check: DoctorCheck, call: Callable[[], DoctorResult]) -> DoctorResult:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 -- the report must always be complete
        return DoctorResult(
            check, DoctorOutcome.FAIL, "check_error",
            detail=redaction.scrub_exception(exc),
        )


async def _guard_async(
    check: DoctorCheck, call: Callable[[], Awaitable[DoctorResult]]
) -> DoctorResult:
    try:
        return await call()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the report must always be complete
        return DoctorResult(
            check, DoctorOutcome.FAIL, "check_error",
            detail=redaction.scrub_exception(exc),
        )


async def run_doctor(
    settings: Any,
    *,
    availability: CodexAvailability | None = None,
    home_base: str | Path | None = None,
    mounts: codex_process.Mounts | None = None,
    authority_health: AuthorityHealthProbe | None = None,
    authority_configured: bool | None = None,
    load_tls_files: TLSFilesLoader | None = None,
    load_manifest_entries: ManifestLoader | None = None,
    now: datetime | None = None,
) -> CodexDoctorReport:
    """Run every check and return a complete report. Never raises.

    Every collaborator is injectable so the checks are unit-testable without a
    binary, a mount table, a socket, or a manifest on disk; the defaults are the
    real ones the daemon uses. ``availability`` should be the value the Codex
    manager already computed — passing ``None`` means "this host never probed",
    not "probe again".
    """
    enabled = bool(getattr(settings, "codex_enabled", False))
    external = bool(getattr(settings, "codex_external_auth_enabled", False))

    if home_base is None:
        from server.codex.manager import default_codex_home_base

        home_base = default_codex_home_base()
    if authority_configured is None:
        from server.codex.auth_endpoint import authority_configured as _configured

        authority_configured = _configured()
    if load_tls_files is None:
        from server.codex.auth_endpoint import authority_client_tls_files

        load_tls_files = authority_client_tls_files
    if load_manifest_entries is None:
        def load_manifest_entries() -> Sequence[Any]:
            from ccswitch import account_registry

            return account_registry.all_entries()
    if authority_health is None and authority_configured:
        def authority_health() -> Awaitable[AuthorityHealth]:
            from server.codex.auth_client import AuthorityClient

            return AuthorityClient.from_env().health()

    results = (
        _guard(
            DoctorCheck.EXTERNAL_AUTH,
            lambda: check_external_auth(enabled=enabled, external_auth_enabled=external),
        ),
        _guard(DoctorCheck.BINARY, lambda: check_binary(availability)),
        _guard(
            DoctorCheck.RUNTIME_HOME,
            lambda: check_runtime_home(home_base, mounts=mounts, enabled=enabled),
        ),
        await _guard_async(
            DoctorCheck.AUTHORITY,
            lambda: check_authority(
                authority_health, configured=bool(authority_configured), enabled=enabled
            ),
        ),
        _guard(
            DoctorCheck.MTLS_IDENTITY,
            lambda: check_mtls_identity(
                load_tls_files,
                configured=bool(authority_configured),
                enabled=enabled,
                now=now,
            ),
        ),
        _guard(DoctorCheck.MANIFEST, lambda: check_manifest(load_manifest_entries, enabled=enabled)),
    )
    return CodexDoctorReport(results)


def log_report(report: CodexDoctorReport) -> None:
    """Emit the report as one structured record.

    One record rather than one per check: an operator greps ``codex.doctor.report``
    and gets the whole picture, and a boot with Codex off does not add six lines to
    every journal.
    """
    level = logging.INFO if report.ok else logging.WARNING
    log_event(logger, level, "codex.doctor.report", **report.to_wire())


async def run_and_log(
    settings: Any,
    *,
    availability: CodexAvailability | None = None,
    timeout_s: float = DOCTOR_TIMEOUT_S,
) -> CodexDoctorReport | None:
    """Run the doctor, log it, and swallow every failure. Returns ``None`` if it
    could not produce a report at all.

    This is what the daemon calls. It is bounded because the authority health
    probe carries the 20 s admin deadline, and diagnostics may not hold a daemon
    that owns live `claude` processes hostage — the caller runs it as a background
    task for the same reason.
    """
    try:
        async with asyncio.timeout(timeout_s):
            report = await run_doctor(settings, availability=availability)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        log_event(logger, logging.WARNING, "codex.doctor.timeout", timeoutS=timeout_s)
        return None
    except Exception:  # noqa: BLE001 -- diagnostics never break their caller
        logger.warning("codex doctor could not run", exc_info=True)
        return None
    log_report(report)
    return report


async def main() -> int:
    """``python -m server.codex.doctor`` — print the report as JSON.

    Exit status is the operator-facing verdict: 0 when nothing failed, 1 otherwise,
    so the runbook can use it in a shell conditional.
    """
    from server.config import get_settings

    report = await run_doctor(get_settings())
    print(json.dumps(report.to_wire(), indent=2, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised by the runbook, not tests
    raise SystemExit(asyncio.run(main()))
