from __future__ import annotations
import argparse
import asyncio
import os
import subprocess
import sys

from server.deploy.controller import DeployController, default_daemon_factory


def _daemon_enabled() -> bool:
    try:
        return subprocess.run(["systemctl", "is-enabled", "ccswitch-daemon.service"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        from server.deploy import serve as _serve
        _serve.serve()
        return 0
    p = argparse.ArgumentParser(prog="python -m server.deploy")
    p.add_argument("--once", action="store_true", help="run a single deploy and exit")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--venv-pip", default=None)
    p.add_argument("--daemon-enabled", choices=["true", "false"], default=None)
    args = p.parse_args(argv)

    if not args.once:
        p.error("only --once is supported in Plan 1")

    host = os.environ.get("CCSWITCH_HOST", "127.0.0.1")
    port = os.environ.get("CCSWITCH_PORT", "8787")
    health_url = f"http://{host}:{port}/healthz"
    venv_pip = args.venv_pip or os.path.join(args.repo, ".venv", "bin", "pip")
    daemon_enabled = (args.daemon_enabled == "true") if args.daemon_enabled else _daemon_enabled()

    controller = DeployController(args.repo, health_url=health_url, venv_pip=venv_pip,
                                  daemon_enabled=daemon_enabled,
                                  daemon_client_factory=default_daemon_factory(daemon_enabled))
    status = asyncio.run(controller.deploy_once())
    for phase, state in status.phases.items():
        print(f"[deploy] {phase}: {state}")
    for w in status.warnings:
        print(f"[deploy] WARNING: {w}", file=sys.stderr)
    if status.error:
        print(f"[deploy] FAILED: {status.error}", file=sys.stderr)
        return 1
    print(f"[deploy] live at {status.target_sha[:9]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
