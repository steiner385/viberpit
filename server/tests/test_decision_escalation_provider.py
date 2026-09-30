"""The provider's triggers are one tap BY A PERSON (spec decision 7): the
existing decision/hold services with their actor gate intact."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.automations.decision_escalation import DecisionEscalationActionProvider


def _provider(held):
    loop = SimpleNamespace(_held=held)
    return DecisionEscalationActionProvider(SimpleNamespace(_decision_escalation_loop=loop))


def test_invalid_kind_and_unknown_item():
    p = _provider([])
    assert p.valid_kinds == frozenset({"accept_recommended", "retry"})
    r = asyncio.run(p.trigger("o/k#404", "retry", identity="tony@example.com"))
    assert r.status == "not_found" and r.level == "warn"


def test_accept_without_a_person_is_refused():
    from server.tests.test_decision_escalation_policy import _item
    p = _provider([_item("o/k#1")])
    r = asyncio.run(p.trigger("o/k#1", "accept_recommended", identity=None))
    assert r.status == "forbidden" and "person" in r.message.lower()


def test_a_changed_proposal_is_refused_not_accepted(monkeypatch):
    """The card shows the last tick's proposal; a newer pending one (or a changed
    recommendation) must not be accepted on the strength of that card."""
    from server.automations import decision_escalation as de
    from server.tests.test_decision_escalation_policy import _item
    from server.pm import decisions, holds
    item = _item("o/k#1")  # proposal_id "dp-1", recommended "b"
    p = _provider([item])

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def _wi(db, ref):
        return SimpleNamespace(id="wi-1")

    async def _pending(db, wid):
        return SimpleNamespace(id="dp-2", recommended_key="b")  # a newer proposal
    accepted = []

    async def _accept(*a, **k):
        accepted.append(1)
    monkeypatch.setattr("server.db.db_session", lambda: _DB())
    monkeypatch.setattr(holds, "work_item_for_ref", _wi)
    monkeypatch.setattr(de, "_pending_proposal", _pending)
    monkeypatch.setattr(decisions, "accept", _accept)
    monkeypatch.setattr(decisions, "may_resolve", lambda actor: True)
    r = asyncio.run(p.trigger("o/k#1", "accept_recommended", identity="tony@example.com"))
    assert r.status == "refused" and "changed" in r.message
    assert not accepted and p._loop._held == [item]
