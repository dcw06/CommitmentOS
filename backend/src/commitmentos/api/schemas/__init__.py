from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


class StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionedMutationRequest(StrictApiModel):
    expected_revision: int = Field(ge=1)


class ApprovalResolutionRequest(RevisionedMutationRequest):
    decision: str
    reason: str | None = None
    confirmed_minutes: int | None = Field(
        default=None,
        ge=30,
        le=2400,
        multiple_of=15,
    )
    deadline: datetime | None = None
    ownership_type: str | None = None
    choice: str | None = None


class WorkCheckInApiRequest(RevisionedMutationRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)
    completed: bool
    verified_minutes: int = Field(ge=0)
    checked_in_at: datetime


class CompleteCommitmentApiRequest(RevisionedMutationRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)
    completed_at: datetime
    note: str | None = Field(default=None, max_length=500)


class ReopenCommitmentApiRequest(RevisionedMutationRequest):
    pass


class SetCommitmentPriorityApiRequest(RevisionedMutationRequest):
    priority: int = Field(ge=-100, le=100)


class ChangeCommitmentLifecycleApiRequest(RevisionedMutationRequest):
    target_status: Literal["paused", "active", "dismissed"]


class PlanUndoApiRequest(StrictApiModel):
    idempotency_key: str = Field(min_length=1, max_length=128)


class ControlChangeApiRequest(StrictApiModel):
    control_name: str
    target_mode: str
    reason: str
    expected_control_epoch: int = Field(ge=0)


class CommandResponse(StrictApiModel):
    status: str
    identifiers: Mapping[str, str]
    revision: int | None
    error_code: str | None


class PageResponse(StrictApiModel):
    items: Sequence[Mapping[str, Any]]
    next_cursor: str | None


class HealthResponse(StrictApiModel):
    status: str
    version: str


class OAuthCallbackQuery(StrictApiModel):
    code: str
    state: str
    scope: str | None = None
    error: str | None = None
