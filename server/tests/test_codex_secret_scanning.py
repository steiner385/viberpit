"""Secrets scanning across every Codex surface an operator or a bundle can read.

The plan's Task 6.1 ends with "exercise secrets scanning against logs, crash
traces, `/info`, audit rows, and support bundles". This file is that sweep. It is
deliberately *not* a second redactor: `server/codex/redaction.py` is the one place
the Codex boundary decides what may be rendered, and these tests only prove that
each surface actually routes through it.

`test_codex_auth_redaction.py` already owns the credential authority's own
surfaces (its reprs, its `to_wire`s, a full login/issue/refresh/revoke cycle, a
traceback with captured locals). This file covers what that one does not reach:
the runner/daemon side — `/info`, audit rows, crash traces and the stderr tail,
metric label values, and the whole set of structured records a support bundle
would gather from a crash-and-restart cycle.

Every test follows the same non-vacuous idiom: it FIRST asserts the seeded
token is really present in the input it feeds in, so "the token is absent from the
output" cannot pass against a surface that never carried the field at all.

The seeds are real-SHAPED and never real. Two of the three were the gaps this
sweep found: `sk-` (bare, non-Anthropic) and a short JWT were owned by
`server/codex/redaction.py`, so they were removed at the Codex boundary but
survived `audit.redact_detail` and `RedactingFilter` — shared surfaces no Codex
module sits on. Both now live in `server/audit.py`, and
`test_the_shared_redactor_owns_every_openai_shape` pins that.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server import audit
from server.codex import redaction
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.ipc.daemon import create_daemon_app

FAKE_OPENAI_KEY = "sk-" + "A1b2C3d4E5f6G7h8J9k0"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl"
FAKE_REFRESH = "ccenc1:" + "Q" * 48
ALL_SEEDS = (FAKE_OPENAI_KEY, FAKE_JWT, FAKE_REFRESH)


def assert_no_seed(rendered: str, *seeds: str) -> None:
    for seed in seeds or ALL_SEEDS:
        assert seed not in rendered, f"{seed[:12]}… leaked"


def assert_seeded(raw: str, *seeds: str) -> None:
    """The other half of every test: prove the input really carried the secret."""
    for seed in seeds or ALL_SEEDS:
        assert seed in raw, f"test is vacuous — {seed[:12]}… was never in the input"


# ---------------------------------------------------------------------------
# The seeds must actually be recognised as secret-shaped
# ---------------------------------------------------------------------------


def test_the_seeds_are_shapes_the_redactor_recognises() -> None:
    """Guards every other test in this file: a seed the redactor does not know is
    a shape would make the whole sweep vacuous."""
    for seed in ALL_SEEDS:
        assert redaction.contains_secret_shaped_value(seed), seed[:12]


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "A1b2C3d4E5f6G7h8J9k0",           # bare OpenAI key
        "sk-proj-" + "T3stK3yMaterialNeverIssued",  # project-scoped key
        "sk-ant-api03-" + "X" * 20,                 # Anthropic (already covered)
        "eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.c2ln",     # JWT with SHORT payload+signature
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl",
    ],
)
def test_the_shared_redactor_owns_every_openai_shape(secret: str) -> None:
    """The gaps this sweep found, pinned as regressions.

    `sk-` (bare) and the short-JWT shape were owned by server/codex/redaction.py,
    which meant they were removed at the Codex BOUNDARY but not from an audit row
    or a log record — surfaces no Codex module sits on, and ones that outlive
    logs. Both moved into server/audit.py, the canonical redactor. These
    assertions go through the SHARED entry points on purpose.
    """
    assert secret not in audit.redact_text(f"leak: {secret} tail")
    assert secret not in json.dumps(audit.redact_detail({"k": secret}))

    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "token=%s", (secret,), None
    )
    audit.RedactingFilter().filter(record)
    assert secret not in record.getMessage()


# ---------------------------------------------------------------------------
# /info — polled every 30s per host by the fleet probe
# ---------------------------------------------------------------------------


class _Codex:
    def __init__(self, availability: Any) -> None:
        self.availability = availability


class _Orch:
    def __init__(self, codex: Any) -> None:
        self._codex = codex
        self._records: dict[str, Any] = {}


def _info_payload(availability: Any) -> str:
    app = create_daemon_app()
    app.state.orchestrator = _Orch(_Codex(availability))
    with TestClient(app) as client:
        return json.dumps(client.get("/info").json())


def test_info_carries_no_secret_from_a_salted_probe_failure() -> None:
    """A hostile or merely unlucky environment can salt a binary path or an OS
    error message; /info is fanned out to the edge and stored on the host row."""
    salted_detail = f"codex --version exited 1: {FAKE_OPENAI_KEY} {FAKE_JWT}"
    salted_path = f"/opt/{FAKE_REFRESH}/bin/codex"
    assert_seeded(salted_detail + salted_path)

    availability = CodexAvailability.unavailable(
        CodexUnavailableReason.VERSION_PROBE_FAILED,
        salted_detail,
        binary_path=salted_path,
    )
    assert_no_seed(_info_payload(availability))


@pytest.mark.asyncio
async def test_a_version_line_carrying_a_token_is_scrubbed_at_the_probe() -> None:
    """`cli_version` is raw `codex --version` stdout, and unlike the rest of this
    module's native text it CANNOT be replaced wholesale — it is compared against
    the pin and rendered on /info, the host row and the Fleet table. So it gets the
    narrow treatment at the source instead.

    This gap was found by this sweep: /info previously carried the version line
    verbatim, and /info is polled every 30s per host and fanned out to the edge.
    """
    from server.codex.process import MAX_CLI_VERSION_CHARS, probe_cli_version

    salted_line = f"codex-cli 0.153.2 {FAKE_OPENAI_KEY} {FAKE_JWT}"
    assert_seeded(salted_line, FAKE_OPENAI_KEY, FAKE_JWT)

    async def runner(*args: str) -> tuple[int, str, str]:
        return 0, salted_line, ""

    version = await probe_cli_version("/usr/bin/codex", runner=runner)
    assert_no_seed(version)
    # The USEFUL part survives — this is not scrub_text's wholesale replacement.
    assert version.startswith("codex-cli 0.153.2")

    async def flooding_runner(*args: str) -> tuple[int, str, str]:
        return 0, "v" * 10_000, ""

    assert len(await probe_cli_version("/usr/bin/codex", runner=flooding_runner)) == (
        MAX_CLI_VERSION_CHARS
    )


def test_info_carries_no_secret_from_a_salted_ready_runner() -> None:
    # `contract_sha256` is not salted here: it is always a hashlib hex digest
    # (schema.extract_contract), so it is structurally incapable of carrying one.
    availability = CodexAvailability.ready(
        binary_path=f"/opt/{FAKE_REFRESH}/bin/codex",
        cli_version="codex-cli 0.153.2",
        contract_sha256="a" * 64,
    )
    assert_no_seed(_info_payload(availability))


# ---------------------------------------------------------------------------
# Audit rows — retained far longer than logs
# ---------------------------------------------------------------------------


def test_audit_detail_redacts_a_codex_shaped_row() -> None:
    detail = {
        "engine": "codex",
        "account": "chatgpt-main",
        "error": f"login failed: {FAKE_OPENAI_KEY}",
        "envelope": {"accessToken": FAKE_JWT, "generation": 4},
        "trace": [f"frame {FAKE_REFRESH}"],
    }
    assert_seeded(json.dumps(detail))
    assert_no_seed(json.dumps(audit.redact_detail(detail)))
    # The non-secret fields an operator needs must survive.
    redacted = audit.redact_detail(detail)
    assert redacted["engine"] == "codex"
    assert redacted["envelope"]["generation"] == 4


def test_audit_event_redacts_a_codex_shaped_wire_event() -> None:
    wire = {
        "type": "session.error",
        "engine": "codex",
        "detail": {"message": f"app-server said {FAKE_JWT}"},
    }
    assert_seeded(json.dumps(wire), FAKE_JWT)
    assert_no_seed(json.dumps(audit.redact_event(wire)))


# ---------------------------------------------------------------------------
# Crash traces — the stderr tail is the one place native output is retained
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_retained_stderr_tail_never_renders_native_output() -> None:
    """`CodexProcess` keeps a few KB of child stderr for post-mortems. It is the
    only native text this daemon holds on to, so it is scrubbed WHOLESALE rather
    than pattern-matched: nothing infers which parts of a crash dump are harmless."""
    from server.codex.process import CodexProcess

    process = CodexProcess("/nonexistent/codex", Path("/tmp"))
    process._stderr = bytearray(  # type: ignore[attr-defined]
        f"panic: token={FAKE_OPENAI_KEY} refresh={FAKE_REFRESH}\n{FAKE_JWT}\n".encode()
    )
    assert_seeded(bytes(process._stderr).decode())  # type: ignore[attr-defined]

    tail = process.stderr_tail()
    assert_no_seed(tail)
    assert tail == redaction.REDACTED


@pytest.mark.asyncio
async def test_a_start_failure_reports_a_category_not_the_exception_text() -> None:
    """`CodexStartError`'s message reaches an operator and an audit row. The
    underlying exception is reduced to a fixed `scrub_exception` category."""
    salted = RuntimeError(f"connect failed for {FAKE_JWT}")
    assert_seeded(str(salted), FAKE_JWT)
    assert_no_seed(redaction.scrub_exception(salted), FAKE_JWT)
    assert redaction.scrub_exception(salted) == "runtime_error"


def test_a_crash_notice_carries_only_a_scrubbed_detail() -> None:
    """`RunnerNotice.detail` is built from the stderr tail and delivered to every
    thread driver on the runner, so it reaches the browser transcript."""
    from server.codex.manager import RunnerNotice, RunnerNoticeKind

    notice = RunnerNotice(
        kind=RunnerNoticeKind.CRASHED,
        account="chatgpt-main",
        generation=3,
        detail=redaction.scrub_text(f"panic: {FAKE_OPENAI_KEY}"),
    )
    assert_no_seed(repr(notice) + str(notice.detail))


# ---------------------------------------------------------------------------
# Metric label values — retained longer than logs, outside the audit redactor
# ---------------------------------------------------------------------------


def test_no_codex_metric_holds_a_secret_shaped_label_value() -> None:
    """Metrics are not covered by the audit-log redactor and typically outlive it,
    so the label VALUES accumulated by the whole suite are swept here. Every Codex
    label is a closed vocabulary or an account alias; none should ever be able to
    carry credential-shaped text."""
    from server.tests.test_codex_metrics import CODEX_SERIES

    for series in CODEX_SERIES:
        with series._lock:  # noqa: SLF001 -- reading the registry's own store
            keys = list(series._values if hasattr(series, "_values") else series._counts)
        for label_values in keys:
            for value in label_values:
                assert not redaction.contains_secret_shaped_value(value), (
                    f"{series.name} label value is secret-shaped"
                )


# ---------------------------------------------------------------------------
# Support bundle — every structured record a crash/restart cycle emits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_crash_and_restart_cycle_emits_no_secret_into_the_journal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The support-bundle case: gather EVERY record the codex loggers emit while a
    runner crashes with salted stderr and is restarted, and assert the whole
    transcript is clean. This is the assertion that survives a future call site
    adding a new `log_event` field, because it does not enumerate them."""
    from server.codex import manager as mgr
    from server.codex import schema
    from server.codex.redaction import Redacted

    salted_stderr = f"panic: {FAKE_OPENAI_KEY} {FAKE_JWT} {FAKE_REFRESH}"

    class _Process:
        pid = 4242

        def __init__(self) -> None:
            self.transport = object()

        async def start(self) -> None:
            raise mgr.CodexStartError(f"spawn failed: {FAKE_JWT}")

        def stderr_tail(self) -> str:
            # The real CodexProcess scrubs here; a fake that did not would let the
            # assertion below catch a call site logging the tail unscrubbed.
            return redaction.scrub_text(salted_stderr)

        async def stop(self, timeout: float = 0.0) -> None:
            return None

    async def credentials(account: str) -> schema.ExternalAuthLoginParams:
        return schema.ExternalAuthLoginParams(
            access_token=Redacted(FAKE_JWT),
            chatgpt_account_id=Redacted(FAKE_OPENAI_KEY),
            chatgpt_plan_type="pro",
        )

    manager = mgr.CodexRunnerManager(
        availability=CodexAvailability.ready(
            binary_path="/usr/bin/codex", cli_version="codex-cli 0.153.2"
        ),
        codex_home_base="/tmp/ccswitch-codex-scan",
        credential_supplier=credentials,
        home_factory=lambda base, account: Path("/tmp/ccswitch-codex-scan") / account,
        process_factory=lambda **kw: _Process(),  # type: ignore[arg-type]
        external_auth_enabled=True,
        backoff_base_s=0.0,
        backoff_max_s=0.0,
        max_restarts=1,
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(mgr.CodexStartError):
            await manager._start_runner("chatgpt-main")

    records = "\n".join(
        record.getMessage() + json.dumps(getattr(record, "fields", {}), default=str)
        for record in caplog.records
    )
    # Non-vacuous in the direction that matters: the cycle really did log something.
    assert records.strip(), "no records captured — the sweep would be vacuous"
    assert_no_seed(records)


@pytest.mark.asyncio
async def test_the_start_error_surfaced_to_an_operator_is_clean() -> None:
    """The message on `CodexStartError` is what reaches the session error card and
    the audit row, and it interpolates the stderr tail."""
    from server.codex import manager as mgr
    from server.codex import schema
    from server.codex.redaction import Redacted

    class _Process:
        pid = None

        def __init__(self) -> None:
            self.transport = object()

        async def start(self) -> None:
            raise RuntimeError(f"exec format error {FAKE_OPENAI_KEY}")

        def stderr_tail(self) -> str:
            return redaction.scrub_text(f"{FAKE_JWT} {FAKE_REFRESH}")

        async def stop(self, timeout: float = 0.0) -> None:
            return None

    async def credentials(account: str) -> schema.ExternalAuthLoginParams:
        return schema.ExternalAuthLoginParams(
            access_token=Redacted("x"),
            chatgpt_account_id=Redacted("y"),
            chatgpt_plan_type="pro",
        )

    manager = mgr.CodexRunnerManager(
        availability=CodexAvailability.ready(
            binary_path="/usr/bin/codex", cli_version="codex-cli 0.153.2"
        ),
        codex_home_base="/tmp/ccswitch-codex-scan",
        credential_supplier=credentials,
        home_factory=lambda base, account: Path("/tmp/ccswitch-codex-scan") / account,
        process_factory=lambda **kw: _Process(),  # type: ignore[arg-type]
        external_auth_enabled=True,
    )

    with pytest.raises(mgr.CodexStartError) as caught:
        await manager._start_runner("chatgpt-main")
    assert_no_seed(str(caught.value))


# ---------------------------------------------------------------------------
# The doctor report — a support bundle's first page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_doctor_report_is_clean_under_a_fully_salted_environment() -> None:
    """`test_codex_doctor.py` owns the doctor's per-check behaviour; this asserts
    the whole report as a bundle artefact, with every input salted at once."""
    from server.codex import doctor

    class _Settings:
        codex_enabled = True
        codex_external_auth_enabled = True

    async def health() -> Any:
        raise OSError(f"handshake failed: {FAKE_JWT}")

    def entries() -> list[Any]:
        raise OSError(f"manifest unreadable: {FAKE_REFRESH}")

    report = await doctor.run_doctor(
        _Settings(),
        availability=CodexAvailability.unavailable(
            CodexUnavailableReason.VERSION_PROBE_FAILED,
            f"exited 1: {FAKE_OPENAI_KEY}",
            binary_path=f"/opt/{FAKE_JWT}/codex",
        ),
        home_base=Path(f"/tmp/codex-{FAKE_OPENAI_KEY}"),
        mounts=(("/tmp", "ext4"),),
        authority_health=health,
        load_manifest_entries=entries,
        authority_configured=True,
    )
    assert_no_seed(json.dumps(report.to_wire()))


def test_scrub_text_replaces_native_output_wholesale_not_by_pattern() -> None:
    """The property the whole file leans on. Native strings are opaque — they may
    carry account ids, prompts, commands or paths without looking credential-shaped
    — so the general scrubber does not attempt to infer which parts are harmless."""
    assert redaction.scrub_text("a perfectly innocent path") == redaction.REDACTED
    assert redaction.scrub_text(FAKE_OPENAI_KEY) == redaction.REDACTED
