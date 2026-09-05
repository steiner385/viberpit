"""Native OpenAI Codex `app-server` boundary.

`schema.py` pins the supported protocol contract and its DTOs, `protocol.py` is
the single async JSONL client that speaks it, and `redaction.py` is the one place
that decides what may reach a log, a `repr`, or an exception message.

Nothing in this package reads `CCSWITCH_*` environment variables: feature-flag and
configuration decisions are passed in by the caller, so the same client is usable
from the daemon, from tests, and from an opt-in script.
"""
from __future__ import annotations
