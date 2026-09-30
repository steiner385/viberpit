# Decision escalation PR 6 — readable large assistant results — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The assistant can read `get_product_deps`, `list_prs` and `list_automations` — today 58–329 KB payloads — because each returns a projected/summary shape by default, and every tool result is capped at 48 KB with an explicit marker saying what was cut.

**Architecture:** Three tool handlers gain presentation-only projection and filters (the precedent is `_trim_session` / `list_prs`' `repo` filter); the deps endpoint gains additive query params (`active`, `open_only`, `summary`) whose defaults preserve today's web response; a pure `cap_result` runs inside `wrap_untrusted`, the one envelope every handler's return passes through. `reverse_blockers` lands in `server/pm/drift.py` as the single reverse dependency index (PR 2's escalation module imports it from there).

**Tech Stack:** Python 3 / FastAPI (server), pytest, httpx MockTransport for tool tests.

**Spec:** `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §6 (rows 4–6 and "Result byte cap"), §7, §10 PR 6.

## Global Constraints

- Branch off `origin/master` (`git fetch && git checkout -b feat/assistant-readable-results origin/master`).
- Test env and gates exactly as in plan 1's Global Constraints (`env -u …` + the main checkout's `.venv`; ruff, mypy, `pytest tests/`, touched files, full `-m "not slow"` run in the background; commit/PR text via files; trailers).
- One tool → one endpoint (`test_every_tool_hits_an_allowed_route` enforces it). Projection and filtering happen in the handler; no new business logic.
- The envelope's existing `truncated: int` (from `Trimmed`) keeps its meaning; the byte cap reports under a NEW key `capped` so no existing assertion changes. (Spec §6 wrote it as `truncated: {…}`; amend that sentence in the spec in this PR.)
- `_ALL_OPTIONAL_TOOLS` in `server/tests/test_assistant_tools.py` already lists `list_prs` and `list_automations`; `get_product_deps` keeps `slug` required.
- No new tools, so the doc pack table, the eval case count and the pinned case hash are untouched.

## Review Focus

1. A payload over the cap with NO list to shrink (a dict of many 2000-char strings) must be returned as-is with no `capped` key — halving nothing forever is an infinite loop. Pinned in Task 1.
2. `list_prs(state="open")` must include `draft` PRs (state literal `'draft'` is an open PR) and exclude `merged`/`closed`; an operator asking "what's open" expects to see their draft. Pinned in Task 2.
3. `get_product_deps` for a product with an unlinked board (`gh is None`) in summary mode must still answer with `project: null` and zero drift counts, not 500. Pinned in Task 4.
4. `reverse_blockers` must ignore inactive edges and self-edges (`src == dst`), else a stale prose dep inflates "blocks N". Pinned in Task 3.
5. `list_automations(verbose=false)` must keep `status.products` (the runner's per-product rows) — dropping it would hide the very breakdown PR 1 added. Pinned in Task 2.

---

### Task 1: Byte cap in the envelope

**Files:**
- Modify: `server/assistant/tools.py:277-317` (`_scrub`, `Trimmed`, `wrap_untrusted`)
- Test: `server/tests/test_assistant_tools.py`

**Interfaces:**
- Produces: `cap_result(data: Any, *, max_bytes: int = _MAX_RESULT_BYTES) -> tuple[Any, dict | None]`; envelope key `capped: {"path": str, "kept": int, "dropped": int}` when a cut happened.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_assistant_tools.py`:

