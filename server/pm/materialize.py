"""Pure decomposition core for plan materialization (spec 003 §7b, #992).

A merged plan with no epic is handed to a decompose session; its fenced JSON
becomes a reviewable ``MaterializationProposal``; accepting it creates the epic,
the tasks as sub-issues, and the blocked-by chain. Everything in this module is
pure — prompt text in, structures out, mutation documents out. The orchestrator
(Task 14) and the apply path (Task 15) own the I/O and the journal.

Board membership, Stage and Score are deliberately NOT written here: the
reconciler owns every board field (spec §6).
"""
from __future__ import annotations

from dataclasses import dataclass

from .fenced_json import FencedJsonError, extract_fenced_json
from .gh_mutations import _q
from .reconcile_diff import Mutation


class MaterializationParseError(ValueError):
    """A decompose session's output is not a usable decomposition."""


@dataclass(frozen=True)
class TaskSpec:
    title: str
    body: str
    depends_on: tuple[int, ...] = ()  # indexes into Decomposition.tasks


@dataclass(frozen=True)
class Decomposition:
    epic_title: str
    epic_body: str
    tasks: tuple[TaskSpec, ...]


# --------------------------------------------------------------------------- #
# Prompt                                                                       #
# --------------------------------------------------------------------------- #

def build_materialize_prompt(slug: str, repo: str, plan_markdown: str) -> str:
    """Seed prompt for a decompose session (spec 003 §7b).

    The session only *proposes*: the JSON it returns is parsed into a
    `Decomposition`, stored as a proposal and shown to a human — nothing is
    created on GitHub until that human accepts."""
    return (
        f"You are decomposing the merged implementation plan for initiative "
        f"`{slug}` (repo `{repo}`) into a GitHub epic and its task issues.\n\n"
        "PLAN:\n"
        f"{plan_markdown}\n\n"
        "Produce ONE epic and an ordered list of tasks. Order the tasks as the "
        "phases of the plan: each task is treated as blocked by the task before "
        "it, so sibling order IS the execution order. Use `depends_on` only for "
        "an ADDITIONAL dependency on an earlier task (by its 0-based index in "
        "`tasks`); never point at a later task. Each task must be independently "
        "implementable and verifiable by one agent session; give it a body with "
        "the files to touch and how to verify.\n\n"
        "If the plan's Tests section (or the spec it came from) cites stable "
        "requirement/acceptance-criteria IDs (`FR-NNN`, `AC-NNN`, `US-N`), end "
        "each task's body with a `Covers: FR-…, AC-…` line naming every ID that "
        "task's phase satisfies, copied verbatim from the plan — never invent an "
        "ID that is not already in the plan. Omit the line for a task the plan "
        "gives no IDs for. Give the epic body a matching `## Requirements "
        "covered` index: one line per ID, naming the task(s) that cover it.\n\n"
        "Do NOT create anything on GitHub — this is a proposal a human reviews.\n\n"
        "Return ONLY a JSON object in a ```json fenced block, of exactly this "
        "shape:\n"
        "```json\n"
        '{"epic_title": "...", "epic_body": "...", '
        '"tasks": [{"title": "...", "body": "...", "depends_on": [0]}]}\n'
        "```"
    )


# --------------------------------------------------------------------------- #
# Parser                                                                       #
# --------------------------------------------------------------------------- #

