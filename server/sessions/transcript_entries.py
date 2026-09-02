"""Which of a transcript's ``type:"user"`` entries are a HUMAN prompt.

The CLI journals several kinds of ``type:"user"`` records nobody typed. Both readers
of the transcript file — replay (what the session view renders after a reload) and
the fallback titler (what a session gets named after) — must agree on which to
ignore, so the predicate lives here, once.

Not a human prompt:

* ``isMeta`` — harness-injected context: a Skill body, the "Continue from where you
  left off." resume prompt, Stop-hook notices, the local-command caveat.
* ``isCompactSummary`` — the CLI's auto-compaction summary.
* ``<task-notification>`` — a background task's termination, delivered as a
  plain-string user entry (NOT flagged).
* CLI local-command bookkeeping — ``driver.set_model()`` (like any client-side slash
  command) is journaled as ``<command-name>…``, ``<local-command-stdout>…`` /
  ``<local-command-stderr>…`` entries, of which only the caveat is flagged ``isMeta``.
  Observed 2026-09-02: a fresh Fable session's transcript opened with exactly those
  three and the titler named it "Set model to Fable".
* ``[Request interrupted by user…]`` — the interrupt marker.
"""
from __future__ import annotations

_PLUMBING_PREFIXES: tuple[str, ...] = (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<task-notification>",
    "[Request interrupted by user",
)


def is_cli_plumbing_text(text: str) -> bool:
    """True when *text* (a user entry's joined text) is CLI/harness bookkeeping."""
    return text.lstrip().startswith(_PLUMBING_PREFIXES)


def is_harness_user_entry(entry: dict, text: str) -> bool:
    """True when the ``type:"user"`` transcript *entry* with joined *text* is not a
    human prompt — flagged meta/compaction, or CLI plumbing by text shape."""
    return (bool(entry.get("isMeta")) or bool(entry.get("isCompactSummary"))
            or is_cli_plumbing_text(text))
