#!/usr/bin/env python3
"""Opt-in Codex external-auth compatibility spike.

The harness starts one managed-auth authority app-server locally and two runner
app-servers with separate temporary ``CODEX_HOME`` directories. Access tokens
and the native ChatGPT account id enter runner app-servers through JSON-RPC
stdin. For exact remote-home leak detection, those runner-visible values also
enter one short-lived authenticated SSH scanner through stdin; the refresh
token never does. Raw protocol frames and child stderr are never forwarded to
this script's stdout/stderr.

By default no thread or model turn is created. A second exact opt-in can use one
minimal, tool-free model turn per runner as the refresh trigger when the
read-only rate-limit endpoint does not exercise the host refresh callback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence, get_args

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.audit import redact_text  # noqa: E402 -- repo root is added for direct script execution
from server.deploy.steps import SSH_HOST_RE  # noqa: E402


OPT_IN_FLAG = "CCSWITCH_TEST_CODEX_EXTERNAL_AUTH"
MODEL_TURN_OPT_IN_FLAG = "CCSWITCH_TEST_CODEX_MODEL_TURN"
ACCOUNT_ALIAS_ENV = "CCSWITCH_TEST_CODEX_ACCOUNT"
RUNNERS_ENV = "CCSWITCH_TEST_CODEX_RUNNERS"
AUTHORITY_HOME_ENV = "CCSWITCH_TEST_CODEX_AUTHORITY_HOME"
CODEX_BIN_ENV = "CCSWITCH_CODEX_BIN"
DEFAULT_RUNNERS = "local,rocky"
REDACTED = "[REDACTED]"
REQUEST_TIMEOUT_S = 45.0
PROCESS_TERMINATE_GRACE_S = 2.0
CALLBACK_QUIET_INTERVAL_S = 0.1
CALLBACK_FENCE_DEADLINE_S = 2.0
MODEL_TURN_TIMEOUT_S = 90.0
TOOL_CATALOG_PREFLIGHT_TIMEOUT_S = 30.0
MODEL_TURN_MODEL = "gpt-5.6-luna"
MODEL_TURN_EXPECTED_TEXT = "OK"
MODEL_TURN_DEVELOPER_INSTRUCTIONS = (
    "Do not call, invoke, or use any tool or perform any external activity. "
    f"Respond with exactly {MODEL_TURN_EXPECTED_TEXT} and no other text."
)
MODEL_TURN_PROMPT = (
    f"Reply with exactly {MODEL_TURN_EXPECTED_TEXT} and no other text. Do not use tools."
)
_CLEANUP_FAILED_NOTE = "ccswitch spike cleanup also failed; details suppressed"

ProtocolMethod = Literal[
    "initialize",
    "account/login/start",
    "account/read",
    "account/rateLimits/read",
    "model/list",
    "account/chatgptAuthTokens/refresh",
    "thread/start",
    "turn/start",
    "turn/completed",
    "item/started",
    "item/completed",
]
ProtocolStage = Literal[
    "initialize",
    "authority_account_read",
    "authority_refresh",
    "initial_login",
    "initial_read_only",
    "tool_catalog_preflight",
    "model_thread_start",
    "invalid_login",
    "refresh_callback",
    "refresh_trigger",
    "recovery_read_only",
    "protocol_transport",
]
ProtocolRole = Literal["authority", "runner"]
_PROTOCOL_METHODS: frozenset[str] = frozenset(get_args(ProtocolMethod))
_PROTOCOL_STAGES: frozenset[str] = frozenset(get_args(ProtocolStage))
_PROTOCOL_ROLES: frozenset[str] = frozenset(get_args(ProtocolRole))

_SECRET_KEYS = frozenset(
    {
        "accessToken",
        "access_token",
        "refreshToken",
        "refresh_token",
        "idToken",
        "id_token",
        "chatgptAccountId",
        "chatgpt_account_id",
        "accountId",
        "previousAccountId",
        "account_id",
        "threadId",
        "thread_id",
        "turnId",
        "turn_id",
    }
)
_REMOTE_HOME_RE = re.compile(r"^/tmp/ccswitch-codex-spike\.[A-Za-z0-9]+$")
_RELEVANT_SCHEMA_FILES = (
    "v1/InitializeParams.json",
    "v2/LoginAccountParams.json",
    "v2/LoginAccountResponse.json",
    "v2/GetAccountParams.json",
    "v2/GetAccountResponse.json",
    "v2/GetAccountRateLimitsResponse.json",
    "v2/ModelListParams.json",
    "v2/ModelListResponse.json",
    "v2/ThreadStartParams.json",
    "v2/ThreadStartResponse.json",
    "v2/TurnStartParams.json",
    "v2/TurnStartResponse.json",
    "v2/TurnCompletedNotification.json",
    "v2/ItemStartedNotification.json",
    "v2/ItemCompletedNotification.json",
    "ChatgptAuthTokensRefreshParams.json",
    "ChatgptAuthTokensRefreshResponse.json",
    "ClientRequest.json",
    "ServerNotification.json",
    "ServerRequest.json",
)
_REQUIRED_SCHEMA_MARKERS = (
    "experimentalApi",
    "chatgptAuthTokens",
    "account/chatgptAuthTokens/refresh",
    "account/read",
    "account/rateLimits/read",
    "model/list",
    "thread/start",
    "turn/start",
    "turn/completed",
    "item/started",
    "item/completed",
)

# This program executes on each runner without any managed credentials. It points the
# pinned app-server at a loopback-only fake Responses provider and reports only whether
# the resulting HTTP request contained the exact empty tool catalog. Raw JSON-RPC frames,
# request payloads, native identifiers, and child stderr remain inside the short-lived
# process and are discarded.
_TOOL_CATALOG_PREFLIGHT_PROGRAM = r"""
import http.server
import json
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import time


class CaptureHandler(http.server.BaseHTTPRequestHandler):
    observed = False
    empty = False

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 8 * 1024 * 1024:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            tools = payload.get("tools")
            type(self).empty = "tools" not in payload or tools == []
        except BaseException:
            type(self).empty = False
        finally:
            type(self).observed = True
        body = b'{"error":{"message":"preflight complete","type":"invalid_request_error"}}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def write_frame(process, frame):
    process.stdin.write(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
    process.stdin.flush()


def read_frame(process, deadline):
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while time.monotonic() < deadline:
            if not selector.select(max(0.0, deadline - time.monotonic())):
                continue
            line = process.stdout.readline()
            if not line:
                raise RuntimeError
            frame = json.loads(line)
            if isinstance(frame, dict):
                return frame
        raise TimeoutError
    finally:
        selector.close()


def request(process, request_id, method, params, deadline):
    write_frame(process, {"id": request_id, "method": method, "params": params})
    while True:
        frame = read_frame(process, deadline)
        if frame.get("id") != request_id:
            continue
        if "error" in frame or "result" not in frame:
            raise RuntimeError
        return frame["result"]


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def main():
    process = None
    server = None
    server_thread = None
    empty = False
    try:
        supplied = json.loads(sys.stdin.buffer.read())
        thread_params = supplied["thread"]
        turn_params = supplied["turn"]
        if not isinstance(thread_params, dict) or not isinstance(turn_params, dict):
            raise ValueError
        model = thread_params.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError
        with tempfile.TemporaryDirectory(prefix="ccswitch-codex-tool-preflight.") as root:
            thread_params["cwd"] = root
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            port = server.server_address[1]
            environment = {
                key: os.environ[key]
                for key in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
                if key in os.environ
            }
            environment["CODEX_HOME"] = root
            provider = "ccswitch_tool_preflight"
            command = [
                sys.argv[1],
                "app-server",
                "--stdio",
                "--strict-config",
                "-c", f'model_provider="{provider}"',
                "-c", "model=" + json.dumps(model),
                "-c", f'model_providers.{provider}.name="ccswitch tool preflight"',
                "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{port}/v1"',
                "-c", f'model_providers.{provider}.wire_api="responses"',
                "-c", f'model_providers.{provider}.experimental_bearer_token="preflight"',
                "-c", f'model_providers.{provider}.request_max_retries=0',
                "-c", f'model_providers.{provider}.stream_max_retries=0',
            ]
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            deadline = time.monotonic() + 20
            request(
                process,
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "ccswitch-tool-catalog-preflight",
                        "title": "ccswitch tool catalog preflight",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                deadline,
            )
            write_frame(process, {"method": "initialized", "params": {}})
            thread = request(process, 2, "thread/start", thread_params, deadline)
            native_thread = thread.get("thread") if isinstance(thread, dict) else None
            native_thread_id = (
                native_thread.get("id") if isinstance(native_thread, dict) else None
            )
            if not isinstance(native_thread_id, str) or not native_thread_id:
                raise RuntimeError
            turn_params["threadId"] = native_thread_id
            request(process, 3, "turn/start", turn_params, deadline)
            while not CaptureHandler.observed and time.monotonic() < deadline:
                time.sleep(0.01)
            empty = CaptureHandler.observed and CaptureHandler.empty
            stop_process(process)
            process = None
            server.shutdown()
            server.server_close()
            server = None
            server_thread.join(timeout=2)
            server_thread = None
    except BaseException:
        empty = False
    finally:
        stop_process(process)
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2)
    sys.stdout.write(json.dumps({"empty": bool(empty)}, separators=(",", ":")))
    return 0 if empty else 1


