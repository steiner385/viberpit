#!/usr/bin/env python3
"""Opt-in, read-only Codex external-auth compatibility spike.

The harness starts one managed-auth authority app-server locally and two runner
app-servers with separate temporary ``CODEX_HOME`` directories. Tokens and the
native ChatGPT account id exist only in this process and JSON-RPC stdin pipes.
Raw protocol frames and child stderr are never forwarded to this script's
stdout/stderr.

No thread or model turn is created. The only network-backed calls are account,
model, rate-limit, and managed token refresh operations exposed by app-server.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.audit import redact_text  # noqa: E402 -- repo root is added for direct script execution


OPT_IN_FLAG = "CCSWITCH_TEST_CODEX_EXTERNAL_AUTH"
ACCOUNT_ALIAS_ENV = "CCSWITCH_TEST_CODEX_ACCOUNT"
RUNNERS_ENV = "CCSWITCH_TEST_CODEX_RUNNERS"
AUTHORITY_HOME_ENV = "CCSWITCH_TEST_CODEX_AUTHORITY_HOME"
CODEX_BIN_ENV = "CCSWITCH_CODEX_BIN"
DEFAULT_RUNNERS = "local,rocky"
REDACTED = "[REDACTED]"
REQUEST_TIMEOUT_S = 45.0
PROCESS_TERMINATE_GRACE_S = 2.0
_CLEANUP_FAILED_NOTE = "ccswitch spike cleanup also failed; details suppressed"

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
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
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
    "ChatgptAuthTokensRefreshParams.json",
    "ChatgptAuthTokensRefreshResponse.json",
    "ClientRequest.json",
    "ServerRequest.json",
)
_REQUIRED_SCHEMA_MARKERS = (
    "experimentalApi",
    "chatgptAuthTokens",
    "account/chatgptAuthTokens/refresh",
    "account/read",
    "account/rateLimits/read",
    "model/list",
)


class SpikeError(RuntimeError):
    """A sanitized spike failure."""


class SpikeConfigurationError(SpikeError):
    """The explicit live-test gate or safe configuration is missing."""


class ProtocolError(SpikeError):
    """The app-server protocol failed without retaining raw frame content."""


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


def validate_model_response(response: Any) -> None:
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
class SecretValues:
    """Exact live values that must never reach a user-visible sink."""

    access_token: str
    refresh_token: str
    account_id: str
    invalid_access_token: str
    native_ids: tuple[str, ...] = ()
    prior_values: tuple[str, ...] = ()
    refresh_history: tuple[str, ...] = ()

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
    def values(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.access_token,
                    self.refresh_token,
                    self.account_id,
                    self.invalid_access_token,
                    *self.native_ids,
                    *self.prior_values,
                )
            )
        )

    @property
    def runner_values(self) -> tuple[str, ...]:
        """Values a runner actually received; deliberately excludes refresh tokens."""
        return tuple(
            value
            for value in self.values
            if value != self.refresh_token
            and value not in self.refresh_history
        )

    def find_leaks(self, text: str | bytes) -> tuple[str, ...]:
        raw = text if isinstance(text, bytes) else text.encode("utf-8", errors="replace")
        labels: list[str] = []
        for index, value in enumerate(self.values):
            if value.encode("utf-8") in raw:
                labels.append(f"sensitive-value-{index + 1}")
        return tuple(labels)

    def with_native_id(self, native_id: str) -> SecretValues:
        if not native_id:
            return self
        return replace(self, native_ids=(*self.native_ids, native_id))

    def merge(self, newer: SecretValues) -> SecretValues:
        return replace(
            newer,
            native_ids=tuple(dict.fromkeys((*self.native_ids, *newer.native_ids))),
            prior_values=tuple(dict.fromkeys((*self.values, *newer.prior_values))),
            refresh_history=tuple(
                dict.fromkeys(
                    (*self.refresh_history, self.refresh_token, *newer.refresh_history)
                )
            ),
        )


def sanitize_text(text: str, secrets: SecretValues | None = None) -> str:
    """Use the application's canonical redactor plus exact live-value replacement."""
    clean = redact_text(text)
    if secrets is not None:
        for value in sorted(secrets.values, key=len, reverse=True):
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
    secrets: SecretValues,
    *,
    values: Sequence[str] | None = None,
) -> HomeScan:
    """Scan a temporary local runner home without returning file names/content."""
    needles = tuple(values) if values is not None else secrets.values
    encoded = tuple(value.encode("utf-8") for value in needles)
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
            if candidate is not None and not _HOST_RE.fullmatch(candidate):
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
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        target.ssh_host,
        "--",
        "sh",
        "-c",
        (
            'exec env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" '
            'CODEX_HOME="$1" "$2" app-server --stdio'
        ),
        "ccswitch-codex-spike",
        str(home),
        codex_bin,
    ]
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
        ),
        experimental_api_required=True,
    )


ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class JsonRpcProcess:
    """Small spike-only JSONL client that never emits raw frames or child stderr."""

    def __init__(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        *,
        handler: ServerRequestHandler | None = None,
    ) -> None:
        self._command = list(command)
        self._environment = dict(environment)
        self._handler = handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._stderr = bytearray()
        self._background_errors: list[BaseException] = []

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
        )
        await self.notify("initialized", {})

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                try:
                    frame = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ProtocolError("app-server emitted a malformed JSONL frame") from exc
                if "id" in frame and "method" in frame:
                    asyncio.create_task(self._answer_server_request(frame))
                    continue
                if "id" in frame:
                    request_id = frame.get("id")
                    future = self._pending.pop(request_id, None)
                    if future is None:
                        raise ProtocolError("app-server replied with an unknown request id")
                    if "error" in frame:
                        future.set_exception(ProtocolError("app-server request returned an error"))
                    else:
                        future.set_result(frame.get("result"))
            if self._pending:
                raise ProtocolError("app-server closed with pending requests")
        except BaseException as exc:
            self._background_errors.append(exc)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ProtocolError("app-server reader stopped"))
            self._pending.clear()

    async def _answer_server_request(self, frame: dict[str, Any]) -> None:
        try:
            if self._handler is None:
                raise ProtocolError("unexpected app-server request")
            result = await self._handler(frame)
            await self._write({"id": frame["id"], "result": result})
        except BaseException as exc:
            self._background_errors.append(exc)
            await self._write(
                {
                    "id": frame.get("id"),
                    "error": {"code": -32603, "message": "ccswitch spike rejected request"},
                }
            )

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

    async def request_frame(self, frame: dict[str, Any]) -> Any:
        request_id = frame.get("id")
        if not isinstance(request_id, int):
            raise ProtocolError("client request lacks an integer id")
        if request_id in self._pending:
            raise ProtocolError("duplicate client request id")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        await self._write(frame)
        try:
            return await asyncio.wait_for(future, REQUEST_TIMEOUT_S)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ProtocolError("app-server request timed out") from exc

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        return await self.request_frame({"id": request_id, "method": method, "params": dict(params)})

    async def external_login(self, secrets: SecretValues) -> None:
        request_id = self._next_id
        self._next_id += 1
        await self.request_frame(build_external_login_frame(request_id, secrets))

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._write({"method": method, "params": dict(params)})

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

    def assert_background_clean(self) -> None:
        if self._background_errors:
            first = self._background_errors[0]
            if isinstance(first, SpikeError):
                raise first
            raise ProtocolError("app-server background task failed") from first


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
        self.generation = 0
        self.refresh_calls = 0
        self.callback_count = 0

    @property
    def current(self) -> SecretValues:
        return self._current

    async def handle(self, frame: dict[str, Any]) -> dict[str, Any]:
        if frame.get("method") != "account/chatgptAuthTokens/refresh":
            raise ProtocolError("runner requested an unsupported experimental method")
        params = frame.get("params")
        if not isinstance(params, dict) or params.get("reason") != "unauthorized":
            raise ProtocolError("runner refresh request has an invalid reason")
        previous = params.get("previousAccountId")
        if previous not in (None, self._current.account_id):
            raise ProtocolError("runner refresh request is for a different account")
        self.callback_count += 1
        rejected_generation = 0
        async with self._lock:
            if self.generation <= rejected_generation:
                self.refresh_calls += 1
                account_response = await self._authority.request(
                    "account/read", {"refreshToken": True}
                )
                validate_account_response(account_response)
                newer = load_managed_secrets(self._authority_home)
                if newer.account_id != self._current.account_id:
                    raise ProtocolError("authority refresh changed the native account")
                if newer.access_token in {
                    self._current.access_token,
                    self._current.invalid_access_token,
                }:
                    raise ProtocolError("authority refresh did not advance the managed access token")
                self._current = self._current.merge(newer)
                self.generation += 1
            current = self._current
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
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            target.ssh_host,
            "--",
            "mkdir",
            "-m",
            "700",
            "--",
            str(home),
        ]
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
    client = JsonRpcProcess(command, environment, handler=handler)
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
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            target.ssh_host,
            "--",
            "python3",
            "-c",
            _REMOTE_IDENTITY_PROGRAM,
        ]
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
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            target.ssh_host,
            "--",
            "sh",
            "-c",
            'exec env -i HOME="$HOME" PATH="$PATH" "$1" --version',
            "ccswitch-codex-spike",
            codex_bin,
        ]
    )
    observed = stdout.decode("utf-8", errors="replace").strip()
    if code != 0 or observed != expected:
        raise SpikeConfigurationError("a runner lacks the exact supported Codex CLI version")


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


