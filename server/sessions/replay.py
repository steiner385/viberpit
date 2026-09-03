from __future__ import annotations
import json, asyncio, logging, os, time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from server.config import env_float
from . import events as ev
from .transcript_entries import is_harness_user_entry

log = logging.getLogger("server.sessions.replay")


# Wall-clock ceiling for the cross-cwd sidecar scan. The live fleet has ~1,696
# project dirs on an sshfs mount at ~54ms RTT, so an unbounded scan hangs the
# transcript RPC for 30s+ (RC2). Sub-agent reconstruction is an enhancement to
# the Task card; opening the session is not optional — so we cap the search and
# return what we found.
#
# Bound at IMPORT time: an env override must be set before this module is imported.
# In-process (tests) the only way to change it is to patch the constant, or pass
# ``budget_s=`` to _find_subagent_meta_files.
_SUBAGENT_SCAN_BUDGET_S = env_float("CCSWITCH_SUBAGENT_SCAN_BUDGET_S", 1.0, minimum=0.001)

# Sub-agent sidecar locations change only when a sub-agent starts or finishes, so a
# short TTL removes the repeat cost of reattach / scrollback paging without going stale.
_SUBAGENT_CACHE_TTL_S = env_float("CCSWITCH_SUBAGENT_CACHE_TTL_S", 30.0, minimum=0.0)

# INVARIANT — the cache key is (projects_root, sid) and deliberately OMITS `preferred`.
# That is only sound because a result is cached ONLY when the scan ran to completion
# (`complete is True`), and a complete scan has visited EVERY directory under
# projects_root — so its result is identical for any `preferred`, i.e. preferred-
# independent. If partial/truncated results are ever cached, `preferred` MUST become
# part of the key (a truncated scan seeded from a different preferred dir returns a
# different subset) — otherwise one caller's short answer is served to all the others.
#
# THREADING: this dict is read and written from asyncio.to_thread() worker threads
# with no lock. That is safe: dict __getitem__/__setitem__/get are individually
# atomic under the GIL and the only mutations here are a whole-value get and a
# whole-value set (never a read-modify-write of one entry). A racing double-scan
# just writes the same value twice. Don't "fix" this with a lock, and don't turn
# the set into a compound update (e.g. mutating the cached list in place) — that
# would break the atomicity this relies on.
_subagent_cache: dict[tuple[str, str], tuple[float, list[Path]]] = {}


def _subagent_cache_clear() -> None:
    """Drop the discovery cache (tests, and any explicit invalidation)."""
    _subagent_cache.clear()


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


