from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from commitmentos.domain.commitments.models import (
    BlockingStatus,
    Commitment,
    CommitmentProjection,
)
from commitmentos.domain.planning.models import PortfolioPlan
from commitmentos.domain.planning.risk import RiskCalculator
from commitmentos.domain.progress.models import WorkBlock
from commitmentos.domain.progress.service import ProgressCalculator
from commitmentos.domain.shared.types import CanonicalEncoder


def projection_hash(commitment: Commitment) -> str:
    projection = commitment.projection
    if projection is None:
        return ""
    return CanonicalEncoder.hash(
        [
            "commitment-projection:v1",
            commitment.commitment_id,
            projection.source_commitment_revision,
            projection.source_work_block_revision_hash,
            projection.planner_run_id,
            projection.calculator_version,
            projection.remaining_minutes,
            projection.risk_level.value,
        ]
    )


@dataclass(frozen=True, slots=True)
class ProjectionCheck:
    valid: bool
    reason: str | None
    projection: CommitmentProjection | None


class ProjectionGuard:
    def __init__(
        self,
        progress_calculator: ProgressCalculator,
        risk_calculator: RiskCalculator,
        calculator_version: str,
    ) -> None:
        self._progress_calculator = progress_calculator
        self._risk_calculator = risk_calculator
        self._calculator_version = calculator_version

    def validate(
        self,
        commitment: Commitment,
        work_blocks: Sequence[WorkBlock],
    ) -> ProjectionCheck:
        projection = commitment.projection
        if projection is None:
            return ProjectionCheck(False, "projection_missing", None)
        revision_hash = self._progress_calculator.work_block_revision_hash(work_blocks)
        if not projection.matches(commitment.revision, revision_hash):
            return ProjectionCheck(False, "projection_inputs_changed", projection)
        if projection.calculator_version != self._calculator_version:
            return ProjectionCheck(False, "projection_calculator_changed", projection)
        return ProjectionCheck(True, None, projection)

    def rebuild(
        self,
        commitment: Commitment,
        work_blocks: Sequence[WorkBlock],
        plan: PortfolioPlan,
        at: datetime,
    ) -> CommitmentProjection:
        allocation = next(
            (
                item
                for item in plan.allocations
                if item.commitment_id == commitment.commitment_id
            ),
            None,
        )
        if allocation is None:
            raise ValueError("planner run has no allocation for commitment")
        verified = self._progress_calculator.verified_minutes(work_blocks)
        remaining = self._progress_calculator.active_remaining_minutes(
            commitment,
            work_blocks,
        )
        if not commitment.effort.confirmed_minutes:
            blocking = BlockingStatus.WAITING
        elif allocation.shortfall_minutes > 0:
            blocking = BlockingStatus.BLOCKED
        else:
            blocking = BlockingStatus.CLEAR
        return CommitmentProjection(
            verified_completed_minutes=verified,
            remaining_minutes=remaining,
            risk_level=allocation.risk_level,
            blocking_status=blocking,
            source_commitment_revision=commitment.revision,
            source_work_block_revision_hash=(
                self._progress_calculator.work_block_revision_hash(work_blocks)
            ),
            planner_run_id=plan.planner_run_id,
            calculator_version=self._calculator_version,
            computed_at=at,
        )
