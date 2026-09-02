"""Untiered aliases (`best`, `default`, `opusplan`) must be gateable by name.

`model_tier()` returns None for aliases with no fixed tier, and the policy used to
return "allow" the moment the tier was None — so `best` bypassed the approval gate
entirely, no matter what the denylist said. `best` can resolve to the most expensive
model available, which is precisely what an operator would want to gate.

A denylist entry now matches EITHER the model's tier OR its bare alias, so
`CCSWITCH_MODEL_APPROVAL_DENYLIST=fable,best` gates both.
"""
import pytest

from server.sessions import model_policy


class _Row:
    def __init__(self, denylist=None, action=None):
        self.model_approval_denylist = denylist
        self.model_approval_action = action


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Db:
    """Minimal stand-in for the async session: one AppSettings row."""

    def __init__(self, row):
        self._row = row
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        return _Result(self._row)


async def _policy(model, denylist, action="require_approval"):
    return await model_policy.resolve_model_policy(
        _Db(_Row(denylist, action)), model=model)


@pytest.mark.asyncio
async def test_best_is_gateable_by_name():
    """The bug: `best` bypassed the gate because its tier is None."""
    assert await _policy("best", "fable,best") == "require_approval"


@pytest.mark.asyncio
async def test_default_is_gateable_by_name():
    assert await _policy("default", "default") == "require_approval"


@pytest.mark.asyncio
async def test_best_still_allowed_when_not_on_the_denylist():
    """No behaviour change for existing installs whose denylist is just `fable`."""
    assert await _policy("best", "fable") == "allow"


@pytest.mark.asyncio
async def test_untiered_alias_respects_the_block_action():
    assert await _policy("best", "best", action="block") == "block"


@pytest.mark.asyncio
async def test_bracket_suffix_is_stripped_before_matching():
    """`best[1m]` is the same alias as `best`."""
    assert await _policy("best[1m]", "best") == "require_approval"


@pytest.mark.asyncio
async def test_alias_matching_is_case_insensitive():
    assert await _policy("BEST", "best") == "require_approval"


@pytest.mark.asyncio
async def test_tier_matching_still_works():
    """Regression: the pre-existing tier path is untouched."""
    assert await _policy("fable", "fable") == "require_approval"
    assert await _policy("claude-fable-5-1", "fable") == "require_approval"
    assert await _policy("fable[1m]", "fable") == "require_approval"
    assert await _policy("opus", "fable") == "allow"


@pytest.mark.asyncio
async def test_opusplan_matches_both_its_tier_and_its_alias():
    """`opusplan` maps to tier `opus`, so either entry gates it."""
    assert await _policy("opusplan", "opus") == "require_approval"
    assert await _policy("opusplan", "opusplan") == "require_approval"


@pytest.mark.asyncio
async def test_no_model_is_always_allowed_without_touching_the_db():
    """model=None means "account default" — the hot path must stay a fast allow."""
    db = _Db(_Row("best,fable"))
    assert await model_policy.resolve_model_policy(db, model=None) == "allow"
    assert db.executed == 0, "model=None must not read AppSettings"


@pytest.mark.asyncio
async def test_empty_model_string_is_allowed_without_touching_the_db():
    db = _Db(_Row("best,fable"))
    assert await model_policy.resolve_model_policy(db, model="") == "allow"
    assert db.executed == 0
