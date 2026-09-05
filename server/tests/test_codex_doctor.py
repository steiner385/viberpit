"""The Codex startup doctor: one report, no second opinion, no leaked secret.

Three properties are worth asserting here and they are asserted separately:

* **Every check degrades.** ``run_doctor`` is called from the daemon lifespan, so
  a check that raises must become a ``FAIL`` result inside a complete report —
  never an exception that reaches a daemon which is also serving Claude sessions.
* **The doctor reports verdicts it did not invent.** Availability comes from
  ``server.codex.process.probe_availability`` and locality from
  ``assert_local_filesystem``; the doctor may not re-decide either. The tests
  therefore feed the *typed* values those producers return.
* **Nothing credential-shaped survives.** The redaction tests below follow the
  non-vacuous idiom of ``test_codex_auth_redaction.py``: each first asserts the
  seeded token IS present in the raw input, so "the token is absent from the
  report" cannot pass against a report that never carried the field at all.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ccswitch import account_registry
from server.codex import doctor
from server.codex.auth_endpoint import AuthorityClientTLSFiles, AuthorityConfigError
from server.codex.auth_types import AuthorityHealth
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.codex.doctor import DoctorCheck, DoctorOutcome
from server.codex.schema import (
    PINNED_CLI_VERSION,
    PINNED_CONTRACT_SHA256,
    ContractDrift,
    ContractDriftReason,
)

# A real-SHAPED credential (never a real one). Both shapes are the ones
# server/codex/redaction.py owns beyond the shared server.audit patterns.
FAKE_OPENAI_KEY = "sk-" + "A1b2C3d4E5f6G7h8J9k0"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl"


def _local_mounts(*points: str) -> tuple[tuple[str, str], ...]:
    return tuple((point, "ext4") for point in points)


# ---------------------------------------------------------------------------
# DoctorResult / report aggregation
# ---------------------------------------------------------------------------


def test_report_outcome_is_the_worst_of_its_checks() -> None:
    ok = doctor.DoctorResult(DoctorCheck.BINARY, DoctorOutcome.OK, "ready")
    warn = doctor.DoctorResult(DoctorCheck.MANIFEST, DoctorOutcome.WARN, "no_openai_accounts")
    fail = doctor.DoctorResult(DoctorCheck.AUTHORITY, DoctorOutcome.FAIL, "unreachable")
    skipped = doctor.DoctorResult(DoctorCheck.MTLS_IDENTITY, DoctorOutcome.SKIPPED, "disabled")

    assert doctor.CodexDoctorReport((ok, skipped)).outcome is DoctorOutcome.OK
    assert doctor.CodexDoctorReport((ok, warn)).outcome is DoctorOutcome.WARN
    assert doctor.CodexDoctorReport((ok, warn, fail)).outcome is DoctorOutcome.FAIL
    # A report of nothing but skips is not "ok" — it is skipped.
    assert doctor.CodexDoctorReport((skipped,)).outcome is DoctorOutcome.SKIPPED
    assert doctor.CodexDoctorReport(()).outcome is DoctorOutcome.SKIPPED


def test_report_wire_form_is_camel_case_and_json_safe() -> None:
    report = doctor.CodexDoctorReport(
        (doctor.DoctorResult(DoctorCheck.BINARY, DoctorOutcome.OK, "ready",
                             facts={"cliVersion": PINNED_CLI_VERSION}),)
    )
    wire = report.to_wire()
    # json.dumps is the real assertion: a StrEnum or Path left in facts fails here.
    json.dumps(wire)
    assert wire["outcome"] == "ok"
    assert wire["checks"][0]["check"] == "binary"
    assert wire["checks"][0]["facts"]["cliVersion"] == PINNED_CLI_VERSION


def test_result_drops_a_secret_shaped_detail_but_keeps_authored_text() -> None:
    authored = doctor.DoctorResult(
        DoctorCheck.BINARY, DoctorOutcome.FAIL, "binary_not_found",
        detail="codex is not installed",
    )
    assert authored.detail == "codex is not installed"

    # Non-vacuous: the token really is in the string handed to the constructor.
    salted = f"codex --version failed: {FAKE_OPENAI_KEY}"
    assert FAKE_OPENAI_KEY in salted
    leaked = doctor.DoctorResult(
        DoctorCheck.BINARY, DoctorOutcome.FAIL, "version_probe_failed", detail=salted
    )
    assert FAKE_OPENAI_KEY not in leaked.detail
    assert leaked.detail == doctor.redaction.REDACTED


def test_result_drops_a_secret_shaped_fact_value() -> None:
    salted_path = f"/opt/codex/{FAKE_JWT}/bin/codex"
    assert FAKE_JWT in salted_path
    result = doctor.DoctorResult(
        DoctorCheck.BINARY, DoctorOutcome.OK, "ready", facts={"binaryPath": salted_path}
    )
    assert FAKE_JWT not in json.dumps(result.to_wire())


# ---------------------------------------------------------------------------
# external-auth capability
# ---------------------------------------------------------------------------


def test_external_auth_skipped_when_codex_is_disabled() -> None:
    result = doctor.check_external_auth(enabled=False, external_auth_enabled=False)
    assert result.outcome is DoctorOutcome.SKIPPED
    assert result.code == "codex_disabled"


def test_external_auth_fails_loudly_when_codex_is_on_without_it() -> None:
    """Codex without external auth cannot authenticate — that is a FAIL, not a WARN.

    Runners hold no credential by design, so the app-server has no way to log in.
    """
    result = doctor.check_external_auth(enabled=True, external_auth_enabled=False)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "external_auth_disabled"


def test_external_auth_ok_when_both_flags_are_on() -> None:
    result = doctor.check_external_auth(enabled=True, external_auth_enabled=True)
    assert result.outcome is DoctorOutcome.OK


# ---------------------------------------------------------------------------
# binary version / schema — consumed from CodexAvailability, never re-probed
# ---------------------------------------------------------------------------


def test_binary_ok_carries_the_observed_and_pinned_contract() -> None:
    result = doctor.check_binary(
        CodexAvailability.ready(
            binary_path="/usr/local/bin/codex",
            cli_version=PINNED_CLI_VERSION,
            contract_sha256=PINNED_CONTRACT_SHA256,
        )
    )
    assert result.outcome is DoctorOutcome.OK
    assert result.facts["cliVersion"] == PINNED_CLI_VERSION
    assert result.facts["pinnedCliVersion"] == PINNED_CLI_VERSION
    assert result.facts["contractSha256"] == PINNED_CONTRACT_SHA256
    assert result.facts["pinnedContractSha256"] == PINNED_CONTRACT_SHA256


def test_binary_warns_when_the_operator_admitted_a_drifting_version() -> None:
    drift = ContractDrift(
        reasons=(ContractDriftReason.CLI_VERSION,),
        expected_cli_version=PINNED_CLI_VERSION,
        observed_cli_version="codex-cli 0.153.3",
        expected_contract_sha256=PINNED_CONTRACT_SHA256,
        observed_contract_sha256=PINNED_CONTRACT_SHA256,
    )
    result = doctor.check_binary(
        CodexAvailability.ready(
            binary_path="/usr/local/bin/codex",
            cli_version="codex-cli 0.153.3",
            accepted_drift=drift,
            contract_sha256=PINNED_CONTRACT_SHA256,
        )
    )
    assert result.outcome is DoctorOutcome.WARN
    assert result.code == "version_drift_accepted"
    assert result.facts["acceptedDrift"] == ["cli_version_mismatch"]


@pytest.mark.parametrize(
    "reason",
    [
        CodexUnavailableReason.BINARY_NOT_FOUND,
        CodexUnavailableReason.VERSION_PROBE_FAILED,
        CodexUnavailableReason.UNSUPPORTED_VERSION,
        CodexUnavailableReason.RUNTIME_HOME_REFUSED,
    ],
)
def test_binary_fails_with_the_availability_reason_code(reason: CodexUnavailableReason) -> None:
    """The doctor forwards the probe's typed reason — it does not invent its own."""
    result = doctor.check_binary(CodexAvailability.unavailable(reason, "detail"))
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == reason.value


