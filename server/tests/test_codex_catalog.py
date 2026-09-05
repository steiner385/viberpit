"""Codex model catalog: pagination, caching, error typing, and pair validation.

Every payload is a wire dict parsed by the real `schema.parse_model_list_response`
before it reaches the catalog, so a drift in the pinned `model/list` contract
fails here instead of passing a hand-rolled DTO straight through.
"""
from __future__ import annotations

from typing import Any

import pytest

from server.codex import protocol, schema
from server.codex.catalog import (
    DEFAULT_MODEL_SENTINEL,
    MAX_MODEL_PAGES,
    CodexModelCatalog,
    ModelCatalogError,
    ModelEffortMismatch,
    ModelOption,
)
from server.codex.redaction import REDACTED


# ---------------------------------------------------------------------------
# Wire payloads and fakes
# ---------------------------------------------------------------------------


def _model_wire(
    model_id: str,
    *,
    display_name: str | None = None,
    hidden: bool = False,
    is_default: bool = False,
    efforts: tuple[str, ...] = ("low", "medium", "high"),
    default_effort: str = "medium",
) -> dict[str, Any]:
    return {
        "id": model_id,
        "model": model_id,
        "displayName": display_name or model_id.upper(),
        "description": f"{model_id} for coding",
        "hidden": hidden,
        "isDefault": is_default,
        "defaultReasoningEffort": default_effort,
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": f"{effort} reasoning"}
            for effort in efforts
        ],
        "inputModalities": ["text", "image"],
    }


def _page(
    models: list[dict[str, Any]], *, cursor: str | None = None
) -> schema.ModelListResponse:
    payload: dict[str, Any] = {"data": models}
    if cursor is not None:
        payload["nextCursor"] = cursor
    return schema.parse_model_list_response(payload)


class FakeModelClient:
    """Serves a fixed list of pages and records the cursors it was asked for."""

    def __init__(self, pages: list[schema.ModelListResponse]) -> None:
        self.pages = pages
        self.cursors: list[str | None] = []

    async def list_models(
        self, params: schema.ModelListParams | None = None
    ) -> schema.ModelListResponse:
        self.cursors.append(None if params is None else params.cursor)
        return self.pages[min(len(self.cursors) - 1, len(self.pages) - 1)]

    @property
    def calls(self) -> int:
        return len(self.cursors)