raise SystemExit(main())
""".strip()


class SpikeError(RuntimeError):
    """A sanitized spike failure."""


class SpikeConfigurationError(SpikeError):
    """The explicit live-test gate or safe configuration is missing."""


class ProtocolError(SpikeError):
    """The app-server protocol failed without retaining raw frame content."""

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        stage: str | None = None,
        role: str | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method if method in _PROTOCOL_METHODS else None
        self.stage = stage if stage in _PROTOCOL_STAGES else None
        self.role = role if role in _PROTOCOL_ROLES else None

    def with_context(
        self,
        *,
        method: str | None = None,
        stage: str | None = None,
        role: str | None = None,
    ) -> ProtocolError:
        """Fill missing diagnostic fields from fixed allowlists only."""
        if self.method is None and method in _PROTOCOL_METHODS:
            self.method = method
        if self.stage is None and stage in _PROTOCOL_STAGES:
            self.stage = stage
        if self.role is None and role in _PROTOCOL_ROLES:
            self.role = role
        return self


class NativeRequestError(ProtocolError):
    """A sanitized native error response eligible for refresh-state classification."""


def validate_account_response(response: Any) -> None:
    """Require an authenticated ChatGPT account response, not merely JSON-RPC success."""
    if not isinstance(response, dict):
        raise ProtocolError("account/read returned an invalid response")
    if not isinstance(response.get("requiresOpenaiAuth"), bool):
        raise ProtocolError("account/read omitted its auth requirement state")
    account = response.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise ProtocolError("account/read did not confirm a ChatGPT account")
    plan_type = account.get("planType")
    if not isinstance(plan_type, str) or not plan_type:
        raise ProtocolError("account/read omitted the ChatGPT plan type")
    if "email" not in account or account["email"] is not None and not isinstance(
        account["email"], str
    ):
        raise ProtocolError("account/read returned an invalid ChatGPT identity shape")


def validate_model_response(response: Any, *, required_model: str | None = None) -> None:
    """Require a nonempty, structurally meaningful model catalog."""
    if not isinstance(response, dict):
        raise ProtocolError("model/list returned an invalid response")
    models = response.get("data")
    if not isinstance(models, list) or not models:
        raise ProtocolError("model/list returned no available models")
    for model in models:
        if not isinstance(model, dict):
            raise ProtocolError("model/list returned an invalid model entry")
        for key in ("id", "model", "displayName", "description", "defaultReasoningEffort"):
            if not isinstance(model.get(key), str) or not model[key]:
                raise ProtocolError("model/list returned an incomplete model entry")
        if not isinstance(model.get("isDefault"), bool) or not isinstance(
            model.get("hidden"), bool
        ):
            raise ProtocolError("model/list returned invalid visibility markers")
        efforts = model.get("supportedReasoningEfforts")
        if not isinstance(efforts, list) or any(
            not isinstance(effort, dict)
            or not isinstance(effort.get("reasoningEffort"), str)
            or not effort["reasoningEffort"]
            or not isinstance(effort.get("description"), str)
            for effort in efforts
        ):
            raise ProtocolError("model/list returned invalid reasoning capabilities")
    if required_model is not None and not any(
        model.get("id") == required_model and model.get("model") == required_model
        for model in models
    ):
        raise ProtocolError("model/list did not include the required model")


def _valid_rate_window(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    used = value.get("usedPercent")
    return isinstance(used, int) and not isinstance(used, bool) and 0 <= used <= 100


def validate_rate_limit_response(response: Any, *, expected_account_id: str) -> None:
    """Require a quota snapshot with a real window and no contradictory account binding."""
    if not isinstance(response, dict):
        raise ProtocolError("account/rateLimits/read returned an invalid response")
    response_account = response.get("accountId")
    if response_account is not None and response_account != expected_account_id:
        raise ProtocolError("account/rateLimits/read returned a different account")
    root_snapshot = response.get("rateLimits")
    if not isinstance(root_snapshot, dict):
        raise ProtocolError("account/rateLimits/read omitted its quota snapshot")
    snapshots = [root_snapshot]
    by_limit = response.get("rateLimitsByLimitId")
    if by_limit is not None:
        if not isinstance(by_limit, dict):
            raise ProtocolError("account/rateLimits/read returned invalid quota buckets")
        snapshots.extend(by_limit.values())
    windows = [
        snapshot.get(key)
        for snapshot in snapshots
        if isinstance(snapshot, dict)
        for key in ("primary", "secondary")
        if snapshot.get(key) is not None
    ]
    if not windows or any(not _valid_rate_window(window) for window in windows):
        raise ProtocolError("account/rateLimits/read returned no valid quota window")


@dataclass(frozen=True)
class RedactionValues:
    """Exact auth and native identifiers forbidden from every observable sink."""

    exact_values: tuple[str, ...]

    def find_leaks(self, text: str | bytes) -> tuple[str, ...]:
        raw = text if isinstance(text, bytes) else text.encode("utf-8", errors="replace")
        labels: list[str] = []
        for index, value in enumerate(self.exact_values):
            if value.encode("utf-8") in raw:
                labels.append(f"sensitive-value-{index + 1}")
        return tuple(labels)


@dataclass(frozen=True)
class RunnerHomeAuthValues:
    """Runner-visible auth/account material forbidden from temporary home files."""

    exact_values: tuple[str, ...]


@dataclass(frozen=True)
class SecretValues:
    """Exact live values that must never reach a user-visible sink."""

    access_token: str
    refresh_token: str
    account_id: str
    invalid_access_token: str
    native_ids: tuple[str, ...] = ()
    historical_runner_auth_values: tuple[str, ...] = ()
    historical_refresh_tokens: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.access_token,
            self.refresh_token,
            self.account_id,
            self.invalid_access_token,
        )
        if any(len(value) < 12 for value in required):
            raise ValueError("sensitive auth values must be nonempty and at least 12 characters")

    @property
    def redaction_values(self) -> RedactionValues:
        return RedactionValues(
            tuple(
                dict.fromkeys(
                    (
                        self.access_token,
                        self.refresh_token,
                        self.account_id,
                        self.invalid_access_token,
                        *self.native_ids,
                        *self.historical_runner_auth_values,
                        *self.historical_refresh_tokens,
                    )
                )
            )
        )

    @property
    def runner_home_auth_values(self) -> RunnerHomeAuthValues:
        """Return only auth/account bytes whose persistence would be a real leak."""
        return RunnerHomeAuthValues(
            tuple(
                dict.fromkeys(
                    (
                        self.access_token,
                        self.account_id,
                        self.invalid_access_token,
                        *self.historical_runner_auth_values,
                    )
                )
            )
        )

    def with_native_id(self, native_id: str) -> SecretValues:
        if not native_id:
            return self
        return replace(self, native_ids=(*self.native_ids, native_id))

    def merge(self, newer: SecretValues) -> SecretValues:
        return replace(
            newer,
            native_ids=tuple(dict.fromkeys((*self.native_ids, *newer.native_ids))),
            historical_runner_auth_values=tuple(
                dict.fromkeys(
                    (
                        *self.runner_home_auth_values.exact_values,
                        *newer.historical_runner_auth_values,
                    )
                )
            ),
            historical_refresh_tokens=tuple(
                dict.fromkeys(
                    (
                        *self.historical_refresh_tokens,
                        self.refresh_token,
                        *newer.historical_refresh_tokens,
                    )
                )
            ),
        )


def sanitize_text(text: str, secrets: SecretValues | None = None) -> str:
    """Use the application's canonical redactor plus exact live-value replacement."""
    clean = redact_text(text)
    if secrets is not None:
        for value in sorted(secrets.redaction_values.exact_values, key=len, reverse=True):
            clean = clean.replace(value, REDACTED)
    return clean


def failure_json(exc: BaseException, secrets: SecretValues | None = None) -> str:
    """Serialize only allow-listed failure metadata, never arbitrary exception notes."""
    payload: dict[str, str | bool] = {
        "release_gate": "fail",
        "error": sanitize_text(str(exc), secrets),
    }
    notes = getattr(exc, "__notes__", ())
    if _CLEANUP_FAILED_NOTE in notes:
        payload["cleanup_also_failed"] = True
    if isinstance(exc, ProtocolError):
        if exc.method is not None:
            payload["method"] = exc.method
        if exc.stage is not None:
            payload["stage"] = exc.stage
        if exc.role is not None:
            payload["role"] = exc.role
    return json.dumps(payload, sort_keys=True)


def sanitize_protocol_value(value: Any, secrets: SecretValues | None = None) -> Any:
    """Return a diagnostic-safe copy; the harness never emits raw protocol frames."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED
            if str(key) in _SECRET_KEYS
            else sanitize_protocol_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_protocol_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_protocol_value(item, secrets) for item in value)
    if isinstance(value, str):
        return sanitize_text(value, secrets)
    return value


@dataclass(frozen=True)
class HomeScan:
    auth_json_present: bool
    secret_file_count: int
    symlink_count: int = 0

    @property
    def clean(self) -> bool:
        return not self.auth_json_present and self.secret_file_count == 0 and self.symlink_count == 0


def scan_home(
    home: Path,
    auth_values: RunnerHomeAuthValues,
) -> HomeScan:
    """Scan a temporary local runner home without returning file names/content."""
    encoded = tuple(value.encode("utf-8") for value in auth_values.exact_values)
    auth_json_present = False
    secret_file_count = 0
    symlink_count = 0
    for path in home.rglob("*"):
        if path.is_symlink():
            symlink_count += 1
            continue
        if not path.is_file():
            continue
        if path.name == "auth.json":
            auth_json_present = True
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SpikeError("temporary runner home could not be inspected") from exc
        if any(needle in content for needle in encoded):
            secret_file_count += 1
    return HomeScan(auth_json_present, secret_file_count, symlink_count)


@dataclass(frozen=True)
class RunnerTarget:
    name: str
    ssh_host: str | None

    def __post_init__(self) -> None:
        for candidate in (self.name, self.ssh_host):
            if candidate is not None and not SSH_HOST_RE.fullmatch(candidate):
                raise SpikeConfigurationError("runner names must contain only safe host characters")


@dataclass(frozen=True)
class TopologyEvidence:
    """Allow-listed proof of topology without exposing machine identity digests."""

    distinct_runner_hosts: bool
    authority_on_local_runner: bool
    topology_sha256: str

    def to_wire(self) -> dict[str, bool | str]:
        return {
            "distinct_runner_hosts": self.distinct_runner_hosts,
            "authority_on_local_runner": self.authority_on_local_runner,
            "topology_sha256": self.topology_sha256,
        }


def validate_topology(
    *,
    authority_identity: str,
    runner_identities: tuple[str, str],
) -> TopologyEvidence:
    """Require local authority membership and two distinct physical runner hosts."""
    identities = (authority_identity, *runner_identities)
    if any(not re.fullmatch(r"[0-9a-f]{64}", identity) for identity in identities):
        raise SpikeConfigurationError("host identity preflight returned an invalid digest")
    distinct_runners = len(set(runner_identities)) == 2
    authority_on_runner = runner_identities.count(authority_identity) == 1
    if not distinct_runners:
        raise SpikeConfigurationError("the two runner aliases resolve to the same machine")
    if not authority_on_runner:
        raise SpikeConfigurationError("the local authority must also be exactly one runner")
    topology_payload = "ccswitch-codex-topology-v1\0" + "\0".join(sorted(runner_identities))
    topology_sha256 = hashlib.sha256(topology_payload.encode("ascii")).hexdigest()
    return TopologyEvidence(True, True, topology_sha256)


def _hash_machine_identity(raw: bytes) -> str:
    value = raw.strip()
    if len(value) < 16:
        raise SpikeConfigurationError("stable local machine identity is unavailable")
    return hashlib.sha256(b"ccswitch-codex-host-v1\0" + value).hexdigest()


def local_machine_identity() -> str:
    """Hash the stable machine id in memory; never emit its raw value."""
    try:
        return _hash_machine_identity(Path("/etc/machine-id").read_bytes())
    except OSError as exc:
        raise SpikeConfigurationError("stable local machine identity is unavailable") from exc


def _minimal_environment(*, codex_home: Path | None = None) -> dict[str, str]:
    allowed = ("HOME", "LANG", "LC_ALL", "PATH", "SSH_AUTH_SOCK", "TERM")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home)
    return environment


def build_ssh_command(ssh_host: str, remote_argv: Sequence[str]) -> list[str]:
    """Quote one remote argv for OpenSSH's remote-shell command transport."""
    if not SSH_HOST_RE.fullmatch(ssh_host):
        raise SpikeConfigurationError("runner names must contain only safe host characters")
    if not remote_argv or any("\0" in argument for argument in remote_argv):
        raise SpikeConfigurationError("remote command arguments are invalid")
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        ssh_host,
        "--",
        shlex.join(remote_argv),
    ]


