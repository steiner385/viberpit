"""Per-session command catalog: what this session can actually be asked to do.

Single source of truth for WHAT commands exist (constitution VI). A catalog has
exactly two halves, and keeping them distinct is the whole point of this module:

* **Vendor commands** — what the ENGINE publishes about itself, normalized from
  the driver's ``server_info()``. We never scan `.claude/commands/` ourselves;
  the Claude SDK already enumerates built-ins + custom commands + skills, and the
  Codex app-server answers `skills/list`. Whether an engine has this half at all
  is ``AgentCapabilities.vendor_commands``.
* **App-owned commands** — what ViberPit implements on top of any engine
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
semantics. A vendor entry that shares a name with a gated app-owned client
command (the CLI's own `/mcp`, which only says "use /mcp in the terminal" here)
takes our render only where that gate holds; elsewhere it stays the engine's.
"""
from __future__ import annotations
import re
from collections.abc import Callable
from dataclasses import dataclass

from server.sessions.engine import (AgentCapabilities, flag_settings_supported,
                                    fork_supported, rewind_supported)
from server.sessions.types import is_assistant_kind

# Split a raw prompt into (command-name, args) IFF it is a single-line
# slash command. Mirrors the Composer's runCommandFromText regex exactly
# (web/src/features/sessions/composer/Composer.tsx) so a slash command typed as an
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
CLIENT = {"help", "context", "mcp", "fork", "rewind", "btw", "fast"}
# Commands a coding session offers but the Assistant drawer's own session never does —
# whoever publishes them. `/btw` (D-12) opens a side chat about the session it is typed
# in; the drawer has no side chat about itself.
CODING_ONLY = frozenset({"btw"})
# Commands ViberPit owns outright: the engine's own entry of that name is never offered,
# not even as a passthrough. The CLI publishes `/fast` and `/effort`, but its own toggles
# would change the running process behind the session's persisted record (D-14) — and
# fast mode bills usage credits at a higher rate — so both only ever change through
# ViberPit's gated, recorded path.
#
# `/output-style` (D-16a) is the CLI's style switch, and its own copy writes
# `.claude/settings.local.json` INTO the session worktree; `/plan` is not in the SDK list
# today, but a future headless `/plan` must not bypass the mode picker's overlay either.
APP_ONLY = frozenset({"fast", "effort", "output-style", "plan"})
#: ``SessionOrchestrator.send_prompt``'s answer when the prompt's first line was an
#: ``APP_ONLY`` command: it ran as that app command and nothing reached the engine.
#: Every prompt path (ws, ``POST /prompt``, automations, a spawn's initial prompt)
#: sends through that one choke point, so none can forward an APP_ONLY name.
APP_COMMAND = "app_command"
#: ``send_prompt``'s answer when the prompt's first line was an ``APP_ONLY`` command
#: that was NOT run: a non-human sender (automation, autopilot, the Assistant, a
#: session's own tool), or text the command cannot take whole (more lines, or
#: attachments). Nothing ran and nothing reached the engine; it is never a delivery.
APP_COMMAND_REFUSED = "app_command_refused"

#: Who is sending a prompt, for the APP_ONLY gate (D-16a, security review S1). Only a
#: person may run `/plan off` (a privilege change) or `/fast on` (billing) by typing it:
#: the WS composer, ``POST /sessions/{id}/prompt`` from a Tailscale user, and the
#: initial prompt of a session a user created. Every other sender — and a call that
#: names none — is refused (fail closed).
COMMAND_ORIGIN_COMPOSER = "composer"
COMMAND_ORIGIN_REST = "rest"
COMMAND_ORIGIN_CREATE = "create"
HUMAN_COMMAND_ORIGINS = frozenset({COMMAND_ORIGIN_COMPOSER, COMMAND_ORIGIN_REST,
                                   COMMAND_ORIGIN_CREATE})


@dataclass(frozen=True)
class AppOnlyPrompt:
    """A prompt whose first line is an ``APP_ONLY`` command. ``extra_lines`` is True
    when more text follows it — decided by ``parse_command_line`` itself, the ONE
    "multi-line is prose" rule, so the gate and the parser cannot disagree."""
    name: str
    args: str
    extra_lines: bool


