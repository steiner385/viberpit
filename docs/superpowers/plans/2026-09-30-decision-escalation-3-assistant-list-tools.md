# Decision escalation PR 3 — assistant `list_decisions` and `list_holds` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The assistant can answer "what is blocking KinDash?" and "what decisions are waiting?" — two READ tools over the endpoints PR 2 added — and `resolve_decision` finally has a list companion so proposal ids no longer have to be typed by hand.

**Architecture:** Two `ToolSpec` rows + two `@spec_tool` handlers in `server/assistant/tools.py`, each one GET over loopback, projecting the row to a glance shape (precedent: `_trim_session`, `list_prs`). No new endpoint and no new allow row for `/api/pm/decisions` (already allowlisted); `/api/pm/holds` was allowlisted in PR 2. The rest is the repo's tool-addition recipe: conformance args, an eval case per tool (+ the pinned hash/count), the feature doc's pack table, the web `appActions` map, and an eval fixture handler for both routes so evals see real shapes.

**Tech Stack:** Python 3 / FastAPI, pytest; a TypeScript map + JSON fixture in `web/src/session`.

**Spec:** `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §6 (rows 1–2), §10 PR 3.

**Depends on:** PR 2 merged into `origin/master` (`GET /api/pm/holds`, `GET /api/pm/decisions?projectSlug=&impact=`). Verify on the branch base: `grep -n '"/holds"' server/api/pm.py` and `grep -n "/api/pm/holds" server/assistant/policy.py`.

## Global Constraints

- Branch off `origin/master` (`git fetch && git checkout -b feat/assistant-list-holds-decisions origin/master`).
- Test env and gates as in plan 1 (`env -u …` + `/home/tony/GitHub/viberpit/.venv/bin/python`; ruff, mypy, `pytest tests/`, touched files, the full `-m "not slow"` server run in the background; commit/PR text via files; trailers).
- One tool → one endpoint (`test_every_tool_hits_an_allowed_route`); READ tools use GET.
- Tool names are exactly `list_decisions` and `list_holds`; both are all-optional-argument tools (schema must validate `{}`) → add both to `_ALL_OPTIONAL_TOOLS`.
- Registry counts move 92 → 94 registered, 87 → 89 exposed; pack P moves 12 → 14 in `docs/product/features/ai-assistant.md` (pinned by `test_feature_doc_tool_counts_match_the_registry`).
- Eval cases: two new rows in `docs/superpowers/evals/assistant/cases.jsonl` (128 rows; recompute the pinned projection digest in `server/tests/test_eval_assistant_fixtures.py` and record the previous digest in a comment, as the file does).
- `web/src/session/__fixtures__/appActionToolKeys.json` (sorted list) and `APP_ACTION_TITLES` in `web/src/session/appActions.ts` gain both names (pinned in both languages).
- `resolve_decision`'s corrective text now points at `list_decisions`.
- Projection only; no business logic in handlers. Match the file's WHY-comment idiom.

## Review Focus

1. `list_holds(product=…)` for a slug with nothing held must return `{"products": [...], "held": []}` with the product summary still present — an operator asking "is KinDash starving?" gets the answer even when nothing is parked. Pinned in Task 1.
2. `list_decisions` must not request `impact=true` unless asked (`impact` param, default false) — the nav badge precedent shows this list is polled; the tool should not silently pay for the queue read on every call. Pinned in Task 1.
3. A `limit` outside 1–50 is clamped, never a 422 from the server, and the drop lands in `truncated`. Pinned in Task 1.
4. `list_holds` rows keep `proposalId`, `recommendedKey` and the first option keys so the model can go straight to `resolve_decision` without a second call. Pinned in Task 1.
5. The eval fixture must serve `/api/pm/holds` and `/api/pm/decisions` with the REAL wire shapes (camelCase keys from PR 2's `_held_to_dict` / `_decision_to_dict`), or the eval grades the tools against a fantasy. Pinned in Task 2.

---

### Task 1: The two tools

**Files:**
- Modify: `server/assistant/tools.py` (`TOOL_SPECS` rows near `ToolSpec("list_triage", …)`; handlers next to `list_triage`; `resolve_decision`'s error text)
- Test: `server/tests/test_assistant_tools.py`, `server/tests/test_assistant_conformance.py` (`ARGS`)

**Interfaces:**
- Produces: `list_decisions(product?: str, impact?: bool, limit?: int)` → `GET /api/pm/decisions[?projectSlug=…][&impact=true]`, rows projected to `id, ref, product, title, url, options, recommendedKey, rationale, proposedBy, createdAt` (+ `downstream`, `downstreamDispatchable` when `impact`), newest first, `limit` default 20 (1–50) with `Trimmed`.
- Produces: `list_holds(product?: str, kind?: str, limit?: int)` → `GET /api/pm/holds[?product=…][&kind=…]&limit=<n>`, `products` passed through whole, `held` rows projected to `ref, product, title, url, holdKind, holdReason, holdBy, ageHours, proposalId, recommendedKey, optionKeys, downstream, downstreamDispatchable, releasedBy`, `truncated` from the server's own count.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_assistant_tools.py`:

