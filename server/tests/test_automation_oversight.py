"""Registry metadata stays complete as built-in automations evolve."""
from dataclasses import replace

import pytest

from server.automations.oversight import AutomationOversight, Relationship, Responsibility
from server.automations.pr_doctor import PrDoctor
from server.automations.registry import AutomationRegistry, registry


def test_every_builtin_has_complete_resolving_metadata():
    registry.validate_oversight(require_all=True)
    for automation in registry.all():
        metadata = registry.oversight_for(automation.id)
        assert metadata is not None
        payload = metadata.to_dict()
        assert set(payload['modeDescriptions']) == set(automation.modes)
        assert payload['scope'].strip()
        assert payload['responsibility']['label']
        for relationship in payload['relationships']:
            assert registry.get(relationship['targetId'])
            assert relationship['description'].strip()


def test_registration_rejects_mode_drift_without_registering():
    local = AutomationRegistry()
    metadata = AutomationOversight(Responsibility.EXECUTION, 'Managed PRs', {'off': 'Disabled'})
    with pytest.raises(ValueError, match='mode'):
        local.register(PrDoctor(), oversight=metadata)
    assert local.all() == []


def test_relationships_allow_forward_reference_but_reject_unresolved_target():
    local = AutomationRegistry()
    metadata = registry.oversight_for('pr-doctor')
    assert metadata is not None
    local.register(PrDoctor(), oversight=replace(metadata, relationships=(
        Relationship('uses', 'missing', 'Needs this service'),)))
    with pytest.raises(ValueError, match='missing'):
        local.validate_oversight()


def test_test_automations_can_omit_metadata_but_strict_validation_catches_it():
    local = AutomationRegistry()
    local.register(PrDoctor())
    assert local.oversight_for('pr-doctor') is None
    local.validate_oversight()
    with pytest.raises(ValueError, match='pr-doctor'):
        local.validate_oversight(require_all=True)



def test_serialized_metadata_cannot_mutate_canonical_descriptions():
    metadata = registry.oversight_for('deploy-monitor')
    assert metadata is not None
    payload = metadata.to_dict()
    original = payload['modeDescriptions']['watch']
    payload['modeDescriptions']['watch'] = 'Changed by caller'
    payload['relationships'].clear()
    assert metadata.to_dict()['modeDescriptions']['watch'] == original
    assert metadata.to_dict()['relationships']


def test_validation_catches_modes_changed_after_registration():
    local = AutomationRegistry()
    automation = PrDoctor()
    local.register(automation, oversight=registry.oversight_for(automation.id))
    automation.modes = (*automation.modes, 'new-mode')
    with pytest.raises(ValueError, match='drifted'):
        local.validate_oversight()
