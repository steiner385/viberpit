"""Model + reasoning effort are validated as ONE pair before a Codex session starts.

Two behaviours, and the second is the subtle one:

* An invalid pair is a typed 409 carrying the *current* choices, so the browser can
  re-render the picker instead of showing "something went wrong".
* A miss triggers exactly one forced catalog refresh. The catalog is cached for
  minutes, so the usual cause of a miss is a plan change or a CLI upgrade — one
  refresh answers that. A second retry would turn a genuinely wrong model name
  into a ``model/list`` loop against the account's app-server.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env  # noqa: E402

from server.codex.catalog import CodexModelCatalog, ModelOption  # noqa: E402

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1"}

_MODELS = (
    ModelOption(
        id="gpt-5.1-codex",
        label="GPT-5.1 Codex",
        default=True,
        reasoning_efforts=("low", "medium", "high"),
        default_reasoning_effort="medium",
    ),
    ModelOption(
        id="gpt-5.1-codex-mini",
        label="GPT-5.1 Codex mini",
        reasoning_efforts=("low", "medium"),
        default_reasoning_effort="low",
    ),
)


# ---------------------------------------------------------------------------
# The validator itself
# ---------------------------------------------------------------------------


def test_a_valid_pair_passes_through_unchanged():
    assert CodexModelCatalog.validate(_MODELS, "gpt-5.1-codex", "high") == (
        "gpt-5.1-codex",
        "high",
    )


def test_a_valid_model_with_a_wrong_effort_still_fails():
    """The pair is the unit. `high` is valid — just not on the mini model."""
    from server.codex.catalog import ModelEffortMismatch

    with pytest.raises(ModelEffortMismatch) as exc:
        CodexModelCatalog.validate(_MODELS, "gpt-5.1-codex-mini", "high")
    wire = exc.value.to_wire()
    assert wire["model"] == "gpt-5.1-codex-mini"
    assert wire["effort"] == "high"
    # The 409 has to carry what IS allowed, or the picker cannot recover.
    assert [c["id"] for c in wire["choices"]] == [m.id for m in _MODELS]


def test_no_model_means_the_engine_default_and_may_substitute():
    assert CodexModelCatalog.validate(_MODELS, None, None) == (
        "gpt-5.1-codex",
        "medium",
    )


def test_an_explicit_model_without_an_effort_takes_that_model_s_default():
    assert CodexModelCatalog.validate(_MODELS, "gpt-5.1-codex-mini", None) == (
        "gpt-5.1-codex-mini",
        "low",
    )


# ---------------------------------------------------------------------------
# The orchestrator's refresh-once-on-drift behaviour
# ---------------------------------------------------------------------------


class _FakeCodex:
    def __init__(self, pages: list[tuple[ModelOption, ...]]) -> None:
        self._pages = pages
        self.available = True
        self.availability = type(
            "A", (), {"cli_version": "codex-cli 0.153.2"}
        )()
        self.ensured: list[str] = []

    async def ensure(self, account: str) -> None:
        self.ensured.append(account)

    def client_for(self, account: str):
        return object()


class _FakeCatalog:
    """Serves a stale catalog until `refresh=True` forces the fresh one."""

    def __init__(self, stale, fresh) -> None:
        self._stale = stale
        self._fresh = fresh
        self.calls: list[bool] = []

    async def models(self, account, *, client, cli_version, refresh=False):
        self.calls.append(refresh)
        return self._fresh if refresh else self._stale

    validate = staticmethod(CodexModelCatalog.validate)


def _orchestrator(codex, catalog):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    orch._codex = codex
    orch._codex_model_catalog = catalog
    return orch


def test_drift_is_retried_once_with_a_forced_refresh():
    """A model missing from the cache but present upstream must not be rejected."""
    fresh = _MODELS
    stale = (_MODELS[0],)   # the mini model has not been seen yet
    catalog = _FakeCatalog(stale, fresh)
    orch = _orchestrator(_FakeCodex([]), catalog)

    verdict = asyncio.run(
        orch.codex_validate_model("chatgpt-pro", "gpt-5.1-codex-mini", "low")
    )
    assert verdict == {"ok": True, "model": "gpt-5.1-codex-mini", "effort": "low"}
    assert catalog.calls == [False, True]


def test_a_genuinely_wrong_model_refreshes_once_then_reports_the_mismatch():
    catalog = _FakeCatalog(_MODELS, _MODELS)
    orch = _orchestrator(_FakeCodex([]), catalog)

    verdict = asyncio.run(
        orch.codex_validate_model("chatgpt-pro", "gpt-4-turbo", "high")
    )
    assert verdict["ok"] is False
    assert verdict["error"]["model"] == "gpt-4-turbo"
    # Exactly one retry. Not zero (drift is real), not a loop.
    assert catalog.calls == [False, True]


def test_validation_is_skipped_entirely_when_codex_is_not_available_here():
    orch = _orchestrator(None, _FakeCatalog(_MODELS, _MODELS))
    verdict = asyncio.run(
        orch.codex_validate_model("chatgpt-pro", "anything", "any")
    )
    assert verdict == {"ok": True, "model": "anything", "effort": "any"}


def test_a_catalog_failure_allows_the_pair_rather_than_blocking_the_spawn():
    """We could not enumerate the models, so we cannot say the pair is wrong."""
    from server.codex.catalog import ModelCatalogError

    class _Broken(_FakeCatalog):
        async def models(self, account, *, client, cli_version, refresh=False):
            raise ModelCatalogError("app-server died")

    orch = _orchestrator(_FakeCodex([]), _Broken(_MODELS, _MODELS))
    verdict = asyncio.run(
        orch.codex_validate_model("chatgpt-pro", "gpt-5.1-codex", "high")
    )
    assert verdict["ok"] is True


# ---------------------------------------------------------------------------
# The API boundary
# ---------------------------------------------------------------------------


class _FakeSessions:
    def __init__(self, verdict: dict) -> None:
        self.codex_validate_model = AsyncMock(return_value=verdict)
        self.create_session = AsyncMock()
        self.usage = AsyncMock(return_value=None)

    def daemon_epoch(self):
        return None


def _client(verdict: dict) -> tuple[TestClient, _FakeSessions]:
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(verdict)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_a_mismatch_is_a_409_carrying_the_current_choices():
    mismatch = {
        "error": "model_effort_mismatch",
        "model": "gpt-4-turbo",
        "effort": "high",
        "choices": [{"id": "gpt-5.1-codex", "label": "GPT-5.1 Codex"}],
    }
    with temp_env():
        client, fake = _client({"ok": False, "error": mismatch})
        with client:
            resp = client.post(
                "/api/sessions",
                json={
                    "name": "s",
                    "engine": "codex",
                    "account": "chatgpt-pro",
                    "model": "gpt-4-turbo",
                    "reasoningEffort": "high",
                },
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 409
    assert resp.json()["error"]["choices"][0]["id"] == "gpt-5.1-codex"
    fake.create_session.assert_not_awaited()


def test_no_account_means_nothing_to_validate_against():
    """The catalog is per account; validating against another's would be worse."""
    with temp_env():
        client, fake = _client({"ok": True})
        with client:
            client.post(
                "/api/sessions",
                json={"name": "s", "engine": "codex", "model": "gpt-5.1-codex"},
                headers=IDENTITY_HEADERS,
            )
    fake.codex_validate_model.assert_not_awaited()


def test_a_claude_session_is_never_routed_through_the_codex_validator():
    with temp_env():
        client, fake = _client({"ok": True})
        with client:
            client.post(
                "/api/sessions",
                json={"name": "s", "account": "work", "model": "opus"},
                headers=IDENTITY_HEADERS,
            )
    fake.codex_validate_model.assert_not_awaited()
