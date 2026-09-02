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


@pytest.mark.asyncio
async def test_tier_gate_checks_requested_model_not_current(monkeypatch):
    """Verify the tier policy gate inspects the REQUESTED model, not the current one.

    If the call were swapped to current_model, a session on a blocked tier could
    request an allowed one and bypass the block, or vice versa.
    """
    captured_model = None

    async def _capture_model(db, *, model=None, account=None):
        nonlocal captured_model
        captured_model = model
        return "allow"

    monkeypatch.setattr(model_policy, "resolve_model_policy", _capture_model)
    await _decide("haiku", "fable")
    assert captured_model == "fable", f"Expected 'fable' but gate was called with {captured_model}"


@pytest.mark.asyncio
async def test_same_tier_noop_at_rank_zero(allow_all):
    """Verify same-tier noop works at rank 0 (falsy) to catch any future `if not rank` bugs."""
    assert await _decide("haiku", "haiku") == "noop"


@pytest.mark.asyncio
async def test_downgrade_at_rank_zero(allow_all):
    """Verify downgrade works at rank 0 (falsy) to catch any future `if not rank` bugs."""
    assert await _decide("sonnet", "haiku") == "apply"