```python
_DECISION_ROW = {"id": "dp-1", "workItemId": "wi-1", "repo": "o/k", "number": 4,
                 "ref": "o/k#4", "projectSlug": "kindash", "title": "Retry policy",
                 "url": "https://x/4", "options": [{"key": "a", "label": "A", "detail": None},
                                                   {"key": "b", "label": "B", "detail": "d"}],
                 "recommendedKey": "b", "rationale": "why", "chosenKey": None,
                 "status": "pending", "proposedBy": "session:s1", "resolvedBy": None,
                 "createdAt": "2026-01-01T00:00:00+00:00", "resolvedAt": None,
                 "guidance": {"accept": {"big": "x" * 500}, "reject": {}}}


@pytest.mark.asyncio
async def test_list_decisions_filters_by_product_projects_rows_and_limits():
    seen: dict = {}

    def handler(req: httpx.Request):
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return httpx.Response(200, json=[dict(_DECISION_ROW, id=f"dp-{i}") for i in range(30)])

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_decisions").handler({}))["content"][0]["text"])
    assert seen == {"path": "/api/pm/decisions", "params": {}}, "impact is off unless asked"
    assert len(out["data"]) == 20 and out["truncated"] == 10
    assert set(out["data"][0]) == {"id", "ref", "product", "title", "url", "options",
                                   "recommendedKey", "rationale", "proposedBy", "createdAt"}
    assert out["data"][0]["product"] == "kindash"

    await _tool(tools, "list_decisions").handler({"product": "kindash", "impact": True,
                                                  "limit": 500})
    assert seen["params"] == {"projectSlug": "kindash", "impact": "true"}

    def with_impact(req: httpx.Request):
        return httpx.Response(200, json=[dict(_DECISION_ROW, downstream=7,
                                              downstreamDispatchable=4)])
    out = json.loads((await _tool(build_tools(_api(with_impact)), "list_decisions").handler(
        {"impact": True}))["content"][0]["text"])
    assert out["data"][0]["downstream"] == 7 and out["data"][0]["downstreamDispatchable"] == 4


_HELD_ROW = {"ref": "o/k#4", "repo": "o/k", "number": 4, "product": "kindash",
             "title": "Retry policy", "url": "https://x/4", "holdKind": "decision",
             "holdReason": "decision pending: why", "holdBy": "session:s1",
             "holdSetAt": "2026-01-01T00:00:00+00:00", "ageHours": 31.2,
             "proposalId": "dp-1", "options": [{"key": "a", "label": "A", "detail": None},
                                               {"key": "b", "label": "B", "detail": "d"}],
             "recommendedKey": "b", "downstream": 7, "downstreamDispatchable": 4,
             "downstreamRefs": ["o/k#5", "o/k#6"], "dispatchableOnRelease": True,
             "releasedBy": "Accept an option in the Inbox"}


@pytest.mark.asyncio
async def test_list_holds_forwards_filters_and_projects_rows():
    seen: dict = {}

    def handler(req: httpx.Request):
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return httpx.Response(200, json={
            "products": [{"slug": "kindash", "dispatchable": 0, "skipped": {"held": 3},
                          "heldCount": 3, "starving": True, "oldestHoldHours": 212.4}],
            "held": [_HELD_ROW], "truncated": 2, "generatedAt": "2026-01-01T00:00:00+00:00"})

    tools = build_tools(_api(handler))
    out = json.loads((await _tool(tools, "list_holds").handler(
        {"product": "kindash", "kind": "decision", "limit": 99}))["content"][0]["text"])
    assert seen == {"path": "/api/pm/holds",
                    "params": {"product": "kindash", "kind": "decision", "limit": "50"}}
    assert out["data"]["products"][0]["starving"] is True
    row = out["data"]["held"][0]
    assert set(row) == {"ref", "product", "title", "url", "holdKind", "holdReason", "holdBy",
                        "ageHours", "proposalId", "recommendedKey", "optionKeys", "downstream",
                        "downstreamDispatchable", "releasedBy"}
    assert row["optionKeys"] == ["a", "b"] and row["proposalId"] == "dp-1"
    assert out["truncated"] == 2, "the server's own drop count rides the envelope"

    # Nothing held: the product summary still answers "is it starving?".
    def empty(req: httpx.Request):
        return httpx.Response(200, json={"products": [{"slug": "kindash", "dispatchable": 5,
                                                       "skipped": {}, "heldCount": 0,
                                                       "starving": False,
                                                       "oldestHoldHours": None}],
                                         "held": [], "truncated": 0, "generatedAt": "x"})
    out = json.loads((await _tool(build_tools(_api(empty)), "list_holds").handler({}))
                     ["content"][0]["text"])
    assert out["data"]["held"] == [] and out["data"]["products"][0]["heldCount"] == 0
    assert "truncated" not in out


@pytest.mark.asyncio
async def test_list_holds_rejects_an_unknown_kind_locally():
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        return httpx.Response(200, json={})
    out = await _tool(build_tools(_api(handler)), "list_holds").handler({"kind": "evidence"})
    assert out["is_error"] is True and calls == []


@pytest.mark.asyncio
async def test_resolve_decision_error_points_at_list_decisions():
    out = await _tool(build_tools(_api(lambda r: httpx.Response(200, json={}))),
                      "resolve_decision").handler({"proposal_id": "not valid!", "decision": "reject"})
    assert out["is_error"] is True and "list_decisions" in out["content"][0]["text"]
```