@pytest.mark.parametrize(
    "reason",
    [CodexUnavailableReason.DISABLED, CodexUnavailableReason.EXTERNAL_AUTH_DISABLED],
)
def test_binary_is_skipped_not_failed_for_the_two_flag_reasons(
    reason: CodexUnavailableReason,
) -> None:
    """A flag that is off is not a broken binary; the external-auth check owns that FAIL."""
    result = doctor.check_binary(CodexAvailability.unavailable(reason, "off"))
    assert result.outcome is DoctorOutcome.SKIPPED
    assert result.code == reason.value


def test_binary_is_skipped_when_availability_was_never_computed() -> None:
    result = doctor.check_binary(None)
    assert result.outcome is DoctorOutcome.SKIPPED
    assert result.code == "not_probed"


# ---------------------------------------------------------------------------
# runtime home — locality policy stays in process.assert_local_filesystem
# ---------------------------------------------------------------------------


def test_runtime_home_ok_for_a_local_private_existing_directory(tmp_path: Path) -> None:
    base = tmp_path / "codex"
    base.mkdir(mode=0o700)
    result = doctor.check_runtime_home(base, mounts=_local_mounts(str(tmp_path)))
    assert result.outcome is DoctorOutcome.OK
    assert result.facts["exists"] is True
    assert result.facts["filesystemType"] == "ext4"


