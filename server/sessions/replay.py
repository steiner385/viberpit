from __future__ import annotations
import json, asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from . import events as ev


def _ts_ms(o: dict) -> float | None:
    raw = o.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


@dataclass
class ReplayWindow:
    events: list = field(default_factory=list)
    earliest_turn: int = 0
    total_turns: int = 0


def _read_jsonl(path: str) -> list:
    """Yield the well-formed JSON objects from a transcript ``.jsonl``, skipping
    blank/garbage lines. Returns [] if the file is missing or unreadable."""
    out: list = []
    try:
        f = open(path, errors="ignore")
    except OSError:
        return out
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                out.append(o)
    return out


def _content_blocks(msg: dict) -> list[dict]:
    """Normalize a message's ``content`` to a list of dict blocks.

    Claude transcripts use TWO shapes: a plain string (common for user turns,
    e.g. ``"content": "tell me a lullaby"``) OR a list of content blocks. Coercing
    to a list of dict blocks keeps the iterators below from choking on a str (which
    would iterate characters → ``b.get()`` AttributeError → blank replay)."""
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _assistant_block_events(content: list[dict], uuid, ts, pid: str | None) -> list:
    """Emit the text / thinking / tool-call events for one assistant message's
    blocks, tagged with ``pid`` (None for the main thread, else the spawning
    Task's tool-use id for a sub-agent). Shared by the main and sub-agent parsers
    so the block-level decoding lives in ONE place."""
    out: list = []
    for b in content:
        t = b.get("type")
        if t == "text":
            out.append(ev.TextDelta(uuid, b.get("text", ""), uuid, parent_tool_use_id=pid, ts=ts))
        elif t == "thinking":
            out.append(ev.ThinkingDelta(uuid, b.get("thinking", ""), uuid, parent_tool_use_id=pid, ts=ts))
        elif t == "tool_use":
            if b.get("name") == "AskUserQuestion" and pid is None:
                # Mirror the live translator: AskUserQuestion is presented as the
                # selectable QuestionCard via Question events, never a raw ToolCall
                # (which renders the input as JSON). Reconstruct the Question event(s)
                # from the tool_use input so a detached / reloaded session shows the
                # question, not the raw JSON. (Sub-agents can't prompt the user, so
                # under a pid this falls through to a normal ToolCall — which also keeps
                # the untagged Question event from leaking into the main thread.)
                for qi, q in enumerate((b.get("input") or {}).get("questions") or []):
                    if not isinstance(q, dict):
                        continue
                    opts = [o2 if isinstance(o2, dict) else {"label": str(o2)}
                            for o2 in (q.get("options") or [])]
                    out.append(ev.Question(
                        question_id=f"{b.get('id') or uuid}-{qi}",
                        turn_id=uuid,
                        header=q.get("header", ""),
                        question=q.get("question", ""),
                        options=opts,
                        multi_select=bool(q.get("multiSelect", False)),
                        ts=ts,
                    ))
            else:
                out.append(ev.ToolCall(uuid, b.get("id", ""), b.get("name", ""),
                                       ev.tool_kind(b.get("name", "")), b.get("input", {}), uuid,
                                       parent_tool_use_id=pid, ts=ts))
    return out


def _tool_result_events(content: list[dict], uuid, ts, pid: str | None) -> list:
    """Emit ToolResult events for a user message's tool_result blocks, tagged with
    ``pid``. Shared by the main and sub-agent parsers."""
    return [ev.ToolResult(r["tool_use_id"], not r.get("is_error", False),
                          {"content": r.get("content")}, uuid, parent_tool_use_id=pid, ts=ts)
            for r in content if r.get("type") == "tool_result"]