Add both names to `_ALL_OPTIONAL_TOOLS` in the same file, and to `ARGS` in `server/tests/test_assistant_conformance.py`: `"list_decisions": {}, "list_holds": {},`.

- [ ] **Step 2: Run** — `pytest server/tests/test_assistant_tools.py -k "list_decisions or list_holds or points_at_list_decisions" -v`; Expected: FAIL (tools not registered / KeyError).

- [ ] **Step 3: Implement**

`TOOL_SPECS` (after the `list_triage` row):

```python
    ToolSpec("list_decisions", ToolClass.READ, "List pending decision proposals"),
    ToolSpec("list_holds", ToolClass.READ, "List held items ranked by what they block"),
```

Beside `_SESSION_KEYS`:

```python
# Decision proposals (server/api/pm.py:_decision_to_dict) carry the inbox's
# consequence `guidance` blob and audit columns the model never acts on; this is
# what it needs to pick an option and call resolve_decision.
_DECISION_KEYS = ("id", "ref", "title", "url", "options", "recommendedKey", "rationale",
                  "proposedBy", "createdAt", "downstream", "downstreamDispatchable")
_DECISION_LIST_CAP = 20
_HOLDS_LIST_CAP = 50
# Held rows (server/api/pm.py:_held_to_dict): the full options list rides on the
# proposal (list_decisions); here only the keys, so the row stays one line.
_HELD_KEYS = ("ref", "product", "title", "url", "holdKind", "holdReason", "holdBy", "ageHours",
              "proposalId", "recommendedKey", "downstream", "downstreamDispatchable",
              "releasedBy")


def _trim_decision(d: dict[str, Any]) -> dict[str, Any]:
    out = {k: d[k] for k in _DECISION_KEYS if k in d}
    out["product"] = d.get("projectSlug")
    return out


def _trim_held(h: dict[str, Any]) -> dict[str, Any]:
    out = {k: h[k] for k in _HELD_KEYS if k in h}
    out["optionKeys"] = [str(o.get("key")) for o in (h.get("options") or [])
                         if isinstance(o, dict) and o.get("key")]
    return out
```