def build_runner_process(
    target: RunnerTarget,
    home: Path,
    *,
    codex_bin: str = "codex",
) -> tuple[list[str], dict[str, str]]:
    """Build secret-free process metadata. Auth is sent later over stdin only."""
    if target.ssh_host is None:
        return [codex_bin, "app-server", "--stdio"], _minimal_environment(codex_home=home)
    if not _REMOTE_HOME_RE.fullmatch(str(home)):
        raise SpikeConfigurationError("remote runner home is outside the dedicated /tmp prefix")
    command = build_ssh_command(
        target.ssh_host,
        [
            "sh",
            "-c",
            (
                'exec env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" '
                'CODEX_HOME="$1" "$2" app-server --stdio'
            ),
            "ccswitch-codex-spike",
            str(home),
            codex_bin,
        ],
    )
    return command, _minimal_environment()


def build_external_login_frame(request_id: int, secrets: SecretValues) -> dict[str, Any]:
    """Build the one secret-bearing frame; callers write it directly to child stdin."""
    return {
        "id": request_id,
        "method": "account/login/start",
        "params": {
            "type": "chatgptAuthTokens",
            "accessToken": secrets.access_token,
            "chatgptAccountId": secrets.account_id,
        },
    }


def model_turn_opted_in(flag: str | None) -> bool:
    """Recognize only the exact second live-cost opt-in value."""
    return flag == "1"


def _require_model_turn_opt_in(model_turn_enabled: bool) -> None:
    if not model_turn_enabled:
        raise SpikeConfigurationError(
            f"refusing model turn: set {MODEL_TURN_OPT_IN_FLAG}=1"
        )


def build_model_thread_params(
    cwd: Path,
    *,
    model_turn_enabled: bool,
) -> dict[str, Any]:
    """Build the pinned, tool-free ephemeral-thread request after explicit opt-in."""
    _require_model_turn_opt_in(model_turn_enabled)
    return {
        "approvalPolicy": "never",
        "config": {
            "agents": {"enabled": False},
            "apps": {"_default": {"enabled": False}},
            "features": {
                "apps": False,
                "browser_use": False,
                "browser_use_external": False,
                "browser_use_full_cdp_access": False,
                "code_mode": False,
                "computer_use": False,
                "enable_request_compression": False,
                "goals": False,
                "hooks": False,
                "image_generation": False,
                "multi_agent": False,
                "multi_agent_v2": False,
                "plugin_sharing": False,
                "plugins": False,
                "recommended_plugins": False,
                "remote_plugin": False,
                "shell_tool": False,
                "skill_mcp_dependency_install": False,
                "skill_search": False,
                "sleep_tool": False,
                "tool_suggest": False,
                "unified_exec": False,
                "view_image": False,
                "workspace_dependencies": False,
            },
            "mcp_servers": {},
            "orchestrator": {"skills": {"enabled": False}},
            "skills": {
                "bundled": {"enabled": False},
                "include_instructions": False,
            },
            "tools": {
                "experimental_request_user_input": {"enabled": False},
            },
            "web_search": "disabled",
        },
        "cwd": str(cwd),
        "developerInstructions": MODEL_TURN_DEVELOPER_INSTRUCTIONS,
        "dynamicTools": [],
        "environments": [],
        "ephemeral": True,
        "experimentalRawEvents": False,
        "model": MODEL_TURN_MODEL,
        "sandbox": "read-only",
    }


def build_model_turn_params(
    thread_id: str,
    *,
    model_turn_enabled: bool,
) -> dict[str, Any]:
    """Build the single fixed minimal turn request after explicit opt-in."""
    _require_model_turn_opt_in(model_turn_enabled)
    if not thread_id:
        raise ProtocolError(
            "model thread response omitted its native identifier",
            method="thread/start",
            stage="model_thread_start",
            role="runner",
        )
    return {
        "threadId": thread_id,
        "input": [{"type": "text", "text": MODEL_TURN_PROMPT}],
    }


def require_opt_in(*, flag: str | None, account: str | None) -> str:
    """Require the exact live-test flag and a nonempty non-secret account alias."""
    if flag != "1" or not account or not account.strip():
        raise SpikeConfigurationError(
            f"refusing live probe: set {OPT_IN_FLAG}=1 and {ACCOUNT_ALIAS_ENV}=<alias>"
        )
    return account.strip()


@dataclass(frozen=True)
class SchemaEvidence:
    cli_version: str
    schema_sha256: str
    methods: tuple[str, ...]
    experimental_api_required: bool


