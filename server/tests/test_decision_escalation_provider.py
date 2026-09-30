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