async def scan_remote_home(target: RunnerTarget, home: Path, secrets: SecretValues) -> HomeScan:
    assert target.ssh_host is not None
    if not _REMOTE_HOME_RE.fullmatch(str(home)):
        raise SpikeConfigurationError("refusing to inspect an unsafe remote path")
    # Refresh tokens are intentionally excluded: they were never transmitted to the runner.
    payload = json.dumps(secrets.runner_values, separators=(",", ":")).encode("utf-8")
    code, stdout, _ = await _run_command(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            target.ssh_host,
            "--",
            "python3",
            "-c",
            _REMOTE_SCAN_PROGRAM,
            str(home),
        ],
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
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                runtime.target.ssh_host,
                "--",
                "sh",
                "-c",
                'if test ! -e "$1"; then exit 0; fi; find "$1" -depth -delete',
                "ccswitch-codex-spike",
                str(runtime.home),
            ]
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

    def to_json(self, secrets: SecretValues | None = None) -> str:
        serialized = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        clean = sanitize_text(serialized, secrets)
        if secrets is not None and secrets.find_leaks(clean):
            raise SpikeError("sanitized evidence still contains a sensitive value")
        return clean


async def _verify_read_only(runner: JsonRpcProcess, *, expected_account_id: str) -> None:
    account = await runner.request("account/read", {"refreshToken": False})
    validate_account_response(account)
    models = await runner.request("model/list", {})
    validate_model_response(models)
    rate_limits = await runner.request("account/rateLimits/read", {})
    validate_rate_limit_response(rate_limits, expected_account_id=expected_account_id)


