"""Direction rule composed with the tier denylist. Strictest outcome wins."""
import pytest

from server.sessions import model_policy


@pytest.fixture
def allow_all(monkeypatch):
    """Force the tier policy to 'allow' so direction is the only variable."""
    async def _allow(db, *, model=None, account=None):
        return "allow"
    monkeypatch.setattr(model_policy, "resolve_model_policy", _allow)


async def _decide(current, requested):
    return await model_policy.resolve_self_directed_model_policy(
        None, current_model=current, requested_model=requested)


@pytest.mark.asyncio
async def test_downgrade_auto_applies(allow_all):
    assert await _decide("opus", "haiku") == "apply"


@pytest.mark.asyncio
async def test_upgrade_parks_for_approval(allow_all):
    assert await _decide("sonnet", "fable") == "park"


@pytest.mark.asyncio
async def test_same_tier_is_a_no_op(allow_all):
    assert await _decide("opus", "opusplan") == "noop"


@pytest.mark.asyncio
@pytest.mark.parametrize("current,requested", [
    ("default", "haiku"), ("best", "haiku"), ("opus", "best"), (None, "sonnet"),
])
async def test_indeterminate_direction_parks(allow_all, current, requested):
    """Either side untiered means no honest comparison — never guess a direction."""
    assert await _decide(current, requested) == "park"


@pytest.mark.asyncio
async def test_blocked_tier_is_refused_even_as_a_downgrade(monkeypatch):
    async def _block(db, *, model=None, account=None):
        return "block"
    monkeypatch.setattr(model_policy, "resolve_model_policy", _block)
    assert await _decide("fable", "haiku") == "refuse"


@pytest.mark.asyncio
async def test_gated_tier_parks_even_as_a_downgrade(monkeypatch):
    async def _gated(db, *, model=None, account=None):
        return "require_approval"
    monkeypatch.setattr(model_policy, "resolve_model_policy", _gated)
    assert await _decide("fable", "opus") == "park"
