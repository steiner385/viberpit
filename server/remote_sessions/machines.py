"""Which machine a remote session runs on, by name.

claude.ai tags a Remote Control session ``machine-id:<uuid>`` and reports no host name.
That uuid is the Claude CLI's ``remoteControlMachineId`` in the ``.claude.json`` of the
config dir the session ran from (verified 2026-11-26; the separate ``machineID`` key is
NOT it). So each ViberPit host reports the ids in the ``.claude.json`` files it can see —
the host user's ``~/.claude.json`` and every account config dir ViberPit composes — and
the edge maps an id to the host that reported it (:func:`merge_hosts`). An id from a
machine ViberPit does not run on can be named by the operator (Settings
``remoteMachineLabels``). A listed session's ``machineName`` is the host name, else the
operator's label, else None (:func:`apply_names`).

The ids are not secrets, but they are logged only as counts.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MACHINE_ID_KEY = "remoteControlMachineId"
#: Where an id was found: the host user's own ``~/.claude.json`` names the machine; an
#: account config dir may sit on a store shared between hosts, so it is weaker evidence.
SOURCE_HOME = "home"
SOURCE_ACCOUNT = "account"
_RANK = {SOURCE_HOME: 0, SOURCE_ACCOUNT: 1}

#: What an operator may store: an id as claude.ai tags it, and a short label.
MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_LABEL_CHARS = 64
MAX_LABELS = 200


def _read_id(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    value = data.get(MACHINE_ID_KEY) if isinstance(data, dict) else None
    return value if isinstance(value, str) and MACHINE_ID_RE.match(value) else None


def _candidates() -> list[tuple[Path, str]]:
    from viberpit import paths
    from viberpit.configdir import CLAUDE_JSON_NAME

    out = [(paths.global_claude_json(), SOURCE_HOME)]
    try:
        dirs = sorted(p for p in paths.cfg_root().iterdir() if p.is_dir())
    except OSError:
        dirs = []
    out += [(d / CLAUDE_JSON_NAME, SOURCE_ACCOUNT) for d in dirs]
    return out


def local_machine_ids() -> dict[str, str]:
    """``{machine_id: source}`` for every ``remoteControlMachineId`` this host can see.
    Read-only; a missing or unreadable file is skipped. Blocking (reads files)."""
    found: dict[str, str] = {}
    for path, source in _candidates():
        mid = _read_id(path)
        if mid is not None and _RANK[source] < _RANK.get(found.get(mid, ""), 99):
            found[mid] = source
    log.debug("remote sessions: %d Remote Control machine id(s) on this host", len(found))
    return found


def local_payload() -> dict:
    """The ``remote_control_machine_ids`` RPC answer: ``{host, machineIds: {id: source}}``."""
    from server.hostname import self_host_name
    return {"host": self_host_name(), "machineIds": local_machine_ids()}


def merge_hosts(reports: Iterable[tuple[Any, Any]]) -> dict[str, str]:
    """``{machine_id: host name}`` from ``(host, payload)`` pairs. An id seen in a host's
    own ``~/.claude.json`` beats one seen only in an account config dir; hosts tied at the
    same strength (a shared account store) are all named, ``"a / b"``. A malformed
    payload contributes nothing."""
    best: dict[str, tuple[int, set[str]]] = {}
    for host, payload in reports:
        ids = payload.get("machineIds") if isinstance(payload, dict) else None
        if not isinstance(host, str) or not host or not isinstance(ids, dict):
            continue
        for mid, source in ids.items():
            if not isinstance(mid, str) or not MACHINE_ID_RE.match(mid):
                continue
            rank = _RANK.get(source, _RANK[SOURCE_ACCOUNT])
            cur = best.get(mid)
            if cur is None or rank < cur[0]:
                best[mid] = (rank, {host})
            elif rank == cur[0]:
                cur[1].add(host)
    return {mid: " / ".join(sorted(hosts)) for mid, (_rank, hosts) in best.items()}


def stored_labels(raw: Any) -> dict[str, str]:
    """The operator's labels as stored (``AppSettings.remote_machine_labels``), with
    anything malformed dropped."""
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and MACHINE_ID_RE.match(k)
            and isinstance(v, str) and v.strip()}


def apply_labels(current: dict[str, str], patch: dict[str, Any]) -> dict[str, str]:
    """*current* with *patch* applied: a label sets (trimmed), null or blank removes.
    Raises ValueError naming the first bad entry."""
    out = dict(current)
    for mid, label in patch.items():
        if not isinstance(mid, str) or not MACHINE_ID_RE.match(mid):
            raise ValueError("a machine id must be 1-128 letters, digits, '.', '_' or '-'")
        if label is None or (isinstance(label, str) and not label.strip()):
            out.pop(mid, None)
            continue
        if not isinstance(label, str):
            raise ValueError("a machine label must be a string or null")
        text = " ".join(label.split())
        if len(text) > MAX_LABEL_CHARS:
            raise ValueError(f"a machine label must be at most {MAX_LABEL_CHARS} characters")
        out[mid] = text
    if len(out) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} machine labels")
    return out


def apply_names(sessions: Iterable[Any], hosts: dict[str, str],
                labels: dict[str, str]) -> list[dict]:
    """Each listed session with ``machineName``: the ViberPit host that reported its
    machine id, else the operator's label for it, else None."""
    out = []
    for s in sessions:
        mid = s.get("machineId") if isinstance(s, dict) else None
        if not isinstance(s, dict):
            continue
        name = (hosts.get(mid) or labels.get(mid)) if isinstance(mid, str) else None
        out.append({**s, "machineName": name or None})
    return out
