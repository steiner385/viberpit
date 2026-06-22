"""Run the session daemon on a Unix socket: `python -m server.ipc <socket_path>`."""
import sys

import uvicorn

from server.ipc.daemon import create_daemon_app


def serve(socket_path: str) -> None:
    uvicorn.run(create_daemon_app(), uds=socket_path, log_level="info")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m server.ipc <socket_path>", file=sys.stderr)
        raise SystemExit(2)
    serve(sys.argv[1])