def app_only_command(text: object) -> AppOnlyPrompt | None:
    """The ``APP_ONLY`` command at the head of *text*, or None. Leading and trailing
    blank lines are ignored; any other line makes ``extra_lines`` True."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    head = parse_command_line(stripped.split("\n", 1)[0].strip())
    if head is None or head[0] not in APP_ONLY:
        return None
    return AppOnlyPrompt(head[0], head[1], parse_command_line(stripped) is None)
# Commands executed via a dedicated SDK method (not raw passthrough).
CONTROL = {"model", "clear", "effort", "output-style", "plan"}
# Interactive-only / auth / config commands that make no sense in this web UI.
# Tunable; see docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md.
EXCLUDED = {
    "login", "logout", "config", "doctor", "bug", "init", "ide",
    "terminal-setup", "vim", "resume", "exit", "quit", "upgrade",
    "release-notes", "status", "cost", "export", "add-dir",
}

#: Who owns a command — the engine, or ViberPit. On the wire so the palette can
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
    """One ViberPit-owned command plus the engine capability it needs.

    The gate is a predicate over the whole capability set rather than a single
    field name because the honest condition is sometimes a disjunction:
    `/account` works on an engine that can carry a live conversation across
    accounts OR one that can only start a fresh thread there — both move the
    session, so both earn the entry.
    """

    command: Command
    gate: Callable[[AgentCapabilities], bool]
    #: The command also needs this session's fast mode to be available right now
    #: (D-14: per MODEL, so not an engine capability — `fast_mode_available`).
    needs_fast_mode: bool = False

    def offered(self, capabilities: AgentCapabilities, *, fast_mode_available: bool) -> bool:
        return self.gate(capabilities) and (fast_mode_available or not self.needs_fast_mode)


def _always(_caps: AgentCapabilities) -> bool:
    """Backed by the AgentDriver protocol itself, so every engine has it."""
    return True


def _assistant_enabled(_caps: AgentCapabilities) -> bool:
    """The ``VIBERPIT_ASSISTANT_ENABLED`` kill switch. Engine-independent: `/btw`
    opens the Assistant drawer, which is ViberPit's, not the session's engine's."""
    from server.config import get_settings
    return bool(get_settings().assistant_enabled)


#: ViberPit-invented commands, present on no engine's own list.
#:
#: `/help` and `/context` render client-side from data every driver publishes
#: (the catalog itself; `context_usage()`), and `/clear` is `driver.reconnect()`
#: — all three are AgentDriver protocol surface, so they are ungated. `/model`,
#: `/account`, `/mcp` and `/fork` are gated because an engine can genuinely lack them.
#: `/fork` (D-8) runs in the browser — it forks at the transcript's latest settled
#: reply through ``POST /sessions/{id}/fork`` — behind the same gate as that route.
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
    AppCommand(
        Command(name="mcp", description="Show MCP servers, their sign-in state, and sign in",
                argument_hint=None, kind="client", source=APP),
        gate=lambda caps: caps.mcp_status,
    ),
    AppCommand(
        Command(name="fork", description="Fork this session at the latest reply into a new one",
                argument_hint=None, kind="client", source=APP),
        gate=fork_supported,
    ),
    AppCommand(
        Command(name="rewind", description="Rewind this session to one of your messages",
                argument_hint=None, kind="client", source=APP),
        gate=rewind_supported,
    ),
    # `/btw` (D-12) runs in the browser: it opens the Assistant drawer as a side chat
    # about this session, sending the question when there is one. Coding sessions only
    # (`CODING_ONLY`), and behind the Assistant's own kill switch.
    AppCommand(
        Command(name="btw", description="Ask the Assistant about this session in the side chat",
                argument_hint="<question>", kind="client", source=APP),
        gate=_assistant_enabled,
    ),
    # `/fast` (D-14) toggles the session's fast mode. Offered only where the engine has
    # live flag settings AND the CLI lists the current model as fast-capable. The browser
    # runs it (it posts to `POST /sessions/{id}/fast`); `APP_ONLY` drops the CLI's own.
    AppCommand(
        Command(name="fast", description="Toggle fast mode: faster output on the same model",
                argument_hint="[on|off]", kind="client", source=APP),
        gate=flag_settings_supported,
        needs_fast_mode=True,
    ),
    # `/effort <level>` (D-14) changes the session's effort live — `run_command` routes
    # it through the same `set_model(effort=…)` action as the prompt row's picker, so
    # the record and the CLI never diverge. Claude with live flag settings only.
    AppCommand(
        Command(name="effort", description="Set this session's effort level (default clears it)",
                argument_hint="<level|default>", kind="control", source=APP),
        gate=flag_settings_supported,
    ),
    # `/output-style [style]` (D-16a) sets the session's output style through the same
    # `set_output_style` action as `POST /sessions/{id}/output-style` and the + menu's
    # Style submenu; bare, it lists the styles as a notice. Claude with live flag
    # settings only (the orchestrator also refuses it for the Assistant drawer).
    AppCommand(
        Command(name="output-style", description="Set this session's output style",
                argument_hint="[style]", kind="control", source=APP),
        gate=flag_settings_supported,
    ),
    # `/plan [on|off]` (D-16a) toggles plan mode through `set_permission_mode`, the
    # ModePicker's path (`default` returns to the pre-plan mode). Every engine has it.
    AppCommand(
        Command(name="plan", description="Turn plan mode on or off (bare: toggle)",
                argument_hint="[on|off]", kind="control", source=APP),
        gate=_always,
    ),
)


