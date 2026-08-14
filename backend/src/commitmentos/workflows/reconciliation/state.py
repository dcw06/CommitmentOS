from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Self

from commitmentos.application.dto import FencedLease
from commitmentos.application.ports.model_interpreter import InterpretationResult
from commitmentos.contracts.model_output import CommitmentInterpretationV1, ModelValidationResult
from commitmentos.contracts.observations import ObservationType, ObservationV1
from commitmentos.domain.commitments.identity import IdentityResolution
from commitmentos.domain.controls.models import ControlMode
from commitmentos.domain.planning.calendar_state import CalendarStateSnapshot
from commitmentos.domain.planning.models import PlanDiff, PlannerInput, PortfolioPlan
from commitmentos.domain.policy.models import PolicyDecision
from commitmentos.domain.shared.types import RevisionSet


@dataclass(frozen=True, slots=True)
class ExecutionControlSnapshot:
    observation_mode: ControlMode
    automatic_action_mode: ControlMode
    control_epoch: int


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    planner_input: PlannerInput
    calendar_state: CalendarStateSnapshot
    previous_plan: PortfolioPlan | None


@dataclass(frozen=True, slots=True)
class WorkflowFailure:
    error_code: str
    safe_message: str
    retryable: bool
    node_name: str


@dataclass(frozen=True, slots=True)
class ReconciliationStateV1:
    run_id: str
    observation_id: str
    observation_type: ObservationType | None
    user_id: str
    trace_id: str
    workflow_version: str
    dispatch_generation: int
    claimed_control_epoch: int
    processing_fence: FencedLease
    observation: ObservationV1 | None
    transient_source_context: Any | None
    interpretation_result: InterpretationResult | None
    interpretation_proposal: CommitmentInterpretationV1 | None
    validation_result: ModelValidationResult | None
    identity_resolutions: tuple[IdentityResolution, ...]
    expected_revisions: RevisionSet
    execution_control_snapshot: ExecutionControlSnapshot | None
    portfolio_snapshot: PortfolioSnapshot | None
    planner_result: PortfolioPlan | None
    plan_diff: PlanDiff | None
    policy_results: tuple[PolicyDecision, ...]
    durable_outcome_ids: tuple[str, ...]
    safe_metadata: Mapping[str, Any]
    failure: WorkflowFailure | None

    def evolve(self, **changes: object) -> Self:
        return replace(self, **changes)  # type: ignore[arg-type]

    def fail(self, failure: WorkflowFailure) -> Self:
        return replace(self, failure=failure)

    def require_workflow_version(self, expected_version: str) -> None:
        if self.workflow_version != expected_version:
            raise ValueError(
                f"workflow version is {self.workflow_version}; expected {expected_version}"
            )

    def require_processing_fence(self, expected_fencing_token: str) -> None:
        if self.processing_fence.fencing_token != expected_fencing_token:
            raise ValueError("processing fencing token changed")


class ReconciliationNode(Protocol):
    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...
