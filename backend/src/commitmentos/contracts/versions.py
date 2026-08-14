from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ContractVersions:
    observation_schema: str
    task_schema: str
    extraction_schema: str
    workflow: str
    planner: str
    risk: str
    policy: str
    event_id_algorithm: str


class VersionRegistry:
    def __init__(self, versions: ContractVersions) -> None:
        self._versions = versions

    def current(self) -> ContractVersions:
        return self._versions

    def as_mapping(self) -> Mapping[str, str]:
        return {field.name: getattr(self._versions, field.name) for field in fields(self._versions)}

    def accepts(self, contract_name: str, version: str) -> bool:
        current = self.as_mapping().get(contract_name)
        return current is not None and current == version