```python
def test_cap_result_halves_the_longest_list_until_it_fits():
    from server.assistant.tools import cap_result
    data = {"project": {"title": "x"}, "edges": [{"src": f"o/r#{i}", "dst": "o/r#0",
                                                  "source": "native"} for i in range(4000)],
            "drift": {"proseOnly": [{"src": "a", "dst": "b"}] * 5}}
    out, capped = cap_result(data, max_bytes=20_000)
    assert capped["path"] == "edges"
    assert capped["kept"] == len(out["edges"]) and capped["dropped"] == 4000 - capped["kept"]
    assert len(json.dumps(out, separators=(",", ":"))) <= 20_000
    assert out["drift"] == data["drift"], "only the longest list is cut"


def test_cap_result_cuts_a_top_level_list_and_a_list_one_level_down():
    from server.assistant.tools import cap_result
    rows = [{"n": i, "pad": "x" * 100} for i in range(2000)]
    out, capped = cap_result(rows, max_bytes=10_000)
    assert capped["path"] == "" and len(out) < 2000
    out, capped = cap_result({"data": {"items": rows}}, max_bytes=10_000)
    assert capped["path"] == "data.items" and len(out["data"]["items"]) < 2000


def test_cap_result_leaves_a_small_or_uncuttable_payload_alone():
    from server.assistant.tools import cap_result
    small = {"a": [1, 2, 3]}
    assert cap_result(small, max_bytes=1_000) == (small, None)
    # Over the cap but nothing to shrink: returned as-is, never an infinite loop.
    fat = {f"k{i}": "y" * 1_900 for i in range(30)}
    out, capped = cap_result(fat, max_bytes=1_000)
    assert out == fat and capped is None


@pytest.mark.asyncio
async def test_guarded_envelope_reports_the_cap_under_its_own_key():
    from server.assistant import tools as tools_mod
    big = [{"repo": "o/r", "number": i, "title": "t" * 200, "state": "open",
            "stage": "ci", "checks": "pending"} for i in range(3000)]

    def handler(req: httpx.Request):
        return httpx.Response(200, json=big)

    with patch.object(tools_mod, "_MAX_RESULT_BYTES", 30_000):
        out = await _tool(build_tools(_api(handler)), "list_prs").handler({"limit": 3000})
    body = json.loads(out["content"][0]["text"])
    assert body["capped"]["path"] == "" and body["capped"]["dropped"] > 0
    assert len(out["content"][0]["text"]) <= 30_000 + 512  # envelope overhead only
```