def classify(name: str, capabilities: AgentCapabilities | None = None, *,
             fast_mode_available: bool = False) -> str:
    """The execution kind of *name*. With *capabilities*, a CLIENT name whose app-owned
    entry is gated off for this engine is the engine's own command (passthrough)."""
    if name in CLIENT:
        if capabilities is not None and app_command(
                name, capabilities, fast_mode_available=fast_mode_available) is None:
            return "passthrough"
        return "client"
    if name in CONTROL:
        return "control"
    return "passthrough"


def app_command(name: str, capabilities: AgentCapabilities, *,
                fast_mode_available: bool = False) -> Command | None:
    """The app-owned command *name* for this engine, or None when it is gated off.
    *fast_mode_available* is the session's own fast-mode gate (D-14), for `/fast`.

    THE gate `run_command` consults before intercepting a command, so the
    palette and the dispatcher agree on which app-owned commands exist for a
    session. Consulting it separately is what let `/model` be dispatchable on an
    engine that never offered it.
    """
    lookup = str(name).lstrip("/")
    for entry in APP_COMMANDS:
        if entry.command.name == lookup and entry.offered(
                capabilities, fast_mode_available=fast_mode_available):
            return entry.command
    return None


def _coerce(entry, capabilities: AgentCapabilities, *,
            fast_mode_available: bool = False) -> Command | None:
    if isinstance(entry, str):
        name, desc, hint = entry, "", None
    elif isinstance(entry, dict):
        name = entry.get("name") or entry.get("command") or ""
        desc = entry.get("description") or entry.get("desc") or ""
        hint = entry.get("argumentHint") or entry.get("argument_hint") or entry.get("argHint")
    else:
        return None
    name = str(name).lstrip("/").strip()
    if not name or name in EXCLUDED or name in APP_ONLY:
        return None
    # An engine whose catalog entries are invoked natively never produces a
    # passthrough entry: passthrough means "write `/name` into the prompt and
    # let the engine parse it", which an engine that does not parse slash syntax
    # would forward to the model as literal text.
    kind = ("skill" if capabilities.vendor_skills
            else classify(name, capabilities, fast_mode_available=fast_mode_available))
    return Command(name=name, description=str(desc), argument_hint=hint,
                   kind=kind, source=VENDOR)


def build_catalog(
    server_info: dict | None, capabilities: AgentCapabilities, *, kind: str | None,
    fast_mode_available: bool = False,
) -> list[Command]:
    """The commands *this* session accepts: the engine's, then ViberPit's own.

    *kind* is the session's record kind (``None`` for a coding session), and has no
    default: every caller states it. The Assistant drawer's session gets no
    `CODING_ONLY` command, from either half.

    Vendor entries come first and win a name collision, because when an engine
    publishes a command of its own that name means whatever the engine says it
    means. `capabilities` decides which app-owned commands are appended and
    whether vendor entries are read at all — an engine with
    ``vendor_commands=False`` has no such list, so anything arriving in
    `server_info` for it would be a stale cache rather than a fact.

    *fast_mode_available* is the session's per-model fast-mode gate (D-14,
    ``SessionOrchestrator.fast_mode_available``): `/fast` is offered only when it holds.
    """
    entries = (server_info or {}).get("commands") or []
    out: list[Command] = []
    seen: set[str] = set(CODING_ONLY) if is_assistant_kind(kind) else set()
    if capabilities.vendor_commands:
        for e in entries:
            cmd = _coerce(e, capabilities, fast_mode_available=fast_mode_available)
            if cmd is None or cmd.name in seen:
                continue
            seen.add(cmd.name)
            out.append(cmd)
    for entry in APP_COMMANDS:
        if entry.command.name in seen or not entry.offered(
                capabilities, fast_mode_available=fast_mode_available):
            continue
        seen.add(entry.command.name)
        out.append(entry.command)
    return out
