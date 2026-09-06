"""Routes a command invocation to its execution mechanism, by kind.

Authority for HOW each command executes. Returns a commandResult payload dict
for client/control outcomes (the WS layer wraps it with type/sessionId/ok), or
None for the kinds that stream as a normal turn (passthrough, skill).

`passthrough` and `skill` are the same intent — "run this engine catalog entry"
— reached two different ways, and the split is the point of #1286. Passthrough
writes `/name` into the prompt and relies on the ENGINE parsing its own slash
syntax; an engine that does not parse it would just show the model the literal
text. `skill` hands the engine the catalog entry by identity instead.
"""
from __future__ import annotations
from .catalog import Command


class UnsupportedCommandDispatch(RuntimeError):
    """The driver cannot execute a command its catalog offered.

    Raised rather than silently degraded to text: falling back to `/name`
    passthrough here would re-create exactly the untruthful dispatch the skill
    kind exists to replace, and would do it invisibly.
    """


async def dispatch(driver, cmd: Command, args: str) -> dict | None:
    args = (args or "").strip()

    if cmd.kind == "client":
        if cmd.name == "context":
            return {"name": "context", "render": "context", "data": await driver.context_usage()}
        # /help (and any other client command) renders from the client's cached catalog.
        return {"name": cmd.name, "render": "help", "data": None}

    if cmd.kind == "control":
        # /model never reaches dispatch: run_command intercepts it before the
        # catalog lookup (orchestrator-level — record + DB persistence + the
        # busy stop/switch/restart live there, and it must work driverless).
        if cmd.name == "clear":
            await driver.reconnect()
            return {"name": "clear", "render": "clear", "data": {"text": "Conversation cleared"}}
        raise ValueError(f"unhandled control command: {cmd.name}")

    if cmd.kind == "skill":
        send_skill = getattr(driver, "send_skill", None)
        if send_skill is None:
            raise UnsupportedCommandDispatch(
                f"this engine cannot run the skill {cmd.name!r}"
            )
        await send_skill(cmd.name, args)
        return None

    # passthrough — custom commands, skills, /compact: stream as a normal turn.
    text = f"/{cmd.name}" + (f" {args}" if args else "")
    await driver.send_prompt(text)
    return None