def _validate_model_turn_schema(subset: Mapping[str, Any]) -> None:
    """Fail closed unless the pinned thread/turn contracts have their exact essentials."""
    required_contracts = {
        "v2/ThreadStartParams.json": {
            "approvalPolicy",
            "cwd",
            "developerInstructions",
            "dynamicTools",
            "environments",
            "ephemeral",
            "experimentalRawEvents",
            "model",
            "sandbox",
        },
        "v2/ThreadStartResponse.json": {"approvalPolicy", "cwd", "sandbox", "thread"},
        "v2/TurnStartParams.json": {"input", "threadId"},
        "v2/TurnStartResponse.json": {"turn"},
        "v2/TurnCompletedNotification.json": {"threadId", "turn"},
        "v2/ItemStartedNotification.json": {"item", "threadId", "turnId"},
        "v2/ItemCompletedNotification.json": {"item", "threadId", "turnId"},
    }
    for relative, expected_properties in required_contracts.items():
        document = subset.get(relative)
        properties = document.get("properties") if isinstance(document, dict) else None
        if not isinstance(properties, dict) or not expected_properties.issubset(properties):
            raise SpikeError("generated Codex schema has an incompatible model-turn contract")

    thread_params = subset["v2/ThreadStartParams.json"]
    thread_response = subset["v2/ThreadStartResponse.json"]
    turn_params = subset["v2/TurnStartParams.json"]
    turn_response = subset["v2/TurnStartResponse.json"]
    turn_completed = subset["v2/TurnCompletedNotification.json"]
    item_started = subset["v2/ItemStartedNotification.json"]
    item_completed = subset["v2/ItemCompletedNotification.json"]
    canonical_contracts = json.dumps(
        {
            "thread_params": thread_params,
            "thread_response": thread_response,
            "turn_params": turn_params,
            "turn_response": turn_response,
            "turn_completed": turn_completed,
            "item_started": item_started,
            "item_completed": item_completed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    required_shape_markers = (
        '"never"',
        '"read-only"',
        '"readOnly"',
        '"completed"',
        '"agentMessage"',
        '"commandExecution"',
        '"text"',
    )
    if any(marker not in canonical_contracts for marker in required_shape_markers):
        raise SpikeError("generated Codex schema has an incompatible model-turn shape")
    for document, required_fields in (
        (turn_params, {"input", "threadId"}),
        (turn_response, {"turn"}),
        (turn_completed, {"threadId", "turn"}),
        (item_started, {"item", "startedAtMs", "threadId", "turnId"}),
        (item_completed, {"completedAtMs", "item", "threadId", "turnId"}),
    ):
        required = document.get("required")
        if not isinstance(required, list) or not required_fields.issubset(required):
            raise SpikeError("generated Codex schema has incompatible required fields")


def inspect_schema(codex_bin: str) -> SchemaEvidence:
    """Generate the exact installed schema in a disposable, credential-free directory."""
    version_result = subprocess.run(
        [codex_bin, "--version"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_minimal_environment(),
    )
    cli_version = version_result.stdout.strip()
    if not cli_version or "\n" in cli_version:
        raise SpikeError("Codex CLI returned an invalid version string")

    with tempfile.TemporaryDirectory(prefix="ccswitch-codex-schema-") as temp:
        root = Path(temp)
        subprocess.run(
            [
                codex_bin,
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(root),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_minimal_environment(),
        )
        subset: dict[str, Any] = {}
        for relative in _RELEVANT_SCHEMA_FILES:
            path = root / relative
            if not path.is_file():
                raise SpikeError("generated Codex schema is missing a required contract file")
            subset[relative] = json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(subset, sort_keys=True, separators=(",", ":"))
        missing = tuple(marker for marker in _REQUIRED_SCHEMA_MARKERS if marker not in canonical)
        if missing:
            raise SpikeError("generated Codex schema is missing required external-auth methods")
        _validate_model_turn_schema(subset)
        external_description = json.dumps(subset["v2/LoginAccountParams.json"])
        experimental = "UNSTABLE" in external_description and "INTERNAL USE ONLY" in external_description
        if not experimental:
            raise SpikeError("external-token contract no longer carries the expected unstable marker")
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return SchemaEvidence(
        cli_version=cli_version,
        schema_sha256=digest,
        methods=(
            "initialize",
            "account/login/start",
            "account/read",
            "account/rateLimits/read",
            "model/list",
            "account/chatgptAuthTokens/refresh",
            "thread/start",
            "turn/start",
            "turn/completed",
            "item/started",
            "item/completed",
        ),
        experimental_api_required=True,
    )


ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ServerNotificationHandler = Callable[[str, Any], None]


@dataclass(frozen=True)
class ModelTurnResult:
    """Bounded result state; no raw notification or text payload is retained."""

    tool_free_exact_ok: bool
    native_ids: tuple[str, ...]


class ModelTurnObserver:
    """Reduce one runner's model-turn notifications into fixed safe state."""

    _ALLOWED_ITEM_TYPES = frozenset({"userMessage", "reasoning", "agentMessage"})
    _OBSERVED_METHODS = frozenset(
        {"turn/completed", "item/started", "item/completed"}
    )

    def __init__(self) -> None:
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._completion: asyncio.Future[ModelTurnResult] = (
            asyncio.get_running_loop().create_future()
        )

    def arm(self, thread_id: str) -> None:
        if not thread_id or self._thread_id is not None:
            raise ProtocolError(
                "model thread observer could not be armed",
                method="thread/start",
                stage="model_thread_start",
                role="runner",
            )
        self._thread_id = thread_id

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    def _fail(self, message: str, *, method: str) -> None:
        if not self._completion.done():
            self._completion.set_exception(
                ProtocolError(
                    message,
                    method=method,
                    stage="refresh_trigger",
                    role="runner",
                )
            )

    def _bind_turn_id(self, turn_id: Any, *, method: str) -> bool:
        if not isinstance(turn_id, str) or not turn_id:
            self._fail("model turn notification omitted its native identifier", method=method)
            return False
        if self._turn_id is not None and self._turn_id != turn_id:
            self._fail("model turn notification referenced a different turn", method=method)
            return False
        self._turn_id = turn_id
        return True

    def _validate_items(self, items: Any, *, method: str) -> bool:
        if not isinstance(items, list):
            self._fail("model turn notification returned invalid items", method=method)
            return False
        agent_message_count = 0
        exact_ok = True
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                self._fail("model turn notification returned an invalid item", method=method)
                return False
            item_type = item["type"]
            if item_type not in self._ALLOWED_ITEM_TYPES:
                self._fail(
                    "model turn emitted a prohibited tool or activity item",
                    method=method,
                )
                return False
            if item_type == "agentMessage":
                agent_message_count += 1
                exact_ok = exact_ok and item.get("text") == MODEL_TURN_EXPECTED_TEXT
        if method == "turn/completed" and (
            agent_message_count != 1 or not exact_ok
        ):
            self._fail("model turn assistant output was not exactly OK", method=method)
            return False
        return True

    def observe(self, method: str, params: Any) -> None:
        """Consume relevant fields immediately and discard the raw notification."""
        if (
            self._thread_id is None
            or self._completion.done()
            or method not in self._OBSERVED_METHODS
        ):
            return
        if not isinstance(params, dict):
            self._fail("model turn notification had invalid parameters", method=method)
            return
        if params.get("threadId") != self._thread_id:
            self._fail("model turn notification referenced a different thread", method=method)
            return
        if method in {"item/started", "item/completed"}:
            if not self._bind_turn_id(params.get("turnId"), method=method):
                return
            item = params.get("item")
            self._validate_items([item], method=method)
            return

        turn = params.get("turn")
        if not isinstance(turn, dict):
            self._fail("model turn completion had an invalid response", method=method)
            return
        if not self._bind_turn_id(turn.get("id"), method=method):
            return
        if turn.get("status") != "completed":
            self._fail("model turn did not complete successfully", method=method)
            return
        if not self._validate_items(turn.get("items"), method=method):
            return
        assert self._thread_id is not None and self._turn_id is not None
        self._completion.set_result(
            ModelTurnResult(
                tool_free_exact_ok=True,
                native_ids=(self._thread_id, self._turn_id),
            )
        )

    def bind_turn_response(self, response: Any) -> None:
        if not isinstance(response, dict) or not isinstance(response.get("turn"), dict):
            raise ProtocolError(
                "turn/start returned an invalid response",
                method="turn/start",
                stage="refresh_trigger",
                role="runner",
            )
        turn = response["turn"]
        turn_id = turn.get("id")
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or turn.get("status") not in {"inProgress", "completed"}
            or not isinstance(turn.get("items"), list)
            or self._turn_id is not None
            and self._turn_id != turn_id
        ):
            raise ProtocolError(
                "turn/start returned an invalid or inconsistent response",
                method="turn/start",
                stage="refresh_trigger",
                role="runner",
            )
        self._turn_id = turn_id

    async def wait_for_completion(self) -> ModelTurnResult:
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._completion),
                MODEL_TURN_TIMEOUT_S,
            )
        except TimeoutError as exc:
            self._completion.cancel()
            raise ProtocolError(
                "model turn completion timed out",
                method="turn/completed",
                stage="refresh_trigger",
                role="runner",
            ) from exc

    def discard(self) -> None:
        """Consume or cancel bounded state when turn/start fails first."""
        if self._completion.done():
            if not self._completion.cancelled():
                self._completion.exception()
            return
        self._completion.cancel()


class JsonRpcProcess:
    """Small spike-only JSONL client that never emits raw frames or child stderr."""

    def __init__(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        *,
        role: ProtocolRole,
        handler: ServerRequestHandler | None = None,
        notification_handler: ServerNotificationHandler | None = None,
    ) -> None:
        self._command = list(command)
        self._environment = dict(environment)
        self._role = role if role in _PROTOCOL_ROLES else None
        self._handler = handler
        self._notification_handler = notification_handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[
            int, tuple[asyncio.Future[Any], str | None, str | None]
        ] = {}
        self._abandoned_request_ids: set[int] = set()
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._next_id = 1
        self._stderr = bytearray()
        self._background_errors: list[BaseException] = []
        self._callback_failures: list[ProtocolError | asyncio.CancelledError] = []
        self._callback_activity_sequence = 0
        self._callback_activity_event = asyncio.Event()

    @property
    def captured_stderr(self) -> bytes:
        return bytes(self._stderr)

    async def start(self, *, experimental_api: bool) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "ccswitch-external-auth-spike",
                    "title": "ccswitch external auth spike",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": experimental_api},
            },
            stage="initialize",
        )
        await self.notify("initialized", {}, stage="initialize")

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                try:
                    frame = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ProtocolError("app-server emitted a malformed JSONL frame") from exc
                if "id" in frame and "method" in frame:
                    self._start_server_request(frame)
                    continue
                if "id" not in frame and "method" in frame:
                    method = frame.get("method")
                    if self._notification_handler is not None and isinstance(method, str):
                        self._notification_handler(method, frame.get("params"))
                    continue
                if "id" in frame:
                    request_id = frame.get("id")
                    if type(request_id) is not int:
                        raise ProtocolError("app-server replied with an invalid request id")
                    pending = self._pending.pop(request_id, None)
                    if pending is None:
                        if request_id in self._abandoned_request_ids:
                            if ("result" in frame) == ("error" in frame):
                                raise ProtocolError(
                                    "app-server returned an invalid abandoned response"
                                )
                            self._abandoned_request_ids.remove(request_id)
                            continue
                        raise ProtocolError("app-server replied with an unknown request id")
                    future, method, stage = pending
                    if future.done():
                        continue
                    if "error" in frame:
                        future.set_exception(
                            NativeRequestError(
                                "app-server request returned an error",
                                method=method,
                                stage=stage,
                                role=self._role,
                            )
                        )
                    else:
                        future.set_result(frame.get("result"))
            if self._pending:
                raise ProtocolError("app-server closed with pending requests")
        except BaseException as exc:
            if isinstance(exc, ProtocolError):
                exc.with_context(stage="protocol_transport", role=self._role)
            self._background_errors.append(exc)
            for future, method, stage in self._pending.values():
                if not future.done():
                    future.set_exception(
                        ProtocolError(
                            "app-server reader stopped",
                            method=method,
                            stage=stage,
                            role=self._role,
                        )
                    )
            self._pending.clear()

    def set_notification_handler(self, handler: ServerNotificationHandler) -> None:
        if self._notification_handler is not None:
            raise ProtocolError(
                "app-server notification observer was already configured",
                stage="protocol_transport",
                role=self._role,
            )
        self._notification_handler = handler

    def _start_server_request(self, frame: dict[str, Any]) -> None:
        self._callback_activity_sequence += 1
        self._callback_activity_event.set()
        task = asyncio.create_task(self._answer_server_request(frame))
        self._server_request_tasks.add(task)
        task.add_done_callback(self._consume_server_request_task)

    def _consume_server_request_task(self, task: asyncio.Task[None]) -> None:
        """Retrieve every callback task exception so none escapes the private pipe."""
        self._server_request_tasks.discard(task)
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            contextual_failure = ProtocolError(
                "app-server callback task failed",
                stage="refresh_callback",
                role=self._role,
            )
            self._background_errors.append(contextual_failure)
            self._callback_failures.append(contextual_failure)

    def callback_checkpoint(self) -> int:
        return len(self._callback_failures)

    async def callback_failures_after_quiescence(
        self,
        checkpoint: int,
        *,
        quiet_interval: float = CALLBACK_QUIET_INTERVAL_S,
        max_wait: float = CALLBACK_FENCE_DEADLINE_S,
    ) -> tuple[ProtocolError | asyncio.CancelledError, ...]:
        """Drain callback tasks, then observe a bounded registration quiet period."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            tasks = tuple(self._server_request_tasks)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=remaining)
                if pending:
                    break
                await asyncio.sleep(0)
                continue

            observed_sequence = self._callback_activity_sequence
            self._callback_activity_event.clear()
            if self._callback_activity_sequence != observed_sequence:
                continue
            remaining = deadline - loop.time()
            if remaining < quiet_interval:
                break
            try:
                await asyncio.wait_for(
                    self._callback_activity_event.wait(),
                    quiet_interval,
                )
            except TimeoutError:
                if (
                    self._callback_activity_sequence == observed_sequence
                    and not self._server_request_tasks
                ):
                    return tuple(self._callback_failures[checkpoint:])

        failure = ProtocolError(
            "runner callback activity did not quiesce",
            method="account/chatgptAuthTokens/refresh",
            stage="refresh_callback",
            role=self._role,
        )
        self._background_errors.append(failure)
        self._callback_failures.append(failure)
        return tuple(self._callback_failures[checkpoint:])

    async def _answer_server_request(self, frame: dict[str, Any]) -> None:
        try:
            if self._handler is None:
                raise ProtocolError("unexpected app-server request")
            result = await self._handler(frame)
        except asyncio.CancelledError as exc:
            self._background_errors.append(exc)
            self._callback_failures.append(exc)
            raise
        except BaseException as exc:
            if isinstance(exc, ProtocolError):
                failure = exc.with_context(
                    method=frame.get("method"),
                    stage="refresh_callback",
                    role=self._role,
                )
            else:
                failure = ProtocolError(
                    "app-server callback handler failed",
                    method=frame.get("method"),
                    stage="refresh_callback",
                    role=self._role,
                )
            self._background_errors.append(failure)
            self._callback_failures.append(failure)
            try:
                await self._write(
                    {
                        "id": frame.get("id"),
                        "error": {
                            "code": -32603,
                            "message": "ccswitch spike rejected request",
                        },
                    }
                )
            except asyncio.CancelledError as write_cancellation:
                self._background_errors.append(write_cancellation)
                self._callback_failures.append(write_cancellation)
                raise
            except BaseException as write_failure:
                if isinstance(write_failure, ProtocolError):
                    contextual_write_failure = write_failure.with_context(
                        method=frame.get("method"),
                        stage="refresh_callback",
                        role=self._role,
                    )
                else:
                    contextual_write_failure = ProtocolError(
                        "app-server callback error response transport failed",
                        method=frame.get("method"),
                        stage="refresh_callback",
                        role=self._role,
                    )
                self._background_errors.append(contextual_write_failure)
                self._callback_failures.append(contextual_write_failure)
                if not isinstance(write_failure, Exception):
                    raise
        else:
            try:
                await self._write({"id": frame["id"], "result": result})
            except asyncio.CancelledError as exc:
                self._background_errors.append(exc)
                self._callback_failures.append(exc)
                raise
            except BaseException as exc:
                if isinstance(exc, ProtocolError):
                    failure = exc.with_context(
                        method=frame.get("method"),
                        stage="refresh_callback",
                        role=self._role,
                    )
                else:
                    failure = ProtocolError(
                        "app-server callback response transport failed",
                        method=frame.get("method"),
                        stage="refresh_callback",
                        role=self._role,
                    )
                self._background_errors.append(failure)
                self._callback_failures.append(failure)
                if not isinstance(exc, Exception):
                    raise

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(65536):
            self._stderr.extend(chunk)

    async def _write(self, frame: Mapping[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise ProtocolError("app-server is not running")
        encoded = json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()

    def _abandon_pending(self, request_id: int, future: asyncio.Future[Any]) -> None:
        pending = self._pending.get(request_id)
        if pending is not None and pending[0] is future:
            self._pending.pop(request_id, None)
            self._abandoned_request_ids.add(request_id)
        if not future.done():
            future.cancel()

    async def request_frame(self, frame: dict[str, Any], *, stage: ProtocolStage) -> Any:
        request_id = frame.get("id")
        method = frame.get("method")
        safe_method = method if isinstance(method, str) and method in _PROTOCOL_METHODS else None
        safe_stage = stage if stage in _PROTOCOL_STAGES else None
        if type(request_id) is not int:
            raise ProtocolError(
                "client request lacks an integer id",
                method=safe_method,
                stage=safe_stage,
                role=self._role,
            )
        if request_id in self._pending:
            raise ProtocolError(
                "duplicate client request id",
                method=safe_method,
                stage=safe_stage,
                role=self._role,
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = (future, safe_method, safe_stage)
        try:
            await self._write(frame)
        except BaseException as exc:
            self._abandon_pending(request_id, future)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, ProtocolError):
                raise exc.with_context(method=safe_method, stage=safe_stage, role=self._role)
            if isinstance(exc, Exception):
                raise ProtocolError(
                    "app-server request transport failed",
                    method=safe_method,
                    stage=safe_stage,
                    role=self._role,
                ) from None
            raise
        try:
            return await asyncio.wait_for(asyncio.shield(future), REQUEST_TIMEOUT_S)
        except TimeoutError as exc:
            self._abandon_pending(request_id, future)
            raise ProtocolError(
                "app-server request timed out",
                method=safe_method,
                stage=safe_stage,
                role=self._role,
            ) from exc
        except asyncio.CancelledError:
            self._abandon_pending(request_id, future)
            raise

    async def request(
        self,
        method: ProtocolMethod,
        params: Mapping[str, Any],
        *,
        stage: ProtocolStage,
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        return await self.request_frame(
            {"id": request_id, "method": method, "params": dict(params)},
            stage=stage,
        )

    async def external_login(self, secrets: SecretValues, *, stage: ProtocolStage) -> None:
        request_id = self._next_id
        self._next_id += 1
        await self.request_frame(build_external_login_frame(request_id, secrets), stage=stage)

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        stage: ProtocolStage,
    ) -> None:
        try:
            await self._write({"method": method, "params": dict(params)})
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, ProtocolError):
                raise exc.with_context(stage=stage, role=self._role)
            if isinstance(exc, Exception):
                raise ProtocolError(
                    "app-server notification transport failed",
                    stage=stage,
                    role=self._role,
                ) from None
            raise

    async def close(self) -> None:
        if self._process is None:
            return
        if self._process.stdin is not None and not self._process.stdin.is_closing():
            self._process.stdin.close()
            try:
                await self._process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(self._process.wait(), 10.0)
        except TimeoutError:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 5.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                await task
        if self._server_request_tasks:
            await asyncio.gather(*tuple(self._server_request_tasks), return_exceptions=True)

    def assert_background_clean(self) -> None:
        if self._background_errors:
            first = self._background_errors[0]
            if isinstance(first, SpikeError):
                raise first
            raise ProtocolError(
                "app-server background task failed",
                stage="protocol_transport",
                role=self._role,
            ) from first


def _mutate_access_token(access_token: str) -> str:
    """Invalidate only the JWT signature while keeping claim parsing possible."""
    parts = access_token.split(".")
    if len(parts) != 3 or len(parts[-1]) < 2:
        raise SpikeError("managed access token does not have the expected JWT shape")
    # The first base64url character always carries six signature bits. Changing
    # the final character can alter padding-only bits and leave the decoded
    # signature unchanged for some JWT lengths.
    replacement = "A" if parts[-1][0] != "A" else "B"
    parts[-1] = replacement + parts[-1][1:]
    invalid = ".".join(parts)
    if invalid == access_token:
        raise SpikeError("could not derive an in-memory invalid access token")
    return invalid


def load_managed_secrets(auth_home: Path) -> SecretValues:
    """Read the authority-owned documented auth cache into memory only."""
    auth_path = auth_home / "auth.json"
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = payload["tokens"]
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        account_id = tokens["account_id"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SpikeError("authority managed auth cache is absent or incompatible") from exc
    if not all(isinstance(item, str) for item in (access_token, refresh_token, account_id)):
        raise SpikeError("authority managed auth cache contains invalid field types")
    return SecretValues(
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
        invalid_access_token=_mutate_access_token(access_token),
    )


class RefreshCoalescer:
    """Serialize runner callbacks into one managed app-server refresh operation."""

    def __init__(
        self,
        authority: JsonRpcProcess,
        authority_home: Path,
        initial: SecretValues,
    ) -> None:
        self._authority = authority
        self._authority_home = authority_home
        self._current = initial
        self._lock = asyncio.Lock()
        self._successful_callbacks_by_runner: dict[object, int] = {}
        self.generation = 0
        self.refresh_calls = 0

    @property
    def current(self) -> SecretValues:
        return self._current

    @property
    def callback_count(self) -> int:
        return sum(self._successful_callbacks_by_runner.values())

    def register_runner(self) -> ServerRequestHandler:
        """Return a handler bound to one opaque, stable in-process runner identity."""
        runner_identity = object()
        self._successful_callbacks_by_runner[runner_identity] = 0

        async def handle(frame: dict[str, Any]) -> dict[str, Any]:
            return await self._handle(runner_identity, frame)

        return handle

    def has_exact_callback_coverage(self, *, expected_runner_count: int) -> bool:
        """Require exactly one successful callback from every expected runner."""
        counts = self._successful_callbacks_by_runner.values()
        return (
            expected_runner_count > 0
            and len(self._successful_callbacks_by_runner) == expected_runner_count
            and all(count == 1 for count in counts)
        )

    async def _handle(
        self,
        runner_identity: object,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("method") != "account/chatgptAuthTokens/refresh":
            raise ProtocolError(
                "runner requested an unsupported experimental method",
                stage="refresh_callback",
                role="runner",
            )
        params = frame.get("params")
        if not isinstance(params, dict) or params.get("reason") != "unauthorized":
            raise ProtocolError(
                "runner refresh request has an invalid reason",
                method="account/chatgptAuthTokens/refresh",
                stage="refresh_callback",
                role="runner",
            )
        previous = params.get("previousAccountId")
        if previous not in (None, self._current.account_id):
            raise ProtocolError(
                "runner refresh request is for a different account",
                method="account/chatgptAuthTokens/refresh",
                stage="refresh_callback",
                role="runner",
            )
        rejected_generation = 0
        async with self._lock:
            if self.generation <= rejected_generation:
                self.refresh_calls += 1
                try:
                    account_response = await self._authority.request(
                        "account/read",
                        {"refreshToken": True},
                        stage="authority_refresh",
                    )
                    validate_account_response(account_response)
                    newer = load_managed_secrets(self._authority_home)
                    if newer.account_id != self._current.account_id:
                        raise ProtocolError("authority refresh changed the native account")
                    if newer.access_token in {
                        self._current.access_token,
                        self._current.invalid_access_token,
                    }:
                        raise ProtocolError(
                            "authority refresh did not advance the managed access token"
                        )
                except ProtocolError as exc:
                    raise exc.with_context(
                        method="account/read",
                        stage="authority_refresh",
                        role="authority",
                    )
                self._current = self._current.merge(newer)
                self.generation += 1
            current = self._current
        self._successful_callbacks_by_runner[runner_identity] += 1
        return {
            "accessToken": current.access_token,
            "chatgptAccountId": current.account_id,
        }


@dataclass
class RunnerRuntime:
    target: RunnerTarget
    home: Path
    process: JsonRpcProcess
    is_remote: bool
    cleaned: bool = False


def _validate_thread_start_response(response: Any, *, expected_cwd: Path) -> str:
    if not isinstance(response, dict):
        raise ProtocolError(
            "thread/start returned an invalid response",
            method="thread/start",
            stage="model_thread_start",
            role="runner",
        )
    thread = response.get("thread")
    sandbox = response.get("sandbox")
    if (
        not isinstance(thread, dict)
        or not isinstance(thread.get("id"), str)
        or not thread["id"]
        or thread.get("ephemeral") is not True
        or thread.get("model") != MODEL_TURN_MODEL
        or thread.get("cwd") != str(expected_cwd)
        or response.get("cwd") != str(expected_cwd)
        or response.get("approvalPolicy") != "never"
        or not isinstance(sandbox, dict)
        or sandbox.get("type") != "readOnly"
    ):
        raise ProtocolError(
            "thread/start did not confirm the requested safe ephemeral thread",
            method="thread/start",
            stage="model_thread_start",
            role="runner",
        )
    return thread["id"]


async def _start_model_turn_threads(
    runtimes: Sequence[RunnerRuntime],
    *,
    model_turn_enabled: bool,
    empty_tool_catalog_verified: bool = False,
) -> tuple[tuple[ModelTurnObserver, ...], tuple[str, ...]]:
    """Create no threads unless the second exact gate was enabled by the caller."""
    if not model_turn_enabled:
        return (), ()
    if not empty_tool_catalog_verified:
        raise ProtocolError(
            "refusing model thread before empty tool catalog preflight",
            method="thread/start",
            stage="tool_catalog_preflight",
            role="runner",
        )
    observers = tuple(ModelTurnObserver() for _ in runtimes)
    for runtime, observer in zip(runtimes, observers, strict=True):
        runtime.process.set_notification_handler(observer.observe)
    responses = await asyncio.gather(
        *(
            runtime.process.request(
                "thread/start",
                build_model_thread_params(
                    runtime.home,
                    model_turn_enabled=model_turn_enabled,
                ),
                stage="model_thread_start",
            )
            for runtime in runtimes
        )
    )
    thread_ids = tuple(
        _validate_thread_start_response(response, expected_cwd=runtime.home)
        for runtime, response in zip(runtimes, responses, strict=True)
    )
    for observer, thread_id in zip(observers, thread_ids, strict=True):
        observer.arm(thread_id)
    return observers, thread_ids


async def _run_command(
    command: Sequence[str],
    *,
    stdin: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_minimal_environment(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
    except BaseException as failure:
        traceback = failure.__traceback__
        cleanup_task = asyncio.create_task(_terminate_and_reap(process))
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # Preserve the original failure while making cleanup resistant to
                # a repeated cancellation arriving during child reaping.
                continue
            except BaseException:
                break
        if cleanup_task.done():
            try:
                cleanup_task.result()
            except BaseException:
                pass
        raise failure.with_traceback(traceback)
    return process.returncode or 0, stdout, stderr


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    """Terminate, then kill if needed, and always await the child to exit."""
    if process.returncode is not None:
        await process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        await process.wait()
        return
    wait_task = asyncio.create_task(process.wait())
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), PROCESS_TERMINATE_GRACE_S)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await wait_task


def plan_runner_home(target: RunnerTarget) -> tuple[Path, bool]:
    """Choose a unique path locally so cleanup can be registered before creation."""
    path = Path(f"/tmp/ccswitch-codex-spike.{secrets.token_hex(16)}")
    if not _REMOTE_HOME_RE.fullmatch(str(path)):
        raise SpikeConfigurationError("planned runner home failed path validation")
    return path, target.ssh_host is not None


async def provision_runner_home(target: RunnerTarget, home: Path) -> None:
    """Create a pre-registered runner home without returning a new unknown path."""
    if not _REMOTE_HOME_RE.fullmatch(str(home)):
        raise SpikeConfigurationError("runner home is outside the dedicated /tmp prefix")
    if target.ssh_host is None:
        home.mkdir(mode=0o700, parents=False, exist_ok=False)
        return
    code, _, _ = await _run_command(
        build_ssh_command(
            target.ssh_host,
            [
                "mkdir",
                "-m",
                "700",
                "--",
                str(home),
            ],
        )
    )
    if code != 0:
        raise SpikeConfigurationError("remote runner home could not be created")


async def create_runner_runtime(
    target: RunnerTarget,
    *,
    codex_bin: str,
    handler: ServerRequestHandler | None,
    runtimes: list[RunnerRuntime],
) -> RunnerRuntime:
    """Register the process/home cleanup record before any remote mutation."""
    home, is_remote = plan_runner_home(target)
    command, environment = build_runner_process(target, home, codex_bin=codex_bin)
    client = JsonRpcProcess(command, environment, role="runner", handler=handler)
    runtime = RunnerRuntime(target, home, client, is_remote)
    runtimes.append(runtime)
    await provision_runner_home(target, home)
    return runtime


_REMOTE_IDENTITY_PROGRAM = r"""
import hashlib
import pathlib