def _build_events(path: str) -> tuple[list, object | None]:
    """Parse the full main JSONL into (events, meta_event). No trimming.

    Sub-agent (``isSidechain``) entries are skipped here: for Layout-A single-file
    transcripts this keeps their steps out of the main thread; for Layout-B they
    live in separate sidecar files that ``_subagent_events`` reconstructs."""
    out: list = []
    meta_event = None
    for o in _read_jsonl(path):
        if o.get("isSidechain"):
            continue
        typ = o.get("type")
        msg = o.get("message") or {}
        content = _content_blocks(msg)
        uuid = o.get("uuid")
        ts = _ts_ms(o)
        if typ == "user":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            imgs = []
            for b in content:
                if b.get("type") == "image":
                    src = b.get("source") or {}
                    # Skip blocks whose base64 payload was elided/truncated in the
                    # transcript: `data:{mt};base64,` with no payload is a structurally
                    # invalid URL the browser rejects (net::ERR_INVALID_URL).
                    data = src.get("data") or ""
                    if src.get("type") == "base64" and data:
                        mt = src.get("media_type", "image/png")
                        imgs.append({"url": f"data:{mt};base64,{data}",
                                     "mime": mt, "name": ""})
            # `isMeta` user entries are harness-injected context, not a real user
            # turn: the Skill-tool body ("Base directory for this skill: …"), the
            # "Continue from where you left off." resume prompt, Stop-hook notices,
            # etc. Rendering them makes the skill's own prompt look like the user's
            # (or our) message — suppress the bubble. (Any tool_results on the entry
            # still emit below; in practice these meta entries carry only text.)
            #
            # A background task's termination is delivered the same way — a plain-string
            # `type:"user"` entry holding a `<task-notification>` block — but is NOT
            # flagged `isMeta`. It is still harness plumbing (the live translator drops
            # it, forwarding only tool_result blocks from user messages), so suppress its
            # raw-XML bubble too. The tool_result-carried form still renders in its card.
            is_task_notification = text.lstrip().startswith("<task-notification>")
            # The CLI's auto-compaction summary ("This session is being continued from a
            # previous conversation…") is a `type:"user"` entry flagged `isCompactSummary`
            # / `isVisibleInTranscriptOnly` — NOT `isMeta`. It is a CLI-local artifact, not
            # a real user prompt (the live stream never shows it). Emitting it as a
            # UserMessage both renders a bogus giant bubble AND — because it is a
            # UserMessage — becomes the `drop_incomplete_turn` boundary, so a live reattach
            # would drop EVERY completed post-compaction turn (they live only on disk, not
            # in the in-progress live buffer) and they vanish from the reloaded transcript.
            is_compact_summary = bool(o.get("isCompactSummary"))
            if (text or imgs) and not o.get("isMeta") and not is_task_notification \
                    and not is_compact_summary:
                out.append(ev.UserMessage(turn_id=uuid, text=text, src_uuid=uuid,
                                          images=imgs or None, ts=ts))
            out.extend(_tool_result_events(content, uuid, ts, None))
        elif typ == "assistant":
            # Reconstruct the model once (status strip needs it after re-attach).
            model = msg.get("model")
            if model and meta_event is None:
                # Held aside (not appended) so trimming can't slice it away on
                # long sessions — the status strip needs the model after re-attach.
                meta_event = ev.SessionMeta(model=model, session_id=o.get("sessionId") or "")
            out.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid))
            # Per-turn model, mirroring the live translator, so replayed/historical
            # sessions show the model-switch divider when the model changes mid-session.
            if model:
                out.append(ev.TurnModel(turn_id=uuid, model=model))
            out.extend(_assistant_block_events(content, uuid, ts, None))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            out.append(ev.TurnEnd(uuid, usage, None, uuid))
    return out, meta_event


