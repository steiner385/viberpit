"""The one excerpt cut for helper prompts built from a transcript.

Every helper that reads a session's transcript (the autopilot decider and nudge
composer via ``SessionOrchestrator._autopilot_transcript_tail``) has a per-entry
character budget. Until 2026-09-11 that budget was applied as a bare head slice —
``text[:max_chars]`` — which threw away the END of a reply (where a session states
its hand-off, question or next step) and left no trace that anything was removed.
The composer then read a reply ending mid-sentence at exactly 1500 chars and told
the session, four times in one afternoon, that its reply had "cut off mid-sentence"
(session c07bbc41, 2026-09-10; every reply was complete on disk at 2.0–3.1k chars).

``elide_middle`` keeps the head AND the tail and puts an explicit marker between
them that names the harness as the one doing the cutting. The decider/composer
guidance files quote the marker's wording, so a prompt reader knows what it is.
"""
from __future__ import annotations

from ccswitch import brand


def elide_middle(text: str, max_chars: int) -> str:
    """Shorten *text* to roughly *max_chars* by cutting the MIDDLE out.

    Returns *text* unchanged when it fits. Otherwise keeps the first third and the
    last two thirds of the budget (the ending carries the hand-off) around a marker
    that states how many characters were omitted and that the harness — not the
    session — did it. The marker is extra to the budget, never subtracted from it.
    """
    if len(text) <= max_chars:
        return text
    head_n = max(0, max_chars // 3)
    tail_n = max(0, max_chars - head_n)
    omitted = len(text) - head_n - tail_n
    marker = (f"\n[… {omitted} chars omitted here by the {brand.NAME} harness to fit the "
              "excerpt budget — an excerpt cut, not the reply ending …]\n")
    tail = text[-tail_n:] if tail_n > 0 else ""
    return text[:head_n] + marker + tail
