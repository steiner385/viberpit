"""Parse a git remote URL into GitHub's ``owner/name`` slug.

One parser for every place that needs to name a checkout's repository from its
``origin`` remote: the SHA fence (``server/github/sha_fence.py``) and the deploy
controller (``server/deploy/git.py``) both used to carry their own copy, and two
copies of URL parsing disagree on exactly the odd forms (trailing slash, ``ssh://``
scheme) that only show up on a host you did not test on.
"""
from __future__ import annotations


def slug_from_remote_url(url: str) -> str:
    """``owner/name`` for an ssh (``git@host:o/n.git``, ``ssh://git@host/o/n.git``) or
    https (``https://host/o/n[.git][/]``) remote URL. Raises ``ValueError`` for anything
    that does not resolve to exactly one ``owner/name`` pair."""
    url = (url or "").strip()
    if not url:
        raise ValueError("empty remote url")
    tail = url.rsplit(":", 1)[-1] if url.startswith("git@") else url.rstrip("/").split("/", 3)[-1]
    tail = tail.removesuffix(".git").strip("/")
    if tail.count("/") != 1 or not all(tail.split("/")):
        raise ValueError(f"unrecognised remote url: {url!r}")
    return tail
