# Per-Model Usage Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Report Anthropic's per-model usage windows (e.g. the separate Fable weekly limit) on every usage surface, and pick, rotate and monitor accounts for the model a session actually runs.

**Architecture:** The quota DTO (`QuotaWindow.scope = "model"` + `applies_to_models`), the ledger blob, the RPC/HTTP encoding and the TS types already carry model-scoped windows — only the Anthropic parser never produces one and the server never passes a model into the (already model-aware) selector. This plan adds the producer, one server-side definition of "the model key a quota window is matched against" (`model_policy.quota_model`), threads it through selection and the rotation monitor, carries the SDK's `rate_limit_type` for observability, and filters the web's single window reader by model.

**Tech Stack:** Python 3.12 (`src/ccswitch` core, FastAPI/asyncio server), pytest; React + TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-09-14-per-model-usage-windows-design.md`

## Global Constraints

- Branch `feat/per-model-usage-windows` is already created off `origin/master`; commit every task on it. Never commit to `master`.
- Core tests must run with the checkout's own source: `PYTHONPATH=$PWD/src <venv>/bin/python -m pytest tests/...` where `<venv>` is `/home/tony/GitHub/viberpit-deploy/.venv` (the worktree has no venv). Confirm once with `PYTHONPATH=$PWD/src <venv>/bin/python -c "import ccswitch.quota as m; print(m.__file__)"` — it must print a path under this worktree.
- Server tests: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <venv>/bin/python -m pytest server/tests/<file> -q`.
- Web: `web/node_modules` must be a symlink to `/home/tony/GitHub/viberpit-deploy/web/node_modules` (create it if missing: `ln -s /home/tony/GitHub/viberpit-deploy/web/node_modules web/node_modules`). Run web commands as `(cd web && npm run <script>)` subshells — never a persisted `cd web`.
- Commit messages go in a file and are passed with `git commit -F <file>`; every message ends with the trailer line `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Window keys are a wire contract: model-scoped Anthropic windows are keyed `"<group>:<model_key>"` (e.g. `weekly:fable`) with `applies_to_models == (model_key,)`, where `model_key` is the lowercase tier token `model_catalog.model_tier` produces (`fable`, `opus`, `sonnet`, `haiku`).
- Account-scoped windows always precede model-scoped ones in `QuotaSnapshot.windows`.
- No new tier vocabulary anywhere: the server maps through `model_catalog.model_tier` / `model_policy.launch_model`; the web through `session/modelName.ts:modelTier`.
- `QUOTA_BLOB_VERSION` stays `2`.

---

### Task 1: Parser emits model-scoped windows

**Files:**
- Modify: `src/ccswitch/quota.py:277-297` (`anthropic_snapshot`)
- Modify: `src/ccswitch/oauth.py:181-212` (`parse_usage`)
- Test: `tests/test_oauth.py`, `tests/test_quota.py`

**Interfaces:**
- Produces: `anthropic_snapshot(..., *, model_windows: Sequence[QuotaWindow] = ())` appends the given windows after the two account-wide ones. `parse_usage(payload)` returns a snapshot whose windows are `session`, `weekly`, then one `"<group>:<model_key>"` window per scoped `limits[]` entry (payload order).

- [ ] **Step 1: Write the failing parser tests**

Append to `tests/test_oauth.py` (inside the existing test class, same style as `test_parse_usage_prefers_limits_array`):

```python
    # Verified live shape, 2026-09-14 (percentages only). The scoped entry names the
    # model by display_name with a null id.
    SCOPED_PAYLOAD = {
        "five_hour": {"utilization": 3.0, "resets_at": "2026-09-14T16:40:00Z"},
        "seven_day": {"utilization": 91.0, "resets_at": "2026-09-14T20:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "limits": [
            {"kind": "session", "group": "session", "percent": 3,
             "resets_at": "2026-09-14T16:40:00Z", "scope": None},
            {"kind": "weekly_all", "group": "weekly", "percent": 91,
             "resets_at": "2026-09-14T20:00:00Z", "scope": None},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 100,
             "resets_at": "2026-09-14T19:59:59Z",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ],
    }

    def test_parse_usage_emits_a_model_scoped_window_after_the_account_ones(self):
        from ccswitch.oauth import parse_usage
        from ccswitch.quota import SCOPE_ACCOUNT, SCOPE_MODEL
        u = parse_usage(self.SCOPED_PAYLOAD)
        self.assertEqual([w.key for w in u.windows], ["session", "weekly", "weekly:fable"])
        fable = u.window("weekly:fable")
        self.assertEqual(fable.used_pct, 100.0)
        self.assertEqual(fable.label, "Fable weekly")
        self.assertEqual(fable.scope, SCOPE_MODEL)
        self.assertEqual(fable.applies_to_models, ("fable",))
        self.assertEqual(fable.duration_seconds, 7 * 24 * 3600)
        self.assertEqual(fable.resets_at.isoformat(), "2026-09-14T19:59:59+00:00")
        self.assertEqual(u.window("weekly").scope, SCOPE_ACCOUNT)
        self.assertEqual(u.window("weekly").used_pct, 91.0)

    def test_parse_usage_scoped_window_with_no_active_reset_is_still_emitted(self):
        from ccswitch.oauth import parse_usage
        payload = dict(self.SCOPED_PAYLOAD)
        payload["limits"] = self.SCOPED_PAYLOAD["limits"][:2] + [
            {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "resets_at": None,
             "scope": {"model": {"id": None, "display_name": "Opus"}, "surface": None}},
        ]
        u = parse_usage(payload)
        w = u.window("weekly:opus")
        self.assertIsNotNone(w)
        self.assertEqual(w.used_pct, 0.0)
        self.assertIsNone(w.resets_at)

    def test_parse_usage_scoped_model_id_wins_over_display_name(self):
        from ccswitch.oauth import parse_usage
        payload = dict(self.SCOPED_PAYLOAD)
        payload["limits"] = self.SCOPED_PAYLOAD["limits"][:2] + [
            {"kind": "weekly_scoped", "group": "weekly", "percent": 40,
             "resets_at": "2026-09-14T19:59:59Z",
             "scope": {"model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
                       "surface": None}},
        ]
        u = parse_usage(payload)
        self.assertEqual(u.window("weekly:opus").applies_to_models, ("opus",))

    def test_parse_usage_scoped_entry_without_a_model_is_skipped(self):
        from ccswitch.oauth import parse_usage
        payload = dict(self.SCOPED_PAYLOAD)
        payload["limits"] = self.SCOPED_PAYLOAD["limits"][:2] + [
            {"kind": "weekly_scoped", "group": "weekly", "percent": 40,
             "resets_at": "2026-09-14T19:59:59Z",
             "scope": {"model": None, "surface": "cowork"}},
            {"kind": "weekly_scoped", "group": "weekly", "percent": "n/a",
             "resets_at": None,
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ]
        u = parse_usage(payload)
        self.assertEqual([w.key for w in u.windows], ["session", "weekly"])

    def test_parse_usage_without_limits_reads_legacy_seven_day_model_keys(self):
        from ccswitch.oauth import parse_usage
        payload = {k: v for k, v in self.SCOPED_PAYLOAD.items() if k != "limits"}
        payload["seven_day_opus"] = {"utilization": 55.0, "resets_at": "2026-09-14T20:00:00Z"}
        u = parse_usage(payload)
        self.assertEqual([w.key for w in u.windows], ["session", "weekly", "weekly:opus"])
        self.assertEqual(u.window("weekly:opus").applies_to_models, ("opus",))
```

Append to `tests/test_quota.py`:

```python
def test_anthropic_snapshot_appends_model_windows_after_the_account_pair():
    fable = QuotaWindow(key="weekly:fable", label="Fable weekly", used_pct=100.0,
                        resets_at=NOW, duration_seconds=7 * 24 * 3600,
                        scope=SCOPE_MODEL, applies_to_models=("fable",))
    u = anthropic_snapshot(3.0, NOW, 91.0, NOW, fetched_at=NOW, model_windows=[fable])
    assert [w.key for w in u.windows] == ["session", "weekly", "weekly:fable"]
    # The Fable window binds Fable only; Sonnet sees the account-wide weekly.
    assert u.binding_window("fable").key == "weekly:fable"
    assert u.binding_window("sonnet").key == "weekly"
    assert u.binding_window(None).key == "weekly:fable"   # unknown model: conservative
    # Round-trips through the persisted encoding unchanged, same blob version.
    again = QuotaSnapshot.from_dict(u.to_dict())
    assert again.window("weekly:fable").applies_to_models == ("fable",)
    assert u.to_dict()["v"] == quota.QUOTA_BLOB_VERSION == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/test_oauth.py tests/test_quota.py -q -k "scoped or model_windows or legacy_seven_day"`
Expected: FAIL — `anthropic_snapshot() got an unexpected keyword argument 'model_windows'` and key lists equal to `["session", "weekly"]`.

- [ ] **Step 3: Extend `anthropic_snapshot`**

In `src/ccswitch/quota.py` change the signature and body:

```python
def anthropic_snapshot(session_pct: float,
                       session_resets_at: datetime | None,
                       weekly_pct: float,
                       weekly_resets_at: datetime | None,
                       extra_usage_enabled: bool = False,
                       *,
                       fetched_at: datetime | None = None,
                       availability: str = AVAILABLE,
                       model_windows: Sequence[QuotaWindow] = ()) -> QuotaSnapshot:
    """Anthropic reports TWO account-wide windows (rolling 5-hour, rolling 7-day) and,
    for some plans, extra windows that meter ONE model (e.g. a separate Fable weekly).
    *model_windows* are appended AFTER the account-wide pair: ``binding_window``'s
    tie rule and ``reserve_window`` both depend on provider order."""
    return QuotaSnapshot(
        provider="anthropic",
        fetched_at=fetched_at or datetime.now(timezone.utc),
        availability=availability,
        windows=(
            QuotaWindow(key=WINDOW_SESSION, label="session", used_pct=float(session_pct),
                        resets_at=session_resets_at, duration_seconds=_FIVE_HOURS_S),
            QuotaWindow(key=WINDOW_WEEKLY, label="weekly", used_pct=float(weekly_pct),
                        resets_at=weekly_resets_at, duration_seconds=_SEVEN_DAYS_S),
            *model_windows,
        ),
        extra_usage_enabled=bool(extra_usage_enabled),
    )
```

Add `Sequence` to the `typing` import at the top of the module.

- [ ] **Step 4: Extend `parse_usage`**

In `src/ccswitch/oauth.py`, change the import to
`from .quota import (QuotaSnapshot, QuotaWindow, SCOPE_MODEL, anthropic_snapshot, _FIVE_HOURS_S, _SEVEN_DAYS_S)`
and add `import re` to the stdlib imports. Then add the helpers above `parse_usage` and extend it:

```python
_MODEL_ID_TIER = re.compile(r"^claude-([a-z]+)-")

_GROUP_DURATION_S = {"weekly": _SEVEN_DAYS_S, "session": _FIVE_HOURS_S}


def _scoped_model_key(scope: object) -> tuple[str, str] | None:
    """``(model_key, display_name)`` for a ``limits[]`` entry's ``scope``, or None.

    Anthropic names the scoped model by ``display_name`` with a null ``id`` (verified
    2026-09-14). The key is the lowercase tier token the server's
    ``model_catalog.model_tier`` produces for the same model, so a window keyed
    ``fable`` matches a session on ``fable``, ``fable[1m]`` or ``claude-fable-5-1``.
    An ``id``, when present, wins over the display name.
    """
    if not isinstance(scope, dict):
        return None
    model = scope.get("model")
    if not isinstance(model, dict):
        return None
    display = str(model.get("display_name") or "").strip()
    model_id = str(model.get("id") or "").strip().lower()
    m = _MODEL_ID_TIER.match(model_id) if model_id else None
    if m:
        return m.group(1), display or m.group(1).capitalize()
    if display:
        return display.split()[0].lower(), display
    return None


def _model_window(group: str, key: str, display: str, percent: object,
                  resets_at: object) -> QuotaWindow | None:
    try:
        pct = float(percent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return QuotaWindow(
        key=f"{group}:{key}", label=f"{display} {group}", used_pct=pct,
        resets_at=_parse_iso(resets_at if isinstance(resets_at, str) else None),
        duration_seconds=_GROUP_DURATION_S.get(group),
        scope=SCOPE_MODEL, applies_to_models=(key,),
    )


def _model_windows(payload: dict) -> list[QuotaWindow]:
    """The model-scoped windows in *payload*, in payload order.

    Reads ``limits[]`` entries whose ``scope.model`` is set. When the payload has no
    ``limits`` list at all (older shape), falls back to the top-level
    ``seven_day_<model>`` objects — the same fallback the account-wide pair has.
    A scoped entry with no active window (``resets_at`` null, 0%) is still emitted:
    the badge should show it, and ``QuotaWindow`` models a None reset as "not
    perishable yet". A malformed entry is skipped, never raised — one odd bucket
    must not blank an account's usage.
    """
    limits = payload.get("limits")
    out: list[QuotaWindow] = []
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            scoped = _scoped_model_key(entry.get("scope"))
            if scoped is None:
                continue
            key, display = scoped
            w = _model_window(str(entry.get("group") or "weekly"), key, display,
                              entry.get("percent"), entry.get("resets_at"))
            if w is not None:
                out.append(w)
        return out
    for name, obj in payload.items():
        if not (isinstance(name, str) and name.startswith("seven_day_")
                and isinstance(obj, dict)):
            continue
        key = name[len("seven_day_"):]
        w = _model_window("weekly", key, key.capitalize(),
                          obj.get("utilization"), obj.get("resets_at"))
        if w is not None:
            out.append(w)
    return out
```

and in `parse_usage` change the final `return` to:

```python
    return anthropic_snapshot(session_pct, session_reset, weekly_pct, weekly_reset,
                              extra, fetched_at=fetched_at,
                              model_windows=_model_windows(payload))
```

Note: the legacy fallback deliberately does not filter `seven_day_<x>` names against a
tier list (core has none). On the live payload those keys are all null, so nothing is
emitted; the `limits` branch is what production takes.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/test_oauth.py tests/test_quota.py -q`
Expected: all PASS (including the pre-existing parser tests — the two account-wide windows are unchanged).

- [ ] **Step 6: Commit**

```bash
cat > /tmp/c1.txt <<'EOF'
feat(quota): parse Anthropic's model-scoped usage windows

Anthropic meters some models separately (a Fable weekly, verified live
2026-09-14 as a limits[] entry with scope.model.display_name and a null id).
parse_usage dropped it. Emit one SCOPE_MODEL window per scoped entry, keyed
"<group>:<tier>" and applying to that tier only, after the account-wide pair.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add src/ccswitch/quota.py src/ccswitch/oauth.py tests/test_oauth.py tests/test_quota.py
git commit -F /tmp/c1.txt
```

---

### Task 2: `reserve_window` and the selector's model rules

**Files:**
- Modify: `src/ccswitch/quota.py:214-231` (`reserve_window`)
- Test: `tests/test_quota.py`, `tests/test_selector.py`

**Interfaces:**
- Consumes: `anthropic_snapshot(..., model_windows=)` from Task 1.
- Produces: `QuotaSnapshot.reserve_window()` never returns a `SCOPE_MODEL` window when an account-scoped one exists. Pins (by test) that `rank_accounts(model=...)` already behaves per spec.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_quota.py`:

```python
def test_reserve_window_ignores_model_scoped_windows():
    fable = QuotaWindow(key="weekly:fable", label="Fable weekly", used_pct=100.0,
                        resets_at=NOW, duration_seconds=7 * 24 * 3600,
                        scope=SCOPE_MODEL, applies_to_models=("fable",))
    u = anthropic_snapshot(3.0, NOW, 91.0, NOW, fetched_at=NOW, model_windows=[fable])
    assert u.reserve_window().key == "weekly"
```

Append to `tests/test_selector.py` inside `TestSelector`:

```python
    # --- model-scoped windows (per-model limits) ---

    def _kindash_like(self):
        """weekly 91% / Fable weekly 100%: usable for Sonnet, exhausted for Fable."""
        from ccswitch.quota import QuotaWindow, SCOPE_MODEL, anthropic_snapshot
        fable_reset = self.now + timedelta(hours=8)
        fable = QuotaWindow(key="weekly:fable", label="Fable weekly", used_pct=100.0,
                            resets_at=fable_reset, duration_seconds=7 * 24 * 3600,
                            scope=SCOPE_MODEL, applies_to_models=("fable",))
        return anthropic_snapshot(3.0, self.now + timedelta(hours=1), 91.0,
                                  self.now + timedelta(days=2),
                                  model_windows=[fable]), fable_reset

    def test_model_scoped_ceiling_excludes_only_that_model(self):
        from ccswitch.selector import select_best, AllAccountsBusy
        u, fable_reset = self._kindash_like()
        self.assertEqual(select_best({"k": u}, set(), self.cfg, self.now, model="sonnet"), "k")
        with self.assertRaises(AllAccountsBusy) as ctx:
            select_best({"k": u}, set(), self.cfg, self.now, model="fable")
        self.assertEqual(ctx.exception.earliest_reset, fable_reset)

    def test_unknown_model_treats_a_model_scoped_ceiling_as_binding(self):
        """Pinned on purpose: a caller that names no model must not be handed an
        account that 429s on its first turn if that turn happens to be Fable."""
        from ccswitch.selector import select_best, AllAccountsBusy
        u, _ = self._kindash_like()
        with self.assertRaises(AllAccountsBusy):
            select_best({"k": u}, set(), self.cfg, self.now, model=None)

    def test_unknown_scoped_bucket_never_matches_a_known_tier(self):
        from ccswitch.quota import QuotaWindow, SCOPE_MODEL, anthropic_snapshot
        from ccswitch.selector import select_best, AllAccountsBusy
        promo = QuotaWindow(key="weekly:promo", label="Promo weekly", used_pct=100.0,
                            resets_at=self.now + timedelta(days=1), scope=SCOPE_MODEL,
                            applies_to_models=("promo",))
        u = anthropic_snapshot(3.0, self.now + timedelta(hours=1), 10.0,
                               self.now + timedelta(days=2), model_windows=[promo])
        self.assertEqual(select_best({"k": u}, set(), self.cfg, self.now, model="fable"), "k")
        with self.assertRaises(AllAccountsBusy):   # model-blind caller: conservative
            select_best({"k": u}, set(), self.cfg, self.now, model=None)
```

Check `select_best`'s signature in `src/ccswitch/selector.py:334` — it forwards `**kw`/`model` to `select_picked`; if it does not accept `model`, add `model: str | None = None` to it and pass it through to `select_picked(..., model=model)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/test_quota.py::test_reserve_window_ignores_model_scoped_windows tests/test_selector.py -q -k "model_scoped or unknown_model or unknown_scoped"`
Expected: the `reserve_window` test FAILS (returns `weekly:fable`, the first maximal-duration window is `weekly` — if it PASSES already, keep the test as a pin and move on); selector tests either PASS (the selector is already model-aware) or FAIL on a missing `model` kwarg on `select_best`.

- [ ] **Step 3: Restrict `reserve_window` to account-scoped windows**

In `src/ccswitch/quota.py` replace the body of `reserve_window`:

```python
        if not self.windows:
            return None
        # A reserve protects the ACCOUNT's long-horizon allowance; a window that
        # meters one model (a Fable weekly) is never the thing it measures.
        candidates = [w for w in self.windows if w.scope == SCOPE_ACCOUNT] or list(self.windows)
        if len(candidates) == 1:
            return candidates[0]
        timed = [w for w in candidates if w.duration_seconds is not None]
        if not timed:
            return None
        return max(timed, key=lambda w: w.duration_seconds or 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/test_quota.py tests/test_selector.py tests/test_selector_copilot_parity.py tests/test_selector_filtering.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c2.txt <<'EOF'
feat(quota): reserve binds an account-scoped window; pin the selector's model rules

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add src/ccswitch/quota.py src/ccswitch/selector.py tests/test_quota.py tests/test_selector.py
git commit -F /tmp/c2.txt
```

---

### Task 3: `quota_model` — one definition of the model a window is matched against

**Files:**
- Modify: `server/sessions/model_policy.py` (add `quota_model` after `launch_model`, line ~101)
- Create: `server/tests/test_quota_model.py`

**Interfaces:**
- Consumes: `model_policy.launch_model(model, *, engine)`, `model_catalog.model_tier`.
- Produces: `quota_model(model: str | None, *, engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_quota_model.py`:

```python
"""quota_model: the key a session's model is matched against quota windows with."""
import json

import pytest

from server.sessions.engine import AgentEngine
from server.sessions.model_policy import quota_model


@pytest.mark.parametrize("model, expected", [
    ("fable", "fable"),
    ("fable[1m]", "fable"),
    ("claude-fable-5-1", "fable"),
    ("Opus", "opus"),
    ("opusplan", "opus"),
    ("claude-haiku-4-5-20251001", "haiku"),
    ("best", None),                 # no fixed tier: every window applies
    ("some-unknown-id", None),
])
def test_explicit_models_map_to_their_tier(model, expected, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert quota_model(model) == expected


def test_default_resolves_to_the_inherited_fleet_model(monkeypatch, tmp_path):
    """A 'Default' session launches on the shared settings model (#1460), so it is
    judged on that model's windows — with the fleet default on Fable, a Fable-
    exhausted account must not receive a Default session."""
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    (tmp_path / "settings.json").write_text(json.dumps({"model": "fable"}))
    from ccswitch import paths
    monkeypatch.setattr(paths, "shared_base", lambda: tmp_path)
    assert quota_model(None) == "fable"
    assert quota_model("default") == "fable"


def test_default_with_nothing_pinned_is_indeterminate(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    from ccswitch import paths
    monkeypatch.setattr(paths, "shared_base", lambda: tmp_path)   # no settings files
    assert quota_model(None) is None


def test_non_claude_engines_are_model_blind():
    assert quota_model("gpt-5-codex", engine=AgentEngine.CODEX) is None
    assert quota_model("fable", engine="codex") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_quota_model.py -q`
Expected: FAIL with `ImportError: cannot import name 'quota_model'`.

- [ ] **Step 3: Implement `quota_model`**

In `server/sessions/model_policy.py`, directly after `launch_model`:

```python
def quota_model(model: str | None, *,
                engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str | None:
    """The key quota windows are matched against for a session that runs *model*.

    The ONE place a session model becomes a ``QuotaWindow.applies_to_models`` key;
    every selection / rotation call site passes this and nothing re-derives it.

    Claude: the tier of the model the session will ACTUALLY launch on —
    ``launch_model`` resolves ``default``/None to the inherited settings model, so a
    "Default" session is judged on the fleet default the binary would pick. None when
    the tier is indeterminate (``best``, an unknown id, nothing pinned) or the engine
    is not Claude: the selector then applies EVERY window, which is the conservative
    direction (``QuotaWindow.applies_to``).
    """
    if str(getattr(engine, "value", engine)) != AgentEngine.CLAUDE_CODE.value:
        return None
    return model_tier(launch_model(model, engine=engine))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command. Expected: all PASS. Also run `server/tests/test_model_policy.py` to confirm nothing else moved.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c3.txt <<'EOF'
feat(sessions): quota_model — the one model key quota windows are matched with

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/sessions/model_policy.py server/tests/test_quota_model.py
git commit -F /tmp/c3.txt
```

---

### Task 4: Thread the model through selection

**Files:**
- Modify: `src/ccswitch/accounts.py:564-672` (`pick_account_detailed`, `pick_account`)
- Modify: `server/sessions/selection.py:21-54` (`select_account`)
- Modify: `server/sessions/orchestrator.py` call sites: `_spawn_session` (~3518), `_backlog_healthy_account` (~5031), `rotate_to_best` (~12857), `_rotate_away` (~15273)
- Test: `tests/test_accounts_pick_model.py` (new), `server/tests/test_selection.py`

**Interfaces:**
- Consumes: `quota_model` (Task 3).
- Produces: `pick_account_detailed(..., model: str | None = None)`, `pick_account(cfg, exclude, load, *, model=None)`, `select_account(..., model: str | None = None)`; all forward to `select_picked(..., model=model)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_accounts_pick_model.py`:

```python
"""pick_account_detailed forwards *model* to the selector."""
import unittest
from unittest.mock import patch

from tests.helpers import temp_env, usage


class TestPickModel(unittest.TestCase):
    def test_model_reaches_select_picked(self):
        with temp_env():
            from ccswitch import accounts
            from ccswitch.config import load_config
            from ccswitch.selector import Picked
            cfg = load_config()
            seen = {}

            def fake_select_picked(usages, reserved, cfg_, now, exclude, load, *a, **kw):
                seen["model"] = kw.get("model")
                return Picked(name="alpha", reason="test", optimistic=False)

            with patch.object(accounts, "select_picked", fake_select_picked), \
                 patch.object(accounts, "usage_for", lambda n: usage()), \
                 patch.object(accounts, "list_account_names", lambda: ["alpha"]):
                accounts.pick_account_detailed(cfg, reserve=False, model="fable")
        self.assertEqual(seen["model"], "fable")
```

Before running, open `src/ccswitch/accounts.py:564-600` and confirm the names the
function uses to list accounts and read usage (`usage_for` is confirmed; the
account-name lister may be `account_registry`-based — patch whatever `pick_account_detailed`
actually calls, and construct `Picked` with the fields its dataclass declares in
`selector.py`). Adjust the two patches accordingly; the assertion is the point.

Append to `server/tests/test_selection.py`:

```python
def test_select_account_forwards_model(monkeypatch):
    from server.sessions import selection
    seen = {}

    def fake_pick(cfg, *, exclude, load, reserve, soft_exclude, require_engine,
                  default_engine, model=None):
        seen["model"] = model
        from ccswitch.selector import Picked
        return Picked(name="alpha", reason="test", optimistic=False)

    monkeypatch.setattr(selection, "pick_account_detailed", fake_pick)
    with temp_env():
        selection.select_account(model="sonnet")
    assert seen["model"] == "sonnet"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest tests/test_accounts_pick_model.py -q` and the server command for `server/tests/test_selection.py -q -k forwards_model`.
Expected: FAIL with `unexpected keyword argument 'model'`.

- [ ] **Step 3: Thread `model` through core and server selection**

`src/ccswitch/accounts.py` — add `model: str | None = None` as the last keyword-only
parameter of `pick_account_detailed`, document it in the docstring as "the
`QuotaWindow.applies_to_models` key of the model the session will run; None applies
every window (conservative)", and pass `model=model` inside `_pick`'s
`select_picked(...)` call. Give `pick_account` the same keyword and forward it:

```python
def pick_account(cfg: Config, exclude: set[str] = frozenset(),
                 load: dict[str, float] | None = None, *,
                 model: str | None = None) -> str:
    """Pick for the legacy Claude launchers; never cross into Codex accounts."""
    return pick_account_detailed(cfg, exclude, load, model=model).name
```

`server/sessions/selection.py` — add `model: str | None = None` to `select_account`
(keyword-only, after `engine`), a docstring line ("*model*: the quota key from
`model_policy.quota_model`; None applies every window"), and forward `model=model`.

`server/sessions/orchestrator.py` — at each call site add the argument. Import inside
the method bodies the way neighbouring code does (`from server.sessions.model_policy
import quota_model`), or add a module-level import if `model_policy` imports cleanly
without a cycle (check: `model_policy` imports `server.config`, `server.db.models`,
`server.sessions.engine`, `server.sessions.model_catalog` — none import the
orchestrator, so a module-level import is fine).

  - `_spawn_session` (~3518): `select_account(exclude=..., load=..., engine=selected_engine, model=quota_model(model, engine=selected_engine))` — `model` is the method parameter.
  - `_backlog_healthy_account` (~5031): the probe has no per-dispatch model in scope; pass `model=quota_model(None, engine=engine)` so a Default launch is judged on the inherited fleet model.
  - `rotate_to_best` (~12857) and `_rotate_away` (~15273): `model=quota_model(record.model, engine=record.engine)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run the two Step 2 commands plus `tests/test_accounts*.py`, `tests/test_launcher*.py`, `tests/test_runner*.py` (core callers untouched but re-check), and `server/tests/test_selection.py server/tests/test_rotate_to_best.py server/tests/test_manual_switch_and_pin.py -q`.
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c4.txt <<'EOF'
feat(selection): pick, rotate and probe accounts for the model the session runs

select_account/pick_account_detailed gain `model`; the orchestrator passes
quota_model(...) at spawn, backlog probe, rotate_to_best and _rotate_away, so a
Fable-exhausted account still serves Sonnet and is never handed to Fable.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add src/ccswitch/accounts.py server/sessions/selection.py server/sessions/orchestrator.py tests/test_accounts_pick_model.py server/tests/test_selection.py
git commit -F /tmp/c4.txt
```

---

### Task 5: Rotation monitor and dead-account checks judge per session

**Files:**
- Modify: `server/sessions/orchestrator.py`: `_limiting_window` (~530), `_account_limited` (~10585), `_rotation_monitor` (~14890-14997), `_account_is_dead` (~15022), `_sweep_needs_attention` limited branch (~15070-15090), `_recover_session` process_crash branch (~15184)
- Test: `server/tests/test_dead_account_rotation.py`

**Interfaces:**
- Consumes: `quota_model` (Task 3); `selector.exhausted/at_ceiling(..., model=)`; `QuotaSnapshot.windows_at_or_over(pct, model)`.
- Produces: `_limiting_window(usage, ceiling, model=None)`, `_account_is_dead(account, model=None)`, `_account_limited(account, model=None)`; `_account_limited_notified` holds `"<account>:<window.key>"` strings.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_dead_account_rotation.py` (reuse `_make_orch`, `_rec`, `_poller_with_failures`, `_run_monitor_one_event`):

```python
# ---------------------------------------------------------------------------
# Per-model windows: only sessions whose model the exhausted window meters move
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
from ccswitch.quota import QuotaWindow, SCOPE_MODEL

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _fable_exhausted_snapshot():
    """weekly 91% / Fable weekly 100% — the live 2026-09-14 kindash shape."""
    fable = QuotaWindow(key="weekly:fable", label="Fable weekly", used_pct=100.0,
                        resets_at=_NOW + timedelta(hours=8), duration_seconds=7 * 24 * 3600,
                        scope=SCOPE_MODEL, applies_to_models=("fable",))
    return anthropic_snapshot(3.0, _NOW + timedelta(hours=1), 91.0, _NOW + timedelta(days=2),
                              fetched_at=_NOW, model_windows=[fable])


def _two_recs(o, account="copilot_acct"):
    fab = SessionRecord(id="fab", name="f", account=account, engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd="/tmp/x",
                        status="idle", model="fable")
    son = SessionRecord(id="son", name="s", account=account, engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c2", cols=80, rows=24, cwd="/tmp/x",
                        status="idle", model="sonnet")
    for r in (fab, son):
        o._records[r.id] = r
        o._drivers[r.id] = object()
        o._last_prompt[r.id] = "go"
    return fab, son


@pytest.mark.asyncio
async def test_model_scoped_ceiling_rotates_only_sessions_on_that_model(monkeypatch):
    o = _make_orch()
    _two_recs(o)
    o._poller = _poller_with_failures(0)
    sent = []

    async def fake_notify(kind, title, body, **kw):
        sent.append((kind, body))
    monkeypatch.setattr(orch_mod.push_mod, "notify_event", fake_notify)

    async def spy_do_rotate(record, *, reason):
        o.calls.append(("do_rotate", record.id, reason))
    o._do_rotate = spy_do_rotate

    await _run_monitor_one_event(o, {"account": "copilot_acct",
                                     "usage": _fable_exhausted_snapshot()})
    assert ("do_rotate", "fab", "Fable weekly limit reached") in o.calls
    assert not any(c[1] == "son" for c in o.calls)
    assert sent == [("account_limited", "copilot_acct: Fable weekly limit reached")]
    assert "copilot_acct:weekly:fable" in o._account_limited_notified


@pytest.mark.asyncio
async def test_pinned_fable_session_parks_with_the_scoped_window_reset(monkeypatch):
    o = _make_orch()
    fab, _son = _two_recs(o)
    fab.account_pinned = True
    o._poller = _poller_with_failures(0)

    async def fake_notify(*a, **kw):
        return None
    monkeypatch.setattr(orch_mod.push_mod, "notify_event", fake_notify)
    persisted = []

    async def spy_persist(sid, status):
        persisted.append((sid, status))
    o._persist_status = spy_persist

    await _run_monitor_one_event(o, {"account": "copilot_acct",
                                     "usage": _fable_exhausted_snapshot()})
    assert persisted == [("fab", "limited")]
    assert fab.paused_reason == "pinned_account_limited"
    assert fab.paused_until == (_NOW + timedelta(hours=8)).replace(tzinfo=None)


def test_account_is_dead_and_limited_are_model_aware():
    o = _make_orch()
    o._poller = _poller_with_failures(0, cached_usage=_fable_exhausted_snapshot())
    assert o._account_is_dead("copilot_acct", model="fable") is True
    assert o._account_is_dead("copilot_acct", model="sonnet") is False
    assert o._account_is_dead("copilot_acct") is True          # unknown model: conservative
    assert o._account_limited("copilot_acct", model="sonnet") is False
    assert o._account_limited("copilot_acct", model="fable") is True


@pytest.mark.asyncio
async def test_crash_on_a_fable_exhausted_account_rebuilds_a_sonnet_session_in_place():
    o = _make_orch()
    rec = _rec(o, account="copilot_acct")
    rec.model = "sonnet"
    o._poller = _poller_with_failures(0, cached_usage=_fable_exhausted_snapshot())
    await o._recover_session(rec, "process_crash", None)
    assert ("reassign", "s1", "copilot_acct", "process_crash") in o.calls
    assert not any(c[0] == "rotate" for c in o.calls)
```

Also update the existing monitor tests in this file that assert on
`o._account_limited_notified` contents (if any assert `"copilot_acct" in ...`): the
key is now `f"{account}:{window.key}"` for a usage-ceiling entry and stays the bare
account name for the `account_unreachable` branch (there is no window to name).

- [ ] **Step 2: Run the tests to verify they fail**

Run the server pytest command for `server/tests/test_dead_account_rotation.py -q`.
Expected: the four new tests FAIL (`unexpected keyword argument 'model'`; both sessions rotated; notification body reads `weekly limit reached` or similar).

- [ ] **Step 3: Make the helpers model-aware**

`_limiting_window`:

```python
def _limiting_window(usage, ceiling: float, model: str | None = None):
    """The window that is actually holding *model* back on this account, or None.

    The FIRST window at/over *ceiling* in provider order among those that meter
    *model* — for Anthropic that is session, weekly, then any model-scoped window —
    which is exactly what the pair math it replaced did. Falls back to the binding
    window so a caller below the ceiling still gets the constraining one.
    """
    if usage is None:
        return None
    over = usage.windows_at_or_over(ceiling, model)
    return over[0] if over else usage.binding_window(model)
```

`_account_limited(self, account: str, model: str | None = None)`: pass `model` to
`at_ceiling(usage, _ROTATION_CEILING, model)`. `_account_is_dead(self, account: str,
model: str | None = None)`: pass `model=model` to `exhausted(...)`. Extend both
docstrings with one line: "*model* is the `quota_model` key; None applies every window."

- [ ] **Step 4: Rewrite the usage branch of `_rotation_monitor`**

Add a module-level import `from server.sessions.model_policy import quota_model` (or a
local import at the top of the method, matching Task 4's choice). Replace the
`else:` branch (lines ~14914-14927) and the per-record loop as follows. The
`usage is None` branch is unchanged (`reason = "account_unreachable"`, notification key
stays the bare account name).

```python
                else:
                    if not usage.windows_at_or_over(_ROTATION_CEILING):
                        # Nothing at ceiling for ANY model: recovery branch.
                        self._maybe_sweep_on_recovery()
                        self._account_limited_notified = {
                            k for k in self._account_limited_notified
                            if not (k == account or k.startswith(f"{account}:"))}
                        continue
                    if not exhausted(usage, _ROTATION_CEILING,
                                     extra_usage_allowed=self._extra_usage_allowed(account)):
                        # At ceiling but with paid overflow the selector would still hand
                        # out (rank_accounts' extra_paid tier): nothing to move.
                        self._maybe_sweep_on_recovery()
                        continue
                    # Notify once per (account, window): a Fable exhaustion and a later
                    # account-wide one are different news. Drop keys for windows that
                    # fell back under the ceiling so a re-exhaustion re-fires.
                    over_keys = {f"{account}:{w.key}"
                                 for w in usage.windows_at_or_over(_ROTATION_CEILING)}
                    self._account_limited_notified = {
                        k for k in self._account_limited_notified
                        if not k.startswith(f"{account}:") or k in over_keys}
                    for w in usage.windows_at_or_over(_ROTATION_CEILING):
                        key = f"{account}:{w.key}"
                        if key in self._account_limited_notified:
                            continue
                        self._account_limited_notified.add(key)
                        await push_mod.notify_event(
                            "account_limited", f"{brand.NAME} — account limited",
                            f"{account}: {w.label} limit reached",
                            account=account, orch=self)
                    reason = "usage limit reached"   # refined per session below
                    session_pct = 0.0
```

Then move the existing account-level notification block (`if account not in
self._account_limited_notified: ... notify_event(...)`) **inside the `usage is None`
branch** so the unreachable case still notifies once per account, and delete it from
the shared path. In the per-record loop, after the `_owns_record` check and before the
driverless check, insert the per-session judgement:

```python
                    if usage is not None:
                        m = quota_model(record.model, engine=record.engine)
                        if not exhausted(usage, _ROTATION_CEILING,
                                         extra_usage_allowed=self._extra_usage_allowed(account),
                                         model=m):
                            continue   # this session's model still has headroom here
                        limiting = _limiting_window(usage, _ROTATION_CEILING, m)
                        session_pct = limiting.used_pct if limiting is not None else 0.0
                        reason = (f"{limiting.label} limit reached" if limiting is not None
                                  else "usage limit reached")
```

and in the pinned branch replace `_limiting_window(usage, _ROTATION_CEILING)` with
`_limiting_window(usage, _ROTATION_CEILING, quota_model(record.model, engine=record.engine))`.

Note `reason` / `session_pct` must be assigned in the `usage is None` branch before the
loop (they already are) and re-assigned per record when `usage` is not None.

- [ ] **Step 5: Per-session model in the sweep and crash recovery**

`_sweep_needs_attention` limited branch (~15082): `at_ceiling(usage, _ROTATION_CEILING,
quota_model(record.model, engine=record.engine))`.
`_recover_session` process_crash branch (~15184): `self._account_is_dead(record.account,
model=quota_model(record.model, engine=record.engine))`.

- [ ] **Step 6: Run the tests to verify they pass**

Run the server command for `server/tests/test_dead_account_rotation.py server/tests/test_stuck_session_sweep.py server/tests/test_manual_switch_and_pin.py server/tests/test_backlog*.py server/tests/test_rotation*.py -q`.
Expected: all PASS. If an existing test asserted the old notification body `"<account>: weekly limit reached"`, it still passes (label `weekly`); if one asserted the bare account key in `_account_limited_notified` for a ceiling event, update it to the `account:window` key and say so in the commit body.

- [ ] **Step 7: Commit**

```bash
cat > /tmp/c5.txt <<'EOF'
feat(rotation): judge usage-ceiling rotation per session model

The rotation monitor, the stuck-session sweep, crash recovery and the backlog
gate now ask whether the window at ceiling meters THIS session's model. A
Sonnet session on a Fable-exhausted account stays; the Fable one moves, and the
reason and the account_limited push name the window ("Fable weekly").

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/sessions/orchestrator.py server/tests/test_dead_account_rotation.py
git commit -F /tmp/c5.txt
```

---

### Task 6: Carry the SDK's `rate_limit_type`

**Files:**
- Modify: `server/sessions/events.py:262-267` (`RateLimit`), `:288-294` (`TurnFailure`)
- Modify: `server/sessions/translator.py:260-265`
- Modify: `server/sessions/claude_driver.py:1001-1017` (`_maybe_signal_recovery`), `:1050-1057` (RateLimit stash)
- Test: `server/tests/test_translator_ratelimit.py`, `server/tests/test_driver_recovery.py`

**Interfaces:**
- Produces: `ev.RateLimit(resets_at, status, rate_limit_type: str | None = None)`; `ev.TurnFailure(kind, account, retry_after, limit_type: str | None = None)` with wire key `limitType`; driver attribute `_last_rate_limit_type: str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_translator_ratelimit.py`:

```python
def test_rate_limit_event_carries_its_type():
    tr = Translator()
    out = tr.feed(RateLimitEvent(rate_limit_info=_info(), uuid="u1", session_id="s1"))
    rl = [e for e in out if isinstance(e, ev.RateLimit)][0]
    assert rl.rate_limit_type == "five_hour"


def test_turn_failure_wire_includes_limit_type():
    tf = ev.TurnFailure(kind="rate_limit", account="a", retry_after=5.0,
                        limit_type="seven_day_opus")
    assert tf.to_wire()["limitType"] == "seven_day_opus"
    assert ev.TurnFailure(kind="auth_401", account="a").to_wire()["limitType"] is None
```

Open `server/tests/test_driver_recovery.py`, find the test that feeds a `RateLimitEvent`
then a 429 result and asserts on the emitted `TurnFailure` (search `retry_after`). Copy
its arrangement into a new test in the same file that feeds
`RateLimitInfo(..., rate_limit_type="seven_day_opus")` and asserts the emitted
`TurnFailure.limit_type == "seven_day_opus"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run the server command for `server/tests/test_translator_ratelimit.py server/tests/test_driver_recovery.py -q`.
Expected: FAIL (`unexpected keyword argument 'rate_limit_type'` / `'limit_type'`).

- [ ] **Step 3: Implement**

`events.py`:

```python
@dataclass
class RateLimit:
    """Decoded SDK RateLimitEvent. Internal carrier only: the driver consumes it for
    retry timing (resets_at, epoch seconds) and never forwards it, so it has no
    wire form. ``rate_limit_type`` names the bucket (five_hour, seven_day,
    seven_day_opus, …) so a model-scoped 429 is distinguishable in the logs."""
    resets_at: int | None = None; status: str | None = None
    rate_limit_type: str | None = None
```

```python
@dataclass
class TurnFailure:
    """A turn failed with a recoverable kind (rate_limit / auth_401). Informational
    for the UI; the orchestrator drives the actual recovery off the same signal.
    ``limit_type`` is the SDK bucket that tripped (rate_limit only), for display."""
    kind: str; account: str; retry_after: float | None = None
    limit_type: str | None = None
    def to_wire(self): return {"type": "turnFailure", "kind": self.kind,
                               "account": self.account, "retryAfter": self.retry_after,
                               "limitType": self.limit_type}
```

`translator.py`: `return [ev.RateLimit(resets_at=getattr(info, "resets_at", None),
status=getattr(info, "status", None),
rate_limit_type=getattr(info, "rate_limit_type", None))]`.

`claude_driver.py`: where `_last_rate_reset` is initialised (search `self._last_rate_reset = None`) add `self._last_rate_limit_type: str | None = None`; in the RateLimit stash branch add `self._last_rate_limit_type = e.rate_limit_type` (unconditionally, before the `resets_at` check); in `_maybe_signal_recovery`:

```python
        limit_type = self._last_rate_limit_type if failure.kind == "rate_limit" else None
        self._emit(ev.TurnFailure(
            kind=failure.kind, account=self.account, retry_after=retry_after,
            limit_type=limit_type))
        if limit_type:
            log.info("driver.rate_limit session=%s account=%s bucket=%s retry_after=%s",
                     self.session_id, self.account, limit_type, retry_after)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command plus `server/tests/test_events*.py server/tests/test_translator*.py -q`. Expected: all PASS. Then run `mypy server/ --ignore-missing-imports` (venv python `-m mypy`) — expected clean.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c6.txt <<'EOF'
feat(sessions): carry the SDK rate-limit bucket through RateLimit and turnFailure

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add server/sessions/events.py server/sessions/translator.py server/sessions/claude_driver.py server/tests/test_translator_ratelimit.py server/tests/test_driver_recovery.py
git commit -F /tmp/c6.txt
```

---

### Task 7: Web — model-aware window reader and consumers

**Files:**
- Modify: `web/src/lib/usageWindows.ts`
- Modify: `web/src/features/sessions/list/SessionCard.tsx:668`
- Modify: `web/src/features/sessions/SessionMeta.tsx:25-30, 260`
- Test: `web/src/lib/usageWindows.test.ts`

**Interfaces:**
- Consumes: `modelTier(id)` from `web/src/session/modelName.ts`; `UsageWindow.scope` / `appliesToModels` (already typed).
- Produces: `windowsFor(usage, model)`, `bindingWindow(usage, model?)`, `windowLabel(w)`; `reserveWindow` prefers account scope.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/lib/usageWindows.test.ts` (reuse the file's existing snapshot helper; if it builds `AccountUsage` inline, follow that shape):

```ts
import { windowsFor, windowLabel } from './usageWindows'

const kindash: AccountUsage = {
  provider: 'anthropic', fetchedAt: '2026-09-14T12:00:00Z', availability: 'available',
  reason: null, staleAsOf: null, extraUsageEnabled: false,
  windows: [
    { key: 'session', label: 'session', pct: 3, resetsAt: '2026-09-14T16:40:00Z', durationSeconds: 18000, scope: 'account', appliesToModels: [] },
    { key: 'weekly', label: 'weekly', pct: 91, resetsAt: '2026-09-14T20:00:00Z', durationSeconds: 604800, scope: 'account', appliesToModels: [] },
    { key: 'weekly:fable', label: 'Fable weekly', pct: 100, resetsAt: '2026-09-14T19:59:59Z', durationSeconds: 604800, scope: 'model', appliesToModels: ['fable'] },
  ],
}

describe('model-scoped windows', () => {
  it('applies a model window only to its own tier, and every window to an unknown model', () => {
    expect(windowsFor(kindash, 'sonnet').map((w) => w.key)).toEqual(['session', 'weekly'])
    expect(windowsFor(kindash, 'claude-fable-5-1').map((w) => w.key)).toEqual(['session', 'weekly', 'weekly:fable'])
    expect(windowsFor(kindash, null).map((w) => w.key)).toEqual(['session', 'weekly', 'weekly:fable'])
    expect(windowsFor(kindash, 'best').map((w) => w.key)).toEqual(['session', 'weekly', 'weekly:fable'])
  })
  it('binds per model', () => {
    expect(bindingWindow(kindash, 'sonnet')?.key).toBe('weekly')
    expect(bindingWindow(kindash, 'fable')?.key).toBe('weekly:fable')
    expect(bindingWindow(kindash)?.key).toBe('weekly:fable')
  })
  it('never measures a reserve on a model window', () => {
    expect(reserveWindow(kindash)?.key).toBe('weekly')
  })
  it('labels account windows by abbreviation and model windows by the provider label', () => {
    expect(windowLabel(kindash.windows[1])).toBe('wk')
    expect(windowLabel(kindash.windows[2])).toBe('Fable weekly')
    expect(windowLabel({ key: 'primary', label: 'primary', pct: 1, resetsAt: null })).toBe('primary')
  })
})
```

Check the top of the test file for how `AccountUsage` is imported/typed and whether
`staleAsOf`/`reason` are required fields in `web/src/api/types.ts:41-59`; drop any field
the type does not declare.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/lib/usageWindows.test.ts)`
Expected: FAIL — `windowsFor`/`windowLabel` are not exported; `reserveWindow` returns `weekly` already or `weekly:fable` (either way the suite is red on the missing exports).

- [ ] **Step 3: Implement**

In `web/src/lib/usageWindows.ts` add `import { modelTier } from '@/session/modelName'` and:

```ts
/**
 * The windows that meter `model` — mirrors `QuotaSnapshot.windows_for`. A window
 * scoped to specific models applies when the session's TIER is listed; a model
 * with no fixed tier (null, 'default', 'best', unknown id) matches every window,
 * the conservative rule the server uses for the same reason.
 */
export function windowsFor(usage: AccountUsage | null | undefined, model: string | null | undefined): UsageWindow[] {
  const tier = modelTier(model)
  return usageWindows(usage).filter((w) => {
    if (w.scope !== 'model' || !w.appliesToModels || w.appliesToModels.length === 0) return true
    if (tier == null) return true
    return w.appliesToModels.includes(tier)
  })
}

/** Compact labels for account-wide windows; a model window keeps the provider's
 *  label ("Fable weekly") because the model IS the information. */
const ACCOUNT_ABBR: Record<string, string> = { session: 'sess', weekly: 'wk', premium: 'prem' }

export function windowLabel(w: UsageWindow): string {
  if (w.scope === 'model') return w.label
  return ACCOUNT_ABBR[w.key] ?? w.label
}
```

Change `bindingWindow` to take an optional second parameter and filter through
`windowsFor(usage, model)` instead of `usageWindows(usage)`. Change `reserveWindow` so
`const all = usageWindows(usage); const windows = all.filter((w) => w.scope !== 'model'); const pool = windows.length > 0 ? windows : all` and use `pool` where it used `windows`.

`SessionCard.tsx:668`: `const w = bindingWindow(account?.usage, session.model)`.

`SessionMeta.tsx`: delete the local `QUOTA_ABBR` map, import `windowLabel` from
`@/lib/usageWindows`, and render `{windowLabel(w)} {w.pct}%`. Keep `quotaTitle(w.label, w.resetsAt)` as is.

- [ ] **Step 4: Run the full web gates**

Run: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) 2>&1 | tee /tmp/web-gates.log | tail -30`
Expected: all four green (run the FULL vitest suite — ~47 files mock `@/api/hooks`). Check `grep -c "failed" /tmp/web-gates.log` shows no failing files; vitest's exit code is masked by `tee`, so read the summary lines.

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c7.txt <<'EOF'
feat(web): read per-model usage windows — bind by session model, label the model

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add web/src/lib/usageWindows.ts web/src/lib/usageWindows.test.ts web/src/features/sessions/list/SessionCard.tsx web/src/features/sessions/SessionMeta.tsx
git commit -F /tmp/c7.txt
```

---

### Task 8: Docs and the full gate

**Files:**
- Modify: `docs/product/features/usage-aware-rotation.md`

- [ ] **Step 1: Add the "Per-model windows" section**

Append to `docs/product/features/usage-aware-rotation.md`:

```markdown
## Per-model windows (2026-09-14)

Anthropic meters some models separately from the account-wide 5-hour and 7-day
windows. The usage payload carries them as `limits[]` entries with
`kind: "weekly_scoped"` and `scope.model.display_name` (the `id` is null); e.g. an
account can read weekly 91% but Fable 100%.

- **Parsing.** `oauth.parse_usage` emits one `QuotaWindow` per scoped entry after the
  account-wide pair: key `"<group>:<tier>"` (`weekly:fable`), `scope = "model"`,
  `applies_to_models = ("fable",)`. The tier token is what
  `server/sessions/model_catalog.model_tier` yields for the session's model.
- **Selection.** `select_account(..., model=quota_model(...))`: the selector applies
  only the windows that meter that tier, so a Fable-exhausted account still serves
  Sonnet. A caller that names no model (CLI launchers, non-Claude engines, `best`)
  gets **every** window applied — conservative by design.
- **Rotation.** The poller-driven monitor moves only sessions whose model the
  exhausted window meters; the reason and the `account_limited` push name the window
  ("Fable weekly limit reached"), once per account+window.
- **Display.** Every window is a badge on the Accounts page; session cards bind on
  the session's own model (`lib/usageWindows.ts:windowsFor`).
- **Not done here:** downgrading a session's model when every account is exhausted
  for it (a policy decision), per-model reserves, Codex per-model selection.
```

- [ ] **Step 2: Run the four Python gates and the brand ratchet**

```bash
V=/home/tony/GitHub/viberpit-deploy/.venv/bin/python
$V -m ruff check src/ server/
$V -m mypy server/ --ignore-missing-imports 2>&1 | tail -3
PYTHONPATH=$PWD/src $V -m pytest tests/ -q 2>&1 | tail -3
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_CERT -u CCSWITCH_SESSIOND_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src $V -m pytest server/tests/test_brand_ratchet.py -q
```

Expected: ruff clean, mypy `Success`, core suite green, ratchet green. The full
`server/tests/` run is launched by the controller (Task 9), not here.

- [ ] **Step 3: Commit**

```bash
cat > /tmp/c8.txt <<'EOF'
docs(usage): describe per-model windows, selection and rotation rules

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
git add docs/product/features/usage-aware-rotation.md
git commit -F /tmp/c8.txt
```

---

### Task 9 (controller, not a subagent): full server suite, PR, auto-merge

- [ ] Run the full server suite detached with the test-slot acquire loop from `.agents/instructions/core.md` in the SAME shell, output tee'd to `/tmp/per-model-server-tests.log`, `-n auto --dist loadfile --timeout=300 -m "not slow"` first, then the `slow` remainder.
- [ ] Re-run any failure alone; compare against the known rocky/dobby full-suite-only flakes in memory before treating it as real.
- [ ] `git push -u origin feat/per-model-usage-windows`; `gh pr create --title "feat(usage): per-model usage windows — report them and switch accounts on a per-model limit" --body-file /tmp/pr-body.md` (body ends with the Claude Code trailer; `Refs` the 2026-09-04 spec's PR #1094 as PR 1); `gh pr merge <#> --squash --auto`.