class EndlessPager:
    """A peer that always hands back another cursor."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_models(
        self, params: schema.ModelListParams | None = None
    ) -> schema.ModelListResponse:
        self.calls += 1
        return _page([_model_wire(f"model-{self.calls}")], cursor=f"c{self.calls}")


class FailingClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def list_models(
        self, params: schema.ModelListParams | None = None
    ) -> schema.ModelListResponse:
        self.calls += 1
        raise self.error


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _one_page_client(*models: dict[str, Any]) -> FakeModelClient:
    return FakeModelClient([_page(list(models))])


def _options(*specs: tuple[str, tuple[str, ...], str | None, bool]) -> list[ModelOption]:
    return [
        ModelOption(
            id=model_id,
            label=model_id.upper(),
            reasoning_efforts=efforts,
            default_reasoning_effort=default_effort,
            default=is_default,
        )
        for model_id, efforts, default_effort, is_default in specs
    ]


# ---------------------------------------------------------------------------
# Reading model/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_follows_the_cursor_to_completion() -> None:
    client = FakeModelClient(
        [
            _page([_model_wire("a")], cursor="c1"),
            _page([_model_wire("b")], cursor="c2"),
            _page([_model_wire("c")]),
        ]
    )
    catalog = CodexModelCatalog()

    options = await catalog.models("acct", client=client, cli_version="0.153.2")

    assert [option.id for option in options] == ["a", "b", "c"]
    assert client.cursors == [None, "c1", "c2"]


@pytest.mark.asyncio
async def test_pagination_stops_at_the_page_cap() -> None:
    client = EndlessPager()
    catalog = CodexModelCatalog()

    options = await catalog.models("acct", client=client, cli_version="0.153.2")

    assert client.calls == MAX_MODEL_PAGES
    assert len(options) == MAX_MODEL_PAGES


@pytest.mark.asyncio
async def test_hidden_models_are_filtered_out() -> None:
    client = _one_page_client(
        _model_wire("visible"), _model_wire("internal", hidden=True)
    )
    catalog = CodexModelCatalog()

    options = await catalog.models("acct", client=client, cli_version="0.153.2")

    assert [option.id for option in options] == ["visible"]


@pytest.mark.asyncio
async def test_option_carries_display_name_default_and_efforts() -> None:
    client = _one_page_client(
        _model_wire(
            "gpt-5-codex",
            display_name="GPT-5 Codex",
            is_default=True,
            efforts=("low", "high"),
            default_effort="high",
        )
    )
    catalog = CodexModelCatalog()

    (option,) = await catalog.models("acct", client=client, cli_version="0.153.2")

    assert option.id == "gpt-5-codex"
    assert option.label == "GPT-5 Codex"
    assert option.default is True
    assert option.reasoning_efforts == ("low", "high")
    assert option.default_reasoning_effort == "high"
    assert option.input_modalities == ("text", "image")


def test_wire_shape_is_a_superset_of_the_claude_picker() -> None:
    """One `/api/models` response shape serves both engines.

    The Claude catalog (`server/sessions/model_catalog.catalog`) emits
    `{"id", "label"}`; a Codex option must carry those two keys unchanged so the
    browser needs no per-engine branch to render the list.
    """
    wire = ModelOption(id="gpt-5-codex", label="GPT-5 Codex").to_wire()

    assert {"id", "label"} <= set(wire)
    assert wire == {
        "id": "gpt-5-codex",
        "label": "GPT-5 Codex",
        "description": "",
        "default": False,
        "reasoningEfforts": [],
        "defaultReasoningEffort": None,
        "inputModalities": [],
    }


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_call_within_the_ttl_is_served_from_cache() -> None:
    client = _one_page_client(_model_wire("a"))
    clock = FakeClock()
    catalog = CodexModelCatalog(cache_ttl_s=300.0, clock=clock)

    await catalog.models("acct", client=client, cli_version="0.153.2")
    clock.now += 299.0
    await catalog.models("acct", client=client, cli_version="0.153.2")

    assert client.calls == 1


@pytest.mark.asyncio
async def test_cache_expires_after_the_ttl() -> None:
    client = _one_page_client(_model_wire("a"))
    clock = FakeClock()
    catalog = CodexModelCatalog(cache_ttl_s=300.0, clock=clock)

    await catalog.models("acct", client=client, cli_version="0.153.2")
    clock.now += 301.0
    await catalog.models("acct", client=client, cli_version="0.153.2")

    assert client.calls == 2


@pytest.mark.asyncio
async def test_cache_key_separates_account_cli_version_and_config() -> None:
    client = _one_page_client(_model_wire("a"))
    catalog = CodexModelCatalog()

    await catalog.models("acct", client=client, cli_version="0.153.2")
    await catalog.models("other", client=client, cli_version="0.153.2")
    await catalog.models("acct", client=client, cli_version="0.154.0")
    await catalog.models(
        "acct", client=client, cli_version="0.153.2", config_key="profile=work"
    )

    assert client.calls == 4


@pytest.mark.asyncio
async def test_refresh_bypasses_the_cache() -> None:
    client = _one_page_client(_model_wire("a"))
    catalog = CodexModelCatalog()

    await catalog.models("acct", client=client, cli_version="0.153.2")
    await catalog.models("acct", client=client, cli_version="0.153.2", refresh=True)

    assert client.calls == 2


@pytest.mark.asyncio
async def test_invalidate_drops_one_account_then_all_of_them() -> None:
    client = _one_page_client(_model_wire("a"))
    catalog = CodexModelCatalog()

    await catalog.models("acct", client=client, cli_version="0.153.2")
    await catalog.models("other", client=client, cli_version="0.153.2")
    catalog.invalidate("acct")
    await catalog.models("acct", client=client, cli_version="0.153.2")
    await catalog.models("other", client=client, cli_version="0.153.2")
    assert client.calls == 3

    catalog.invalidate()
    await catalog.models("other", client=client, cli_version="0.153.2")
    assert client.calls == 4


@pytest.mark.asyncio
async def test_an_empty_catalog_is_not_cached() -> None:
    """An empty list is almost always a transient failure; caching it would leave
    the picker empty for the whole TTL."""
    client = FakeModelClient([_page([]), _page([_model_wire("a")])])
    catalog = CodexModelCatalog()

    first = await catalog.models("acct", client=client, cli_version="0.153.2")
    second = await catalog.models("acct", client=client, cli_version="0.153.2")

    assert first == ()
    assert [option.id for option in second] == ["a"]
    assert client.calls == 2


# ---------------------------------------------------------------------------
# Typed unavailability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_failure_becomes_a_scrubbed_catalog_error() -> None:
    client = FailingClient(
        protocol.CodexRequestFailed(
            -32603, "upstream rejected sk-abcdefghijklmnop", method="model/list"
        )
    )
    catalog = CodexModelCatalog()

    with pytest.raises(ModelCatalogError) as excinfo:
        await catalog.models("acct", client=client, cli_version="0.153.2")

    assert "sk-abcdefghijklmnop" not in str(excinfo.value)
    assert REDACTED in str(excinfo.value)


@pytest.mark.parametrize(
    "error",
    [
        protocol.CodexRequestTimeout("model/list exceeded its deadline"),
        protocol.CodexTransportClosed("app-server stream ended"),
        schema.CodexSchemaError("model/list model field 'id' is required"),
    ],
)
@pytest.mark.asyncio
async def test_every_protocol_failure_becomes_a_catalog_error(
    error: BaseException,
) -> None:
    catalog = CodexModelCatalog()

    with pytest.raises(ModelCatalogError):
        await catalog.models(
            "acct", client=FailingClient(error), cli_version="0.153.2"
        )


@pytest.mark.asyncio
async def test_a_failed_read_is_not_cached_as_an_answer() -> None:
    client = FailingClient(protocol.CodexTransportClosed("stream ended"))
    catalog = CodexModelCatalog()

    for _ in range(2):
        with pytest.raises(ModelCatalogError):
            await catalog.models("acct", client=client, cli_version="0.153.2")

    assert client.calls == 2


@pytest.mark.asyncio
async def test_missing_client_is_a_typed_error_not_an_empty_list() -> None:
    catalog = CodexModelCatalog()

    with pytest.raises(ModelCatalogError):
        await catalog.models("acct", client=None, cli_version="0.153.2")


# ---------------------------------------------------------------------------
# validate(): the pair is one selection
# ---------------------------------------------------------------------------


CHOICES = _options(
    ("gpt-5-codex", ("low", "medium", "high"), "medium", True),
    ("gpt-5", ("minimal", "low"), "low", False),
)


@pytest.mark.parametrize("requested", [None, "", DEFAULT_MODEL_SENTINEL, "Default"])
def test_default_request_picks_the_default_option(requested: str | None) -> None:
    assert CodexModelCatalog.validate(CHOICES, requested, None) == (
        "gpt-5-codex",
        "medium",
    )


def test_default_request_falls_back_to_the_first_option() -> None:
    choices = _options(("gpt-5", ("low",), "low", False))

    assert CodexModelCatalog.validate(choices, None, None) == ("gpt-5", "low")


def test_default_request_keeps_a_supported_explicit_effort() -> None:
    assert CodexModelCatalog.validate(CHOICES, None, "high") == ("gpt-5-codex", "high")


def test_default_request_substitutes_an_unsupported_effort() -> None:
    """The only silent substitution: the caller named no pair to honour."""
    assert CodexModelCatalog.validate(CHOICES, "default", "xtreme") == (
        "gpt-5-codex",
        "medium",
    )


def test_explicit_model_without_an_effort_uses_its_own_default() -> None:
    assert CodexModelCatalog.validate(CHOICES, "gpt-5", None) == ("gpt-5", "low")


def test_explicit_pair_is_returned_unchanged() -> None:
    assert CodexModelCatalog.validate(CHOICES, "gpt-5", "minimal") == ("gpt-5", "minimal")


def test_valid_model_with_an_effort_it_does_not_accept_raises() -> None:
    """The pair is validated as one unit — `high` is valid, but not for `gpt-5`."""
    with pytest.raises(ModelEffortMismatch) as excinfo:
        CodexModelCatalog.validate(CHOICES, "gpt-5", "high")

    assert excinfo.value.model == "gpt-5"
    assert excinfo.value.effort == "high"
    assert excinfo.value.choices == tuple(CHOICES)


def test_unknown_model_raises() -> None:
    with pytest.raises(ModelEffortMismatch):
        CodexModelCatalog.validate(CHOICES, "gpt-4o", "low")


def test_empty_catalog_raises_instead_of_passing_the_request_through() -> None:
    with pytest.raises(ModelEffortMismatch) as excinfo:
        CodexModelCatalog.validate([], "gpt-5-codex", "low")

    assert excinfo.value.choices == ()


def test_an_option_advertising_no_efforts_accepts_any_effort() -> None:
    """An empty effort list is "nothing to check against", not a rejection."""
    choices = _options(("gpt-5-codex", (), None, True))

    assert CodexModelCatalog.validate(choices, "gpt-5-codex", "high") == (
        "gpt-5-codex",
        "high",
    )


def test_mismatch_wire_carries_the_current_choices() -> None:
    error = ModelEffortMismatch("gpt-5", "high", CHOICES)

    wire = error.to_wire()

    assert wire["error"] == "model_effort_mismatch"
    assert wire["model"] == "gpt-5"
    assert wire["effort"] == "high"
    assert [choice["id"] for choice in wire["choices"]] == ["gpt-5-codex", "gpt-5"]
    assert wire["choices"][0]["reasoningEfforts"] == ["low", "medium", "high"]