def test_runtime_home_ok_when_absent_because_it_is_created_on_demand(tmp_path: Path) -> None:
    """``ensure_codex_home`` makes the directory at first use; absence is not a fault."""
    result = doctor.check_runtime_home(
        tmp_path / "codex", mounts=_local_mounts(str(tmp_path))
    )
    assert result.outcome is DoctorOutcome.OK
    assert result.facts["exists"] is False


def test_runtime_home_fails_on_a_network_filesystem(tmp_path: Path) -> None:
    """#1005: Codex SQLite/WAL on sshfs is the incident this refusal exists for."""
    mounts = ((str(tmp_path), "fuse.sshfs"),)
    result = doctor.check_runtime_home(tmp_path / "codex", mounts=mounts)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "network_filesystem"
    assert result.facts["filesystemType"] == "fuse.sshfs"


def test_runtime_home_fails_when_the_mount_table_cannot_prove_locality(tmp_path: Path) -> None:
    result = doctor.check_runtime_home(tmp_path / "codex", mounts=())
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "filesystem_undeterminable"


def test_runtime_home_fails_on_a_symlinked_path_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    result = doctor.check_runtime_home(link, mounts=_local_mounts(str(tmp_path)))
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "symlinked_path"


def test_runtime_home_warns_when_the_mode_is_wider_than_0700(tmp_path: Path) -> None:
    base = tmp_path / "codex"
    base.mkdir(mode=0o755)
    base.chmod(0o755)
    result = doctor.check_runtime_home(base, mounts=_local_mounts(str(tmp_path)))
    assert result.outcome is DoctorOutcome.WARN
    assert result.code == "permissive_mode"
    assert result.facts["mode"] == "0755"


def test_runtime_home_fails_when_another_user_owns_it(tmp_path: Path) -> None:
    base = tmp_path / "codex"
    base.mkdir(mode=0o700)
    # An account home owned by anyone else — notably the ccswitch-auth user whose
    # managed home a symlink could aim at — must not be written to by this daemon.
    result = doctor.check_runtime_home(
        base, mounts=_local_mounts(str(tmp_path)), uid=os.getuid() + 1
    )
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "foreign_owner"


def test_runtime_home_is_skipped_when_codex_is_disabled(tmp_path: Path) -> None:
    result = doctor.check_runtime_home(
        tmp_path / "codex", mounts=(), enabled=False
    )
    assert result.outcome is DoctorOutcome.SKIPPED