async def run_spike(
    *,
    codex_bin: str,
    authority_home: Path,
    targets: tuple[RunnerTarget, RunnerTarget],
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
    authority_command = [codex_bin, "app-server", "--stdio"]
    authority = JsonRpcProcess(
        authority_command,
        _minimal_environment(codex_home=authority_home),
    )
    runners: list[RunnerRuntime] = []
    all_secrets: SecretValues | None = None
    homes_clean = False
    cleanup_complete = False
    stderr_clean = False
    coalescer: RefreshCoalescer | None = None
    processes_closed = False
    try:
        await authority.start(experimental_api=False)
        authority_account = await authority.request("account/read", {"refreshToken": False})
        validate_account_response(authority_account)
        initial = load_managed_secrets(authority_home)
        all_secrets = initial
        coalescer = RefreshCoalescer(authority, authority_home, initial)

        for target in targets:
            runtime = await create_runner_runtime(
                target,
                codex_bin=codex_bin,
                handler=coalescer.handle,
                runtimes=runners,
            )
            await runtime.process.start(experimental_api=True)

        await asyncio.gather(*(runtime.process.external_login(initial) for runtime in runners))
        await asyncio.gather(
            *(
                _verify_read_only(
                    runtime.process,
                    expected_account_id=initial.account_id,
                )
                for runtime in runners
            )
        )

        invalid = replace(initial, access_token=initial.invalid_access_token)
        await asyncio.gather(*(runtime.process.external_login(invalid) for runtime in runners))
        await asyncio.gather(
            *(runtime.process.request("account/rateLimits/read", {}) for runtime in runners)
        )
        if coalescer.callback_count != 2:
            raise SpikeError("both runners did not request external-token refresh")
        if coalescer.refresh_calls != 1 or coalescer.generation != 1:
            raise SpikeError("runner refresh callbacks did not coalesce to one newer generation")
        if coalescer.current.access_token in {
            initial.access_token,
            initial.invalid_access_token,
        }:
            raise SpikeError("corrupted runners did not receive an advanced access token")
        all_secrets = initial.merge(coalescer.current)
        await asyncio.gather(
            *(
                _verify_read_only(
                    runtime.process,
                    expected_account_id=coalescer.current.account_id,
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
        for runtime in runners:
            if runtime.is_remote:
                scans.append(await scan_remote_home(runtime.target, runtime.home, all_secrets))
            else:
                scans.append(
                    scan_home(
                        runtime.home,
                        all_secrets,
                        values=all_secrets.runner_values,
                    )
                )
        homes_clean = all(scan.clean for scan in scans)
        stderr_clean = all(
            not all_secrets.find_leaks(runtime.process.captured_stderr) for runtime in runners
        ) and not all_secrets.find_leaks(authority.captured_stderr)
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
        refresh_callback_observed=coalescer.callback_count == len(runners),
        refresh_calls=coalescer.refresh_calls,
        credential_generation=coalescer.generation,
        runner_homes_clean=homes_clean,
        redaction_clean=stderr_clean,
        cleanup_complete=cleanup_complete,
        release_gate="pass" if cleanup_complete else "fail",
        protocol_methods=schema.methods,
        experimental_api_required=schema.experimental_api_required,
    )
    return evidence, all_secrets


async def async_main() -> int:
    require_opt_in(
        flag=os.environ.get(OPT_IN_FLAG),
        account=os.environ.get(ACCOUNT_ALIAS_ENV),
    )
    targets = parse_runners(os.environ.get(RUNNERS_ENV))
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
        )
        output = evidence.to_json(secrets)
        if secrets.find_leaks(output):
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