(`from unittest.mock import patch` at the top of the file if missing.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `<venv>/bin/python -m pytest server/tests/test_assistant_tools.py -k "cap_result or envelope_reports" -v`
Expected: FAIL with `ImportError: cannot import name 'cap_result'`.

- [ ] **Step 3: Implement**

In `server/assistant/tools.py`, after `Trimmed`:

```python
# The CLI hands a tool result to the model as one text block; past a few tens of
# kilobytes it crowds out the conversation and, for the largest endpoints
# (`/api/prs`, `/deps`), can exceed what the transport accepts outright. Every
# handler's return passes through `wrap_untrusted`, so this is the one place a
# result is bounded. The cut is reported under its OWN key (`capped`) — `truncated`
# is `Trimmed`'s per-tool drop count and keeps its meaning.
_MAX_RESULT_BYTES = 48_000


def _byte_len(data: Any) -> int:
    return len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())


def _longest_list(data: Any) -> tuple[str, list, Any, Any] | None:
    """(dotted path, the list, its parent container, its key) for the longest list at
    depth 0, 1 or 2 — where every oversized payload in this file keeps its rows."""
    best: tuple[str, list, Any, Any] | None = None

    def consider(path: str, lst: list, parent: Any, key: Any) -> None:
        nonlocal best
        if best is None or len(lst) > len(best[1]):
            best = (path, lst, parent, key)

    if isinstance(data, list):
        consider("", data, None, None)
        return best
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        if isinstance(v, list):
            consider(str(k), v, data, k)
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, list):
                    consider(f"{k}.{k2}", v2, v, k2)
    return best


def cap_result(data: Any, *, max_bytes: int = _MAX_RESULT_BYTES) -> tuple[Any, dict | None]:
    """Shrink `data` under `max_bytes` by halving its longest list until it fits.
    Returns `(data, None)` when it already fits or nothing can be cut; otherwise a
    copy and `{"path", "kept", "dropped"}` naming the list that was cut."""
    if _byte_len(data) <= max_bytes:
        return data, None
    found = _longest_list(data)
    if found is None or len(found[1]) <= 1:
        return data, None
    path, lst, _parent, _key = found
    original = len(lst)
    keep = original
    out = data
    while keep > 1:
        keep //= 2
        out = _with_list_cut(data, path, keep)
        if _byte_len(out) <= max_bytes:
            break
    return out, {"path": path, "kept": keep, "dropped": original - keep}


def _with_list_cut(data: Any, path: str, keep: int) -> Any:
    """A shallow-copied `data` with the list at dotted `path` cut to `keep` rows."""
    if path == "":
        return list(data[:keep])
    keys = path.split(".")
    out = dict(data)
    node = out
    for k in keys[:-1]:
        node[k] = dict(node[k])
        node = node[k]
    node[keys[-1]] = list(node[keys[-1]][:keep])
    return out


def wrap_untrusted(data: Any, *, truncated: int = 0) -> str:
    scrubbed, capped = cap_result(_scrub(data))
    env: dict[str, Any] = {
        "source": f"{brand.SLUG}-api", "trust": "untrusted-data",
        # B6: the model's ONLY clock. …(keep the existing comment)…
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": scrubbed,
    }
    if truncated:
        env["truncated"] = truncated
    if capped:
        env["capped"] = capped
    return json.dumps(env, separators=(",", ":"), ensure_ascii=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `<venv>/bin/python -m pytest server/tests/test_assistant_tools.py -v`
Expected: PASS. (If `test_guarded_envelope_reports_the_cap_under_its_own_key` fails on the byte bound, the `list_prs` `limit` of Task 2 is not in yet — that test may be moved to the end of Task 2; keep it.)

- [ ] **Step 5: Commit** — `feat(assistant): cap every tool result at 48 KB and say what was cut`.

---

### Task 2: `list_prs` and `list_automations` projections

**Files:**
- Modify: `server/assistant/tools.py:560-565`, `:611-615`, and the `_SESSION_KEYS` block (~328) for the new key tuples
- Test: `server/tests/test_assistant_tools.py`

**Interfaces:**
- Produces: `list_prs(repo?, state?='open'|'merged'|'closed'|'all', limit?=30, verbose?=false)`; `list_automations(verbose?=false)`; helpers `_trim_pr(p)`, `_trim_automation(a)`.

- [ ] **Step 1: Write the failing tests**

```python
_PR_ROW = {"repo": "o/r", "number": 1, "title": "t", "state": "open", "merged": False,
           "is_draft": False, "stage": "ci", "checks": "pending", "mergeable": True,
           "merge_state_status": "CLEAN", "auto_merge_enabled": True, "url": "u",
           "head_ref": "b", "updated_at": "2026-01-01T00:00:00Z",
           "session": {"id": "s1", "name": "n"},
           "checks_detail": [{"name": "x"}] * 40, "check_groups": [{"checks": [{}] * 20}] * 5,
           "node_states": [{}] * 10, "deployed_envs": [], "eta_seconds": 1}


@pytest.mark.asyncio
async def test_list_prs_projects_rows_filters_state_and_limits():
    rows = [dict(_PR_ROW, number=1),
            dict(_PR_ROW, number=2, state="draft", is_draft=True),
            dict(_PR_ROW, number=3, state="merged", merged=True, stage="merged"),
            dict(_PR_ROW, number=4, state="closed")]

    def handler(req: httpx.Request):
        return httpx.Response(200, json=rows)

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_prs").handler({}))["content"][0]["text"])
    numbers = [p["number"] for p in out["data"]]
    assert numbers == [1, 2], "default state=open keeps drafts, drops merged/closed"
    row = out["data"][0]
    assert set(row) == {"repo", "number", "title", "state", "is_draft", "stage", "checks",
                        "mergeable", "merge_state_status", "auto_merge_enabled", "url",
                        "head_ref", "updated_at", "session"}
    out = json.loads((await _tool(tools, "list_prs").handler(
        {"state": "all", "limit": 3}))["content"][0]["text"])
    assert [p["number"] for p in out["data"]] == [1, 2, 3] and out["truncated"] == 1
    out = json.loads((await _tool(tools, "list_prs").handler(
        {"state": "merged"}))["content"][0]["text"])
    assert [p["number"] for p in out["data"]] == [3]
    out = json.loads((await _tool(tools, "list_prs").handler(
        {"verbose": True}))["content"][0]["text"])
    assert "checks_detail" in out["data"][0] and len(out["data"]) <= 5


@pytest.mark.asyncio
async def test_list_automations_projects_by_default_and_keeps_products():
    row = {"id": "backlog-runner", "name": "Backlog Runner", "description": "d" * 500,
           "enabled": True, "mode": "auto", "modes": ["off", "auto"],
           "status": {"text": "1 work in flight", "level": "active",
                      "counts": {"eligible": 3, "held": 4},
                      "products": [{"slug": "kindash", "eligible": 0, "held": 4}],
                      "waitingReason": "human"},
           "actionableCount": 2, "oversight": {"x": 1},
           "configSchema": [{"key": "k"}] * 20, "enginePolicy": "claude_only",
           "operatorTriggered": False, "workClass": "development",
           "config": {"max_concurrent": 2}, "schedule": None,
           "lastActiveAt": "2026-01-01T00:00:00Z", "activeSessions": 1}

    def handler(req: httpx.Request):
        return httpx.Response(200, json=[row])

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_automations").handler({}))["content"][0]["text"])
    a = out["data"][0]
    assert set(a) == {"id", "name", "enabled", "mode", "status", "actionableCount",
                      "lastActiveAt", "activeSessions", "workClass"}
    assert a["status"]["products"] == row["status"]["products"]
    assert a["status"]["counts"] == {"eligible": 3, "held": 4}
    out = json.loads((await _tool(tools, "list_automations").handler(
        {"verbose": True}))["content"][0]["text"])
    assert "configSchema" in out["data"][0] and "config" in out["data"][0]
```

- [ ] **Step 2: Run to verify they fail** — `-k "list_prs_projects or list_automations_projects"`; Expected: FAIL (full rows returned).

- [ ] **Step 3: Implement**

Beside `_SESSION_KEYS`:

```python
# The PR population row (`PipelinePr.to_dict`, server/github/pr_status.py) carries
# 30+ keys; `checks_detail`, `check_groups` and `node_states` are what make one
# repo's list 100 KB+. This is the glance shape; `get_pr` is the drill-in.
_PR_KEYS = ("repo", "number", "title", "state", "is_draft", "stage", "checks", "mergeable",
            "merge_state_status", "auto_merge_enabled", "url", "head_ref", "updated_at",
            "session")
_PR_VERBOSE_CAP = 5
_PR_LIST_CAP = 30
# `state` literals from `PullRequestInfo.state`: 'open' | 'draft' | 'merged' | 'closed'.
# "open" as a filter means "not finished" — a draft is an open PR to its author.
_PR_STATE_FILTERS = {"open": ("open", "draft"), "merged": ("merged",),
                     "closed": ("closed",), "all": ("open", "draft", "merged", "closed")}

# The automation row (server/api/automations.py:list_automations) ships each
# automation's whole `configSchema`; the glance shape keeps what answers "is it on,
# what is it doing, does it need me". `status.products` stays — it is the runner's
# per-product breakdown.
_AUTOMATION_KEYS = ("id", "name", "enabled", "mode", "status", "actionableCount",
                    "lastActiveAt", "activeSessions", "workClass")


def _trim_pr(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p[k] for k in _PR_KEYS if k in p}


def _trim_automation(a: dict[str, Any]) -> dict[str, Any]:
    return {k: a[k] for k in _AUTOMATION_KEYS if k in a}
```

Handlers:

```python
    @spec_tool("list_prs",
               "Pull requests across watched repos with stage, check status, mergeability "
               "and the owning session — a glance shape; use get_pr for check runs and "
               "reviews. state: 'open' (default; includes drafts), 'merged', 'closed' or "
               "'all'. Optional repo filter 'owner/name'. limit defaults to 30. verbose=true "
               "returns the full row for at most 5 PRs.",
               _schema(repo=str, state=str, limit=int, verbose=bool))
    @guarded
    async def list_prs(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/prs")
        state = (args.get("state") or "open").strip().lower()
        if state not in _PR_STATE_FILTERS:
            raise ToolArgError(f"state must be one of {sorted(_PR_STATE_FILTERS)}, got {state!r}")
        keep = _PR_STATE_FILTERS[state]
        rows = [p for p in rows
                if (not args.get("repo") or p.get("repo") == args["repo"])
                and str(p.get("state") or "").lower() in keep]
        if args.get("verbose"):
            return Trimmed(rows[:_PR_VERBOSE_CAP], max(0, len(rows) - _PR_VERBOSE_CAP))
        n = max(1, min(int(args.get("limit") or _PR_LIST_CAP), 200))
        return Trimmed([_trim_pr(p) for p in rows[:n]], max(0, len(rows) - n))

    @spec_tool("list_automations",
               "Automations (autopilot, backlog runner, PR doctor, CI/CD doctor…) with "
               "enabled flag, mode, live status text/level/counts, the runner's per-product "
               "rows, how many items need a person (actionableCount) and last activity. "
               "verbose=true adds config and configSchema.",
               _schema(verbose=bool))
    @guarded
    async def list_automations(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/automations")
        if args.get("verbose"):
            return rows
        return [_trim_automation(a) for a in rows]
```

Check `ToolArgError` is what the other handlers raise for a bad argument (`grep -n "class ToolArgError\|raise ToolArgError" server/assistant/tools.py`); use the same.

- [ ] **Step 4: Run** — `server/tests/test_assistant_tools.py server/tests/test_assistant_conformance.py -v`; Expected: PASS. The eval fixture `_prs()` already filters by `repo` only — the handler now filters state, so `scripts/eval_assistant.py` needs no change unless its rows lack `state`; check `sed -n 261,320p scripts/eval_assistant.py` and add `"state": "open"` to any row that lacks it.

- [ ] **Step 5: Commit** — `feat(assistant): list_prs and list_automations return a glance shape by default`.

---

### Task 3: `reverse_blockers` and `top_blockers` in `server/pm/drift.py`

**Files:**
- Modify: `server/pm/drift.py`
- Test: `server/tests/test_pm_drift.py` (create if absent; check `ls server/tests/test_pm_drift*.py` first and append if present)

**Interfaces:**
- Produces: `reverse_blockers(edges: Iterable[DepEdgeView]) -> dict[str, set[str]]` (dst → srcs; active edges only; self-edges ignored) and `top_blockers(edges, open_refs, *, n=10) -> list[dict]` (`[{"ref", "blocks", "state"}]`, `blocks` = number of OPEN srcs, sorted by blocks desc then ref). PR 2's `server/pm/escalation.py` imports `reverse_blockers` from here.

- [ ] **Step 1: Write the failing tests**

```python
from server.pm.drift import DepEdgeView, reverse_blockers, top_blockers


def _e(src, dst, *, active=True, source="native", dst_state="open"):
    return DepEdgeView(src_ref=src, dst_ref=dst, source=source, active=active,
                       dst_state=dst_state)


def test_reverse_blockers_indexes_active_edges_only_and_skips_self_edges():
    edges = [_e("o/r#2", "o/r#1"), _e("o/r#3", "o/r#1"), _e("o/r#4", "o/r#1", active=False),
             _e("o/r#5", "o/r#5"), _e("o/r#3", "o/r#1", source="prose")]
    assert reverse_blockers(edges) == {"o/r#1": {"o/r#2", "o/r#3"}}


def test_top_blockers_counts_open_dependents_and_orders_by_count_then_ref():
    edges = [_e("o/r#2", "o/r#1"), _e("o/r#3", "o/r#1"), _e("o/r#9", "o/r#1"),
             _e("o/r#4", "o/r#7"), _e("o/r#6", "o/r#8")]
    open_refs = {"o/r#2", "o/r#3", "o/r#4", "o/r#6"}   # #9 is closed
    out = top_blockers(edges, open_refs, n=2, states={"o/r#1": "open", "o/r#7": "closed"})
    assert out == [{"ref": "o/r#1", "blocks": 2, "state": "open"},
                   {"ref": "o/r#7", "blocks": 1, "state": "closed"}]
```

- [ ] **Step 2: Run** — Expected: FAIL with ImportError.

- [ ] **Step 3: Implement** (append to `server/pm/drift.py`):

```python
def reverse_blockers(edges: Iterable[DepEdgeView]) -> dict[str, set[str]]:
    """THE reverse dependency index: blocker ref → the refs it blocks (spec
    2026-09-30 §3). Active edges only — an inactive row is a dep that was removed or
    never resolved — and a self-edge is a parser artefact, not a block. Shared by the
    deps summary here and the escalation read model (`server/pm/escalation.py`)."""
    out: dict[str, set[str]] = {}
    for e in edges:
        if not e.active or not e.src_ref or not e.dst_ref or e.src_ref == e.dst_ref:
            continue
        out.setdefault(e.dst_ref, set()).add(e.src_ref)
    return out


def top_blockers(edges: Iterable[DepEdgeView], open_refs: Set[str], *, n: int = 10,
                 states: Mapping[str, str | None] | None = None) -> list[dict]:
    """The `n` refs that block the most OPEN issues, for the deps summary."""
    rev = reverse_blockers(edges)
    rows = [{"ref": ref, "blocks": len(srcs & open_refs),
             "state": (states or {}).get(ref)} for ref, srcs in rev.items()]
    rows = [r for r in rows if r["blocks"]]
    rows.sort(key=lambda r: (-r["blocks"], r["ref"]))
    return rows[:n]
```

Add `from collections.abc import Iterable` to the existing import line.

- [ ] **Step 4: Run** — Expected: PASS. **Step 5: Commit** — `feat(pm): reverse dependency index and top blockers in drift.py`.

---

### Task 4: `GET /api/pm/projects/{slug}/deps` filters and summary; `get_product_deps` summary by default

**Files:**
- Modify: `server/api/pm.py:719-812`
- Modify: `server/assistant/tools.py:753-760`
- Modify: `scripts/eval_assistant.py:478` (`_product_deps`) and the `/deps` branch (~1084)
- Test: `server/tests/test_pm_deps_api.py`, `server/tests/test_assistant_tools.py`

**Interfaces:**
- Consumes: Task 3's `top_blockers`.
- Produces: query params `active: bool = False`, `open_only: bool = False`, `summary: bool = False`. Summary shape: `{"project", "edges": {"total", "active", "bySource": {source: n}}, "drift": {"proseOnly": n, "unresolvedTargets": n, "missingFromProject": n, "checklistEpics": n}, "topBlockers": [...]}`.

- [ ] **Step 1: Write the failing tests**

Read `server/tests/test_pm_deps_api.py` first and reuse its `_client` / seeding helpers. Add:

```python
def test_deps_endpoint_filters_and_summarises():
    with _client() as c:
        # seed with the file's existing helper: one active native edge #101 -> #102
        # (both open), plus add one INACTIVE edge #103 -> #102 and a closed src #104 -> #102
        _seed(c)  # whatever the file's seeding helper is called
        full = c.get("/api/pm/projects/viberpit/deps", headers=IDENTITY_HEADERS).json()
        assert len(full["edges"]) == 3, "defaults preserve today's response"

        active = c.get("/api/pm/projects/viberpit/deps?active=true&open_only=true",
                       headers=IDENTITY_HEADERS).json()
        assert [e["src"] for e in active["edges"]] == ["steiner385/viberpit#101"]

        s = c.get("/api/pm/projects/viberpit/deps?summary=true", headers=IDENTITY_HEADERS).json()
        assert s["edges"] == {"total": 3, "active": 2, "bySource": {"native": 3}}
        assert set(s["drift"]) == {"proseOnly", "unresolvedTargets", "missingFromProject",
                                   "checklistEpics"}
        assert all(isinstance(v, int) for v in s["drift"].values())
        assert s["topBlockers"] == [{"ref": "steiner385/viberpit#102", "blocks": 1,
                                     "state": "open"}]
        assert "project" in s


def test_deps_summary_for_a_product_without_a_board_answers_null_project():
    with _client() as c:
        _seed_project_without_board(c)  # a Project + ProjectRepo, no GhProject
        s = c.get("/api/pm/projects/viberpit/deps?summary=true", headers=IDENTITY_HEADERS).json()
        assert s["project"] is None and s["drift"]["missingFromProject"] == 0
```

Adjust the seeded counts to the helper's actual fixture; the assertions' *shape* is the requirement. And in `test_assistant_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_product_deps_asks_for_the_summary_by_default():
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"edges": {"total": 1}})

    tools = build_tools(_api(handler))
    await _tool(tools, "get_product_deps").handler({"slug": "kindash"})
    assert seen["params"] == {"summary": "true", "active": "true", "open_only": "true"}
    await _tool(tools, "get_product_deps").handler({"slug": "kindash", "summary": False})
    assert seen["params"] == {"active": "true", "open_only": "true"}
```

Update the existing parametrised row at `test_assistant_tools.py:864` (`("get_product_deps", {"slug": "kindash"}, "GET", "/api/pm/projects/kindash/deps")`) only if it asserts on params; a path-only assertion still holds.

- [ ] **Step 2: Run** — Expected: FAIL (`summary` ignored; params absent).

- [ ] **Step 3: Implement the route**

Signature:

```python
@router.get("/projects/{slug}/deps")
async def project_deps(
    slug: str,
    active: bool = False,
    open_only: bool = False,
    summary: bool = False,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Dep edges + drift audit for a project (spec 003 Phase A). ``active`` keeps
    active edges only, ``open_only`` keeps edges whose src is an open mirrored issue,
    ``summary`` replaces the lists with counts and the top blockers (spec 2026-09-30
    §7). Defaults are today's full response."""
```

After `edges = [...]` and before `drift = compute_drift(...)`, apply the filters to the LIST THAT IS RETURNED but compute drift on the unfiltered `edges` (drift is an audit of everything):

```python
    from server.pm.drift import top_blockers
    shown = [e for e in edges
             if (not active or e.active) and (not open_only or e.src_ref in open_refs)]
    drift = compute_drift(edges, open_refs, project_refs,
                          {ref: items[ref].body for ref in open_refs},
                          epic_refs={ref for ref, w in items.items() if w.is_epic})
    if gh is None:
        drift.missing_from_project = []
    project = None if gh is None else {
        "number": gh.gh_project_number, "title": gh.title,
        "lastSyncedAt": gh.last_synced_at.isoformat() if gh.last_synced_at else None}
    if summary:
        by_source: dict[str, int] = {}
        for e in shown:
            by_source[e.source] = by_source.get(e.source, 0) + 1
        return {
            "project": project,
            "edges": {"total": len(shown), "active": sum(e.active for e in shown),
                      "bySource": by_source},
            "drift": {"proseOnly": len(drift.prose_only),
                      "unresolvedTargets": len(drift.unresolved_targets),
                      "missingFromProject": len(drift.missing_from_project),
                      "checklistEpics": len(drift.checklist_epics)},
            "topBlockers": top_blockers(
                shown, open_refs, n=10,
                states={ref: _dst_state(ref) for ref in {e.dst_ref for e in shown}}),
        }
    return {
        "project": project,
        "edges": [{"src": e.src_ref, "dst": e.dst_ref, "source": e.source,
                   "active": e.active, "dstState": e.dst_state} for e in shown],
        "drift": {…unchanged…},
    }
```

Tool:

```python
    @spec_tool("get_product_deps",
               "Issue-level dependencies for a product. Default (summary=true): edge "
               "counts by source, drift counts (prose-only deps, unresolved targets, "
               "issues missing from the board, checklist epics) and the ten issues that "
               "block the most open work. summary=false returns the active, open-source "
               "edges themselves (large; capped).",
               _schema(("slug",), slug=str, summary=bool))
    @guarded
    async def get_product_deps(args: dict[str, Any]) -> Any:
        params: dict[str, str] = {"active": "true", "open_only": "true"}
        if args.get("summary", True):
            params = {"summary": "true", **params}
        return await api.get(f"/api/pm/projects/{_slug(args['slug'])}/deps", params=params)
```

Eval fixture: in `scripts/eval_assistant.py`'s `/deps` branch, when `params.get("summary") == "true"` return `{"project": d["project"], "edges": {"total": len(d["edges"]), "active": sum(e["active"] for e in d["edges"]), "bySource": {...}}, "drift": {k: len(v) for k, v in d["drift"].items()}, "topBlockers": []}` built from `_product_deps(slug)`.

- [ ] **Step 4: Run** — `server/tests/test_pm_deps_api.py server/tests/test_assistant_tools.py server/tests/test_assistant_conformance.py server/tests/test_eval_assistant_grader.py -v`; Expected: PASS. Also `grep -rn "fetchProjectDeps\|/deps" web/src/api/pm.ts` — the web call passes no params, so its response is unchanged; `(cd web && npm run typecheck)` stays green.

- [ ] **Step 5: Commit** — `feat(pm): deps endpoint gains active/open_only/summary; get_product_deps reads the summary`.

---

### Task 5: Spec amendment, gate, PR

- [ ] **Step 1:** In `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §6 "Result byte cap", change `"truncated": {"path": "edges", "kept": 40, "dropped": 212}` to `"capped": {"path": "edges", "kept": 40, "dropped": 212}` and `_serialize_result`/`wrap_untrusted` wording to match Task 1; add one sentence: "`truncated` stays `Trimmed`'s integer drop count."
- [ ] **Step 2:** `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/ -q`, then the full `server/tests/ -n auto --dist loadfile --timeout=300 -m "not slow"` in the background tee'd to `/tmp/pr6-full.log`. Re-run any red file alone before blaming the change.
- [ ] **Step 3:** In-flight PR check for `server/assistant/tools.py|server/api/pm.py|server/pm/drift.py` (GraphQL query from core.md).
- [ ] **Step 4:** Push `feat/assistant-readable-results`; `gh pr create --title "feat(assistant): readable large results — summary/limit modes and a 48 KB result cap" --body-file /tmp/pr6-body.md` (`Refs …decision-escalation-design.md §6/§7 (PR 6 of 6)`, trailer); `gh pr merge <#> --squash --auto`.