def _collapse_recovery_resend_dupes(out: list) -> list:
    """Suppress the duplicate user turn a recovery *resend* leaves in the transcript.

    Rate-limit / crash recovery resends the last prompt into a ``resume=``d driver
    (orchestrator ``_replay_same_account`` / ``_reassign``). The ORIGINAL user turn was
    already persisted to the CLI's JSONL before the failure, so the resend appends a
    SECOND identical UserMessage to the same file. The CLI owns the JSONL (constitution
    I — never fork/patch the binary), so we cannot dedupe at write time; we suppress on
    replay so every disk-replay stops rendering ``[user X (no reply)] [user X] [reply]``.

    Suppress the EARLIER of two user turns iff its FULL identity — text AND images —
    is IDENTICAL to the immediately-FOLLOWING user turn AND it produced NO
    assistant/tool content between the two (a dead "dropped turn" head). A true
    recovery resend re-persists the exact same prompt it failed to complete, images
    included, so requiring image equality (not just text) keeps this from collapsing
    two genuinely-different image-bearing turns that merely share caption text (e.g.
    two blank-caption screenshots) — that would silently drop the first image.
    Intervening non-content lines (isMeta / system / summary) emit no events in
    ``_build_events``, so the two dead-adjacent UserMessages sit at consecutive
    indices; ANY real content event (TurnStart/TextDelta/ToolCall/ToolResult/…) pushes
    them apart and correctly defeats suppression — an interrupted turn whose partial
    output is genuine transcript must keep rendering. The later turn carries the real
    completion and is kept; applied across every adjacent pair so N consecutive dead
    resends collapse to the final one.

    Distinct from the trailing-incomplete-turn drop (``drop_incomplete_turn`` /
    ``_is_in_progress_user``): this runs on SETTLED history anywhere in the file; that
    one trims the IN-PROGRESS tail owned by the live buffer. The two never entangle."""
    user_idx = [i for i, e in enumerate(out) if isinstance(e, ev.UserMessage)]
    drop: set[int] = set()
    for a, b in zip(user_idx, user_idx[1:]):
        if b == a + 1 and out[a].text == out[b].text and out[a].images == out[b].images:
            drop.add(a)
    if not drop:
        return out
    return [e for i, e in enumerate(out) if i not in drop]


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
            # Only a HUMAN prompt renders as a user bubble. The CLI journals several
            # `type:"user"` entries nobody typed — `isMeta` context (a Skill body, the
            # resume prompt), the auto-compaction summary (`isCompactSummary`, which as a
            # UserMessage would also become the `drop_incomplete_turn` boundary and wipe
            # every completed post-compaction turn on a live reattach), a background
            # task's `<task-notification>`, and the `/model` local-command records a
            # `set_model` writes (`<command-name>…`, `<local-command-stdout>Set model
            # to fable…`) — none of which the live stream shows. The predicate is shared
            # with the fallback titler (transcript_entries) so the two readers of the
            # transcript can't disagree about what a prompt is. (Any tool_results on the
            # entry still emit below.)
            if (text or imgs) and not is_harness_user_entry(o, text):
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
    # Collapse the duplicate user turn a recovery resend leaves behind. Done here, in
    # the single shared reconstruction path, so every consumer (replay_window /
    # transcript_events / export / replay_events) inherits it and the drop_incomplete
    # tail-trim below anchors on the deduped last user entry.
    return _collapse_recovery_resend_dupes(out), meta_event


