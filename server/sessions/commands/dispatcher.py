"""Routes a command invocation to its execution mechanism, by kind.

Authority for HOW each command executes. Returns a commandResult payload dict
for client/control outcomes (the WS layer wraps it with type/sessionId/ok), or
None for passthrough (which streams as a normal turn).
"""
from __future__ import annotations
from .catalog import Command


async def dispatch(driver, cmd: Command, args: str) -> dict | None:
    args = (args or "").strip()

    if cmd.kind == "client":
        if cmd.name == "context":
            return {"name": "context", "render": "context", "data": await driver.context_usage()}
        # /help (and any other client command) renders from the client's cached catalog.
        return {"name": cmd.name, "render": "help", "data": None}

    if cmd.kind == "control":
        if cmd.name == "model":
            await driver.set_model(args or None)
            return {"name": "model", "render": "notice", "data": {"text": f"Model → {args or 'default'}"}}
        if cmd.name == "clear":
            await driver.reconnect()
            return {"name": "clear", "render": "clear", "data": {"text": "Conversation cleared"}}

    # passthrough — custom commands, skills, /compact: stream as a normal turn.
    text = f"/{cmd.name}" + (f" {args}" if args else "")
    await driver.send_prompt(text)
    return None
