import asyncio

import pytest

from server.sessions import model_catalog as mc
from server.api.models import get_models, router


@pytest.fixture(autouse=True)
def reset_cache():
    """Isolate each test from module-level _cache state."""
    original = mc._cache
    yield
    mc._cache = original


def test_router_is_mounted_at_api_models():
    assert router.prefix == "/api/models"


def test_route_returns_catalog(monkeypatch):
    monkeypatch.setattr(mc, "_cache", mc.ALIASES[:])
    result = asyncio.run(get_models())
    assert result == mc.catalog()


def test_route_without_refresh_does_not_clear_cache(monkeypatch):
    sentinel = ["opus", "sonnet"]
    monkeypatch.setattr(mc, "_cache", sentinel)
    asyncio.run(get_models(refresh=False))
    assert mc._cache is sentinel  # unchanged


def test_refresh_param_clears_cache_before_returning(monkeypatch):
    monkeypatch.setattr(mc, "_cache", ["opus"])
    # After refresh=True the cache is cleared and re-probed; just verify it
    # no longer holds the stale sentinel.  (The re-probe may load ALIASES or
    # the live binary depending on the environment — both are correct.)
    asyncio.run(get_models(refresh=True))
    # _cache was reset to None and then re-populated by catalog(); it should
    # not still be the stale ["opus"] sentinel.
    assert mc._cache != ["opus"]
