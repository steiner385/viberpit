#!/usr/bin/env python3
"""winsync: mirror a git worktree into the Windows VM and print the Windows path.

    winsync [--dest WINPATH] [path]

Sends the files `git ls-files -co --exclude-standard` lists (tracked plus untracked-but-not-
ignored, so never .git internals, build output or anything .gitignore'd) as a tar over scp, and
extracts it with Windows' built-in tar.exe into the destination (default C:\\work\\<worktree
directory name>). A minimal .git (HEAD plus the current branch ref) rides along so tools that read
the commit can. Files a previous winsync sent that are gone now are deleted on the Windows side;
anything the VM created itself (a venv, build output) is left alone.

Exit status: 0, 2 usage or not a git worktree, 255 VM unreachable or extraction failed.
Environment: VIBERPIT_WINDOWS_VM_SSH_HOST (ssh host alias, default win11).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import _winvm

TOOL = "winsync"
MANIFEST = ".winsync-manifest.json"
SSH_CALL_TIMEOUT_S = 600  # a large worktree over the NAT link
_WIN_ABS = re.compile(r"^[A-Za-z]:\\")

# `# winsync:extract` is the marker the tests' ssh shim keys on. Only base64 is formatted in.
_EXTRACT_PS1 = r"""# winsync:extract
$ErrorActionPreference = 'Stop'
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{spec}')) | ConvertFrom-Json
$dest = $spec.dest
$tarPath = Join-Path $HOME $spec.tar
$mf = Join-Path $dest '{manifest}'
try {{
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  $old = @()
  if (Test-Path -LiteralPath $mf) {{ $old = (Get-Content -LiteralPath $mf -Raw -Encoding UTF8 | ConvertFrom-Json).files }}
  tar.exe -x -f $tarPath -C $dest
  if ($LASTEXITCODE) {{ throw "tar.exe exited $LASTEXITCODE" }}
  $keep = @{{}}
  foreach ($f in (Get-Content -LiteralPath $mf -Raw -Encoding UTF8 | ConvertFrom-Json).files) {{ $keep[$f] = $true }}
  $removed = 0
  foreach ($f in $old) {{
    $p = Join-Path $dest $f
    if (-not $keep.ContainsKey($f) -and (Test-Path -LiteralPath $p -PathType Leaf)) {{
      Remove-Item -LiteralPath $p -Force; $removed++
    }}
  }}
  "removed=$removed"
}} finally {{
  Remove-Item -LiteralPath $tarPath -Force -ErrorAction SilentlyContinue
}}
"""


def _git(root: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    return proc.stdout.decode("utf-8", "surrogateescape").strip() if proc.returncode == 0 else None


def list_files(root: Path) -> list[str]:
    """Tracked plus untracked-unignored files that exist on disk (a deleted tracked file is not sent)."""
    raw = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                         check=True, capture_output=True).stdout
    names = {n.decode("utf-8", "surrogateescape") for n in raw.split(b"\0") if n}
    return sorted(n for n in names if os.path.isfile(root / n))


def _git_meta(root: Path) -> dict[str, bytes]:
    """A minimal .git: HEAD and the current branch ref. Objects never leave the host."""
    sha = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if not sha:
        return {}  # no commits yet
    branch = _git(root, "symbolic-ref", "-q", "HEAD")
    if not branch:
        return {".git/HEAD": f"{sha}\n".encode()}  # detached
    return {".git/HEAD": f"ref: {branch}\n".encode(), f".git/{branch}": f"{sha}\n".encode()}


def build_tar(root: Path, files: list[str], fileobj) -> int:
    extra = _git_meta(root)
    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT, dereference=True) as tar:
        for name in files:
            tar.add(root / name, arcname=name, recursive=False)
        manifest = json.dumps({"files": files + sorted(extra)}).encode()
        for name, data in [*sorted(extra.items()), (MANIFEST, manifest)]:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            tar.addfile(info, io.BytesIO(data))
    return len(files)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=TOOL, formatter_class=argparse.RawDescriptionHelpFormatter,
                                description=_winvm.help_description(__doc__),
                                usage="%(prog)s [--dest WINPATH] [path]")
    p.add_argument("--dest", metavar="WINPATH",
                   help=r"absolute Windows destination (default C:\work\<worktree directory name>)")
    p.add_argument("path", nargs="?", default=".", help="any path inside the worktree (default: cwd)")
    args = p.parse_args(argv)
    if args.dest is not None and not _WIN_ABS.match(args.dest):
        p.error(rf"--dest must be an absolute Windows path like C:\work\repo, got {args.dest!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    top = _git(Path(args.path), "rev-parse", "--show-toplevel")
    if not top:
        return _winvm.die(TOOL, f"{args.path} is not inside a git worktree", 2)
    root = Path(top)
    dest = (args.dest or rf"C:\work\{root.name}").rstrip("\\")
    files = list_files(root)
    remote_tar = f"winsync-{uuid.uuid4().hex[:12]}.tar"
    with tempfile.NamedTemporaryFile(prefix="winsync-", suffix=".tar") as tmp:
        count = build_tar(root, files, tmp)
        tmp.flush()
        try:
            sent = _winvm.scp_to(tmp.name, remote_tar, timeout=SSH_CALL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _winvm.die(TOOL, f"copy to {_winvm.ssh_host()} timed out", _winvm.EXIT_UNREACHABLE)
    if sent.returncode != 0:
        return _winvm.unreachable(TOOL, sent)
    spec = _winvm.b64_utf8(json.dumps({"dest": dest, "tar": remote_tar}))
    try:
        proc = _winvm.run_ps(_EXTRACT_PS1.format(spec=spec, manifest=MANIFEST), timeout=SSH_CALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _winvm.die(TOOL, f"extract on {_winvm.ssh_host()} timed out", _winvm.EXIT_UNREACHABLE)
    if proc.returncode == _winvm.EXIT_UNREACHABLE:
        return _winvm.unreachable(TOOL, proc)
    if proc.returncode != 0:
        return _winvm.die(TOOL, f"extract failed ({_winvm.ssh_diagnostic(proc)})", _winvm.EXIT_UNREACHABLE)
    removed = re.search(r"removed=(\d+)", proc.stdout.decode("utf-8", "replace"))
    print(f"{TOOL}: {count} files -> {dest}" + (f", {removed.group(1)} stale removed" if removed else ""),
          file=sys.stderr)
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
