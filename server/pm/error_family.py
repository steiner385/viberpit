"""The error-triage fingerprint FAMILY (spec 2026-09-18 §5).

Error triage deduplicates by *fingerprint*, which hashes the exception class and
the innermost app frame alongside the logger and event
(``server/obs/error_signature.py``). That is the right identity for "have we seen
THIS record before", and the wrong one for dispatch: one broken call site raises
from three frames, files three issues, and the runner would investigate the same
defect three times — three sessions, three sets of findings, three chances to
close two of them as duplicates of a fourth.

A *family* is the coarser key those siblings share: ``logger:event``. It is
DERIVED, never stored — the signature rows already carry both columns, so a family
is a read away and cannot drift out of sync with the fingerprints beneath it. An
item with no derivable family groups alone: a missing key is the absence of
evidence of relatedness, not evidence that two reports belong together.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

#: The automation whose config names the repository auto-error issues are filed
#: into. Matching a ref against ITS repo (not just the issue number) is the point:
#: issue numbers are unique per repository, so ``other/repo#101`` and the triage
#: repo's ``#101`` are different issues that would otherwise share a family.
TRIAGE_AUTOMATION_ID = "error-triage"


def family_key(logger: str | None, event: str | None) -> str | None:
    """``logger:event`` when both halves are present, else ``None`` (no family).

    Blank is as absent as ``NULL``: a signature with an empty event would otherwise
    join every other empty-event signature of the same logger into one enormous
    pseudo-family.
    """
    lg = (logger or "").strip()
    ev = (event or "").strip()
    return f"{lg}:{ev}" if lg and ev else None


def triage_target_repo(db: "Session") -> str:
    """The repo error triage files into, from its own automation config — the one
    place that decides it (``ErrorTriage.config_schema``). Sync; the caller is
    already inside ``db.run_sync``."""
    from server.automations.config_store import read_effective_config_sync

    config = read_effective_config_sync(db, TRIAGE_AUTOMATION_ID)
    return str(config.get("target_repo") or "")


def families_for_refs(db: "Session", refs: Sequence[str]) -> dict[str, str]:
    """``ref -> family`` for every ref that is an auto-filed error report with a
    derivable family. Refs with no signature row, no family key, or a repo that is
    not the triage target are simply absent.

    Sync — the orchestrator calls it under ``db.run_sync``, in the same session as
    the queue read it annotates.
    """
    from sqlalchemy import select

    from server.db.models import ErrorSignature

    from .refs import split_ref

    repo = triage_target_repo(db)
    if not repo or not refs:
        return {}
    by_number: dict[int, str] = {}
    for ref in refs:
        parts = split_ref(ref)
        if parts is not None and parts[0] == repo:
            # First ref wins, so a duplicated ref cannot flip the mapping.
            by_number.setdefault(parts[1], ref)
    if not by_number:
        return {}

    rows: Any = db.execute(select(
        ErrorSignature.issue_number, ErrorSignature.logger, ErrorSignature.event,
    ).where(ErrorSignature.issue_number.in_(sorted(by_number)))
        .order_by(ErrorSignature.fingerprint)).all()
    out: dict[str, str] = {}
    for number, logger, event in rows:
        key = family_key(logger, event)
        filed_ref = by_number.get(number)
        if key is None or filed_ref is None:
            continue
        # Two signatures refiled onto one issue: the first by fingerprint order
        # wins, so the answer is stable across refreshes.
        out.setdefault(filed_ref, key)
    return out
