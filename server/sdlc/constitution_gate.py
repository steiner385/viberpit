"""Amendment gate for ``.specify/memory/constitution.md`` (constitution Governance).

``MANAGED_DENY`` and the ``PreToolUse`` hook enforce *authorship method* — which
tool or shell command touched the file. Method is not enumerable: every new tool,
shell idiom, or agent is a fresh hole, and neither layer applies to a human's
editor or a web commit.

This module enforces *properties of the resulting change* instead, which is
tool-agnostic and cannot be routed around:

* the version was bumped,
* the Sync Impact Report records the transition,
* ``Last Amended`` moved,
* the pull request carries an explicit owner acknowledgment.

The Governance section requires all four of an amendment; before this gate they
were convention, checked only by whoever reviewed the PR — and #1098 auto-merged
before review comments landed at all.

Pure by design: I/O lives in ``scripts/check_constitution_amendment.py``.
"""
from __future__ import annotations

import re

__all__ = [
    "ACKNOWLEDGMENT_MARKER",
    "check_amendment",
    "check_guardrail_change",
    "classify_document",
    "parse_footer",
]

# The deliberate, human signal that an amendment was approved by the owner. The
# Governance section requires acknowledgement in the PR description for any change
# touching a principle's meaning; we require it for every constitution edit,
# because "does this change a principle's meaning" is not machine-decidable.
ACKNOWLEDGMENT_MARKER = "owner acknowledgment:"

_VERSION_RE = re.compile(
    r"\*\*Version\*\*:\s*(?P<version>\d+\.\d+\.\d+)\s*\|"
    r"\s*\*\*Ratified\*\*:\s*(?P<ratified>[\d-]+)\s*\|"
    r"\s*\*\*Last Amended\*\*:\s*(?P<amended>[\d-]+)"
)