raw = pathlib.Path('/etc/machine-id').read_bytes().strip()
if len(raw) < 16:
    raise SystemExit(2)
print(hashlib.sha256(b'ccswitch-codex-host-v1\0' + raw).hexdigest())
""".strip()


async def runner_machine_identity(target: RunnerTarget) -> str:
    if target.ssh_host is None:
        return local_machine_identity()
    code, stdout, _ = await _run_command(
        build_ssh_command(
            target.ssh_host,
            ["python3", "-c", _REMOTE_IDENTITY_PROGRAM],
        )
    )
    observed = stdout.decode("ascii", errors="replace").strip()
    if code != 0 or not re.fullmatch(r"[0-9a-f]{64}", observed):
        raise SpikeConfigurationError("remote stable machine identity is unavailable")
    return observed


async def inspect_topology(
    targets: tuple[RunnerTarget, RunnerTarget],
) -> TopologyEvidence:
    authority_identity = local_machine_identity()
    runner_identities = await asyncio.gather(
        *(runner_machine_identity(target) for target in targets)
    )
    return validate_topology(
        authority_identity=authority_identity,
        runner_identities=(runner_identities[0], runner_identities[1]),
    )


async def verify_runner_version(
    target: RunnerTarget,
    *,
    codex_bin: str,
    expected: str,
) -> None:
    """Require every remote runner to expose the exact inspected CLI version."""
    if target.ssh_host is None:
        return
    code, stdout, _ = await _run_command(
        build_ssh_command(
            target.ssh_host,
            [
                "sh",
                "-c",
                'exec env -i HOME="$HOME" PATH="$PATH" "$1" --version',
                "ccswitch-codex-spike",
                codex_bin,
            ],
        )
    )
    observed = stdout.decode("utf-8", errors="replace").strip()
    if code != 0 or observed != expected:
        raise SpikeConfigurationError("a runner lacks the exact supported Codex CLI version")


async def verify_empty_tool_catalog(
    target: RunnerTarget,
    *,
    codex_bin: str,
) -> None:
    """Prove the pinned runner emits an exact empty tool catalog before a live turn."""
    payload = json.dumps(
        {
            "thread": build_model_thread_params(
                Path("/tmp/ccswitch-codex-tool-preflight.placeholder"),
                model_turn_enabled=True,
            ),
            "turn": build_model_turn_params(
                "ccswitch-tool-preflight-placeholder",
                model_turn_enabled=True,
            ),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    command = ["python3", "-c", _TOOL_CATALOG_PREFLIGHT_PROGRAM, codex_bin]
    if target.ssh_host is not None:
        command = build_ssh_command(target.ssh_host, command)
    try:
        code, stdout, _ = await _run_command(
            command,
            stdin=payload,
            timeout=TOOL_CATALOG_PREFLIGHT_TIMEOUT_S,
        )
        result = json.loads(stdout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ProtocolError(
            "runner could not prove an empty outgoing model tool catalog",
            method="turn/start",
            stage="tool_catalog_preflight",
            role="runner",
        ) from exc
    if code != 0 or result != {"empty": True}:
        raise ProtocolError(
            "runner could not prove an empty outgoing model tool catalog",
            method="turn/start",
            stage="tool_catalog_preflight",
            role="runner",
        )


_REMOTE_SCAN_PROGRAM = r"""
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
values = json.loads(sys.stdin.buffer.read())
needles = [value.encode() for value in values]
auth = False
leaks = 0
links = 0
for path in root.rglob('*'):
    if path.is_symlink():
        links += 1
        continue
    if not path.is_file():
        continue
    auth = auth or path.name == 'auth.json'
    data = path.read_bytes()
    if any(needle in data for needle in needles):
        leaks += 1
