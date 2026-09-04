"""Security contract for the opt-in Codex external-auth spike harness."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "spike_codex_external_auth.py"
)
_SPEC = importlib.util.spec_from_file_location("spike_codex_external_auth", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
spike = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = spike
_SPEC.loader.exec_module(spike)


def _secrets():
    return spike.SecretValues(
        access_token="eyJhbGciOiJub25lIn0.secret-payload.live-signature",
        refresh_token="refresh-token-that-must-never-escape",
        account_id="acct-secret-workspace-id",
        invalid_access_token="eyJhbGciOiJub25lIn0.secret-payload.bad-signature",
    )


@pytest.mark.parametrize(
    "value",
    [
        "eyJhbGciOiJub25lIn0.secret-payload.live-signature",
        "refresh-token-that-must-never-escape",
        "acct-secret-workspace-id",
        "eyJhbGciOiJub25lIn0.secret-payload.bad-signature",
    ],
)
def test_sanitize_text_removes_every_exact_sensitive_value(value: str):
    secrets = _secrets()

    sanitized = spike.sanitize_text(f"prefix {value} suffix", secrets)

    assert value not in sanitized
    assert "[REDACTED]" in sanitized


def test_sanitize_protocol_value_redacts_secret_fields_and_nested_values():
    secrets = _secrets()
    frame = {
        "id": 4,
        "method": "account/login/start",
        "params": {
            "type": "chatgptAuthTokens",
            "accessToken": secrets.access_token,
            "chatgptAccountId": secrets.account_id,
            "nested": [secrets.refresh_token, {"threadId": "thread-secret-id"}],
        },
    }

    sanitized = spike.sanitize_protocol_value(
        frame,
        secrets.with_native_id("thread-secret-id"),
    )
    encoded = json.dumps(sanitized, sort_keys=True)

    assert secrets.find_leaks(encoded) == ()
    assert "thread-secret-id" not in encoded
    assert sanitized["params"]["accessToken"] == "[REDACTED]"
    assert sanitized["params"]["chatgptAccountId"] == "[REDACTED]"


def test_build_login_frame_keeps_secrets_only_in_stdin_payload():
    secrets = _secrets()
    target = spike.RunnerTarget(name="runner-one", ssh_host=None)

    command, environment = spike.build_runner_process(target, Path("/tmp/safe-home"))
    frame = spike.build_external_login_frame(7, secrets)

    process_metadata = " ".join(command) + json.dumps(environment, sort_keys=True)
    assert secrets.find_leaks(process_metadata) == ()
    assert frame["params"]["accessToken"] == secrets.access_token
    assert frame["params"]["chatgptAccountId"] == secrets.account_id


def test_build_ssh_command_preserves_multiline_python_program_as_one_argument():
    program = 'import sys\nprint("safe", sys.argv[1])'
    remote_argv = ["python3", "-c", program, "argument with spaces"]

    command = spike.build_ssh_command("runner.example", remote_argv)

    assert command[:-1] == [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "runner.example",
        "--",
    ]
    assert shlex.split(command[-1]) == remote_argv


@pytest.mark.parametrize("ssh_host", ["-oProxyCommand=touch", "runner;touch"])
def test_build_ssh_command_rejects_unsafe_destination(ssh_host: str):
    with pytest.raises(spike.SpikeConfigurationError):
        spike.build_ssh_command(ssh_host, ["true"])


def test_build_ssh_command_rejects_empty_or_nul_remote_argv():
    with pytest.raises(spike.SpikeConfigurationError):
        spike.build_ssh_command("runner.example", [])
    with pytest.raises(spike.SpikeConfigurationError):
        spike.build_ssh_command("runner.example", ["printf", "unsafe\0argument"])


def test_remote_runner_process_quotes_shell_and_app_server_argv_without_injection():
    target = spike.RunnerTarget(name="runner-one", ssh_host="runner.example")
    home = Path("/tmp/ccswitch-codex-spike." + "a" * 32)
    hostile_codex_path = "/opt/Codex CLI/codex; printf command-injected"

    command, environment = spike.build_runner_process(
        target,
        home,
        codex_bin=hostile_codex_path,
    )

    remote_argv = shlex.split(command[-1])
    assert remote_argv[:2] == ["sh", "-c"]
    assert remote_argv[3:] == [
        "ccswitch-codex-spike",
        str(home),
        hostile_codex_path,
    ]
    assert 'CODEX_HOME="$1" "$2" app-server --stdio' in remote_argv[2]
    assert environment == spike._minimal_environment()


def test_scan_home_flags_auth_json_and_exact_secret_bytes(tmp_path: Path):
    secrets = _secrets()
    (tmp_path / "runtime.db").write_bytes(
        b"prefix" + secrets.access_token.encode() + b"suffix"
    )
    (tmp_path / "auth.json").write_text("{}")

    result = spike.scan_home(tmp_path, secrets)

    assert result.auth_json_present is True
    assert result.secret_file_count == 1


@pytest.mark.asyncio
async def test_remote_scan_secrets_use_only_authenticated_ssh_stdin(monkeypatch):
    secrets = _secrets()
    captured = {}

    async def capture_command(command, *, stdin=None, timeout=30.0):
        captured["command"] = command
        captured["stdin"] = stdin
        captured["timeout"] = timeout
        return 0, b'{"auth":false,"leaks":0,"links":0}', b""

    monkeypatch.setattr(spike, "_run_command", capture_command)
    target = spike.RunnerTarget("runner.example", "runner.example")
    home = Path("/tmp/ccswitch-codex-spike." + "a" * 32)

    result = await spike.scan_remote_home(target, home, secrets)

    assert result.clean is True
    stdin_values = tuple(json.loads(captured["stdin"]))
    assert stdin_values == secrets.runner_values
    assert secrets.access_token in stdin_values
    assert secrets.account_id in stdin_values
    assert secrets.refresh_token not in stdin_values
    process_metadata = json.dumps(captured["command"]) + json.dumps(
        spike._minimal_environment(),
        sort_keys=True,
    )
    assert secrets.find_leaks(process_metadata) == ()


def test_safe_evidence_never_serializes_secret_values():
    secrets = _secrets()
    evidence = spike.SpikeEvidence(
        cli_version="codex-cli 0.153.2",
        schema_sha256="a" * 64,
        runner_count=2,
        distinct_runner_hosts=True,
        authority_on_local_runner=True,
        topology_sha256="b" * 64,
        healthy_read_only_calls=True,
        refresh_callback_observed=True,
        refresh_calls=1,
        credential_generation=1,
        runner_homes_clean=True,
        redaction_clean=True,
        cleanup_complete=True,
        release_gate="pass",
    )

    serialized = evidence.to_json(secrets)

    assert secrets.find_leaks(serialized) == ()
    assert "access_token" not in serialized
    assert "account_id" not in serialized


@pytest.mark.asyncio
async def test_native_error_response_reports_only_allowlisted_request_context(monkeypatch):
    secrets = _secrets()
    native_detail = {
        "code": -32000,
        "message": f"token rejected: {secrets.access_token}",
        "data": {
            "account": secrets.account_id,
            "refresh": secrets.refresh_token,
            "host": "dobby.internal",
            "path": "/home/operator/.codex/auth.json",
            "stderr": "native app-server diagnostic",
        },
    }
    stdout = spike.asyncio.StreamReader()
    stdout.feed_data(
        json.dumps({"id": 1, "error": native_detail}, separators=(",", ":")).encode()
        + b"\n"
    )
    stdout.feed_eof()
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")
    client._process = SimpleNamespace(stdout=stdout)

    async def discard_write(_frame):
        return None

    monkeypatch.setattr(client, "_write", discard_write)
    request = spike.asyncio.create_task(
        client.request("model/list", {}, stage="initial_read_only")
    )
    await spike.asyncio.sleep(0)
    await client._read_stdout()

    with pytest.raises(spike.ProtocolError) as raised:
        await request

    # run_spike cannot return its SecretValues on failure, so this proves the
    # native response is safe even when exact-value replacement is unavailable.
    output = spike.failure_json(raised.value)
    assert json.loads(output) == {
        "error": "app-server request returned an error",
        "method": "model/list",
        "release_gate": "fail",
        "role": "runner",
        "stage": "initial_read_only",
    }
    for forbidden in (
        *secrets.values,
        "-32000",
        "token rejected",
        "dobby.internal",
        "/home/operator/.codex/auth.json",
        "native app-server diagnostic",
    ):
        assert forbidden not in output


@pytest.mark.asyncio
async def test_request_tracker_drops_method_outside_fixed_allowlist(monkeypatch):
    unsafe_method = "method-containing-secret"
    stdout = spike.asyncio.StreamReader()
    stdout.feed_data(b'{"id":1,"error":{"message":"raw native detail"}}\n')
    stdout.feed_eof()
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")
    client._process = SimpleNamespace(stdout=stdout)

    async def discard_write(_frame):
        return None

    monkeypatch.setattr(client, "_write", discard_write)
    request = spike.asyncio.create_task(
        client.request_frame(
            {"id": 1, "method": unsafe_method, "params": {}},
            stage="initial_login",
        )
    )
    await spike.asyncio.sleep(0)
    await client._read_stdout()

    with pytest.raises(spike.ProtocolError) as raised:
        await request

    output = spike.failure_json(raised.value)
    assert json.loads(output) == {
        "error": "app-server request returned an error",
        "release_gate": "fail",
        "role": "runner",
        "stage": "initial_login",
    }
    assert unsafe_method not in output
    assert "raw native detail" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        BrokenPipeError("broken-pipe native detail"),
        ConnectionResetError("connection-reset native detail"),
        RuntimeError("unexpected write native detail"),
    ],
)
async def test_request_write_failure_clears_pending_with_sanitized_context(
    monkeypatch,
    failure,
):
    client = spike.JsonRpcProcess(["codex"], {}, role="authority")

    async def fail_write(_frame):
        raise failure

    monkeypatch.setattr(client, "_write", fail_write)

    with pytest.raises(spike.ProtocolError) as raised:
        await client.request(
            "account/read",
            {"refreshToken": False},
            stage="authority_account_read",
        )

    assert client._pending == {}
    output = spike.failure_json(raised.value)
    assert json.loads(output) == {
        "error": "app-server request transport failed",
        "method": "account/read",
        "release_gate": "fail",
        "role": "authority",
        "stage": "authority_account_read",
    }
    assert str(failure) not in output


@pytest.mark.asyncio
async def test_request_write_cancellation_clears_pending_and_preserves_identity(monkeypatch):
    cancellation = spike.asyncio.CancelledError("cancellation native detail")
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")

    async def cancel_write(_frame):
        raise cancellation

    monkeypatch.setattr(client, "_write", cancel_write)

    with pytest.raises(spike.asyncio.CancelledError) as raised:
        await client.request("model/list", {}, stage="initial_read_only")

    assert raised.value is cancellation
    assert client._pending == {}


@pytest.mark.asyncio
async def test_late_response_after_cancellation_does_not_disrupt_concurrent_request(monkeypatch):
    stdout = spike.asyncio.StreamReader()
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")
    client._process = SimpleNamespace(stdout=stdout)

    async def discard_write(_frame):
        return None

    monkeypatch.setattr(client, "_write", discard_write)
    canceled = spike.asyncio.create_task(
        client.request("model/list", {}, stage="initial_read_only")
    )
    concurrent = spike.asyncio.create_task(
        client.request("account/read", {}, stage="initial_read_only")
    )
    await spike.asyncio.sleep(0)
    canceled.cancel()
    with pytest.raises(spike.asyncio.CancelledError):
        await canceled

    stdout.feed_data(
        b'{"id":1,"error":{"message":"late native detail"}}\n'
        b'{"id":2,"result":{"safe":true}}\n'
    )
    stdout.feed_eof()
    await client._read_stdout()

    assert await concurrent == {"safe": True}
    assert client._pending == {}
    client.assert_background_clean()


@pytest.mark.asyncio
async def test_boolean_response_id_does_not_alias_live_integer_request(monkeypatch):
    stdout = spike.asyncio.StreamReader()
    stdout.feed_data(b'{"id":true,"result":{"must_not":"alias request 1"}}\n')
    stdout.feed_eof()
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")
    client._process = SimpleNamespace(stdout=stdout)

    async def discard_write(_frame):
        return None

    monkeypatch.setattr(client, "_write", discard_write)
    request = spike.asyncio.create_task(
        client.request("model/list", {}, stage="initial_read_only")
    )
    await spike.asyncio.sleep(0)
    await client._read_stdout()

    with pytest.raises(spike.ProtocolError, match="reader stopped"):
        await request
    with pytest.raises(spike.ProtocolError) as background:
        client.assert_background_clean()
    assert json.loads(spike.failure_json(background.value)) == {
        "error": "app-server replied with an invalid request id",
        "release_gate": "fail",
        "role": "runner",
        "stage": "protocol_transport",
    }


@pytest.mark.asyncio
async def test_boolean_response_id_does_not_alias_abandoned_integer_request(monkeypatch):
    stdout = spike.asyncio.StreamReader()
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")
    client._process = SimpleNamespace(stdout=stdout)

    async def discard_write(_frame):
        return None

    monkeypatch.setattr(client, "_write", discard_write)
    request = spike.asyncio.create_task(
        client.request("model/list", {}, stage="initial_read_only")
    )
    await spike.asyncio.sleep(0)
    request.cancel()
    with pytest.raises(spike.asyncio.CancelledError):
        await request

    stdout.feed_data(b'{"id":true,"error":{"message":"must not match tombstone 1"}}\n')
    stdout.feed_eof()
    await client._read_stdout()

    assert client._abandoned_request_ids == {1}
    with pytest.raises(spike.ProtocolError, match="invalid request id"):
        client.assert_background_clean()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_id", "message"),
    [(999, "unknown request id"), ("not-an-integer", "invalid request id")],
)
async def test_unknown_or_malformed_response_id_fails_safely(response_id, message):
    stdout = spike.asyncio.StreamReader()
    stdout.feed_data(
        json.dumps({"id": response_id, "result": {}}, separators=(",", ":")).encode()
        + b"\n"
    )
    stdout.feed_eof()
    client = spike.JsonRpcProcess(["codex"], {}, role="authority")
    client._process = SimpleNamespace(stdout=stdout)

    await client._read_stdout()

    with pytest.raises(spike.ProtocolError, match=message):
        client.assert_background_clean()


@pytest.mark.asyncio
async def test_callback_spike_failure_has_fixed_context_and_no_arbitrary_detail(monkeypatch):
    handler_detail = "handler detail containing fake-refresh-token"
    replies = []

    async def fail_handler(_frame):
        raise spike.SpikeError(handler_detail)

    async def capture_write(frame):
        replies.append(frame)

    client = spike.JsonRpcProcess(["codex"], {}, role="runner", handler=fail_handler)
    monkeypatch.setattr(client, "_write", capture_write)
    frame = {
        "id": 11,
        "method": "account/chatgptAuthTokens/refresh",
        "params": {"reason": "unauthorized"},
    }

    await client._answer_server_request(frame)

    assert replies == [
        {
            "id": 11,
            "error": {"code": -32603, "message": "ccswitch spike rejected request"},
        }
    ]
    with pytest.raises(spike.ProtocolError) as raised:
        client.assert_background_clean()
    output = spike.failure_json(raised.value)
    assert json.loads(output) == {
        "error": "app-server callback handler failed",
        "method": "account/chatgptAuthTokens/refresh",
        "release_gate": "fail",
        "role": "runner",
        "stage": "refresh_callback",
    }
    assert handler_detail not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write_failure",
    [
        BrokenPipeError("callback broken-pipe detail"),
        ConnectionResetError("callback connection-reset detail"),
    ],
)
async def test_callback_error_reply_transport_failure_is_sanitized(
    monkeypatch,
    write_failure,
):
    async def fail_handler(_frame):
        raise spike.SpikeError("handler detail")

    async def fail_write(_frame):
        raise write_failure

    client = spike.JsonRpcProcess(["codex"], {}, role="runner", handler=fail_handler)
    monkeypatch.setattr(client, "_write", fail_write)

    await client._answer_server_request(
        {"id": 12, "method": "account/chatgptAuthTokens/refresh", "params": {}}
    )

    assert len(client._background_errors) == 2
    output = spike.failure_json(client._background_errors[1])
    assert json.loads(output) == {
        "error": "app-server callback error response transport failed",
        "method": "account/chatgptAuthTokens/refresh",
        "release_gate": "fail",
        "role": "runner",
        "stage": "refresh_callback",
    }
    assert str(write_failure) not in output


@pytest.mark.asyncio
async def test_callback_error_reply_cancellation_preserves_identity(monkeypatch):
    cancellation = spike.asyncio.CancelledError("callback cancellation detail")
    handler_failure = spike.ProtocolError("sanitized callback handler failure")

    async def fail_handler(_frame):
        raise handler_failure

    async def cancel_write(_frame):
        raise cancellation

    client = spike.JsonRpcProcess(["codex"], {}, role="runner", handler=fail_handler)
    monkeypatch.setattr(client, "_write", cancel_write)

    with pytest.raises(spike.asyncio.CancelledError) as raised:
        await client._answer_server_request(
            {"id": 13, "method": "account/chatgptAuthTokens/refresh", "params": {}}
        )

    assert raised.value is cancellation
    assert client._background_errors[-1] is cancellation
    assert await client.callback_failures_after_quiescence(
        0,
        quiet_interval=0.001,
        max_wait=0.1,
    ) == (handler_failure, cancellation)


@pytest.mark.asyncio
async def test_spawned_callback_cancellation_is_consumed_and_tracked():
    cancellation = spike.asyncio.CancelledError("spawned callback cancellation")

    async def cancel_handler(_frame):
        raise cancellation

    client = spike.JsonRpcProcess(["codex"], {}, role="runner", handler=cancel_handler)
    client._start_server_request(
        {"id": 14, "method": "account/chatgptAuthTokens/refresh", "params": {}}
    )
    await spike.asyncio.sleep(0)
    await spike.asyncio.sleep(0)

    assert client._server_request_tasks == set()
    assert client._background_errors == [cancellation]


def _quiescence_client(monkeypatch, *, release=None):
    async def handler(_frame):
        if release is not None:
            await release.wait()
        return {"safe": True}

    async def discard_write(_frame):
        return None

    client = spike.JsonRpcProcess(["codex"], {}, role="runner", handler=handler)
    monkeypatch.setattr(client, "_write", discard_write)
    return client


def _safe_callback_frame(request_id):
    return {
        "id": request_id,
        "method": "account/chatgptAuthTokens/refresh",
        "params": {"reason": "unauthorized"},
    }


@pytest.mark.asyncio
async def test_callback_registered_before_quiescence_fence_is_drained(monkeypatch):
    client = _quiescence_client(monkeypatch)
    checkpoint = client.callback_checkpoint()
    client._start_server_request(_safe_callback_frame(31))

    failures = await client.callback_failures_after_quiescence(
        checkpoint,
        quiet_interval=0.001,
        max_wait=0.1,
    )

    assert failures == ()
    assert client._server_request_tasks == set()


@pytest.mark.asyncio
async def test_callback_registered_within_quiet_interval_resets_fence(monkeypatch):
    client = _quiescence_client(monkeypatch)
    checkpoint = client.callback_checkpoint()
    fence = spike.asyncio.create_task(
        client.callback_failures_after_quiescence(
            checkpoint,
            quiet_interval=0.02,
            max_wait=0.2,
        )
    )
    await spike.asyncio.sleep(0)
    assert fence.done() is False

    client._start_server_request(_safe_callback_frame(32))

    assert await fence == ()
    assert client._server_request_tasks == set()


@pytest.mark.asyncio
async def test_callback_registered_after_quiet_boundary_is_not_misattributed(monkeypatch):
    client = _quiescence_client(monkeypatch)
    checkpoint = client.callback_checkpoint()

    assert await client.callback_failures_after_quiescence(
        checkpoint,
        quiet_interval=0.001,
        max_wait=0.1,
    ) == ()

    client._start_server_request(_safe_callback_frame(33))
    assert await client.callback_failures_after_quiescence(
        checkpoint,
        quiet_interval=0.001,
        max_wait=0.1,
    ) == ()


@pytest.mark.asyncio
async def test_callback_fence_deadline_records_static_anomalous_failure(monkeypatch):
    release = spike.asyncio.Event()
    client = _quiescence_client(monkeypatch, release=release)
    checkpoint = client.callback_checkpoint()
    client._start_server_request(_safe_callback_frame(34))

    failures = await client.callback_failures_after_quiescence(
        checkpoint,
        quiet_interval=0.01,
        max_wait=0.001,
    )

    assert json.loads(spike.failure_json(failures[0])) == {
        "error": "runner callback activity did not quiesce",
        "method": "account/chatgptAuthTokens/refresh",
        "release_gate": "fail",
        "role": "runner",
        "stage": "refresh_callback",
    }
    release.set()
    await spike.asyncio.gather(*tuple(client._server_request_tasks), return_exceptions=True)


class _RefreshTriggerRunner:
    def __init__(
        self,
        *,
        outcome=None,
        callback_failure=None,
        calls=None,
        name="runner",
        on_request=None,
    ):
        self.outcome = outcome
        self.callback_failure = callback_failure
        self.calls = calls if calls is not None else []
        self.name = name
        self.on_request = on_request

    def callback_checkpoint(self):
        self.calls.append((self.name, "checkpoint"))
        return 7

    async def request(self, method, params, *, stage):
        self.calls.append((self.name, "request"))
        assert method == "account/rateLimits/read"
        assert params == {}
        assert stage == "refresh_trigger"
        if self.on_request is not None:
            self.on_request()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def callback_failures_after_quiescence(self, checkpoint):
        self.calls.append((self.name, "callback"))
        assert checkpoint == 7
        if self.callback_failure is None:
            return ()
        if isinstance(self.callback_failure, tuple):
            return self.callback_failure
        return (self.callback_failure,)


def _refresh_coalescer(*, callback_count=1, refresh_calls=1, generation=1):
    return SimpleNamespace(
        callback_count=callback_count,
        refresh_calls=refresh_calls,
        generation=generation,
    )


@pytest.mark.asyncio
async def test_refresh_trigger_prefers_sanitized_callback_root_failure():
    downstream = spike.ProtocolError(
        "app-server request returned an error",
        method="account/rateLimits/read",
        stage="refresh_trigger",
        role="runner",
    )
    callback_root = spike.ProtocolError(
        "app-server callback handler failed",
        method="account/chatgptAuthTokens/refresh",
        stage="refresh_callback",
        role="runner",
    )
    runner = _RefreshTriggerRunner(outcome=downstream, callback_failure=callback_root)

    with pytest.raises(spike.ProtocolError) as raised:
        await spike._run_refresh_triggers([runner], coalescer=_refresh_coalescer())

    assert raised.value is callback_root
    assert json.loads(spike.failure_json(raised.value)) == {
        "error": "app-server callback handler failed",
        "method": "account/chatgptAuthTokens/refresh",
        "release_gate": "fail",
        "role": "runner",
        "stage": "refresh_callback",
    }


@pytest.mark.asyncio
async def test_refresh_trigger_with_successful_callback_keeps_success():
    coalescer = _refresh_coalescer(callback_count=2, refresh_calls=1, generation=1)

    def advance_generation():
        coalescer.callback_count = 3
        coalescer.refresh_calls = 2
        coalescer.generation = 2

    runner = _RefreshTriggerRunner(
        outcome={"rateLimits": {}},
        callback_failure=None,
        on_request=advance_generation,
    )

    await spike._run_refresh_triggers(
        [runner],
        coalescer=coalescer,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after", "message"),
    [
        ((2, 1, 1), "refresh trigger failed before any runner callback"),
        ((3, 1, 1), "refresh trigger failed after only some runner callbacks"),
        ((4, 2, 2), "refresh trigger failed after callback refresh advanced"),
        ((5, 2, 2), "refresh trigger observed incomplete or anomalous callback state"),
        ((4, 3, 2), "refresh trigger observed incomplete or anomalous callback state"),
    ],
)
async def test_refresh_trigger_classifies_only_post_checkpoint_coalescer_transition(after, message):
    downstream = spike.ProtocolError(
        "app-server request returned an error",
        method="account/rateLimits/read",
        stage="refresh_trigger",
        role="runner",
    )
    coalescer = _refresh_coalescer(callback_count=2, refresh_calls=1, generation=1)

    def apply_transition():
        coalescer.callback_count, coalescer.refresh_calls, coalescer.generation = after

    runners = [
        _RefreshTriggerRunner(
            outcome=downstream,
            callback_failure=None,
            name="first",
            on_request=apply_transition,
        ),
        _RefreshTriggerRunner(outcome={"safe": True}, callback_failure=None, name="second"),
    ]

    with pytest.raises(spike.ProtocolError) as raised:
        await spike._run_refresh_triggers(
            runners,
            coalescer=coalescer,
        )

    assert json.loads(spike.failure_json(raised.value)) == {
        "error": message,
        "method": "account/rateLimits/read",
        "release_gate": "fail",
        "role": "runner",
        "stage": "refresh_trigger",
    }


@pytest.mark.asyncio
async def test_concurrent_refresh_trigger_failures_are_drained_before_stable_precedence():
    calls = []
    first_root = spike.ProtocolError(
        "first sanitized callback root",
        method="account/chatgptAuthTokens/refresh",
        stage="refresh_callback",
        role="runner",
    )
    second_root = spike.ProtocolError(
        "second sanitized callback root",
        method="account/chatgptAuthTokens/refresh",
        stage="refresh_callback",
        role="runner",
    )
    runners = [
        _RefreshTriggerRunner(
            outcome=spike.ProtocolError("first downstream"),
            callback_failure=first_root,
            calls=calls,
            name="first",
        ),
        _RefreshTriggerRunner(
            outcome=spike.ProtocolError("second downstream"),
            callback_failure=second_root,
            calls=calls,
            name="second",
        ),
    ]

    with pytest.raises(spike.ProtocolError) as raised:
        await spike._run_refresh_triggers(
            runners,
            coalescer=_refresh_coalescer(callback_count=2),
        )

    assert raised.value is first_root
    assert ("first", "request") in calls and ("second", "request") in calls
    assert ("first", "callback") in calls and ("second", "callback") in calls


@pytest.mark.asyncio
async def test_refresh_trigger_preserves_callback_cancellation_identity():
    cancellation = spike.asyncio.CancelledError("callback cancellation")
    runner = _RefreshTriggerRunner(
        outcome=spike.ProtocolError("downstream"),
        callback_failure=cancellation,
    )

    with pytest.raises(spike.asyncio.CancelledError) as raised:
        await spike._run_refresh_triggers([runner], coalescer=_refresh_coalescer())

    assert raised.value is cancellation


@pytest.mark.asyncio
async def test_later_reply_cancellation_dominates_handler_error_in_same_callback_slice():
    handler_failure = spike.ProtocolError("sanitized callback handler failure")
    cancellation = spike.asyncio.CancelledError("error reply cancellation")
    runner = _RefreshTriggerRunner(
        outcome=spike.ProtocolError("downstream"),
        callback_failure=(handler_failure, cancellation),
    )

    with pytest.raises(spike.asyncio.CancelledError) as raised:
        await spike._run_refresh_triggers([runner], coalescer=_refresh_coalescer())

    assert raised.value is cancellation


@pytest.mark.asyncio
async def test_second_runner_callback_cancellation_dominates_first_runner_protocol_error():
    first_root = spike.ProtocolError("first callback root")
    cancellation = spike.asyncio.CancelledError("second runner callback cancellation")
    runners = [
        _RefreshTriggerRunner(
            outcome=spike.ProtocolError("first downstream"),
            callback_failure=first_root,
            name="first",
        ),
        _RefreshTriggerRunner(
            outcome=spike.ProtocolError("second downstream"),
            callback_failure=cancellation,
            name="second",
        ),
    ]

    with pytest.raises(spike.asyncio.CancelledError) as raised:
        await spike._run_refresh_triggers(
            runners,
            coalescer=_refresh_coalescer(callback_count=2),
        )

    assert raised.value is cancellation


@pytest.mark.asyncio
async def test_downstream_cancellation_dominates_callback_protocol_error():
    callback_root = spike.ProtocolError("sanitized callback root")
    cancellation = spike.asyncio.CancelledError("downstream cancellation")
    runner = _RefreshTriggerRunner(
        outcome=cancellation,
        callback_failure=callback_root,
    )

    with pytest.raises(spike.asyncio.CancelledError) as raised:
        await spike._run_refresh_triggers([runner], coalescer=_refresh_coalescer())

    assert raised.value is cancellation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        BrokenPipeError("broken-pipe notification detail"),
        ConnectionResetError("connection-reset notification detail"),
        RuntimeError("unexpected notification detail"),
    ],
)
async def test_notification_write_failure_has_sanitized_context(monkeypatch, failure):
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")

    async def fail_write(_frame):
        raise failure

    monkeypatch.setattr(client, "_write", fail_write)

    with pytest.raises(spike.ProtocolError) as raised:
        await client.notify("initialized", {}, stage="initialize")

    output = spike.failure_json(raised.value)
    assert json.loads(output) == {
        "error": "app-server notification transport failed",
        "release_gate": "fail",
        "role": "runner",
        "stage": "initialize",
    }
    assert str(failure) not in output


@pytest.mark.asyncio
async def test_notification_write_cancellation_preserves_identity(monkeypatch):
    cancellation = spike.asyncio.CancelledError("notification cancellation detail")
    client = spike.JsonRpcProcess(["codex"], {}, role="runner")

    async def cancel_write(_frame):
        raise cancellation

    monkeypatch.setattr(client, "_write", cancel_write)

    with pytest.raises(spike.asyncio.CancelledError) as raised:
        await client.notify("initialized", {}, stage="initialize")

    assert raised.value is cancellation


@pytest.mark.parametrize(
    ("flag", "account"),
    [(None, None), ("0", "openai-main"), ("1", None), ("true", "openai-main")],
)
def test_opt_in_requires_exact_flag_and_nonempty_account(flag: str | None, account: str | None):
    with pytest.raises(spike.SpikeConfigurationError):
        spike.require_opt_in(flag=flag, account=account)


def test_opt_in_accepts_exact_flag_and_account_alias():
    assert spike.require_opt_in(flag="1", account="openai-main") == "openai-main"


def test_secret_values_reject_empty_or_too_short_values():
    with pytest.raises(ValueError):
        spike.SecretValues(
            access_token="short",
            refresh_token="refresh-token-that-must-never-escape",
            account_id="acct-secret-workspace-id",
            invalid_access_token="another-secret-value",
        )


def test_runner_scan_values_never_include_current_or_historical_refresh_tokens():
    initial = _secrets()
    newer = spike.SecretValues(
        access_token="new-access-token-that-is-long-enough",
        refresh_token="new-refresh-token-that-must-stay-authority-only",
        account_id=initial.account_id,
        invalid_access_token="new-invalid-token-that-is-long-enough",
    )

    merged = initial.merge(newer)

    assert initial.refresh_token not in merged.runner_values
    assert newer.refresh_token not in merged.runner_values
    assert initial.access_token in merged.runner_values
    assert newer.access_token in merged.runner_values


def test_invalid_token_mutates_a_significant_signature_character_only():
    valid = "header.payload.Abcdefghijklmnopqrstuvwxyz"

    invalid = spike._mutate_access_token(valid)

    valid_parts = valid.split(".")
    invalid_parts = invalid.split(".")
    assert invalid_parts[:2] == valid_parts[:2]
    assert invalid_parts[2][0] != valid_parts[2][0]
    assert invalid_parts[2][1:] == valid_parts[2][1:]


def test_topology_requires_two_distinct_runner_machine_identities():
    local = "1" * 64

    with pytest.raises(spike.SpikeConfigurationError):
        spike.validate_topology(
            authority_identity=local,
            runner_identities=(local, local),
        )


def test_topology_requires_authority_machine_to_be_one_runner():
    with pytest.raises(spike.SpikeConfigurationError):
        spike.validate_topology(
            authority_identity="1" * 64,
            runner_identities=("2" * 64, "3" * 64),
        )


def test_topology_evidence_is_allow_listed_and_does_not_expose_machine_hashes():
    authority = "1" * 64
    remote = "2" * 64

    topology = spike.validate_topology(
        authority_identity=authority,
        runner_identities=(authority, remote),
    )

    serialized = json.dumps(topology.to_wire(), sort_keys=True)
    assert topology.distinct_runner_hosts is True
    assert topology.authority_on_local_runner is True
    assert authority not in serialized
    assert remote not in serialized
    assert len(topology.topology_sha256) == 64


def test_parse_runners_requires_exactly_one_local_runner():
    with pytest.raises(spike.SpikeConfigurationError):
        spike.parse_runners("runner-one,runner-two")


def _valid_account_response():
    return {
        "account": {"type": "chatgpt", "email": None, "planType": "plus"},
        "requiresOpenaiAuth": True,
    }


def _valid_model_response():
    return {
        "data": [
            {
                "id": "codex-model",
                "model": "codex-model",
                "displayName": "Codex Model",
                "description": "A model for coding tasks",
                "isDefault": True,
                "hidden": False,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [],
            }
        ],
        "nextCursor": None,
    }


def _valid_rate_limit_response(account_id: str = "acct-secret-workspace-id"):
    return {
        "accountId": account_id,
        "rateLimits": {
            "primary": {
                "usedPercent": 25,
                "resetsAt": None,
                "windowDurationMins": 300,
            }
        },
        "rateLimitsByLimitId": None,
    }


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"account": None, "requiresOpenaiAuth": True},
        {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True},
        {"account": {"type": "chatgpt", "planType": "plus"}},
    ],
)
def test_account_read_validation_rejects_unauthenticated_or_malformed_content(response):
    with pytest.raises(spike.ProtocolError):
        spike.validate_account_response(response)


def test_account_read_validation_accepts_authenticated_chatgpt_content():
    spike.validate_account_response(_valid_account_response())


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"data": []},
        {"data": [{"id": "", "model": "m", "displayName": "M", "isDefault": True}]},
        {"data": [{"id": "m", "model": "m", "displayName": "M", "isDefault": "yes"}]},
    ],
)
def test_model_list_validation_rejects_empty_or_malformed_content(response):
    with pytest.raises(spike.ProtocolError):
        spike.validate_model_response(response)


def test_model_list_validation_accepts_nonempty_catalog():
    spike.validate_model_response(_valid_model_response())


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"rateLimits": {}},
        {"rateLimits": {"primary": {"usedPercent": "25"}}},
        {"rateLimits": {"primary": {"usedPercent": 101}}},
        _valid_rate_limit_response("different-account-id"),
    ],
)
def test_rate_limit_validation_rejects_unbound_or_malformed_content(response):
    with pytest.raises(spike.ProtocolError):
        spike.validate_rate_limit_response(response, expected_account_id="acct-secret-workspace-id")


def test_rate_limit_validation_accepts_bound_quota_window():
    spike.validate_rate_limit_response(
        _valid_rate_limit_response(),
        expected_account_id="acct-secret-workspace-id",
    )


@pytest.mark.asyncio
async def test_read_only_verification_rejects_empty_success_responses():
    class EmptyRunner:
        async def request(self, _method, _params, *, stage):
            assert stage == "initial_read_only"
            return {}

    with pytest.raises(spike.ProtocolError):
        await spike._verify_read_only(
            EmptyRunner(),
            expected_account_id="acct-secret-workspace-id",
            stage="initial_read_only",
        )


@pytest.mark.asyncio
async def test_read_only_verification_validates_all_three_authenticated_responses():
    class ValidRunner:
        async def request(self, method, _params, *, stage):
            assert stage == "initial_read_only"
            return {
                "account/read": _valid_account_response(),
                "model/list": _valid_model_response(),
                "account/rateLimits/read": _valid_rate_limit_response(),
            }[method]

    await spike._verify_read_only(
        ValidRunner(),
        expected_account_id="acct-secret-workspace-id",
        stage="initial_read_only",
    )


class _FakeAuthority:
    def __init__(self):
        self.calls = 0

    async def request(self, method, params, *, stage):
        assert method == "account/read"
        assert params == {"refreshToken": True}
        assert stage == "authority_refresh"
        self.calls += 1
        return _valid_account_response()


def _refresh_frame(account_id: str = "acct-secret-workspace-id"):
    return {
        "id": 10,
        "method": "account/chatgptAuthTokens/refresh",
        "params": {"reason": "unauthorized", "previousAccountId": account_id},
    }


@pytest.mark.asyncio
async def test_refresh_generation_does_not_advance_when_access_token_is_unchanged(
    monkeypatch,
    tmp_path,
):
    initial = _secrets()
    authority = _FakeAuthority()
    coalescer = spike.RefreshCoalescer(authority, tmp_path, initial)
    monkeypatch.setattr(spike, "load_managed_secrets", lambda _home: initial)

    with pytest.raises(spike.ProtocolError):
        await coalescer.handle(_refresh_frame())

    assert coalescer.generation == 0
    assert coalescer.current.access_token == initial.access_token


@pytest.mark.asyncio
async def test_concurrent_refresh_callbacks_share_one_advanced_access_token(monkeypatch, tmp_path):
    initial = _secrets()
    newer = spike.SecretValues(
        access_token="header.payload.new-advanced-signature",
        refresh_token="new-refresh-token-that-stays-authority-only",
        account_id=initial.account_id,
        invalid_access_token="header.payload.new-invalid-signature",
    )
    authority = _FakeAuthority()
    coalescer = spike.RefreshCoalescer(authority, tmp_path, initial)
    monkeypatch.setattr(spike, "load_managed_secrets", lambda _home: newer)

    first, second = await spike.asyncio.gather(
        coalescer.handle(_refresh_frame()),
        coalescer.handle(_refresh_frame()),
    )

    assert authority.calls == 1
    assert coalescer.refresh_calls == 1
    assert coalescer.generation == 1
    assert first["accessToken"] == newer.access_token
    assert second["accessToken"] == newer.access_token
    assert first["accessToken"] != initial.access_token


@pytest.mark.asyncio
async def test_run_command_timeout_kills_and_reaps_child(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.terminate_called = False
            self.kill_called = False
            self.reaped = False
            self.killed = spike.asyncio.Event()

        async def communicate(self, _stdin):
            await spike.asyncio.Event().wait()

        def terminate(self):
            self.terminate_called = True

        def kill(self):
            self.kill_called = True
            self.killed.set()

        async def wait(self):
            if not self.kill_called:
                await self.killed.wait()
            self.reaped = True
            self.returncode = -9
            return self.returncode

    process = FakeProcess()

    async def fake_create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(spike.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(spike, "PROCESS_TERMINATE_GRACE_S", 0.001)

    with pytest.raises(TimeoutError):
        await spike._run_command(["never-completes"], timeout=0.001)

    assert process.terminate_called is True
    assert process.kill_called is True
    assert process.reaped is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("communicate failed"), spike.asyncio.CancelledError()])
async def test_run_command_reaps_and_preserves_non_timeout_failure(monkeypatch, failure):
    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.terminate_called = False
            self.reaped = False

        async def communicate(self, _stdin):
            raise failure

        def terminate(self):
            self.terminate_called = True
            self.returncode = -15

        async def wait(self):
            self.reaped = True
            return self.returncode

    process = FakeProcess()

    async def fake_create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(spike.asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(type(failure)) as raised:
        await spike._run_command(["fails-during-communicate"])

    assert raised.value is failure
    assert process.terminate_called is True
    assert process.reaped is True


@pytest.mark.asyncio
async def test_runner_home_is_registered_before_remote_creation_can_be_cancelled(monkeypatch):
    runtimes = []
    cleanup_commands = []
    target = spike.RunnerTarget("remote-runner", "remote-runner")

    async def cancelled_provision(_target, _home):
        raise spike.asyncio.CancelledError()

    monkeypatch.setattr(spike, "provision_runner_home", cancelled_provision)
    monkeypatch.setattr(spike.secrets, "token_hex", lambda _size: "a" * 32)

    with pytest.raises(spike.asyncio.CancelledError):
        await spike.create_runner_runtime(
            target,
            codex_bin="codex",
            handler=None,
            runtimes=runtimes,
        )

    assert len(runtimes) == 1
    assert str(runtimes[0].home) == "/tmp/ccswitch-codex-spike." + "a" * 32
    assert runtimes[0].is_remote is True

    async def successful_cleanup(command, **_kwargs):
        cleanup_commands.append(command)
        return 0, b"", b""

    monkeypatch.setattr(spike, "_run_command", successful_cleanup)
    assert await spike.cleanup_runner(runtimes[0]) is True
    assert str(runtimes[0].home) in shlex.split(cleanup_commands[0][-1])


@pytest.mark.asyncio
async def test_cleanup_treats_uncreated_local_home_as_complete():
    home = Path("/tmp/ccswitch-codex-spike." + "c" * 32)
    assert not home.exists()
    runtime = spike.RunnerRuntime(
        spike.RunnerTarget("local-runner", None),
        home,
        SimpleNamespace(),
        False,
    )

    assert await spike.cleanup_runner(runtime) is True
    assert runtime.cleaned is True


@pytest.mark.asyncio
async def test_finalize_cleans_every_home_even_when_process_close_fails(monkeypatch, tmp_path):
    cleaned = []

    class FailingProcess:
        async def close(self):
            raise RuntimeError("close failed")

    class CleanProcess:
        async def close(self):
            return None

    runtimes = [
        spike.RunnerRuntime(
            spike.RunnerTarget("local-runner", None),
            tmp_path / "one",
            FailingProcess(),
            False,
        ),
        spike.RunnerRuntime(
            spike.RunnerTarget("remote-runner", "remote-runner"),
            Path("/tmp/ccswitch-codex-spike.safe"),
            CleanProcess(),
            True,
        ),
    ]

    async def fake_cleanup(runtime):
        cleaned.append(runtime.target.name)
        return True

    monkeypatch.setattr(spike, "cleanup_runner", fake_cleanup)

    with pytest.raises(spike.SpikeError):
        await spike.finalize_runtimes(
            runtimes,
            SimpleNamespace(close=CleanProcess().close),
            processes_closed=False,
        )

    assert cleaned == ["local-runner", "remote-runner"]


@pytest.mark.asyncio
async def test_body_failure_remains_primary_when_finalization_also_fails(monkeypatch):
    body_failure = spike.ProtocolError("primary protocol failure")
    cleanup_failure = RuntimeError("cleanup detail must not escape")

    async def failing_finalize(*_args, **_kwargs):
        raise cleanup_failure

    monkeypatch.setattr(spike, "finalize_runtimes", failing_finalize)

    with pytest.raises(spike.ProtocolError) as raised:
        try:
            raise body_failure
        except BaseException as primary:
            await spike.finalize_preserving_primary(
                [],
                SimpleNamespace(),
                processes_closed=False,
                primary_failure=primary,
            )
            raise

    assert raised.value is body_failure
    notes = getattr(raised.value, "__notes__", [])
    assert notes == ["ccswitch spike cleanup also failed; details suppressed"]
    assert "cleanup detail" not in " ".join(notes)


@pytest.mark.asyncio
async def test_cleanup_only_failure_remains_primary(monkeypatch):
    cleanup_failure = RuntimeError("cleanup-only failure")

    async def failing_finalize(*_args, **_kwargs):
        raise cleanup_failure

    monkeypatch.setattr(spike, "finalize_runtimes", failing_finalize)

    with pytest.raises(RuntimeError) as raised:
        await spike.finalize_preserving_primary(
            [],
            SimpleNamespace(),
            processes_closed=True,
            primary_failure=None,
        )

    assert raised.value is cleanup_failure


@pytest.mark.asyncio
async def test_failure_json_reports_combined_cleanup_failure_without_secret(
    monkeypatch, capsys
):
    cleanup_secret = "cleanup-secret-that-must-never-escape"
    body_failure = spike.ProtocolError("primary protocol failure")

    async def failing_finalize(*_args, **_kwargs):
        raise RuntimeError(f"cleanup failed with {cleanup_secret}")

    async def failing_run_spike(**_kwargs):
        try:
            raise body_failure
        except BaseException as primary:
            await spike.finalize_preserving_primary(
                [],
                SimpleNamespace(),
                processes_closed=False,
                primary_failure=primary,
            )
            raise

    monkeypatch.setenv(spike.OPT_IN_FLAG, "1")
    monkeypatch.setenv(spike.ACCOUNT_ALIAS_ENV, "test-account")
    monkeypatch.setenv(spike.RUNNERS_ENV, "local,rocky")
    monkeypatch.setattr(spike, "finalize_runtimes", failing_finalize)
    monkeypatch.setattr(spike, "run_spike", failing_run_spike)

    assert await spike.async_main() == 1

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "cleanup_also_failed": True,
        "error": "primary protocol failure",
        "release_gate": "fail",
    }
    assert cleanup_secret not in output


@pytest.mark.asyncio
async def test_failure_json_omits_cleanup_field_and_arbitrary_notes(monkeypatch, capsys):
    note_secret = "arbitrary-note-secret-that-must-never-escape"
    body_failure = spike.ProtocolError("ordinary protocol failure")
    body_failure.add_note(note_secret)

    async def failing_run_spike(**_kwargs):
        raise body_failure

    monkeypatch.setenv(spike.OPT_IN_FLAG, "1")
    monkeypatch.setenv(spike.ACCOUNT_ALIAS_ENV, "test-account")
    monkeypatch.setenv(spike.RUNNERS_ENV, "local,rocky")
    monkeypatch.setattr(spike, "run_spike", failing_run_spike)

    assert await spike.async_main() == 1

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "error": "ordinary protocol failure",
        "release_gate": "fail",
    }
    assert note_secret not in output