def _subagent_events(main_path: str, task_ids: set[str]) -> list:
    """Reconstruct each sub-agent's own thread (Layout B) for the Task tool-use ids
    in ``task_ids``, emitting its internal turns tagged with the spawning Task's
    tool-use id so the existing reducer routing (``state.subAgents[toolId]``) renders
    them inside the Task card — no frontend change, matching the live view.

    Sidecars live at ``<projects>/<encoded-cwd>/<session-id>/subagents/agent-*.jsonl``
    with an ``agent-*.meta.json`` carrying ``{agentType, description, toolUseId}``.
    The main transcript and the ``subagents/`` dir can live under DIFFERENT
    encoded-cwd dirs (main checkout vs. worktree), so we glob across ALL encoded-cwd
    dirs joining on the session-id, and dedupe by toolUseId (the same sub-agent may
    be recorded under more than one cwd).

    No TurnEnd is emitted for a sub-agent turn: it is not a block-event (so it never
    reaches the sub-thread) and, carrying a pid, would only clobber the main thread's
    usage snapshot. The sub-agent's own prompt is likewise not rendered as a user
    bubble — it is already the Task card's input (mirrors the live path, which
    forwards only tool_results from a sub-agent's user messages)."""
    if not task_ids:
        return []
    try:
        main = Path(main_path)
        sid = main.stem
        projects_root = main.parent.parent
    except (OSError, ValueError):
        return []
    events: list = []
    seen: set[str] = set()
    try:
        metas = sorted(projects_root.glob(f"*/{sid}/subagents/*.meta.json"))
    except OSError:
        return []
    for meta_path in metas:
        try:
            meta = json.loads(meta_path.read_text(errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        tuid = meta.get("toolUseId") if isinstance(meta, dict) else None
        if not isinstance(tuid, str) or tuid not in task_ids or tuid in seen:
            continue
        seen.add(tuid)
        agent_path = meta_path.parent / (meta_path.name[: -len(".meta.json")] + ".jsonl")
        for o in _read_jsonl(str(agent_path)):
            typ = o.get("type")
            msg = o.get("message") or {}
            content = _content_blocks(msg)
            uuid = o.get("uuid")
            ts = _ts_ms(o)
            if typ == "assistant":
                events.append(ev.TurnStart(turn_id=uuid, src_uuid=uuid, parent_tool_use_id=tuid))
                model = msg.get("model")
                if model:
                    events.append(ev.TurnModel(turn_id=uuid, model=model, parent_tool_use_id=tuid))
                events.extend(_assistant_block_events(content, uuid, ts, tuid))
            elif typ == "user":
                events.extend(_tool_result_events(content, uuid, ts, tuid))
    return events


def _is_in_progress_user(e, turn_started_at: float | None) -> bool:
    """Decide whether the positionally-last disk UserMessage really belongs to the
    IN-PROGRESS turn (safe to drop — the live buffer/ring re-supplies it) or to a
    PREVIOUS, COMPLETED turn (dropping it amputates history nothing re-supplies).

    The disk JSONL is written by the claude CLI, which LAGS the live event stream:
    on a resumed session the CLI spends seconds booting its MCP fleet before writing
    the new turn's user entry. A live attach in that window sees the previous turn's
    user entry as the file's last — the old positional rule dropped that completed
    turn from the replay, the live stream only re-supplied the current turn, and the
    previous turn silently vanished (no seq gap → no self-heal; the "transcript cut
    off after navigating away and back" bug).

    Invariant (constitution III — verified against the real binary, same host): the
    CLI processes a prompt only AFTER the driver sends it, so the in-progress turn's
    disk user entry is stamped ``timestamp >= turn_started_at`` while every earlier
    turn's entry is strictly older (seconds-to-minutes in the pathological window).
    2s slack absorbs coarse clock granularity; it only risks re-dropping in a sub-2s
    double-prompt race where the entry IS on disk — and dropping is then correct.

    ``turn_started_at is None`` (legacy caller / unknown start time) and a
    timestamp-less entry (``ts is None``) both keep the historical positional drop:
    real CLI transcripts always carry timestamps, and an unprovable entry defaults
    to the duplication-safe behavior the drop has always had."""
    if turn_started_at is None:
        return True
    ts = e.ts  # transcript timestamps parse to MILLISECONDS via _ts_ms
    if ts is None:
        return True
    return ts >= turn_started_at * 1000 - 2000


def _window(path: str, before: int | None, limit: int | None, drop_incomplete_turn: bool = False,
            include_subagents: bool = False,
            turn_started_at: float | None = None) -> ReplayWindow:
    out, meta_event = _build_events(path)
    if drop_incomplete_turn:
        # Drop the trailing in-progress logical turn: everything from the last real
        # UserMessage onward. The live driver buffer / daemon ring owns that turn, and
        # its blocks are flushed to disk mid-stream, so keeping them here duplicates.
        # But ONLY if that UserMessage actually IS the in-progress turn's — see
        # _is_in_progress_user for the resume-lag amputation this guards against.
        last_user = max((i for i, e in enumerate(out) if isinstance(e, ev.UserMessage)),
                        default=None)
        if last_user is not None and _is_in_progress_user(out[last_user], turn_started_at):
            out = out[:last_user]
    starts = [i for i, e in enumerate(out) if isinstance(e, ev.TurnStart)]
    ends   = [i for i, e in enumerate(out) if isinstance(e, ev.TurnEnd)]
    total = len(starts)
    if total == 0:
        # No assistant turns — return any user/tool events as-is (e.g. an
        # image-only user turn with no reply yet, or an empty transcript).
        return ReplayWindow(events=list(out), earliest_turn=0, total_turns=0)
    end = total if before is None else max(0, min(before, total))
    start = 0 if limit is None else max(0, end - limit)  # limit=None → NO cap
    # Include user messages / tool-results that precede the first TurnStart in
    # the window: they sit between the previous TurnEnd (or file start) and the
    # TurnStart. Without this, slicing from starts[start] drops those events.
    slice_lo = 0 if start == 0 else ends[start - 1] + 1
    slice_hi = starts[end] if end < total else len(out)
    window = out[slice_lo:slice_hi]
    if include_subagents:
        # Reconstruct sub-agent threads for exactly the Task calls in THIS window
        # (scoping to the window naturally excludes sub-agents of trimmed turns).
        # Appended after the main slice: the reducer routes by parentToolUseId, so
        # position is irrelevant — the Task card renders wherever its call sits.
        task_ids = {e.tool_id for e in window if isinstance(e, ev.ToolCall)}
        window = window + _subagent_events(path, task_ids)
    if meta_event is not None:
        window = [meta_event] + window
    return ReplayWindow(events=window, earliest_turn=start, total_turns=total)


def _parse(path: str, max_turns: int | None) -> list:
    """Back-compat: last `max_turns` turns as a flat event list (None → ALL turns).

    Main-thread only (no sub-agent reconstruction) — this feeds the sub-agent
    SUMMARY and suggestion generators, which must not see nested sub-agent turns."""
    return _window(path, before=None, limit=max_turns).events


async def replay_window(jsonl_path: str, *, before: int | None = None, limit: int | None = 20,
                        drop_incomplete_turn: bool = False,
                        turn_started_at: float | None = None) -> ReplayWindow:
    # The reattach / transcript-window UI path: reconstruct sub-agent threads so a
    # completed sub-agent shows its internal turns inside its Task card, as it does live.
    # limit=None returns the FULL transcript (no cap) — the review/export path.
    # turn_started_at (epoch SECONDS, from the live driver / daemon turn tracking)
    # scopes drop_incomplete_turn to the turn that actually is in progress.
    return await asyncio.to_thread(_window, jsonl_path, before, limit, drop_incomplete_turn, True,
                                   turn_started_at)


async def replay_events(jsonl_path: str, *, max_turns: int | None = 20) -> list:
    return await asyncio.to_thread(_parse, jsonl_path, max_turns)