print(json.dumps({'auth': auth, 'leaks': leaks, 'links': links}, separators=(',', ':')))
""".strip()


async def scan_remote_home(
    target: RunnerTarget,
    home: Path,
    auth_values: RunnerHomeAuthValues,
) -> HomeScan:
    assert target.ssh_host is not None
    if not _REMOTE_HOME_RE.fullmatch(str(home)):
        raise SpikeConfigurationError("refusing to inspect an unsafe remote path")
    # The runner-visible access/account values cross this second authenticated SSH stdin boundary
    # only for an exact byte scan. Refresh tokens are excluded and output is aggregate counts only.
    payload = json.dumps(auth_values.exact_values, separators=(",", ":")).encode("utf-8")
    code, stdout, _ = await _run_command(
        build_ssh_command(
            target.ssh_host,
            ["python3", "-c", _REMOTE_SCAN_PROGRAM, str(home)],
        ),
        stdin=payload,
    )
    if code != 0:
        raise SpikeError("remote temporary runner home could not be inspected")
    try:
        result = json.loads(stdout)
        return HomeScan(bool(result["auth"]), int(result["leaks"]), int(result["links"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SpikeError("remote temporary runner scan returned an invalid result") from exc


async def cleanup_runner(runtime: RunnerRuntime) -> bool:
    if not _REMOTE_HOME_RE.fullmatch(str(runtime.home)):
        raise SpikeConfigurationError("refusing to clean an unsafe runner path")
    if runtime.is_remote:
        assert runtime.target.ssh_host is not None
        code, _, _ = await _run_command(
            build_ssh_command(
                runtime.target.ssh_host,
                [
                    "sh",
                    "-c",
                    'if test ! -e "$1"; then exit 0; fi; find "$1" -depth -delete',
                    "ccswitch-codex-spike",
                    str(runtime.home),
                ],
            )
        )
        runtime.cleaned = code == 0
        return runtime.cleaned
    if not runtime.home.exists():
        runtime.cleaned = True
        return True
    shutil.rmtree(runtime.home)
    runtime.cleaned = not runtime.home.exists()
    return runtime.cleaned


async def finalize_runtimes(
    runtimes: Sequence[RunnerRuntime],
    authority: JsonRpcProcess,
    *,
    processes_closed: bool,
) -> bool:
    """Close processes and clean every known home even if a close operation fails."""
    close_failed = False
    if not processes_closed:
        close_results = await asyncio.gather(
            *(runtime.process.close() for runtime in runtimes),
            authority.close(),
            return_exceptions=True,
        )
        close_failed = any(isinstance(result, BaseException) for result in close_results)

    cleanup_results = await asyncio.gather(
        *(cleanup_runner(runtime) for runtime in runtimes),
        return_exceptions=True,
    )
    cleanup_complete = len(cleanup_results) == len(runtimes) and all(
        result is True for result in cleanup_results
    )
    if close_failed:
        raise SpikeError("an app-server close failed after temporary-home cleanup")
    if not cleanup_complete:
        raise SpikeError("one or more temporary runner homes could not be removed")
    return True


async def finalize_preserving_primary(
    runtimes: Sequence[RunnerRuntime],
    authority: JsonRpcProcess,
    *,
    processes_closed: bool,
    primary_failure: BaseException | None,
) -> bool:
    """Finalize all runtimes without replacing an active body failure."""
    try:
        return await finalize_runtimes(
            runtimes,
            authority,
            processes_closed=processes_closed,
        )
    except BaseException:
        if primary_failure is None:
            raise
        primary_failure.add_note(_CLEANUP_FAILED_NOTE)
        return False


def parse_runners(value: str | None) -> tuple[RunnerTarget, RunnerTarget]:
    names = [item.strip() for item in (value or DEFAULT_RUNNERS).split(",") if item.strip()]
    if len(names) != 2 or len(set(names)) != 2:
        raise SpikeConfigurationError("exactly two distinct runner hosts are required")
    targets: list[RunnerTarget] = []
    for name in names:
        if name == "local":
            targets.append(RunnerTarget(name=socket.gethostname().split(".")[0], ssh_host=None))
        else:
            targets.append(RunnerTarget(name=name, ssh_host=name))
    if sum(target.ssh_host is None for target in targets) != 1:
        raise SpikeConfigurationError("exactly one local authority runner must be configured")
    return targets[0], targets[1]


@dataclass(frozen=True)
class SpikeEvidence:
    cli_version: str
    schema_sha256: str
    runner_count: int
    distinct_runner_hosts: bool
    authority_on_local_runner: bool
    topology_sha256: str
    healthy_read_only_calls: bool
    refresh_callback_observed: bool
    refresh_calls: int
    credential_generation: int
    runner_homes_clean: bool
    redaction_clean: bool
    cleanup_complete: bool
    release_gate: str
    protocol_methods: tuple[str, ...] = field(default_factory=tuple)
    experimental_api_required: bool = True
    model_turn_trigger_used: bool = False
    empty_model_tool_catalog_preflight: bool = False
    tool_free_exact_ok_turns_completed: bool = False

    def to_json(self, secrets: SecretValues | None = None) -> str:
        serialized = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        clean = sanitize_text(serialized, secrets)
        if secrets is not None and secrets.redaction_values.find_leaks(clean):
            raise SpikeError("sanitized evidence still contains a sensitive value")
        return clean


async def _verify_read_only(
    runner: JsonRpcProcess,
    *,
    expected_account_id: str,
    stage: ProtocolStage,
    required_model: str | None = None,
) -> None:
    method: ProtocolMethod = "account/read"
    try:
        account = await runner.request(method, {"refreshToken": False}, stage=stage)
        validate_account_response(account)
        method = "model/list"
        models = await runner.request(method, {}, stage=stage)
        validate_model_response(models, required_model=required_model)
        method = "account/rateLimits/read"
        rate_limits = await runner.request(method, {}, stage=stage)
        validate_rate_limit_response(rate_limits, expected_account_id=expected_account_id)
    except ProtocolError as exc:
        raise exc.with_context(method=method, stage=stage, role="runner")


async def _capture_refresh_outcome(operation: Awaitable[Any]) -> Any | BaseException:
    """Retain the exact child failure object, including CancelledError identity."""
    try:
        return await operation
    except BaseException as exc:
        return exc


def _classify_refresh_trigger_failure(
    coalescer: RefreshCoalescer,
    *,
    runner_count: int,
    checkpoint: tuple[int, int, int],
    trigger_method: ProtocolMethod,
) -> ProtocolError:
    prior_callbacks, prior_refreshes, prior_generation = checkpoint
    callback_delta = coalescer.callback_count - prior_callbacks
    refresh_delta = coalescer.refresh_calls - prior_refreshes
    generation_delta = coalescer.generation - prior_generation
    anomalous = (
        callback_delta < 0
        or refresh_delta < 0
        or generation_delta < 0
        or callback_delta > runner_count
        or refresh_delta > 1
        or generation_delta > 1
        or generation_delta > refresh_delta
        or (callback_delta == 0 and (refresh_delta != 0 or generation_delta != 0))
    )
    if anomalous:
        message = "refresh trigger observed incomplete or anomalous callback state"
    elif callback_delta == 0:
        message = "refresh trigger failed before any runner callback"
    elif callback_delta < runner_count:
        message = "refresh trigger failed after only some runner callbacks"
    elif (
        callback_delta == runner_count
        and refresh_delta == 1
        and generation_delta == 1
    ):
        message = "refresh trigger failed after callback refresh advanced"
    else:
        message = "refresh trigger observed incomplete or anomalous callback state"
    return ProtocolError(
        message,
        method=trigger_method,
        stage="refresh_trigger",
        role="runner",
    )


async def _run_refresh_triggers(
    runners: Sequence[JsonRpcProcess],
    *,
    coalescer: RefreshCoalescer,
    trigger_operations: Sequence[Awaitable[Any]] | None = None,
    trigger_method: ProtocolMethod = "account/rateLimits/read",
) -> tuple[Any, ...]:
    """Prefer a refresh callback root failure over its downstream request error."""
    if trigger_operations is not None and len(trigger_operations) != len(runners):
        raise ProtocolError(
            "refresh trigger operation count did not match runners",
            method=trigger_method,
            stage="refresh_trigger",
            role="runner",
        )
    checkpoints = [runner.callback_checkpoint() for runner in runners]
    coalescer_checkpoint = (
        coalescer.callback_count,
        coalescer.refresh_calls,
        coalescer.generation,
    )
    operations = trigger_operations or tuple(
        runner.request(
            "account/rateLimits/read",
            {},
            stage="refresh_trigger",
        )
        for runner in runners
    )
    outcomes = await asyncio.gather(
        *(_capture_refresh_outcome(operation) for operation in operations),
    )
    # App-server's host-refresh contract places the callback before retrying the original
    # request. Completed downstream outcomes are therefore the protocol fence; the bounded
    # quiet interval below closes only local stdout-reader/task-registration scheduling races.
    callback_failure_slices = await asyncio.gather(
        *(
            runner.callback_failures_after_quiescence(checkpoint)
            for runner, checkpoint in zip(runners, checkpoints, strict=True)
        )
    )
    for failures in callback_failure_slices:
        for failure in failures:
            if isinstance(failure, asyncio.CancelledError):
                raise failure
    for outcome in outcomes:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
    for failures in callback_failure_slices:
        if failures:
            raise failures[0]
    for outcome in outcomes:
        if isinstance(outcome, NativeRequestError):
            raise _classify_refresh_trigger_failure(
                coalescer,
                runner_count=len(runners),
                checkpoint=coalescer_checkpoint,
                trigger_method=trigger_method,
            )
        if isinstance(outcome, BaseException):
            raise outcome
    return tuple(outcomes)


async def _run_one_model_turn(
    runner: JsonRpcProcess,
    observer: ModelTurnObserver,
    *,
    model_turn_enabled: bool,
) -> ModelTurnResult:
    _require_model_turn_opt_in(model_turn_enabled)
    thread_id = observer.thread_id
    if thread_id is None:
        raise ProtocolError(
            "model turn observer was not armed",
            method="turn/start",
            stage="refresh_trigger",
            role="runner",
        )
    try:
        response = await runner.request(
            "turn/start",
            build_model_turn_params(
                thread_id,
                model_turn_enabled=model_turn_enabled,
            ),
            stage="refresh_trigger",
        )
        observer.bind_turn_response(response)
        return await observer.wait_for_completion()
    except BaseException:
        observer.discard()
        raise


async def _run_model_turn_refresh_triggers(
    runtimes: Sequence[RunnerRuntime],
    observers: Sequence[ModelTurnObserver],
    *,
    coalescer: RefreshCoalescer,
    model_turn_enabled: bool,
    empty_tool_catalog_verified: bool = False,
) -> tuple[ModelTurnResult, ...]:
    """Start exactly one fixed turn per runner and validate both safe completions."""
    _require_model_turn_opt_in(model_turn_enabled)
    if not empty_tool_catalog_verified:
        raise ProtocolError(
            "refusing model turn before empty tool catalog preflight",
            method="turn/start",
            stage="tool_catalog_preflight",
            role="runner",
        )
    if len(runtimes) != len(observers):
        raise ProtocolError(
            "model turn observer count did not match runners",
            method="turn/start",
            stage="refresh_trigger",
            role="runner",
        )
    outcomes = await _run_refresh_triggers(
        [runtime.process for runtime in runtimes],
        coalescer=coalescer,
        trigger_operations=tuple(
            _run_one_model_turn(
                runtime.process,
                observer,
                model_turn_enabled=model_turn_enabled,
            )
            for runtime, observer in zip(runtimes, observers, strict=True)
        ),
        trigger_method="turn/start",
    )
    if any(
        not isinstance(outcome, ModelTurnResult) or not outcome.tool_free_exact_ok
        for outcome in outcomes
    ):
        raise ProtocolError(
            "model turns did not both complete tool-free with exact OK output",
            method="turn/completed",
            stage="refresh_trigger",
            role="runner",
        )
    return tuple(outcome for outcome in outcomes if isinstance(outcome, ModelTurnResult))


async def run_spike(
    *,
    codex_bin: str,
    authority_home: Path,
    targets: tuple[RunnerTarget, RunnerTarget],
    model_turn_enabled: bool = False,
) -> tuple[SpikeEvidence, SecretValues]:
    schema = inspect_schema(codex_bin)
    topology = await inspect_topology(targets)
    await asyncio.gather(
        *(
            verify_runner_version(
                target,
                codex_bin=codex_bin,
                expected=schema.cli_version,
            )
            for target in targets
        )
    )
    empty_tool_catalog_verified = False
    if model_turn_enabled:
        preflight_results = await asyncio.gather(
            *(
                verify_empty_tool_catalog(target, codex_bin=codex_bin)
                for target in targets
            ),
            return_exceptions=True,
        )
        for preflight_result in preflight_results:
            if isinstance(preflight_result, asyncio.CancelledError):
                raise preflight_result
            if isinstance(preflight_result, BaseException):
                raise preflight_result
        empty_tool_catalog_verified = True
    authority_command = [codex_bin, "app-server", "--stdio"]
    authority = JsonRpcProcess(
        authority_command,
        _minimal_environment(codex_home=authority_home),
        role="authority",
    )
    runners: list[RunnerRuntime] = []
    all_secrets: SecretValues | None = None
    homes_clean = False
    cleanup_complete = False
    stderr_clean = False
    coalescer: RefreshCoalescer | None = None
    model_turns_completed = False
    processes_closed = False
    try:
        await authority.start(experimental_api=False)
        try:
            authority_account = await authority.request(
                "account/read",
                {"refreshToken": False},
                stage="authority_account_read",
            )
            validate_account_response(authority_account)
        except ProtocolError as exc:
            raise exc.with_context(
                method="account/read",
                stage="authority_account_read",
                role="authority",
            )
        initial = load_managed_secrets(authority_home)
        all_secrets = initial
        coalescer = RefreshCoalescer(authority, authority_home, initial)

        for target in targets:
            runtime = await create_runner_runtime(
                target,
                codex_bin=codex_bin,
                handler=coalescer.register_runner(),
                runtimes=runners,
            )
            await runtime.process.start(experimental_api=True)

        await asyncio.gather(
            *(
                runtime.process.external_login(initial, stage="initial_login")
                for runtime in runners
            )
        )
        await asyncio.gather(
            *(
                _verify_read_only(
                    runtime.process,
                    expected_account_id=initial.account_id,
                    stage="initial_read_only",
                    required_model=MODEL_TURN_MODEL if model_turn_enabled else None,
                )
                for runtime in runners
            )
        )

        observers, thread_ids = await _start_model_turn_threads(
            runners,
            model_turn_enabled=model_turn_enabled,
            empty_tool_catalog_verified=empty_tool_catalog_verified,
        )
        for thread_id in thread_ids:
            all_secrets = all_secrets.with_native_id(thread_id)

        invalid = replace(initial, access_token=initial.invalid_access_token)
        await asyncio.gather(
            *(
                runtime.process.external_login(invalid, stage="invalid_login")
                for runtime in runners
            )
        )
        if model_turn_enabled:
            turn_results = await _run_model_turn_refresh_triggers(
                runners,
                observers,
                coalescer=coalescer,
                model_turn_enabled=model_turn_enabled,
                empty_tool_catalog_verified=empty_tool_catalog_verified,
            )
            for result in turn_results:
                for native_id in result.native_ids:
                    all_secrets = all_secrets.with_native_id(native_id)
            model_turns_completed = len(turn_results) == len(runners)
        else:
            await _run_refresh_triggers(
                [runtime.process for runtime in runners],
                coalescer=coalescer,
            )
        if not coalescer.has_exact_callback_coverage(
            expected_runner_count=len(runners),
        ):
            raise SpikeError(
                "each runner did not complete exactly one external-token refresh callback"
            )
        if coalescer.refresh_calls != 1 or coalescer.generation != 1:
            raise SpikeError("runner refresh callbacks did not coalesce to one newer generation")
        if coalescer.current.access_token in {
            initial.access_token,
            initial.invalid_access_token,
        }:
            raise SpikeError("corrupted runners did not receive an advanced access token")
        all_secrets = all_secrets.merge(coalescer.current)
        await asyncio.gather(
            *(
                _verify_read_only(
                    runtime.process,
                    expected_account_id=coalescer.current.account_id,
                    stage="recovery_read_only",
                )
                for runtime in runners
            )
        )

        for runtime in runners:
            await runtime.process.close()
        await authority.close()
        processes_closed = True

        for runtime in runners:
            runtime.process.assert_background_clean()
        authority.assert_background_clean()

        scans: list[HomeScan] = []
        runner_home_auth_values = all_secrets.runner_home_auth_values
        for runtime in runners:
            if runtime.is_remote:
                scans.append(
                    await scan_remote_home(
                        runtime.target,
                        runtime.home,
                        runner_home_auth_values,
                    )
                )
            else:
                scans.append(scan_home(runtime.home, runner_home_auth_values))
        homes_clean = all(scan.clean for scan in scans)
        stderr_clean = all(
            not all_secrets.redaction_values.find_leaks(runtime.process.captured_stderr)
            for runtime in runners
        ) and not all_secrets.redaction_values.find_leaks(authority.captured_stderr)
        if not homes_clean:
            raise SpikeError("a temporary runner home retained auth material or an auth.json")
        if not stderr_clean:
            raise SpikeError("a child process wrote auth material to captured stderr")
    except BaseException as primary_failure:
        await finalize_preserving_primary(
            runners,
            authority,
            processes_closed=processes_closed,
            primary_failure=primary_failure,
        )
        raise
    else:
        cleanup_complete = await finalize_preserving_primary(
            runners,
            authority,
            processes_closed=processes_closed,
            primary_failure=None,
        )

    assert all_secrets is not None and coalescer is not None
    evidence = SpikeEvidence(
        cli_version=schema.cli_version,
        schema_sha256=schema.schema_sha256,
        runner_count=len(runners),
        distinct_runner_hosts=topology.distinct_runner_hosts,
        authority_on_local_runner=topology.authority_on_local_runner,
        topology_sha256=topology.topology_sha256,
        healthy_read_only_calls=True,
        refresh_callback_observed=coalescer.has_exact_callback_coverage(
            expected_runner_count=len(runners),
        ),
        refresh_calls=coalescer.refresh_calls,
        credential_generation=coalescer.generation,
        runner_homes_clean=homes_clean,
        redaction_clean=stderr_clean,
        cleanup_complete=cleanup_complete,
        release_gate="pass" if cleanup_complete else "fail",
        protocol_methods=schema.methods,
        experimental_api_required=schema.experimental_api_required,
        model_turn_trigger_used=model_turn_enabled,
        empty_model_tool_catalog_preflight=empty_tool_catalog_verified,
        tool_free_exact_ok_turns_completed=model_turns_completed,
    )
    return evidence, all_secrets


async def async_main() -> int:
    require_opt_in(
        flag=os.environ.get(OPT_IN_FLAG),
        account=os.environ.get(ACCOUNT_ALIAS_ENV),
    )
    targets = parse_runners(os.environ.get(RUNNERS_ENV))
    model_turn_enabled = model_turn_opted_in(os.environ.get(MODEL_TURN_OPT_IN_FLAG))
    codex_bin = os.environ.get(CODEX_BIN_ENV, "codex")
    authority_home = Path(
        os.environ.get(
            AUTHORITY_HOME_ENV,
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        )
    ).expanduser()
    secrets: SecretValues | None = None
    try:
        evidence, secrets = await run_spike(
            codex_bin=codex_bin,
            authority_home=authority_home,
            targets=targets,
            model_turn_enabled=model_turn_enabled,
        )
        output = evidence.to_json(secrets)
        if secrets.redaction_values.find_leaks(output):
            raise SpikeError("final evidence failed exact-value redaction")
        print(output)
        return 0 if evidence.release_gate == "pass" else 1
    except (SpikeError, OSError, subprocess.SubprocessError) as exc:
        print(failure_json(exc, secrets))
        return 1


def main() -> int:
    try:
        return asyncio.run(async_main())
    except SpikeError as exc:
        print(failure_json(exc))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"release_gate": "fail", "error": "interrupted"}, sort_keys=True))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