def _find_subagent_meta_files(projects_root: Path, sid: str, preferred: Path | None = None,
                              *, budget_s: float | None = None) -> tuple[list[Path], bool]:
    """Locate `<projects>/<any-cwd>/<sid>/subagents/*.meta.json`, bounded.

    Returns (sorted meta paths, complete). ``complete`` is False when the scan
    ran out of budget, so the caller knows the list may be short.

    The sidecar usually sits under the SAME encoded-cwd as the main transcript,
    so `preferred` is probed first and costs one filesystem op. The remaining
    directories are still scanned — the sidecar can live under a different cwd
    (main checkout vs. worktree) and that join is load-bearing — but under a
    wall-clock budget so a slow mount degrades instead of hanging.
    """
    budget = _SUBAGENT_SCAN_BUDGET_S if budget_s is None else budget_s
    deadline = time.monotonic() + budget
    found: list[Path] = []
    seen_dirs: set[str] = set()
    complete = True

    key = (str(projects_root), sid)
    hit = _subagent_cache.get(key)
    if hit is not None and time.monotonic() < hit[0]:
        return list(hit[1]), True

    def _collect(cwd_dir: Path) -> bool:
        """Probe one <cwd>/<sid>/subagents dir. Returns False on OSError so the
        caller can mark the overall result incomplete — an error here must not be
        silently reported as if the directory had been fully scanned (that false
        completeness would otherwise get cached and become a permanent wrong answer)."""
        sub = cwd_dir / sid / "subagents"
        try:
            if not sub.is_dir():
                return True
            found.extend(p for p in sub.iterdir() if p.name.endswith(".meta.json"))
            return True
        except OSError:
            return False

    if preferred is not None:
        seen_dirs.add(preferred.name)
        if not _collect(preferred):
            complete = False

    try:
        it = os.scandir(projects_root)
    except OSError:
        # Couldn't even open the root — we scanned nothing beyond the fast path.
        return sorted(found), False

    try:
        with it:
            while True:
                if time.monotonic() >= deadline:
                    complete = False
                    break
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    # A flaky/stale handle on one entry (plausible on sshfs)
                    # must not abort the sweep, but it does mean the sweep
                    # can no longer be trusted as exhaustive.
                    complete = False
                    continue
                try:
                    if entry.name in seen_dirs or not entry.is_dir():
                        continue
                    seen_dirs.add(entry.name)
                    if not _collect(Path(entry.path)):
                        complete = False
                except OSError:
                    complete = False
                    continue
    except OSError:
        complete = False

    if not complete:
        # The scan truncates on essentially every open on the live fleet (1,696 dirs
        # against a 1.0s default budget), so an unconditional warning floods the log on
        # every session open. Severity therefore keys off whether we have any EVIDENCE
        # that sub-agent data exists:
        #   found + truncated  → WARNING. Sidecars demonstrably exist for this session,
        #     so the directories we never reached may hold more of them under a
        #     different encoded cwd (main checkout vs. worktree). Actionable: the Task
        #     card can render an incomplete set of sub-agent threads.
        #   nothing found + truncated → DEBUG. We have no evidence sub-agents exist at
        #     all, and on this fleet (zero `subagents/` dirs across all 8 accounts)
        #     that is the normal steady state on every single request.
        if found:
            log.warning(
                "subagent scan budget exhausted after %.2fs (projects_root=%s sid=%s) — "
                "found %d sidecar(s); sub-agent threads may be incomplete",
                budget, projects_root, sid, len(found),
            )
        else:
            log.debug(
                "subagent scan budget exhausted after %.2fs (projects_root=%s sid=%s) — "
                "no sidecars found; no evidence this session has sub-agents",
                budget, projects_root, sid,
            )
    result = sorted(found)
    if complete:
        _subagent_cache[key] = (time.monotonic() + _SUBAGENT_CACHE_TTL_S, list(result))
    return result, complete


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
    metas, _complete = _find_subagent_meta_files(projects_root, sid, preferred=main.parent)
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
    # A "turn" here is one assistant JSONL entry, so a tool-heavy LOGICAL turn
    # spans many of them — a window whose head lands mid-logical-turn carries the
    # turn's content with NO owning UserMessage at all. Client-side that leaves
    # the sent-prompt buffer unconsumed, and every reconnect re-asserts the
    # prompt at the very bottom of the transcript ("last prompt stuck at the
    # bottom"). Prepend the nearest preceding UserMessage so the turn's prompt
    # always travels with its content. Detection: no UserMessage between the
    # slice head and the window's first TurnStart (a boundary-start window has
    # its user entry in that gap). A drop_incomplete trim removed the open
    # turn's prompt from `out` entirely, so it can never be re-added here — the
    # nearest preceding UserMessage then belongs to a completed turn, which is
    # exactly the prompt its sliced content needs.
    if start > 0 and end > start:
        first_ts = starts[start]
        head_has_user = any(isinstance(e, ev.UserMessage) for e in out[slice_lo:first_ts])
        if not head_has_user:
            owner = next((e for e in reversed(out[:slice_lo])
                          if isinstance(e, ev.UserMessage)), None)
            if owner is not None:
                window = [owner] + window
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


def last_question_events(path: str) -> list["ev.Question"]:
    """Question events for the LAST AskUserQuestion tool_use in *path*, marked orphaned.

    Reuses the same reconstruction the full replay uses (``_assistant_block_events``)
    so the re-offer can never drift from what the transcript already rendered — in
    particular the ``f"{tool_use_id}-{index}"`` ids, which is what lets the client's
    reducer UPGRADE the card already on screen instead of appending a duplicate.

    Only the most recent call is returned: a long session may have asked several
    times, and only the one open at the restart is still awaiting an answer.
    Never raises — a missing or corrupt transcript yields [].

    Synchronous (so it stays exhaustively table-testable) but it reads and parses
    a whole transcript, so callers on the event loop MUST wrap it in
    ``asyncio.to_thread`` — as every other transcript reader in this module does.
    """
    latest: list[ev.Question] = []
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                # _ts_ms, not the raw ISO string: Question.ts is float-millis on
                # the wire (and numeric in the client's EvQuestion). A string
                # would only show when the block does NOT already exist — i.e.
                # the question fell outside the replay window — and the reducer
                # would then mint a block with a string ts.
                qs = [e for e in _assistant_block_events(
                          content, d.get("uuid"), _ts_ms(d), None)
                      if isinstance(e, ev.Question)]
                if qs:
                    latest = qs
    except OSError:
        return []
    for q in latest:
        q.orphaned = True
    return latest
