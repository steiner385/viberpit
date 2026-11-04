"""Shared plumbing for the `winrun` / `winsync` host helpers (stdlib only).

Installed beside them (``~/.local/bin/_winvm.py``): a script's own directory is
``sys.path[0]``, so ``import _winvm`` resolves to this file. See
docs/runbooks/windows-vm-on-a-session-runner.md.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys

# The same setting as server/config.py's `windows_vm_ssh_host` (VIBERPIT_WINDOWS_VM_SSH_HOST,
# default "win11"). Read straight from the environment: these host helpers must never import
# server code. Change the name or default in both places together.
HOST_ENV = "VIBERPIT_WINDOWS_VM_SSH_HOST"
DEFAULT_HOST = "win11"

# Fail fast on a stopped VM, and never prompt for a password or host key.
SSH_OPTS = ("-o", "ConnectTimeout=5", "-o", "BatchMode=yes")

EXIT_UNREACHABLE = 255  # ssh's own code for "could not connect"

# Every script sent to the VM starts with this, then a `# <tool>:<phase>` marker line.
_PREAMBLE = "$ProgressPreference = 'SilentlyContinue'\n"


def ssh_host() -> str:
    return os.environ.get(HOST_ENV) or DEFAULT_HOST


def b64_utf16(script: str) -> str:
    """PowerShell's ``-EncodedCommand`` form: base64 of UTF-16LE. Only [A-Za-z0-9+/=]."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def b64_utf8(data: bytes | str) -> str:
    return base64.b64encode(data.encode("utf-8") if isinstance(data, str) else data).decode("ascii")


# Windows caps a command line at 32767 chars and the encoded script rides on one (twice: the VM's
# default shell starts the inner powershell). Leave headroom for the rest of the line.
MAX_ENCODED_CHARS = 30000


class ScriptTooLong(ValueError):
    """The encoded script would not fit on a Windows command line."""


def run_ps(script: str, *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a PowerShell script in the VM over ssh, sent as ``-EncodedCommand``.

    The ssh command line carries only base64, so no quoting layer (ssh, the VM's default shell,
    CreateProcess) can mangle it. Raises ``ScriptTooLong`` before connecting if it cannot fit, and
    ``subprocess.TimeoutExpired`` past ``timeout``.
    """
    encoded = b64_utf16(_PREAMBLE + script)
    if len(encoded) > MAX_ENCODED_CHARS:
        raise ScriptTooLong(f"{len(encoded)} encoded chars, limit {MAX_ENCODED_CHARS}")
    argv = ["ssh", *SSH_OPTS, ssh_host(), "powershell", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encoded]
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)


def scp_to(local: str, remote_name: str, *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Copy a local file into the VM user's home directory as ``remote_name``."""
    argv = ["scp", "-q", *SSH_OPTS, local, f"{ssh_host()}:{remote_name}"]
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)


_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
                  re.S)
# Anything under ~/.ssh or ~/.config/viberpit, plus the two secret files by name wherever they live.
_SECRET_PATH = re.compile(r"[^\s\"']*(?:/\.ssh/|/\.config/viberpit/|win11_ed25519|win11-vm\.secret)"
                          r"[^\s\"']*")


def redact(text: str) -> str:
    """Strip private-key material and secret file paths from an ssh diagnostic."""
    return _SECRET_PATH.sub("<redacted>", _PEM.sub("<redacted key>", text))


def ssh_diagnostic(proc: subprocess.CompletedProcess) -> str:
    """The last non-empty stderr line of a failed ssh/scp call, redacted, on one line."""
    lines = [ln.strip() for ln in redact(proc.stderr.decode("utf-8", "replace")).splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit {proc.returncode}"


def help_description(doc: str) -> str:
    """A helper's module docstring minus its usage paragraph (argparse prints usage itself)."""
    summary, _usage, rest = doc.split("\n\n", 2)
    return f"{summary}\n\n{rest}"


def die(tool: str, message: str, code: int) -> int:
    print(f"{tool}: {message}", file=sys.stderr)
    return code


def unreachable(tool: str, proc: subprocess.CompletedProcess) -> int:
    return die(tool, f"cannot reach {ssh_host()} ({ssh_diagnostic(proc)})", EXIT_UNREACHABLE)
