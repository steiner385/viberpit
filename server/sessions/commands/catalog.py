"""Per-session command catalog: what this session can actually be asked to do.

Single source of truth for WHAT commands exist (constitution VI). A catalog has
exactly two halves, and keeping them distinct is the whole point of this module:

* **Vendor commands** — what the ENGINE publishes about itself, normalized from
  the driver's ``server_info()``. We never scan `.claude/commands/` ourselves;
  the Claude SDK already enumerates built-ins + custom commands + skills, and the
  Codex app-server answers `skills/list`. Whether an engine has this half at all
  is ``AgentCapabilities.vendor_commands``.
* **App-owned commands** — what CCSWITCH implements on top of any engine
  (`/account`, `/model`, …). These are not the engine's; they are orchestrator
  operations that happen to be typed with a slash. Each one declares the engine
  capability it needs, so a session is offered `/model` only where a model can
  actually be switched.

Before #1286 the app-owned half was one hardcoded `SYNTHETIC` entry and the
vendor half was Claude-only, which left a Codex session with a one-item palette
that hid `/model` even though `run_command` implements it. `APP_COMMANDS` below
is now the declaration BOTH the palette and `run_command` read, so a command
cannot be dispatchable while being invisible, or visible while being refused.

`kind` is assigned here, server-side, so the client never re-derives command
semantics.
"""
from __future__ import annotations
import re
from collections.abc import Callable
from dataclasses import dataclass

from server.sessions.engine import AgentCapabilities

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

#: Who owns a command — the engine, or ccswitch. On the wire so the palette can
#: label an entry honestly instead of implying every command is the engine's.
VENDOR = "vendor"
APP = "app"


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    argument_hint: str | None
    kind: str  # "client" | "control" | "passthrough" | "skill"
    source: str = VENDOR  # VENDOR | APP

    def to_wire(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "argumentHint": self.argument_hint,
            "kind": self.kind,
            "source": self.source,
        }


@dataclass(frozen=True)
class AppCommand:
    """One ccswitch-owned command plus the engine capability it needs.

    The gate is a predicate over the whole capability set rather than a single
    field name because the honest condition is sometimes a disjunction:
    `/account` works on an engine that can carry a live conversation across
    accounts OR one that can only start a fresh thread there — both move the
    session, so both earn the entry.
    """

    command: Command
    gate: Callable[[AgentCapabilities], bool]


def _always(_caps: AgentCapabilities) -> bool:
    """Backed by the AgentDriver protocol itself, so every engine has it."""
    return True


#: ccswitch-invented commands, present on no engine's own list.
#:
#: `/help` and `/context` render client-side from data every driver publishes
#: (the catalog itself; `context_usage()`), and `/clear` is `driver.reconnect()`
#: — all three are AgentDriver protocol surface, so they are ungated. `/model`
#: and `/account` are gated because an engine can genuinely lack them.
APP_COMMANDS: tuple[AppCommand, ...] = (
    AppCommand(
        Command(name="help", description="List the commands this session accepts",
                argument_hint=None, kind="client", source=APP),
        gate=_always,
    ),
    AppCommand(
        Command(name="context", description="Show context-window usage",
                argument_hint=None, kind="client", source=APP),
        gate=_always,
    ),
    AppCommand(
        Command(name="clear", description="Start a fresh conversation",
                argument_hint=None, kind="control", source=APP),
        gate=_always,
    ),
    AppCommand(
        Command(name="model", description="Switch this session to another model",
                argument_hint="<model>", kind="control", source=APP),
        gate=lambda caps: caps.models,
    ),
    AppCommand(
        Command(name="account", description="Switch this session to another account",
                argument_hint="<account>", kind="control", source=APP),
        gate=lambda caps: caps.account_rotation or caps.account_switch_new_thread,
    ),
)


def classify(name: str) -> str:
    if name in CLIENT:
        return "client"
    if name in CONTROL:
        return "control"
    return "passthrough"


def app_command(name: str, capabilities: AgentCapabilities) -> Command | None:
    """The app-owned command *name* for this engine, or None when it is gated off.

    THE gate `run_command` consults before intercepting a command, so the
    palette and the dispatcher agree on which app-owned commands exist for a
    session. Consulting it separately is what let `/model` be dispatchable on an
    engine that never offered it.
    """
    lookup = str(name).lstrip("/")
    for entry in APP_COMMANDS:
        if entry.command.name == lookup and entry.gate(capabilities):
            return entry.command
    return None


def _coerce(entry, *, vendor_skills: bool) -> Command | None:
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
    # An engine whose catalog entries are invoked natively never produces a
    # passthrough entry: passthrough means "write `/name` into the prompt and
    # let the engine parse it", which an engine that does not parse slash syntax
    # would forward to the model as literal text.
    kind = "skill" if vendor_skills else classify(name)
    return Command(name=name, description=str(desc), argument_hint=hint,
                   kind=kind, source=VENDOR)


def build_catalog(
    server_info: dict | None, capabilities: AgentCapabilities
) -> list[Command]:
    """The commands *this* session accepts: the engine's, then ccswitch's own.

    Vendor entries come first and win a name collision, because when an engine
    publishes a command of its own that name means whatever the engine says it
    means. `capabilities` decides which app-owned commands are appended and
    whether vendor entries are read at all — an engine with
    ``vendor_commands=False`` has no such list, so anything arriving in
    `server_info` for it would be a stale cache rather than a fact.
    """
    entries = (server_info or {}).get("commands") or []
    out: list[Command] = []
    seen: set[str] = set()
    if capabilities.vendor_commands:
        for e in entries:
            cmd = _coerce(e, vendor_skills=capabilities.vendor_skills)
            if cmd is None or cmd.name in seen:
                continue
            seen.add(cmd.name)
            out.append(cmd)
    for entry in APP_COMMANDS:
        if entry.command.name in seen or not entry.gate(capabilities):
            continue
        seen.add(entry.command.name)
        out.append(entry.command)
    return out