# ---------------------------------------------------------------------------
# authority reachability — AuthorityClient.health() is the only prober
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authority_ok_when_reachable_with_a_live_sync_lease() -> None:
    async def health() -> AuthorityHealth:
        return AuthorityHealth(
            reachable=True, sync_revision=7, sync_expires_in_s=600.0, accounts=2
        )

    result = await doctor.check_authority(health, configured=True, enabled=True)
    assert result.outcome is DoctorOutcome.OK
    assert result.facts["syncRevision"] == 7
    assert result.facts["accounts"] == 2


@pytest.mark.asyncio
async def test_authority_unconfigured_and_unreachable_are_distinct_codes() -> None:
    async def unreachable() -> AuthorityHealth:
        return AuthorityHealth.unreachable("connection refused")

    missing = await doctor.check_authority(None, configured=False, enabled=True)
    down = await doctor.check_authority(unreachable, configured=True, enabled=True)

    assert missing.outcome is DoctorOutcome.FAIL
    assert missing.code == "unconfigured"
    assert down.outcome is DoctorOutcome.FAIL
    assert down.code == "unreachable"
    assert missing.code != down.code


@pytest.mark.asyncio
async def test_authority_warns_when_the_authorization_lease_has_lapsed() -> None:
    """A lapsed projection stops issuance; the daemon is up but Codex will not run."""
    async def health() -> AuthorityHealth:
        return AuthorityHealth(
            reachable=True, sync_revision=3, sync_expires_in_s=-1.0, accounts=1
        )

    result = await doctor.check_authority(health, configured=True, enabled=True)
    assert result.outcome is DoctorOutcome.WARN
    assert result.code == "sync_lease_lapsed"


@pytest.mark.asyncio
async def test_authority_warns_when_no_authorization_sync_has_landed() -> None:
    async def health() -> AuthorityHealth:
        return AuthorityHealth(reachable=True, sync_revision=None)

    result = await doctor.check_authority(health, configured=True, enabled=True)
    assert result.outcome is DoctorOutcome.WARN
    assert result.code == "no_authorization_sync"


@pytest.mark.asyncio
async def test_authority_probe_that_raises_becomes_a_fail_with_a_category() -> None:
    async def health() -> AuthorityHealth:
        raise OSError(f"tls handshake failed for {FAKE_JWT}")

    result = await doctor.check_authority(health, configured=True, enabled=True)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "probe_failed"
    # scrub_exception reduces to a fixed category — the message never renders.
    assert FAKE_JWT not in json.dumps(result.to_wire())
    assert result.detail == "transport_error"


@pytest.mark.asyncio
async def test_authority_is_skipped_when_codex_is_disabled() -> None:
    result = await doctor.check_authority(None, configured=False, enabled=False)
    assert result.outcome is DoctorOutcome.SKIPPED


# ---------------------------------------------------------------------------
# mTLS identity
# ---------------------------------------------------------------------------


