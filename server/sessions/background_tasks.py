"""Scan a Claude transcript for *open* background tasks (Monitors / backgrounded
Bash|Task) and build the turn that relaunches them after a deploy restart.

Pure + side-effect-free: the transcript .jsonl is the durable source of truth for
both launches (assistant tool_use) and terminations (task-notification), so no live
observer or mirror table is needed (constitution Principle VI)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast

_TUID = re.compile(r"<tool-use-id>(.*?)</tool-use-id>")
_STATUS = re.compile(r"<status>(.*?)</status>")
# completed/failed = the task finished on its own; killed = interrupted (we re-arm it).
_TERMINAL = {"completed", "failed"}

_GUARD = ("This task was interrupted by a server restart and may have partially "
          "completed. Before redoing work, check current state (e.g. `git status`, "
          "what already exists) and continue from where it left off rather than "
          "restarting blindly.")


@dataclass
class OpenBgTask:
    tool_use_id: str
    kind: str          # "monitor" | "task"
    name: str          # originating tool name (Monitor / Bash / Task)
    input: dict        # verbatim tool_use input — replayed to relaunch faithfully
    description: str

    def to_dict(self) -> dict:
        return {"tool_use_id": self.tool_use_id, "kind": self.kind,
                "name": self.name, "input": self.input, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> "OpenBgTask":
        return cls(tool_use_id=d.get("tool_use_id", ""), kind=d.get("kind", "task"),
                   name=d.get("name", ""), input=d.get("input") or {},
                   description=d.get("description", ""))


def _is_background_launch(name: str, inp: dict) -> bool:
    return name == "Monitor" or bool(inp.get("run_in_background"))


def scan_open_background_tasks(jsonl_path: str) -> list[OpenBgTask]:
    launches: dict[str, OpenBgTask] = {}
    terminated: set[str] = set()
    try:
        f = open(jsonl_path, errors="ignore")
    except OSError:
        return []
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict) or o.get("isSidechain"):
                continue
            if o.get("type") == "assistant":
                content = (o.get("message") or {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name = b.get("name") or ""
                        inp: dict = cast(dict, b.get("input")) if isinstance(b.get("input"), dict) else {}
                        tid = b.get("id")
                        if tid and _is_background_launch(name, inp):
                            launches[tid] = OpenBgTask(
                                tool_use_id=tid,
                                kind="monitor" if name == "Monitor" else "task",
                                name=name, input=inp,
                                description=str(inp.get("description") or ""))
            # Terminations: any entry whose content carries a task-notification with a
            # completed/failed status (queue-operation enqueue or the user delivery).
            content = o.get("content")
            if isinstance(content, str) and "<task-notification>" in content:
                ms, mt = _STATUS.search(content), _TUID.search(content)
                if ms and mt and ms.group(1) in _TERMINAL:
                    terminated.add(mt.group(1))
    open_tasks = [t for tid, t in launches.items() if tid not in terminated]
    open_tasks.reverse()   # newest-first (dict preserves file order)
    return open_tasks


def build_rearm_prompt(tasks: list[OpenBgTask]) -> str:
    lines = ["The daemon was restarted (deploy) and these background tasks were "
             "stopped. Relaunch each one with exactly the parameters shown."]
    if any(t.kind != "monitor" for t in tasks):
        lines.append(_GUARD)
    for t in tasks:
        label = t.description or t.name
        lines.append(f"- [{t.kind}] {label}: relaunch the {t.name} tool with input "
                     f"{json.dumps(t.input)}")
    return "\n".join(lines)
