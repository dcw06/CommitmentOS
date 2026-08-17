from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.queries.get_system_status import GetSystemStatus
from commitmentos.contracts.observations import ObservationType
from commitmentos.domain.audit.models import ActivityEventType
from commitmentos.domain.commitments.models import LifecycleStatus, RiskLevel
from commitmentos.domain.planning.models import PlannerRunStatus

OUTCOME_ACTIVITY_SCAN_LIMIT = 250


@dataclass(frozen=True, slots=True)
class TodayView:
    user_id: str
    local_date: date
    timezone: str
    generated_at: datetime
    work_blocks: tuple[Mapping[str, Any], ...]
    outcome_strip: Mapping[str, int]
    newly_detected_candidates: tuple[Mapping[str, Any], ...]
    at_risk_commitments: tuple[Mapping[str, Any], ...]
    pending_approvals: tuple[Mapping[str, Any], ...]
    visible_failure_states: tuple[Mapping[str, Any], ...]


class GetToday:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        clock: Clock,
        system_status: GetSystemStatus,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._system_status = system_status

    async def execute(self, user_id: str, timezone: str) -> TodayView:
        zone = ZoneInfo(timezone)
        now = self._clock.now()
        local_date = now.astimezone(zone).date()
        start, end = self._day_bounds(local_date, timezone)

        async def _load(repositories: RepositorySet) -> tuple[Any, ...]:
            blocks = tuple(
                await repositories.work_blocks.list_for_user_horizon(
                    user_id, start, end
                )
            )
            commitments = tuple(await repositories.commitments.list_active(user_id))
            approvals = tuple(await repositories.approvals.list_pending(user_id))
            candidates = tuple(
                await repositories.commitments.list_for_user(
                    user_id, LifecycleStatus.CANDIDATE, None, 50
                )
            ) + tuple(
                await repositories.commitments.list_for_user(
                    user_id, LifecycleStatus.AWAITING_CONFIRMATION, None, 50
                )
            )
            plans = tuple(
                await repositories.planner_runs.list_for_user(
                    user_id, PlannerRunStatus.PUBLISHED.value, 1
                )
            )
            activity = tuple(
                await repositories.activity.list_for_user(
                    user_id, None, OUTCOME_ACTIVITY_SCAN_LIMIT
                )
            )
            return blocks, commitments, approvals, candidates, plans, activity

        blocks, commitments, approvals, candidates, plans, activity = (
            await self._unit_of_work.read(_load)
        )
        latest_plan = plans[0] if plans else None
        repairs = tuple(
            event
            for event in activity
            if event.event_type == ActivityEventType.PLAN_REPAIRED
            and event.payload.get("source_observation_type")
            in (
                None,
                ObservationType.CALENDAR_EVENT_CHANGED.value,
                ObservationType.CALENDAR_ENVIRONMENTAL_DISRUPTION.value,
                ObservationType.CALENDAR_USER_MOVE_VALID.value,
            )
        )
        status = await self._system_status.execute(user_id)
        return TodayView(
            user_id=user_id,
            local_date=local_date,
            timezone=timezone,
            generated_at=now,
            work_blocks=tuple(
                {
                    "work_block_id": block.work_block_id,
                    "commitment_id": block.commitment_id,
                    "scheduled_start": block.scheduled_start.isoformat(),
                    "scheduled_end": block.scheduled_end.isoformat(),
                    "execution_state": block.execution_state.value,
                    "verified_minutes": block.verified_minutes,
                    "duration_minutes": block.duration_minutes,
                    "title": next(
                        (
                            commitment.title
                            for commitment in commitments
                            if commitment.commitment_id == block.commitment_id
                        ),
                        "",
                    ),
                    "plan_revision": block.plan_revision,
                    "revision": block.revision,
                }
                for block in blocks
            ),
            outcome_strip={
                "commitments_kept_feasible": (
                    sum(
                        allocation.shortfall_minutes == 0
                        for allocation in latest_plan.allocations
                    )
                    if latest_plan is not None
                    else 0
                ),
                "work_minutes_reserved": (
                    sum(block.duration_minutes for block in latest_plan.work_blocks)
                    if latest_plan is not None
                    else 0
                ),
                "conflicts_repaired_automatically": len(repairs),
                "unaffected_blocks_preserved": sum(
                    len(event.payload.get("preserved_work_block_ids", ()))
                    for event in repairs
                ),
                "manual_reschedules_avoided": sum(
                    int(event.payload.get("moved_block_count", 0))
                    for event in repairs
                ),
            },
            newly_detected_candidates=tuple(
                {
                    "commitment_id": commitment.commitment_id,
                    "title": commitment.title,
                    "ownership_type": commitment.ownership_type.value,
                    "lifecycle_status": commitment.lifecycle_status.value,
                    "deadline": commitment.deadline.value.isoformat(),
                    "deadline_confidence": commitment.deadline.confidence,
                    "effort_confidence": commitment.effort.confidence,
                    "updated_at": commitment.updated_at.isoformat(),
                }
                for commitment in sorted(
                    candidates, key=lambda item: item.updated_at, reverse=True
                )
            ),
            at_risk_commitments=tuple(
                {
                    "commitment_id": commitment.commitment_id,
                    "title": commitment.title,
                    "deadline": commitment.deadline.value.isoformat(),
                    "risk_level": (
                        commitment.projection.risk_level.value
                        if commitment.projection is not None
                        and commitment.projection.source_commitment_revision
                        == commitment.revision
                        else RiskLevel.UNKNOWN.value
                    ),
                    "remaining_minutes": (
                        commitment.projection.remaining_minutes
                        if commitment.projection is not None
                        and commitment.projection.source_commitment_revision
                        == commitment.revision
                        else None
                    ),
                }
                for commitment in commitments
                if commitment.projection is None
                or commitment.projection.source_commitment_revision
                != commitment.revision
                or commitment.projection.risk_level
                in (RiskLevel.AT_RISK, RiskLevel.CRITICAL, RiskLevel.OVERDUE)
            ),
            pending_approvals=tuple(approval_summary(item) for item in approvals),
            visible_failure_states=status.failure_states,
        )

    def _day_bounds(self, local_date: date, timezone: str) -> tuple[datetime, datetime]:
        zone = ZoneInfo(timezone)
        start = datetime.combine(local_date, time.min, tzinfo=zone)
        return start, start + timedelta(days=1)


def approval_summary(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "approval_id": value.get("approval_id"),
        "commitment_id": value.get("commitment_id"),
        "request_type": value.get("request_type"),
        "payload": dict(value.get("payload") or {}),
        "created_at": (
            value["created_at"].isoformat()
            if isinstance(value.get("created_at"), datetime)
            else value.get("created_at")
        ),
        "revision": value.get("revision"),
    }
