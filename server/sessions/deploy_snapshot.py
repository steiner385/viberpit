from __future__ import annotations
import json
import os

from server.hostpaths import DEPLOY_SNAPSHOT_PATH as DEFAULT_PATH


def snapshot_path() -> str:
    return os.environ.get("CCSWITCH_DEPLOY_SNAPSHOT", DEFAULT_PATH)


def write_snapshot(live: list[dict], *, path: str | None = None) -> str:
    p = path or snapshot_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        json.dump({"live": live}, fh)
    return p


def read_snapshot(*, path: str | None = None) -> list[dict]:
    p = path or snapshot_path()
    try:
        with open(p) as fh:
            return json.load(fh).get("live", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def clear_snapshot(*, path: str | None = None) -> None:
    p = path or snapshot_path()
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
