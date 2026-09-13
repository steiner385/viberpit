"""Display-only automation responsibilities and relationships; no authority decisions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Literal, TypedDict


class Responsibility(Enum):
    CONNECTION = ('product_information', 'Connect product information')
    ORGANIZATION = ('work_organization', 'Organize work')
    PREPARATION = ('initiative_preparation', 'Prepare initiatives')
    EXECUTION = ('work_execution', 'Execute and finish work')
    IMPROVEMENT = ('improvement_detection', 'Detect improvement work')


RelationshipKind = Literal['requires', 'uses', 'hands_off_to', 'shares_work_with']


class ResponsibilityPayload(TypedDict):
    id: str
    label: str


class RelationshipPayload(TypedDict):
    kind: RelationshipKind
    targetId: str
    description: str


class OversightPayload(TypedDict):
    responsibility: ResponsibilityPayload
    scope: str
    modeDescriptions: dict[str, str]
    relationships: list[RelationshipPayload]


@dataclass(frozen=True)
class Relationship:
    kind: RelationshipKind
    target_id: str
    description: str

    def __post_init__(self) -> None:
        if self.kind not in ('requires', 'uses', 'hands_off_to', 'shares_work_with'):
            raise ValueError(f'unknown relationship kind: {self.kind!r}')
        if not self.target_id.strip() or not self.description.strip():
            raise ValueError('relationship target and description must be nonempty')

    def to_dict(self) -> RelationshipPayload:
        return {'kind': self.kind, 'targetId': self.target_id, 'description': self.description}


@dataclass(frozen=True)
class AutomationOversight:
    responsibility: Responsibility
    scope: str
    mode_descriptions: Mapping[str, str]
    relationships: tuple[Relationship, ...] = ()

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ValueError('automation scope must be nonempty')
        if any(not description.strip() for description in self.mode_descriptions.values()):
            raise ValueError('mode descriptions must be nonempty')
        object.__setattr__(self, 'mode_descriptions', MappingProxyType(dict(self.mode_descriptions)))

    def to_dict(self) -> OversightPayload:
        responsibility_id, label = self.responsibility.value
        return {
            'responsibility': {'id': responsibility_id, 'label': label},
            'scope': self.scope,
            'modeDescriptions': dict(self.mode_descriptions),
            'relationships': [relationship.to_dict() for relationship in self.relationships],
        }
