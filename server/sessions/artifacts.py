"""`share_artifact` — the pure half: argument validation and image ingest.

A session names its deliverables in prose ("see /tmp/shot.png", "PR: https://…") or
they sit inside a tool result — and the transcript collapses every tool row once the
next one follows, so the user reads "I've attached the screenshot" and sees nothing.
`share_artifact` makes the deliverable a first-class, always-visible card.

Three kinds, one value each:

* ``url``   — an absolute http(s) URL. Nothing else: a ``javascript:`` or ``data:``
              value would become a clickable anchor in the user's browser.
* ``path``  — an absolute path on the session host, rendered as a copyable chip. The
              browser cannot open it; the card says where it is.
* ``image`` — an absolute path to a png/jpeg/gif/webp on the session host. The daemon
              that owns the session has the file and the browser does not, so the
              handler ingests it into the content-addressed attachment store (shared
              across hosts) and the card renders ``/api/attachments/{id}`` inline.

The image cap and sniffing are the attachment store's own (``MAX_BYTES``,
``sniff_media_type``) — one rule for "what image bytes does the edge serve", whether
a person uploaded them or a session shared them. Content is sniffed, never trusted
by extension: a text file named ``.png`` is refused, not served as an image.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from server.mcp_spec import ToolArgError
from server.sessions.attachments import MAX_BYTES, sniff_media_type

ARTIFACT_KINDS: tuple[str, ...] = ("url", "path", "image")

#: The image types the card renders inline — the attachment store's image subset.
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

LABEL_MAX = 120
NOTE_MAX = 500


def parse_share_artifact(args: dict) -> dict:
    """Validate a `share_artifact` call; return the normalized artifact fields.

    Raises ``ToolArgError`` with a corrective message the model can act on.
    """
    kind = args.get("kind")
    if kind not in ARTIFACT_KINDS:
        raise ToolArgError(f"kind must be one of {', '.join(ARTIFACT_KINDS)}")
    value = str(args.get("value") or "").strip()
    label = str(args.get("label") or "").strip()
    if not label:
        raise ToolArgError("label is required: a short human name for what this is")
    if kind == "url":
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ToolArgError("value must be an absolute http(s) URL")
    elif not value or not os.path.isabs(value):
        raise ToolArgError("value must be an absolute path on this host")
    out = {"kind": kind, "value": value, "label": label[:LABEL_MAX]}
    note = str(args.get("note") or "").strip()
    if note:
        out["note"] = note[:NOTE_MAX]
    return out


_GITHUB_PR_RE = re.compile(r"^/([^/]+/[^/]+)/pull/(\d+)(?:/|$)")


def github_pr_ref(url: str) -> tuple[str, int] | None:
    """``(owner/repo, number)`` when *url* is a github.com pull-request URL, else None."""
    parts = urlsplit(url)
    if parts.hostname not in ("github.com", "www.github.com"):
        return None
    m = _GITHUB_PR_RE.match(parts.path)
    return (m.group(1), int(m.group(2))) if m else None


def read_image(value: str) -> tuple[bytes, str]:
    """Read an image the session named, on this host. Returns ``(bytes, media_type)``.

    Refuses (as ``ToolArgError``) a missing or non-regular file, one over the
    attachment cap, and any content that does not sniff as a supported image.
    """
    p = Path(value)
    try:
        st = p.stat()
    except FileNotFoundError:
        raise ToolArgError(f"no such file on this host: {value}") from None
    except OSError as e:
        raise ToolArgError(f"cannot read {value}: {e.strerror or e}") from None
    if not p.is_file():
        raise ToolArgError(f"not a regular file: {value}")
    if st.st_size > MAX_BYTES:
        raise ToolArgError(f"{value} is {st.st_size} bytes; the image cap is {MAX_BYTES} bytes")
    data = p.read_bytes()
    media_type = sniff_media_type(data[:12])
    if media_type not in IMAGE_MEDIA_TYPES:
        raise ToolArgError(f"{value} is not an image by content (png, jpeg, gif or webp)")
    return data, media_type