def parse_footer(text: str) -> dict[str, str] | None:
    """The ``Version`` / ``Ratified`` / ``Last Amended`` footer, or None."""
    match = _VERSION_RE.search(text)
    return match.groupdict() if match else None


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
    * ``"no_version_footer"`` — present, not a template, but the footer this gate's
      own ``check_amendment`` requires for every amendment does not parse, so the
      document cannot be confirmed ratified.
    """
    if text is None or not text.strip():
        return "missing"
    if _TEMPLATE_TOKEN_RE.search(text):
        return "template"
    if parse_footer(text) is None:
        return "no_version_footer"
    return None


def _as_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def check_amendment(base_text: str, head_text: str, pr_body: str) -> list[str]:
    """Violations for an amendment; empty means the change may merge.

    *base_text* is the constitution on the target branch, *head_text* the proposed
    version, *pr_body* the pull request description. Callers only invoke this when
    the file actually changed.
    """
    violations: list[str] = []

    base = parse_footer(base_text)
    head = parse_footer(head_text)
    if head is None:
        return ["the Version / Ratified / Last Amended footer is missing or malformed"]
    if base is None:
        # A base without a parseable footer cannot be compared; require the rest.
        base = {"version": "0.0.0", "amended": ""}

    # 1. The version must move forward.
    try:
        if _as_tuple(head["version"]) <= _as_tuple(base["version"]):
            violations.append(
                f"version not bumped: base {base['version']}, head {head['version']} — "
                "MAJOR for a principle removal/redefinition, MINOR for a new principle "
                "or materially expanded guidance, PATCH for clarifications"
            )
    except ValueError:
        violations.append(f"version {head['version']!r} is not semantic (X.Y.Z)")

    # 2. The Sync Impact Report must record the transition, so the reasoning for
    #    the chosen bump is written down rather than inferred from the diff.
    transition = f"{base['version']} → {head['version']}"
    if transition not in head_text:
        violations.append(
            f"Sync Impact Report has no {transition!r} entry — record the version "
            "change and the rationale for MAJOR vs MINOR vs PATCH"
        )

    # 3. Last Amended must move; a stale date makes the history unreadable.
    if head["amended"] == base["amended"]:
        violations.append(
            f"Last Amended is unchanged ({head['amended']}) — set it to the amendment date"
        )

    # 4. A human must acknowledge the amendment in the PR description.
    if not _acknowledged(pr_body):
        violations.append(
            "the pull request description carries no owner acknowledgment — add a line "
            f"beginning {ACKNOWLEDGMENT_MARKER!r} stating what is approved"
            f"{_missing_acknowledgment_detail(pr_body)}"
        )

    return violations


# An HTML comment is invisible in the rendered description, so text inside one is
# not an acknowledgment — it is usually the template *asking* for one. Stripping
# comments first is what stops a checklist from satisfying its own checklist item.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# The label without its trailing colon, so the colon can be matched separately —
# see the closing-decoration comment below.
_ACK_LABEL = ACKNOWLEDGMENT_MARKER.rstrip(":")
# The marker must open a line, so merely discussing the gate in prose does not
# clear it. Everything before it may only be whitespace or decoration: emphasis,
# heading, blockquote, list bullet, bracket, or backtick. Brackets matter in
# practice — a template placeholder is usually written `[ ... ]` and authors
# reasonably type the acknowledgment inside the brackets they were given.
# Widening here cannot admit prose: the marker still has to open the line.
#
# Between the label and its colon, also allow closing emphasis/code decoration
# (``**Owner acknowledgment**:``) — not just opening decoration before the label.
# `**Label**:` is at least as idiomatic as `**Label:**`, and the original regex,
# which escaped the label and colon as one literal, rejected it (#1831).
_ACK_LINE = re.compile(
    r"^[\s>*_#\-\[\]()`\"']*" + re.escape(_ACK_LABEL) + r"[\s*_`]*:\s*\S",
    re.IGNORECASE | re.MULTILINE,
)

# Shown when the body already talks about "acknowledgment" but no line matches
# _ACK_LINE — the author likely wrote one in a shape the gate doesn't recognize,
# so naming the accepted forms is more useful than repeating the generic ask.
_ACCEPTED_FORMS = (
    "'Owner acknowledgment: ...', '**Owner acknowledgment:** ...', "
    "or '**Owner acknowledgment**: ...'"
)


def _acknowledged(pr_body: str) -> bool:
    """True when *pr_body* carries a real acknowledgment line.

    Requires a non-empty statement after the marker: a bare ``Owner
    acknowledgment:`` with nothing following it acknowledges nothing.
    """
    return bool(_ACK_LINE.search(_visible(pr_body)))


def _visible(pr_body: str) -> str:
    return _HTML_COMMENT.sub("", pr_body or "")


def _missing_acknowledgment_detail(pr_body: str) -> str:
    """Extra detail for a missing-acknowledgment violation.

    When the description already mentions "acknowledgment" but no line matches
    the marker, say so and list the accepted forms — the generic instruction
    looks satisfied to an author who already wrote *a* line, and diffing their
    own text against the regex is not a reasonable ask (cost a round trip on
    #1830).
    """
    if re.search(r"acknowledg", _visible(pr_body), re.IGNORECASE):
        return (
            f" — the description already mentions acknowledgment, but no line "
            f"matches; accepted forms: {_ACCEPTED_FORMS}"
        )
    return ""


def check_guardrail_change(changed_paths: list[str], pr_body: str) -> list[str]:
    """Violations for a diff that changes how governance is enforced.

    A change to the hook, the deny rules, or this gate alters what every other
    control can catch, so it needs the same deliberate acknowledgment an amendment
    does — but not a version bump, since these files are not versioned documents.

    *changed_paths* are repo-relative. Returns an empty list when the diff touches
    no guardrail.
    """
    from server.sdlc.governance import GUARDRAIL_PATHS  # local: avoid a cycle

    touched = sorted(set(changed_paths) & set(GUARDRAIL_PATHS))
    if not touched or _acknowledged(pr_body):
        return []
    listed = ", ".join(touched)
    return [
        f"this pull request changes governance enforcement ({listed}) but carries no "
        f"owner acknowledgment — add a line beginning {ACKNOWLEDGMENT_MARKER!r} saying "
        f"what you intend to change and why{_missing_acknowledgment_detail(pr_body)}"
    ]
