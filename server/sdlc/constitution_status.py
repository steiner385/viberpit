"""Governance status of an *onboarded repo's* constitution document (issue #2165).

Read-only classification, no enforcement: pm-sync stores the verdict on the project
row and the spec prompt uses it to decide whether a session can be told to obey the
document. The amendment gate next door (``constitution_gate``) is the opposite
concern — it blocks *this* repo's own governance changes — so this lives in its own
module: ``constitution_gate.py`` is a ``GUARDRAIL_PATHS`` entry whose every edit
escalates to the owner, and a hygiene classifier is not a reason to spend that.

The footer grammar stays single-sourced: ``parse_footer`` is imported, never re-derived.

Pure by design: callers do the I/O.
"""
from __future__ import annotations

import re

from server.sdlc.constitution_gate import parse_footer

__all__ = ["classify_document"]

# spec-kit's generated template uses `[UPPER_CASE_WITH_UNDERSCORES]` placeholders throughout
# (`[PROJECT_NAME]`, `[PRINCIPLE_1_NAME]`, `[CONSTITUTION_VERSION]`, …) — a document that still
# carries one was never filled in, whatever its footer claims.
_TEMPLATE_TOKEN_RE = re.compile(r"\[[A-Z][A-Z0-9_ ]*\]")


def classify_document(text: str | None) -> str | None:
    """Governance status of a constitution document's raw text (issue #2165).

    ``None`` means the document looks ratified: present, not the unfilled spec-kit
    template, and its Version/Ratified/Last Amended footer parses. Otherwise one of:

    * ``"missing"`` — no file (``text`` is ``None`` or blank).
    * ``"template"`` — still carries a spec-kit placeholder token like
      ``[PROJECT_NAME]``; whatever else it says is not binding.
    * ``"no_version_footer"`` — present, not a template, but the footer
      ``constitution_gate.check_amendment`` requires for every amendment does not
      parse, so the document cannot be confirmed ratified.
    """
    if text is None or not text.strip():
        return "missing"
    if _TEMPLATE_TOKEN_RE.search(text):
        return "template"
    if parse_footer(text) is None:
        return "no_version_footer"
    return None
