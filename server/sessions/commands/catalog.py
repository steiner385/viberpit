"""Per-session slash-command catalog, normalized from the SDK's get_server_info().

Single source of truth for WHAT commands exist (constitution VI): we never scan
.claude/commands/ ourselves — the SDK already enumerates built-ins + custom + skills.
`kind` is assigned here, server-side, so the client never re-derives command semantics.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

# Split a raw prompt into (command-name, args) IFF it is a single-line
# slash command. Mirrors the Composer's runCommandFromText regex exactly
# (web/src/components/session/Composer.tsx) so a slash command typed as an
# initial prompt is recognized identically to one typed mid-session.
_CMD_LINE = re.compile(r"^(\S+)\s*(.*)$")


def parse_command_line(raw: str) -> tuple[str, str] | None:
    """Return (name, args) if `raw` is a single-line `/command [args]`, else None.

    `name` has its leading slash stripped. A multi-line string, or one that
    doesn't begin with `/`, is prose — not a command.
    """
    if not raw.startswith("/") or "\n" in raw:
        return None
    m = _CMD_LINE.match(raw[1:])
    if not m:
        return None
    return m.group(1), m.group(2)

# Commands handled by a dedicated client-side render (no model turn).
CLIENT = {"help", "context"}
# Commands executed via a dedicated SDK method (not raw passthrough).
CONTROL = {"model", "clear"}
# Interactive-only / auth / config commands that make no sense in this web UI.
# Tunable; see docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md.
EXCLUDED = {
    "login", "logout", "config", "doctor", "bug", "init", "ide",
    "terminal-setup", "vim", "resume", "exit", "quit", "upgrade",
    "release-notes", "status", "cost", "export", "add-dir",
}


@dataclass
class Command:
    name: str
    description: str
    argument_hint: str | None
    kind: str  # "client" | "control" | "passthrough"

    def to_wire(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "argumentHint": self.argument_hint,
            "kind": self.kind,
        }


# ccswitch-invented control commands not present in the SDK's get_server_info().
SYNTHETIC = [
    Command(name="account", description="Switch this session to another account",
            argument_hint="<account>", kind="control"),
]


def classify(name: str) -> str:
    if name in CLIENT:
        return "client"
    if name in CONTROL:
        return "control"
    return "passthrough"


def _coerce(entry) -> Command | None:
    if isinstance(entry, str):
        name, desc, hint = entry, "", None
    elif isinstance(entry, dict):
        name = entry.get("name") or entry.get("command") or ""
        desc = entry.get("description") or entry.get("desc") or ""
        hint = entry.get("argumentHint") or entry.get("argument_hint") or entry.get("argHint")
    else:
        return None
    name = str(name).lstrip("/").strip()
    if not name or name in EXCLUDED:
        return None
    return Command(name=name, description=str(desc), argument_hint=hint, kind=classify(name))


def build_catalog(server_info: dict | None) -> list[Command]:
    entries = (server_info or {}).get("commands") or []
    out: list[Command] = []
    seen: set[str] = set()
    for e in entries:
        cmd = _coerce(e)
        if cmd is None or cmd.name in seen:
            continue
        seen.add(cmd.name)
        out.append(cmd)
    for syn in SYNTHETIC:
        if syn.name not in seen:
            seen.add(syn.name)
            out.append(syn)
    return out
