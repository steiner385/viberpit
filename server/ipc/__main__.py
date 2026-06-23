"""Run the session daemon: `python -m server.ipc [socket_path]` (default: well-known path)."""
import sys

from server.ipc.serve import serve

if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else None)