def _write_client_material(
    tmp_path: Path, *, not_before: datetime, not_after: datetime, common_name: str
) -> AuthorityClientTLSFiles:
    """Mint a throwaway self-signed client certificate and key on disk."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "client.pem"
    key_path = tmp_path / "client.key"
    ca_path = tmp_path / "server-ca.pem"
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(cert_bytes)
    ca_path.write_bytes(cert_bytes)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return AuthorityClientTLSFiles(
        cert=str(cert_path), key=str(key_path), ca=str(ca_path)
    )


def test_mtls_ok_for_a_valid_certificate_with_a_private_key(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    files = _write_client_material(
        tmp_path,
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=90),
        common_name="codex-runner:dobby",
    )
    result = doctor.check_mtls_identity(lambda: files, configured=True, enabled=True, now=now)
    assert result.outcome is DoctorOutcome.OK
    # The client CN is the runner identity the authority allowlists — non-secret,
    # documented in deploy/README.md, and the one fact an operator needs here.
    assert result.facts["subjectCommonName"] == "codex-runner:dobby"
    # X.509 truncates validity times to whole seconds, so a "90 days from now"
    # certificate lands a fraction of a second short of 90 whole days.
    assert result.facts["daysRemaining"] in (89, 90)


def test_mtls_fails_on_an_expired_certificate(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    files = _write_client_material(
        tmp_path,
        not_before=now - timedelta(days=400),
        not_after=now - timedelta(days=1),
        common_name="codex-runner:rocky",
    )
    result = doctor.check_mtls_identity(lambda: files, configured=True, enabled=True, now=now)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "certificate_expired"


def test_mtls_fails_on_a_not_yet_valid_certificate(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    files = _write_client_material(
        tmp_path,
        not_before=now + timedelta(days=2),
        not_after=now + timedelta(days=90),
        common_name="codex-edge",
    )
    result = doctor.check_mtls_identity(lambda: files, configured=True, enabled=True, now=now)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "certificate_not_yet_valid"


def test_mtls_warns_before_the_certificate_expires(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    files = _write_client_material(
        tmp_path,
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=3),
        common_name="codex-edge",
    )
    result = doctor.check_mtls_identity(lambda: files, configured=True, enabled=True, now=now)
    assert result.outcome is DoctorOutcome.WARN
    assert result.code == "certificate_expiring"


def test_mtls_fails_when_the_private_key_is_group_or_world_readable(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    files = _write_client_material(
        tmp_path,
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=90),
        common_name="codex-runner:dobby",
    )
    Path(files.key).chmod(0o644)
    result = doctor.check_mtls_identity(lambda: files, configured=True, enabled=True, now=now)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "key_permissions_too_open"
    assert result.facts["keyMode"] == "0644"


def test_mtls_fails_when_the_loader_refuses_the_configuration() -> None:
    """The loader already enforces existence, readability and trust-domain
    separation from the session daemon (``assert_distinct_from_sessiond``). The
    doctor reports that refusal rather than re-deriving it."""
    def load() -> AuthorityClientTLSFiles:
        raise AuthorityConfigError(
            "CCSWITCH_CODEX_AUTHORITY_CLIENT_KEY must not be the same key as "
            "CCSWITCH_SESSIOND_CLIENT_KEY"
        )

    result = doctor.check_mtls_identity(load, configured=True, enabled=True)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "tls_material_unusable"


def test_mtls_fails_when_the_certificate_cannot_be_parsed(tmp_path: Path) -> None:
    cert = tmp_path / "client.pem"
    cert.write_text("not a certificate", encoding="utf-8")
    key = tmp_path / "client.key"
    key.write_text("", encoding="utf-8")
    key.chmod(0o600)
    files = AuthorityClientTLSFiles(cert=str(cert), key=str(key), ca=str(cert))
    result = doctor.check_mtls_identity(lambda: files, configured=True, enabled=True)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "certificate_unreadable"


def test_mtls_is_skipped_when_the_authority_is_unconfigured() -> None:
    """The authority check owns the "unconfigured" FAIL; this must not double-report."""
    result = doctor.check_mtls_identity(None, configured=False, enabled=True)
    assert result.outcome is DoctorOutcome.SKIPPED
    assert result.code == "authority_unconfigured"


# ---------------------------------------------------------------------------
# account manifest
# ---------------------------------------------------------------------------


def _openai_entry(name: str, **overrides: Any) -> account_registry.AccountEntry:
    fields: dict[str, Any] = {
        "provider": account_registry.PROVIDER_OPENAI,
        "user_auth_mode": account_registry.USER_AUTH_CHATGPT_DEVICE,
        "runner_auth_mode": account_registry.RUNNER_AUTH_CHATGPT_EXTERNAL_TOKENS,
        "credential_authority": account_registry.AUTHORITY_CODEX_AUTH,
    }
    fields.update(overrides)
    return account_registry.AccountEntry(name=name, **fields)


def test_manifest_ok_for_correctly_routed_openai_accounts() -> None:
    entries = [
        _openai_entry("chatgpt-main"),
        account_registry.AccountEntry(
            name="anthropic-main", provider=account_registry.PROVIDER_ANTHROPIC
        ),
    ]
    result = doctor.check_manifest(lambda: entries, enabled=True)
    assert result.outcome is DoctorOutcome.OK
    assert result.facts["openaiAccounts"] == 1


def test_manifest_warns_when_codex_is_on_but_no_openai_account_exists() -> None:
    result = doctor.check_manifest(lambda: [], enabled=True)
    assert result.outcome is DoctorOutcome.WARN
    assert result.code == "no_openai_accounts"


def test_manifest_fails_when_an_openai_account_is_routed_at_local_credentials() -> None:
    """A runner would try config-dir auth for an account whose credential lives in
    the authority — the exact misconfiguration that puts an auth.json on a runner."""
    entries = [
        _openai_entry("chatgpt-main"),
        _openai_entry(
            "chatgpt-broken",
            runner_auth_mode=account_registry.RUNNER_AUTH_CONFIG_DIR,
            credential_authority=account_registry.AUTHORITY_LOCAL,
        ),
    ]
    result = doctor.check_manifest(lambda: entries, enabled=True)
    assert result.outcome is DoctorOutcome.FAIL
    assert result.code == "openai_account_misrouted"
    assert result.facts["misroutedAccounts"] == ["chatgpt-broken"]


def test_manifest_warns_when_the_registry_cannot_be_read() -> None:
    def load() -> list[account_registry.AccountEntry]:
        raise OSError("manifest unreadable")

    result = doctor.check_manifest(load, enabled=True)
    assert result.outcome is DoctorOutcome.WARN
    assert result.code == "manifest_unreadable"


def test_manifest_is_skipped_when_codex_is_disabled() -> None:
    result = doctor.check_manifest(lambda: [], enabled=False)
    assert result.outcome is DoctorOutcome.SKIPPED


# ---------------------------------------------------------------------------
# run_doctor composition
# ---------------------------------------------------------------------------


class _Settings:
    def __init__(self, *, enabled: bool = True, external: bool = True) -> None:
        self.codex_enabled = enabled
        self.codex_external_auth_enabled = external


@pytest.mark.asyncio
async def test_run_doctor_reports_every_check_exactly_once(tmp_path: Path) -> None:
    report = await doctor.run_doctor(
        _Settings(enabled=False, external=False),
        home_base=tmp_path / "codex",
        mounts=_local_mounts(str(tmp_path)),
        load_manifest_entries=lambda: [],
        authority_configured=False,
    )
    assert [r.check for r in report.results] == list(DoctorCheck)
    assert report.outcome is DoctorOutcome.SKIPPED


@pytest.mark.asyncio
async def test_run_doctor_never_raises_when_a_check_explodes(tmp_path: Path) -> None:
    """The daemon lifespan calls this; an exception here must not reach it."""
    def boom() -> list[account_registry.AccountEntry]:
        raise RuntimeError("boom")

    report = await doctor.run_doctor(
        _Settings(),
        availability=CodexAvailability.ready(
            binary_path="/usr/local/bin/codex", cli_version=PINNED_CLI_VERSION
        ),
        home_base=tmp_path / "codex",
        mounts=_local_mounts(str(tmp_path)),
        load_manifest_entries=boom,
        authority_configured=False,
    )
    manifest = next(r for r in report.results if r.check is DoctorCheck.MANIFEST)
    # A raising loader is the "unreadable" case, and the report is still complete.
    assert manifest.outcome is not DoctorOutcome.OK
    assert len(report.results) == len(DoctorCheck)


@pytest.mark.asyncio
async def test_run_doctor_turns_an_unexpected_check_failure_into_a_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise ZeroDivisionError("unexpected")

    monkeypatch.setattr(doctor, "check_runtime_home", explode)
    report = await doctor.run_doctor(
        _Settings(),
        home_base=tmp_path / "codex",
        mounts=_local_mounts(str(tmp_path)),
        load_manifest_entries=lambda: [],
        authority_configured=False,
    )
    home = next(r for r in report.results if r.check is DoctorCheck.RUNTIME_HOME)
    assert home.outcome is DoctorOutcome.FAIL
    assert home.code == "check_error"
    assert len(report.results) == len(DoctorCheck)


@pytest.mark.asyncio
async def test_run_doctor_log_record_carries_no_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every string the doctor could surface is salted; none may reach the journal."""
    salted_home = tmp_path / f"codex-{FAKE_OPENAI_KEY}"

    async def health() -> AuthorityHealth:
        raise OSError(f"handshake failed: {FAKE_JWT}")

    # Non-vacuous: both tokens really are in the inputs.
    assert FAKE_OPENAI_KEY in str(salted_home)

    with caplog.at_level(logging.DEBUG, logger="server.codex.doctor"):
        report = await doctor.run_doctor(
            _Settings(),
            availability=CodexAvailability.unavailable(
                CodexUnavailableReason.VERSION_PROBE_FAILED,
                f"codex --version exited 1: {FAKE_OPENAI_KEY}",
                binary_path=str(salted_home / "bin" / "codex"),
            ),
            home_base=salted_home,
            mounts=_local_mounts(str(tmp_path)),
            authority_health=health,
            load_manifest_entries=lambda: [],
            authority_configured=True,
        )
        doctor.log_report(report)

    rendered = json.dumps(report.to_wire()) + "\n".join(
        record.getMessage() + json.dumps(getattr(record, "fields", {}), default=str)
        for record in caplog.records
    )
    assert FAKE_OPENAI_KEY not in rendered
    assert FAKE_JWT not in rendered


