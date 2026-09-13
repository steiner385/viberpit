"""Work class: which automation-spawned sessions are routine maintenance.

The sessions list splits live rows into "new work" (foreground) and
"maintenance" (background). The split is declared on each automation class as
``work_class`` and read through ONE accessor, so the automations payload, the
session serializer and the browser cannot drift on what counts as maintenance.

The safe failure is the opposite of ``engine_policy``'s: an origin that forgot
to declare, an ad-hoc recurring id, and a human session all resolve to
``development`` — an oversight makes a session *visible*, never hides it.
"""
from __future__ import annotations

import pytest

from server.automations.base import WORK_CLASSES, work_class_of
from server.automations.registry import (
    assisted_by_label,
    origin_label,
    registry,
    work_class_for_origin,
)
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def test_every_registered_automation_declares_its_work_class():
    """The reviewable record: moving an automation between classes is a
    one-line diff here. Backlog and SDLC sessions produce new work and stay in
    the foreground; the rest are housekeeping."""
    assert {a.id: work_class_of(a) for a in registry.all()} == {
        "backlog-runner": "development",
        "sdlc-intake": "development",
        "sdlc-synthesizer": "development",
        "pr-doctor": "maintenance",
        "pm-onboard": "maintenance",
        "pm-classify": "maintenance",
        "control-band-monitor": "maintenance",
        "autopilot": "maintenance",
        "cicd-doctor": "maintenance",
        "archive-audit": "maintenance",
        "pm-sync": "maintenance",
        "pm-triage": "maintenance",
        "gh-reconcile": "maintenance",
        "error-triage": "maintenance",
        "deploy-monitor": "maintenance",
    }


def test_declared_values_are_members_of_the_closed_set():
    assert set(WORK_CLASSES) == {"development", "maintenance"}
    for a in registry.all():
        assert a.work_class in WORK_CLASSES


class _Undeclared:
    id = "mystery"
    name = "Mystery"


class _Misdeclared:
    id = "typo"
    name = "Typo"
    work_class = "maintenence"


def test_work_class_of_defaults_to_development_when_undeclared():
    assert work_class_of(_Undeclared()) == "development"


def test_work_class_of_rejects_an_unknown_value_loudly():
    with pytest.raises(ValueError):
        work_class_of(_Misdeclared())


def test_origin_lookups_fail_visible():
    assert work_class_for_origin(None) == "development"          # human session
    assert work_class_for_origin("not-registered") == "development"  # ad-hoc recurring id
    assert work_class_for_origin("pr-doctor") == "maintenance"
    assert work_class_for_origin("backlog-runner") == "development"


def test_origin_label_is_the_registry_display_name():
    assert origin_label(None) is None
    assert origin_label("pr-doctor") == "PR Doctor"
    assert origin_label("pm-classify") == "PM Classification"
    # Unregistered ids fall back to the id itself rather than disappearing.
    assert origin_label("custom-bot") == "custom-bot"
    assert assisted_by_label("autopilot") == "Autopilot"
    assert assisted_by_label(None) is None


def _rec(**kw) -> SessionRecord:
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="", cols=80, rows=24, cwd="/tmp", repo=None, **kw)


def test_record_to_dict_emits_work_class_and_labels():
    from server.api.sessions import _record_to_dict

    human = _record_to_dict(_rec(), repo=None)
    assert human["workClass"] == "development"
    assert human["originLabel"] is None
    assert human["assistedByLabel"] is None

    doctor = _record_to_dict(_rec(origin="pr-doctor", origin_ref="o/r#1"), repo=None)
    assert doctor["workClass"] == "maintenance"
    assert doctor["originLabel"] == "PR Doctor"

    runner = _record_to_dict(_rec(origin="backlog-runner", assisted_by="autopilot"), repo=None)
    assert runner["workClass"] == "development"
    assert runner["originLabel"] == "Backlog Runner"
    assert runner["assistedByLabel"] == "Autopilot"
