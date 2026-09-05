#!/usr/bin/env python3
"""Regenerate the pinned Codex app-server contract fixtures from a local binary.

The fixtures under ``server/tests/fixtures/codex_app_server/<version>/`` are the
reviewable record of the exact ``codex app-server generate-json-schema
--experimental`` output that ``server/codex/schema.py`` pins. This script is the
only sanctioned way to move them.

Safety properties:

* the generator runs against a **disposable temporary ``CODEX_HOME``**, never the
  user's — no credential cache, history, or configuration is read or written;
* the child process gets a minimal environment (no ``CODEX_*`` inherited except
  the one this script sets, no auth material);
* nothing is written if any candidate document contains a secret-shaped value,
  as judged by ``server/codex/redaction.py``;
* ``--check`` reports drift without touching the working tree, so CI or an
  operator can ask "has the boundary moved?" without a diff.

Usage::

    python scripts/refresh_codex_contract_fixtures.py --check
    python scripts/refresh_codex_contract_fixtures.py --write
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.codex import schema  # noqa: E402 -- repo root added for direct execution
from server.codex.schema import contains_unexpected_secret_shape  # noqa: E402

CODEX_BIN_ENV = "CCSWITCH_CODEX_BIN"
FIXTURE_PARENT = _REPO_ROOT / "server" / "tests" / "fixtures" / "codex_app_server"


class RefreshError(RuntimeError):
    """A sanitized refresh failure."""


def _minimal_environment(codex_home: Path) -> dict[str, str]:
    allowed = ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _run(argv: list[str], environment: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            argv,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise RefreshError(f"codex binary not found: {argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise RefreshError(f"{argv[0]} {argv[1]} failed with exit {exc.returncode}") from None
    return result.stdout


def generate_contract(codex_bin: str) -> tuple[str, schema.Contract]:
    """Generate the schema in an isolated temp home and extract the contract."""
    with tempfile.TemporaryDirectory(prefix="ccswitch-codex-contract-") as temp:
        root = Path(temp)
        codex_home = root / "home"
        codex_home.mkdir(mode=0o700)
        environment = _minimal_environment(codex_home)
        version = _run([codex_bin, "--version"], environment).strip()
        if not version or "\n" in version:
            raise RefreshError("codex --version returned an invalid version string")
        out = root / "out"
        _run(
            [codex_bin, "app-server", "generate-json-schema", "--experimental", "--out", str(out)],
            environment,
        )
        contract = schema.extract_contract(out)
    return version, contract


def assert_no_secrets(version: str, contract: schema.Contract) -> None:
    """Refuse to write anything that looks like it carries a credential.

    Uses `schema.contains_unexpected_secret_shape`, not the raw redactor: the
    generated schema's prose contains one dotted config path that matches the
    shared signed-cookie shape (see `schema.BENIGN_SCHEMA_PROSE`).
    """
    if contains_unexpected_secret_shape(version):
        raise RefreshError("refusing to write: the CLI version string looks secret-shaped")
    for relative, document in contract.documents.items():
        if contains_unexpected_secret_shape(json.dumps(document)):
            raise RefreshError(f"refusing to write: {relative} contains a secret-shaped value")
    for key, methods in contract.method_index.items():
        for method in methods:
            if contains_unexpected_secret_shape(method):
                raise RefreshError(
                    f"refusing to write: a {key} method name is secret-shaped"
                )


def _document_bytes(document: Any) -> bytes:
    return (json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8")


def build_manifest(version: str, contract: schema.Contract) -> dict[str, Any]:
    return {
        "cliVersion": version,
        "contractSha256": contract.sha256,
        "generator": "scripts/refresh_codex_contract_fixtures.py",
        "hashScope": (
            "sha256 over the curated documents canonicalized as JSON with sorted "
            "keys and compact separators, plus the method-name inventory of every "
            "JSON-RPC envelope. The documents committed here are that same parsed "
            "content re-serialized for readability, not the generator's byte "
            "stream: the hash is invariant to upstream formatting but sensitive to "
            "every key, type, enum member, and required entry. Envelope bodies are "
            "recorded by digest only, so upstream churn in the 150+ methods "
            "ccswitch never calls does not break the pin."
        ),
        "documents": list(contract.documents),
        "envelopes": {
            name: {
                "sha256": contract.envelope_sha256.get(name, ""),
                "methodCount": len(contract.method_index[key]),
            }
            for name, key in schema.ENVELOPE_SCHEMA_FILES.items()
        },
        "methodIndex": schema.METHOD_INDEX_FILENAME,
    }


def write_fixtures(version: str, contract: schema.Contract) -> Path:
    assert_no_secrets(version, contract)
    target = FIXTURE_PARENT / version.split()[-1]
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for relative, document in contract.documents.items():
        (target / schema.fixture_filename(relative)).write_bytes(_document_bytes(document))
    (target / schema.METHOD_INDEX_FILENAME).write_bytes(
        _document_bytes({key: list(value) for key, value in contract.method_index.items()})
    )
    (target / schema.MANIFEST_FILENAME).write_bytes(
        _document_bytes(build_manifest(version, contract))
    )
    return target


def describe_changes(version: str, contract: schema.Contract) -> list[str]:
    """Report what a write would change relative to the committed fixtures."""
    lines: list[str] = []
    if version != schema.PINNED_CLI_VERSION:
        lines.append(
            f"cli version: pinned {schema.PINNED_CLI_VERSION} -> observed {version}"
        )
    if contract.sha256 != schema.PINNED_CONTRACT_SHA256:
        lines.append(
            f"contract sha256: pinned {schema.PINNED_CONTRACT_SHA256} -> "
            f"observed {contract.sha256}"
        )
    existing = FIXTURE_PARENT / schema.PINNED_CLI_VERSION.split()[-1]
    if not existing.is_dir():
        lines.append(f"fixture directory {existing.name} is missing")
        return lines
    committed = schema.load_pinned_fixture_contract(existing)
    for relative in sorted(set(committed.documents) | set(contract.documents)):
        before = committed.documents.get(relative)
        after = contract.documents.get(relative)
        if before is None:
            lines.append(f"document added: {relative}")
        elif after is None:
            lines.append(f"document removed: {relative}")
        elif json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
            lines.append(f"document changed: {relative}")
    for key in sorted(schema.ENVELOPE_SCHEMA_FILES.values()):
        before_methods = set(committed.method_index.get(key, ()))
        after_methods = set(contract.method_index.get(key, ()))
        for method in sorted(after_methods - before_methods):
            lines.append(f"{key}: method added: {method}")
        for method in sorted(before_methods - after_methods):
            lines.append(f"{key}: method removed: {method}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the regenerated fixtures (default is a report-only check)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift only; exit 1 when the observed contract differs from the pin",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get(CODEX_BIN_ENV, "codex"),
        help=f"codex binary to interrogate (default: ${CODEX_BIN_ENV} or 'codex')",
    )
    args = parser.parse_args(argv)

    try:
        version, contract = generate_contract(args.codex_bin)
        changes = describe_changes(version, contract)
        if not changes:
            print(f"codex contract matches the pin ({version}, {contract.sha256})")
            return 0
        print(f"observed {version} sha256 {contract.sha256}")
        for line in changes:
            print(f"  {line}")
        if args.write:
            target = write_fixtures(version, contract)
            print(f"wrote {target.relative_to(_REPO_ROOT)}")
            print(
                "update server/codex/schema.py: "
                f'PINNED_CLI_VERSION = "{version}" '
                f'PINNED_CONTRACT_SHA256 = "{contract.sha256}"'
            )
            return 0
        return 1 if args.check else 1
    except (RefreshError, schema.CodexSchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