@pytest.mark.asyncio
async def test_run_doctor_defaults_the_home_base_to_the_runner_local_root() -> None:
    """The default must be the runner-LOCAL state root, never the shared account
    store — ``manager.default_codex_home_base`` is the one definition of it."""
    from server.codex.manager import default_codex_home_base

    report = await doctor.run_doctor(
        _Settings(enabled=False, external=False),
        load_manifest_entries=lambda: [],
        authority_configured=False,
    )
    home = next(r for r in report.results if r.check is DoctorCheck.RUNTIME_HOME)
    assert home.facts.get("path") in (None, str(default_codex_home_base()))


# ---------------------------------------------------------------------------
# run_and_log — what the daemon lifespan actually calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_and_log_emits_one_structured_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One record, not one per check: a Codex-off boot must not add six log lines."""
    with caplog.at_level(logging.DEBUG, logger="server.codex.doctor"):
        report = await doctor.run_and_log(_Settings(enabled=False, external=False))
    assert report is not None
    events = [getattr(record, "event", None) for record in caplog.records]
    assert events.count("codex.doctor.report") == 1


@pytest.mark.asyncio
async def test_run_and_log_swallows_a_doctor_that_blows_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon lifespan may not be broken by its own diagnostics."""
    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("doctor itself is broken")

    monkeypatch.setattr(doctor, "run_doctor", explode)
    assert await doctor.run_and_log(_Settings()) is None


@pytest.mark.asyncio
async def test_run_and_log_gives_up_rather_than_hanging_on_the_authority() -> None:
    """The authority probe carries a 20 s admin deadline; a wedged one must not
    keep a startup task alive for the life of the daemon."""
    async def never_answers() -> AuthorityHealth:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    async def slow_doctor(*args: Any, **kwargs: Any) -> Any:
        return await doctor.run_doctor(
            _Settings(), authority_health=never_answers, authority_configured=True,
            load_manifest_entries=lambda: [],
        )

    started = asyncio.get_running_loop().time()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doctor, "run_doctor", slow_doctor)
        assert await doctor.run_and_log(_Settings(), timeout_s=0.05) is None
    assert asyncio.get_running_loop().time() - started < 5.0