Handlers (after `list_triage`):

```python
    @spec_tool("list_decisions",
               "Pending decision proposals — the product decisions coding sessions could not "
               "take and parked on a decision hold: the options drafted, the recommended one "
               "and why. Newest first. product narrows to one product slug; impact=true adds "
               "downstream (open issues behind it) and downstreamDispatchable. Use "
               "resolve_decision with the id and an option key to answer one. limit 1-50, "
               "default 20.",
               _schema(product=str, impact=bool, limit=int))
    @guarded
    async def list_decisions(args: dict[str, Any]) -> Any:
        params: dict[str, str] = {}
        if args.get("product"):
            params["projectSlug"] = _slug(args["product"], field="product")
        if args.get("impact"):
            params["impact"] = "true"
        rows = await api.get("/api/pm/decisions", params=params or None)
        n = max(1, min(int(args.get("limit") or _DECISION_LIST_CAP), 50))
        return Trimmed([_trim_decision(d) for d in rows[:n]], max(0, len(rows) - n))

    @spec_tool("list_holds",
               "Parked issues per product, loudest first: what is held (an owner_action hold "
               "needs the owner; a decision hold needs an option picked — see list_decisions), "
               "who parked it and why, how long ago, and what it blocks (downstream / "
               "downstreamDispatchable). `products` says per product how many are "
               "dispatchable and whether the runner is starving. product and kind "
               "('owner_action' | 'decision') narrow; limit 1-50, default 50.",
               _schema(product=str, kind=str, limit=int))
    @guarded
    async def list_holds(args: dict[str, Any]) -> Any:
        params: dict[str, str] = {}
        if args.get("product"):
            params["product"] = _slug(args["product"], field="product")
        kind = args.get("kind")
        if kind is not None:
            if kind not in ("owner_action", "decision"):
                raise ToolArgError("kind must be 'owner_action' or 'decision'")
            params["kind"] = kind
        params["limit"] = str(max(1, min(int(args.get("limit") or _HOLDS_LIST_CAP), 50)))
        data = await api.get("/api/pm/holds", params=params)
        held = [_trim_held(h) for h in (data.get("held") or [])]
        return Trimmed({"products": data.get("products") or [], "held": held},
                       int(data.get("truncated") or 0))
```

Check `_slug`'s signature (`_slug(s, *, field="slug")` at ~line 178) and `ToolArgError` name before using them. Add both handlers to the `base_tools` list right after `list_triage`. Change `resolve_decision`'s message to `"proposal_id must be a proposal id from list_decisions"`.

- [ ] **Step 4: Run** — `pytest server/tests/test_assistant_tools.py server/tests/test_assistant_conformance.py -q`; Expected: the new tests PASS; `test_every_tool_has_an_eval_case` and the doc-count test FAIL (Task 2 fixes them — that is expected at this commit; still commit).
- [ ] **Step 5: Commit** — `feat(assistant): list_decisions and list_holds — the read side of resolve_decision`.

---

### Task 2: Eval cases, fixture, doc counts, web action map

**Files:**
- Modify: `docs/superpowers/evals/assistant/cases.jsonl` (append 2 rows), `server/tests/test_eval_assistant_fixtures.py` (digest + count), `scripts/eval_assistant.py` (`/api/pm/decisions` and `/api/pm/holds` handlers + fixture data), `docs/product/features/ai-assistant.md` (counts + pack P row), `web/src/session/__fixtures__/appActionToolKeys.json`, `web/src/session/appActions.ts`.
- Test: the existing pinned tests (`test_assistant_conformance.py`, `test_eval_assistant_fixtures.py`, `test_eval_assistant_grader.py`, `test_assistant_tools.py::test_app_action_*`, `web/src/session/appActions.test.ts`).

- [ ] **Step 1: Run the pins to see them fail** — `pytest server/tests/test_assistant_conformance.py server/tests/test_eval_assistant_fixtures.py -q`; Expected: `test_every_tool_has_an_eval_case` (both names), `test_feature_doc_tool_counts_match_the_registry` FAIL; `(cd web && npx vitest run src/session/appActions.test.ts)` FAIL.

