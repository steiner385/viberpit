"""THE parser for a ``owner/repo#123`` issue ref (spec 003 §4).

One definition, because the refs are the mirror's join keys: the map API, the
queue loader and the dep writers all turn a ref back into a
``(repo, number)`` pair to look the issue up, and three private copies of the
same two lines are three chances for one of them to disagree about what a
malformed ref means (the answer is ``None`` — never a partial match, never a
raise).
"""
from __future__ import annotations


def split_ref(ref: str) -> tuple[str, int] | None:
    """``"owner/repo#123"`` → ``("owner/repo", 123)``; anything else → ``None``.

    Splits on the LAST ``#`` so a repo name containing one cannot swallow the
    number."""
    repo, _, num = ref.rpartition("#")
    return (repo, int(num)) if repo and num.isdigit() else None
