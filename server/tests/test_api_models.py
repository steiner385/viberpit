from server.sessions import model_catalog as mc
from server.api.models import router


def test_router_is_mounted_at_api_models():
    assert router.prefix == "/api/models"


def test_route_returns_catalog():
    # The handler is a plain function returning the catalog; call it directly.
    import asyncio
    from server.api.models import get_models
    result = asyncio.run(get_models())
    assert result == mc.catalog()
