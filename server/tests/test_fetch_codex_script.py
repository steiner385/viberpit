"""scripts/fetch-codex.sh — digest recording and the no-digest error path.

Runs the real script against a throwaway repo layout (the script resolves its
sha256 file and server/codex/schema.py relative to its own location) and a fake
release tarball fed through ``--from``, so no network is touched.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "fetch-codex.sh"
PIN = "codex-cli 9.9.9"
ASSET = "codex-x86_64-unknown-linux-musl.tar.gz"
HEADER = [
    "# SHA-256 digests of the Codex CLI release artefacts this fleet is allowed to run.",
    "#",
    "# Format: \"<sha256>  <asset-name>\", one per line.",
    "#",
    "#     bash scripts/fetch-codex.sh --record      # on a trusted host, once",
]


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "server" / "codex").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "fetch-codex.sh")
    (root / "scripts" / "codex-cli.sha256").write_text("\n".join(HEADER) + "\n")
    (root / "server" / "codex" / "schema.py").write_text(
        f'PINNED_CLI_VERSION: Final = "{PIN}"\n')
    return root


@pytest.fixture()
def fake_release(tmp_path: Path) -> Path:
    """A tarball holding an executable `codex*` that reports the pinned version."""
    body = f'#!/bin/sh\necho "{PIN}"\n'.encode()
    path = tmp_path / ASSET
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("codex-x86_64-unknown-linux-musl")
        info.size, info.mode = len(body), 0o755
        tar.addfile(info, io.BytesIO(body))
    return path


def _run(repo: Path, *args: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "CODEX_INSTALL_DIR": str(tmp_path / "install")}
    return subprocess.run(["bash", str(repo / "scripts" / "fetch-codex.sh"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_record_keeps_header_in_order_and_appends_digest(fake_repo, fake_release, tmp_path):
    r = _run(fake_repo, "--record", "--from", str(fake_release), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    lines = (fake_repo / "scripts" / "codex-cli.sha256").read_text().splitlines()
    # The comment header survives verbatim and in order — `--record` once sorted
    # the whole file, shuffling the header around the new entry.
    assert lines[: len(HEADER)] == HEADER
    entries = [ln for ln in lines if ln and not ln.startswith("#")]
    assert len(entries) == 1 and entries[0].endswith(f"  {ASSET}")


def test_record_replaces_an_existing_entry_for_the_same_asset(fake_repo, fake_release, tmp_path):
    sha = fake_repo / "scripts" / "codex-cli.sha256"
    sha.write_text(sha.read_text() + f"{'0' * 64}  {ASSET}\n{'1' * 64}  codex-aarch64.tar.gz\n")
    r = _run(fake_repo, "--record", "--from", str(fake_release), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    entries = [ln for ln in sha.read_text().splitlines() if ln and not ln.startswith("#")]
    assert len(entries) == 2                                   # other targets kept
    assert not any(ln.startswith("0" * 64) for ln in entries)  # stale digest replaced
    assert sha.read_text().splitlines()[: len(HEADER)] == HEADER


def test_install_verifies_against_the_recorded_digest(fake_repo, fake_release, tmp_path):
    assert _run(fake_repo, "--record", "--from", str(fake_release), tmp_path=tmp_path).returncode == 0
    r = _run(fake_repo, "--from", str(fake_release), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Checksum OK" in r.stdout
    assert (tmp_path / "install" / "codex").exists()


def test_missing_digest_fails_with_the_explanation_not_silently(fake_repo, fake_release, tmp_path):
    # With no entry for the asset, grep|awk failed under `set -euo pipefail` and
    # killed the script before it printed why — operators saw it exit after the
    # download with no message.
    r = _run(fake_repo, "--from", str(fake_release), tmp_path=tmp_path)
    assert r.returncode == 1
    assert "no recorded SHA256" in r.stderr
    assert not (tmp_path / "install" / "codex").exists()
