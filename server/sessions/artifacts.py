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



def github_pr_refs(text: str) -> set[tuple[str, int]]:
    """Repository-qualified PR links in text; association must be verified separately."""
    refs = set()
    for url in re.findall(r"https?://(?:www\.)?github\.com/[^\s<>\\\"')]+", text):
        ref = github_pr_ref(url.rstrip(".,;"))
        if ref is not None:
            refs.add((ref[0].lower(), ref[1]))
    return refs


def created_pr_refs(events) -> set[tuple[str, int]]:
    """Recover ownership from paired successful shell `gh pr create` results.

    Reads provider-neutral replay events only. Prose, failed tools, view/list
    output and unpaired results cannot associate a PR with the session.
    """
    import json
    import shlex
    from server.sessions.events import ToolCall, ToolResult

    def creates_pr(command: str) -> bool:
        # Overall shell success proves create success only when create is the
        # terminal command, optionally after a successful directory change. Never
        # infer output provenance through pipelines, capture/echo or fallbacks.
        if "$(" in command or "`" in command or "<<" in command:
            return False
        try:
            lexer = shlex.shlex(command.strip(), posix=True, punctuation_chars=";&|()\n")
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return False
        if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] == "&&":
            tokens = tokens[3:]
        return (tokens[:3] == ["gh", "pr", "create"]
                and not any(t in (";", "&&", "||", "|", "(", ")", "\n", "&")
                            for t in tokens[3:]))

    calls: dict[tuple[str | None, str], bool] = {}
    refs: set[tuple[str, int]] = set()
    for event in events:
        key = (getattr(event, "parent_tool_use_id", None), getattr(event, "tool_id", ""))
        if isinstance(event, ToolCall):
            commands = []
            if event.name in ("Bash", "exec_command", "functions.exec_command"):
                commands = [event.input.get("command") or event.input.get("cmd") or ""]
            elif event.name == "functions.exec":
                code = event.input.get("code") or event.input.get("input") or ""
                # One literal command/result wrapper only: unrelated JS output
                # must not masquerade as the command's PR creation result.
                wrapper = re.fullmatch(
                    r"\s*text\(await tools\.exec_command\((\{.*\})\)\);?\s*", code, re.S)
                if wrapper:
                    try:
                        args = json.loads(wrapper.group(1))
                        commands = [args.get("cmd", "")]
                    except (ValueError, AttributeError):
                        pass
            if len(commands) == 1 and isinstance(commands[0], str) and creates_pr(commands[0]):
                calls[key] = event.name == "functions.exec"
        elif isinstance(event, ToolResult) and key in calls:
            requires_exit_code = calls.pop(key)
            if not event.ok:
                continue
            payload = event.payload
            if any(k in payload and payload[k] != 0 for k in ("exitCode", "exit_code")):
                continue
            # Nested exec output carries its shell exit code inside text JSON.
            text = json.dumps(payload, ensure_ascii=False)
            if re.search(r'\\?"exit_code\\?"\s*:\s*(?:null|[1-9][0-9]*|-)', text):
                continue
            if requires_exit_code and not re.search(r'\\?"exit_code\\?"\s*:\s*0\b', text):
                continue
            found = github_pr_refs(text)
            if len(found) == 1:
                refs.update(found)
    return refs


def created_worktree_candidates(events) -> list[tuple[str, str, float, float]]:
    """Explicit new-worktree commands to corroborate with Git, never proof alone."""
    import shlex
    from server.sessions.events import ToolCall, ToolResult
    calls: dict[tuple[str | None, str], tuple[str, str, float]] = {}
    out = []
    for event in events:
        key = (getattr(event, "parent_tool_use_id", None), getattr(event, "tool_id", ""))
        if isinstance(event, ToolCall) and event.name == "Bash" and event.ts is not None:
            command = event.input.get("command", "")
            try:
                lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
                lexer.whitespace_split = True
                tokens = list(lexer)
            except (TypeError, ValueError):
                continue
            for i in range(len(tokens)):
                if tokens[i:i + 3] != ["git", "worktree", "add"]:
                    continue
                if i and tokens[i - 1] not in ("&&", ";"):
                    continue
                args = tokens[i + 3:]
                branch = path = None
                while args and args[0] not in ("&&", ";", "||", "|"):
                    arg = args.pop(0)
                    if arg in ("-q", "--quiet"):
                        continue
                    if arg == "-b" and args:
                        branch = args.pop(0)
                    elif arg.startswith("-"):
                        break
                    elif path is None:
                        path = arg
                if branch and path and os.path.isabs(path):
                    calls[key] = (path, branch, event.ts)
        elif isinstance(event, ToolResult) and key in calls:
            path, branch, start = calls.pop(key)
            if event.ok and event.ts is not None:
                out.append((path, branch, start, event.ts))
    return out

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
    with p.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ToolArgError("image exceeds attachment size limit")
    media_type = sniff_media_type(data[:12])
    if media_type not in IMAGE_MEDIA_TYPES:
        raise ToolArgError(f"{value} is not an image by content (png, jpeg, gif or webp)")
    return data, media_type
