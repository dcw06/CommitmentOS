from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class PolicyDisposition(StrEnum):
    AUTOMATIC = "automatic"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"
    NO_ACTION = "no_action"


@dataclass(frozen=True, slots=True)
class PolicyContext:
    action_type: str
    is_first_plan: bool
    affected_block_count: int
    total_shift_minutes: int
    schedules_outside_preferred_hours: bool
    ownership_valid: bool
    automatic_actions_enabled: bool
    control_epoch: int
    metadata: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    disposition: PolicyDisposition
    reason_code: str
    reason: str
    policy_version: str
    control_epoch: int


@dataclass(frozen=True, slots=True)
class PolicyProfile:
    profile_id: str
    version: str
    maximum_automatic_block_moves: int
    maximum_automatic_shift_minutes: int
    allow_outside_preferred_hours: bool