- [ ] **Step 2: Eval cases** — append to `cases.jsonl` (one JSON object per line, same keys as line 83's `decision-accept-proposal` row):

```json
{"id": "decisions-pending-list", "prompt": "What product decisions are waiting on me?", "route": "/roadmap", "pack": "P", "expect_tools": ["list_decisions"], "forbid_tools": ["resolve_decision"], "rubric": "Lists each pending decision with its issue, the options and the recommended option from list_decisions; does not accept or reject anything unasked."}
{"id": "holds-what-blocks-product", "prompt": "What is blocking KinDash right now, and is the runner starving?", "route": "/roadmap", "pack": "P", "expect_tools": ["list_holds"], "forbid_tools": ["dispatch_backlog"], "rubric": "Uses list_holds with product 'kindash': reports the product's dispatchable count and starving flag, then the held items loudest-first with hold kind, who parked each and what it blocks (downstream); names how each is released."}
```

Then recompute the projection digest exactly as `test_semantic_projection_unchanged` does and update the pinned string, `len(rows) == 128`, and add a comment line `# Before list_decisions/list_holds (spec 2026-09-30 §6, 126 cases): <old digest>` above it.

- [ ] **Step 3: Eval fixture** — in `scripts/eval_assistant.py` add `_decisions()` returning two rows in the `_decision_to_dict` shape (keys: `id, workItemId, repo, number, ref, projectSlug, title, url, options, recommendedKey, rationale, chosenKey, status, proposedBy, resolvedBy, createdAt, resolvedAt, guidance`) for the fixture's existing products (`kindash`-style slugs; read `_product` to pick real slugs), and `_holds()` returning `{"products": [...], "held": [...], "truncated": 0, "generatedAt": fixture.now}` in the `_held_to_dict` shape (keys listed in Task 1's `_HELD_ROW`). Route them beside `/api/pm/triage`: `if path == "/api/pm/decisions": rows = _decisions(); filter by params.get("projectSlug"); if params.get("impact") == "true": add downstream/downstreamDispatchable; return`, and `if path == "/api/pm/holds": return filtered by product/kind`. Keep the existing `POST /api/pm/decisions/...` behaviour untouched.

- [ ] **Step 4: Doc + web** — `docs/product/features/ai-assistant.md:139`: `92` → `94`, `87` → `89`; pack P row `12` → `14` and append "held items and pending decisions listing" to its surface text. `appActionToolKeys.json`: insert `"list_decisions"` and `"list_holds"` in sorted position. `appActions.ts` `APP_ACTION_TITLES`: `list_decisions: 'Listed pending decisions'`, `list_holds: 'Listed held items'` beside `list_triage`.

- [ ] **Step 5: Run** — `pytest server/tests/test_assistant_tools.py server/tests/test_assistant_conformance.py server/tests/test_eval_assistant_fixtures.py server/tests/test_eval_assistant_grader.py server/tests/test_assistant_codex_overlay.py -q` and `(cd web && npx vitest run src/session/appActions.test.ts && npm run typecheck)`; Expected: PASS.
- [ ] **Step 6: Commit** — `feat(assistant): eval cases, fixtures, doc counts and action titles for list_decisions/list_holds`.

---

### Task 3: Gate, PR

- [ ] `ruff check src/ server/`; `mypy server/ --ignore-missing-imports`; `pytest tests/ -q`; full `server/tests/ -n auto --dist loadfile --timeout=300 -m "not slow"` in the background → `/tmp/pr3-full.log` (re-run any red file alone); `(cd web && npm run typecheck && npm run lint && npm run test)` — the full vitest run, because `appActions.test.ts` is not the only file reading the fixture.
- [ ] In-flight PR check for `server/assistant/tools.py|cases.jsonl|scripts/eval_assistant.py|ai-assistant.md|appActions`.
- [ ] Push `feat/assistant-list-holds-decisions`; `gh pr create --title "feat(assistant): list_decisions and list_holds — the read side of resolve_decision" --body-file /tmp/pr3-body.md` (`Refs …decision-escalation-design.md §6 (PR 3 of 6)`; trailer); `gh pr merge <#> --squash --auto`.
