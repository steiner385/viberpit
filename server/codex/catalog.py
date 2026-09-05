"""The Codex model picker: `model/list` turned into selectable options.

Three decisions are worth the words:

* **One picker wire shape for both engines.** `GET /api/models` already answers
  `[{"id", "label"}, ...]` for Claude (`server/sessions/model_catalog.py`, which
  reads the `claude` binary's alias list). `ModelOption.to_wire()` is a strict
  superset of that shape, so one endpoint serves both engines and the browser
  renders a Codex option with the same code path — the extra keys (efforts,
  modalities, default) are additive, never a second model contract.
* **A model and its reasoning effort are ONE selection.** The app-server rejects
  an unsupported pair when the turn starts, which the user sees as a failed turn
  with no way to correct it. `validate()` checks the pair against the live
  catalog up front and raises `ModelEffortMismatch`, which carries the current
  choices so the caller can answer a typed 409 the picker can repair itself
  from. Substituting silently is allowed in exactly one case — the caller asked
  for the engine default — because that request names no pair to honour.
* **A catalog read is a process RPC, so it is cached and bounded.** The cache key
  is `(account, cli_version, config_key)`: a CLI upgrade or a config change can
  add, hide, or re-default models, and serving the previous binary's list would
  offer a model the running process rejects. An EMPTY list is never cached — an
  empty catalog is almost always a transient app-server failure, and caching it
  would leave the picker permanently empty for `cache_ttl_s`. Pagination follows
  `nextCursor` to completion but stops at `MAX_MODEL_PAGES`, so a peer that keeps
  handing back a cursor cannot pin an HTTP request open.

Nothing here reads the environment or a clock directly: the client, the CLI
version, and the clock are injected, which is what lets the tests drive the real
cache logic without a process.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from server.codex import redaction, schema
from server.codex.protocol import CodexProtocolError
from server.obs.context import log_event

logger = logging.getLogger(__name__)

#: Upper bound on `model/list` pages followed for one catalog read.
MAX_MODEL_PAGES: Final = 20

#: The request value that means "whatever the engine picks", mirroring the
#: `default` row the Claude picker shows. It is the only input `validate()` is
#: allowed to substitute for.
DEFAULT_MODEL_SENTINEL: Final = "default"


class ModelLister(Protocol):
    """The one app-server method this module needs (`CodexAppServerClient`)."""

    async def list_models(
        self, params: schema.ModelListParams | None = None
    ) -> schema.ModelListResponse: ...


@dataclass(frozen=True, slots=True)
class ModelOption:
    """One pickable model, with the reasoning efforts it accepts."""

    id: str
    label: str
    description: str = ""
    default: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str | None = None
    input_modalities: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "default": self.default,
            "reasoningEfforts": list(self.reasoning_efforts),
            "defaultReasoningEffort": self.default_reasoning_effort,
            "inputModalities": list(self.input_modalities),
        }


class ModelCatalogError(RuntimeError):
    """The catalog could not be read; the detail is already scrubbed.

    Unavailability is typed rather than an empty list, because an empty list is
    also a legal answer ("this account has no models") and a caller that cannot
    tell them apart will cache a failure as a fact.
    """


class ModelEffortMismatch(RuntimeError):
    """A requested (model, effort) pair is not offered by the live catalog."""

    def __init__(
        self,
        model: str | None,
        effort: str | None,
        choices: Sequence[ModelOption],
    ) -> None:
        self.model = model
        self.effort = effort
        self.choices = tuple(choices)
        # The requested values are caller-supplied strings and this message
        # reaches logs, so scrub before formatting.
        super().__init__(
            redaction.scrub_text(
                f"model {model!r} with reasoning effort {effort!r} "
                "is not offered by the codex model catalog"
            )
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "error": "model_effort_mismatch",
            "model": self.model,
            "effort": self.effort,
            "choices": [option.to_wire() for option in self.choices],
        }


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    stored_at: float
    options: tuple[ModelOption, ...]


def _option_from_model(model: schema.Model) -> ModelOption:
    """Map one pinned `Model` DTO onto a picker option.

    `Model.id` is the option identity, not `Model.model`: the catalog entry is
    what the picker round-trips and what `validate()` resolves a selection back
    to. Surfacing both would put two ids on the wire for one model, and a
    selection that can arrive under either id cannot be validated as one pair.
    """
    return ModelOption(
        id=model.id,
        label=model.display_name or model.id,
        description=model.description,
        default=model.is_default,
        reasoning_efforts=tuple(
            option.reasoning_effort for option in model.supported_reasoning_efforts
        ),
        default_reasoning_effort=model.default_reasoning_effort or None,
        input_modalities=model.input_modalities,
    )


def _effort_supported(option: ModelOption, effort: str) -> bool:
    """Whether *option* accepts *effort*.

    An option that advertises NO efforts is not a rejection: the app-server
    simply told us nothing to check against, so the effort passes through and the
    engine remains the authority. Only a non-empty list is treated as exhaustive.
    """
    return not option.reasoning_efforts or effort in option.reasoning_efforts


class CodexModelCatalog:
    """A short-lived, per-account cache over `model/list`."""

    def __init__(
        self,
        *,
        cache_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._entries: dict[tuple[str, str, str], _CatalogEntry] = {}

    async def models(
        self,
        account: str,
        *,
        client: ModelLister | None,
        cli_version: str,
        config_key: str = "",
        refresh: bool = False,
    ) -> tuple[ModelOption, ...]:
        """Every visible model for *account*, newest read or a cached one.

        Raises `ModelCatalogError` when the app-server cannot answer.
        """
        key = (account, cli_version, config_key)
        if not refresh:
            entry = self._entries.get(key)
            if entry is not None and self._clock() - entry.stored_at < self._cache_ttl_s:
                return entry.options
        options = await self._fetch(account, client)
        if options:
            self._entries[key] = _CatalogEntry(self._clock(), options)
        return options

    def invalidate(self, account: str | None = None) -> None:
        """Drop cached options for one account, or for every account."""
        if account is None:
            self._entries.clear()
            return
        for key in [key for key in self._entries if key[0] == account]:
            del self._entries[key]

    @staticmethod
    def validate(
        models: Sequence[ModelOption],
        model: str | None,
        effort: str | None,
    ) -> tuple[str, str | None]:
        """Resolve a requested pair to the `(model, effort)` to dispatch with.

        The pair is validated as ONE unit: a model that exists but does not
        accept the requested effort raises, because dispatching it would start a
        turn the app-server refuses. An empty catalog raises too — passing an
        unvalidated model through "because we could not check" is how a picker
        selection becomes a failed turn.
        """
        choices = tuple(models)
        if not choices:
            raise ModelEffortMismatch(model, effort, ())
        requested = (model or "").strip()
        if not requested or requested.lower() == DEFAULT_MODEL_SENTINEL:
            # The only silent substitution: the request named no pair to honour.
            option = next((item for item in choices if item.default), choices[0])
            if effort is not None and _effort_supported(option, effort):
                return option.id, effort
            return option.id, option.default_reasoning_effort
        chosen = next((item for item in choices if item.id == requested), None)
        if chosen is None:
            raise ModelEffortMismatch(model, effort, choices)
        if effort is None:
            return chosen.id, chosen.default_reasoning_effort
        if not _effort_supported(chosen, effort):
            raise ModelEffortMismatch(model, effort, choices)
        return chosen.id, effort

    async def _fetch(
        self, account: str, client: ModelLister | None
    ) -> tuple[ModelOption, ...]:
        if client is None:
            raise ModelCatalogError(
                "no codex app-server connection is available for this account"
            )
        collected: list[ModelOption] = []
        cursor: str | None = None
        try:
            for _page in range(MAX_MODEL_PAGES):
                response = await client.list_models(
                    schema.ModelListParams(cursor=cursor)
                )
                collected.extend(
                    _option_from_model(model)
                    for model in response.data
                    if not model.hidden
                )
                cursor = response.next_cursor
                if not cursor:
                    break
            else:
                # Cursor still set at the cap: return what we have rather than
                # keep an HTTP request open behind an endless pager.
                log_event(
                    logger,
                    logging.WARNING,
                    "codex.catalog.page_cap_reached",
                    account=account,
                    pages=MAX_MODEL_PAGES,
                )
        except (CodexProtocolError, schema.CodexSchemaError) as exc:
            raise ModelCatalogError(redaction.scrub_exception(exc)) from exc
        return tuple(collected)
