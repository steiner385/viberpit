"""The parked review's I/O half (design
``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md`` §4): read a parked
issue's merged PRs, ask one bounded stateless helper whether it should stay parked,
and record the verdict.

The same shape as the close-out assessment (``closeout_assessor``) and the same
seams — the merged PRs through ``closeout_assessor.merged_prs`` (the issue's
cross-reference timeline, read with the repo's app token through the injected
``gh_run``) and the model through ``closeout_assessor.ask_helper`` with an
injectable ``_query_fn`` — so tests never touch the network or a model. The model
only classifies; what becomes of a verdict is ``hygiene.parked_review``'s, and it is
only ever an owner recommendation. Nothing here writes to GitHub.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from server.automations.closeout_assessor import ask_helper, merged_prs
from server.pm import parked_review as pr
from server.pm.outcomes import body_fingerprint
from server.pm.queue import QueueRow
from server.time_iso import iso_utc

log = logging.getLogger(__name__)

SUBSYSTEM = "needs_you_hygiene.parked_review"


async def review(row: QueueRow, *, gh_run: Any, _query_fn: Any = None,
                 _config_dir: str | None = None, now: datetime | None = None) -> dict | None:
    """Review the parked issue *row* and answer its ``parked_review.review_state``
    (``verdict`` ``None`` for an unusable reply), or ``None`` when its merged PRs or
    the model could not be read (retried on a later tick)."""
    prs = await asyncio.to_thread(merged_prs, row.ref, gh_run=gh_run)
    if prs is None:
        return None
    reply = await ask_helper(pr.build_prompt(row.title, row.body, prs),
                             system_prompt=pr.SYSTEM_PROMPT, subsystem=SUBSYSTEM,
                             query_fn=_query_fn, config_dir=_config_dir)
    if reply is None:
        return None
    verdict = pr.parse_verdict(reply, (int(p.get("number") or 0) for p in prs))
    if verdict is None:
        # Expected now and then from a model; recorded so the issue waits for the
        # next run rather than being re-asked every tick. INFO: a WARNING would
        # auto-file an issue.
        log.info("parked_reviewer.unusable ref=%s chars=%d starts=%r", row.ref, len(reply),
                 reply[:80])
    return pr.review_state(fingerprint=body_fingerprint(row.title, row.body), verdict=verdict,
                           prs=prs, repo=row.repo,
                           at=str(iso_utc(now or datetime.now(timezone.utc))))


async def review_and_record(ctx: Any, row: QueueRow, *, gh_run: Any, _query_fn: Any = None,
                            _config_dir: str | None = None) -> dict | None:
    """:func:`review`, persisted as *ctx*'s target state ``parked_review.item_target``."""
    state = await review(row, gh_run=gh_run, _query_fn=_query_fn, _config_dir=_config_dir)
    if state is not None:
        await ctx.set_target_state(pr.item_target(row.ref), state)
    return state