def _coerce_index(value: object) -> int:
    if isinstance(value, bool):  # bool is an int subclass — reject it
        raise MaterializationParseError(f"bad depends_on entry {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    # An index we cannot read is silent data loss: dropping it would drop a
    # real dependency from the chain, so fail the whole proposal instead.
    raise MaterializationParseError(f"bad depends_on entry {value!r}")


def _to_task(row: object) -> TaskSpec:
    if not isinstance(row, dict):
        raise MaterializationParseError(f"task is not an object: {row!r}")
    title = str(row.get("title") or "").strip()
    if not title:
        raise MaterializationParseError("task has no title")
    depends = row.get("depends_on") or []
    if not isinstance(depends, list):
        raise MaterializationParseError(f"depends_on is not an array: {depends!r}")
    return TaskSpec(
        title=title,
        body=str(row.get("body") or "").strip(),
        depends_on=tuple(_coerce_index(d) for d in depends),
    )


def parse_materialization_output(text: str) -> Decomposition:
    """Extract the decomposition object from a decompose session's output.

    Tolerant about *where* the JSON is (shared ``fenced_json`` extractor) and
    strict about what it says: a proposal with no readable tasks is worse than
    no proposal, because a human would be asked to accept an empty plan."""
    try:
        data = extract_fenced_json(text, shape="object")
    except FencedJsonError as e:
        raise MaterializationParseError(str(e)) from e
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise MaterializationParseError("output has no `tasks` array")
    if not tasks:
        raise MaterializationParseError("decomposition has no tasks")
    return Decomposition(
        epic_title=str(data.get("epic_title") or "").strip(),
        epic_body=str(data.get("epic_body") or "").strip(),
        tasks=tuple(_to_task(row) for row in tasks),
    )


# --------------------------------------------------------------------------- #
# Dependency chain                                                             #
# --------------------------------------------------------------------------- #

def chain_edges(d: Decomposition) -> list[tuple[int, int]]:
    """`(blocked_index, blocker_index)` pairs: explicit `depends_on` ∪ sibling order.

    Sibling order is the plan's phase order (task k is blocked by task k−1), and
    `depends_on` adds any cross-phase dependency the session called out. Deduped
    and sorted so the journal rows for one proposal are stable across a resume."""
    n = len(d.tasks)
    edges: set[tuple[int, int]] = set()
    for k, task in enumerate(d.tasks):
        if k > 0:
            edges.add((k, k - 1))
        for dep in task.depends_on:
            if not 0 <= dep < n:
                raise MaterializationParseError(
                    f"task {k} depends on out-of-range index {dep}")
            if dep == k:
                raise MaterializationParseError(f"task {k} depends on itself")
            edges.add((k, dep))
    _assert_acyclic(edges, n)
    return sorted(edges)


def _assert_acyclic(edges: set[tuple[int, int]], n: int) -> None:
    """Kahn's algorithm over blocker → blocked; a leftover node means a cycle."""
    blockers: dict[int, set[int]] = {i: set() for i in range(n)}
    for blocked, blocker in edges:
        blockers[blocked].add(blocker)
    ready = [i for i in range(n) if not blockers[i]]
    done: set[int] = set()
    while ready:
        node = ready.pop()
        done.add(node)
        for other, deps in blockers.items():
            if other not in done and deps <= done and other not in ready:
                ready.append(other)
    if len(done) != n:
        raise MaterializationParseError(
            f"dependency cycle among tasks {sorted(set(range(n)) - done)}")


# --------------------------------------------------------------------------- #
# Mutation documents                                                           #
# --------------------------------------------------------------------------- #

def build_create_issue(repository_id: str, title: str, body: str, *,
                       parent_issue_id: str | None = None) -> str:
    """One `createIssue` document, optionally creating the issue as a sub-issue.

    `CreateIssueInput.parentIssueId` is probe-verified (phase0-findings,
    "Materialization probe"): a task becomes a sub-issue of the epic in the SAME
    call, so there is no `addSubIssue` write to journal and no dependence on the
    fresh-id window. Each create is its own call — one journal row maps to one
    GitHub call, which is what makes a resume after a crash safe."""
    parent = f", parentIssueId: {_q(parent_issue_id)}" if parent_issue_id else ""
    return (
        "mutation { createIssue(input: {repositoryId: %s, title: %s, body: %s%s}) "
        "{ issue { id number url } } }"
        % (_q(repository_id), _q(title), _q(body), parent))


def blocked_by_mutation(blocked_node: str, blocker_node: str, *,
                        initiated_by: str) -> Mutation:
    """The chain edge as the reconciler's own `Mutation` — never a second builder.

    Task 15 batches these through `gh_mutations.build_batch`, and because the
    fields match `reconcile_diff.compile_edges` exactly, `key()` is identical to
    the key the reconciler would compute for the same edge — so the journal row
    written here suppresses the reconciler's duplicate write."""
    return Mutation("add_blocked_by", blocked_node, None, None, blocker_node,
                    initiated_by)
