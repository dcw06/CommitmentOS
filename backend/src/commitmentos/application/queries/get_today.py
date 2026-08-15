from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.queries.get_system_status import GetSystemStatus
from commitmentos.domain.commitments.models import RiskLevel


@dataclass(frozen=True, slots=True)
class TodayView:
    user_id: str
    local_date: date
    timezone: str
    generated_at: datetime
    work_blocks: tuple[Mapping[str, Any], ...]
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

        async def _load(repositories: RepositorySet) -> tuple[Any, Any, Any]:
            blocks = tuple(
                await repositories.work_blocks.list_for_user_horizon(
                    user_id, start, end
                )
            )
            commitments = tuple(await repositories.commitments.list_active(user_id))
            approvals = tuple(await repositories.approvals.list_pending(user_id))
            return blocks, commitments, approvals

        blocks, commitments, approvals = await self._unit_of_work.read(_load)
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
                    "plan_revision": block.plan_revision,
                    "revision": block.revision,
                }
                for block in blocks
            ),
            at_risk_commitments=tuple(
                {
                    "commitment_id": commitment.commitment_id,
                    "title": commitment.title,
                    "deadline": commitment.deadline.value.isoformat(),
                    "risk_level": (
                        commitment.projection.risk_level.value
                        if commitment.projection is not None
                        else RiskLevel.UNKNOWN.value
                    ),
                    "remaining_minutes": (
                        commitment.projection.remaining_minutes
                        if commitment.projection is not None
                        else None
                    ),
                }
                for commitment in commitments
                if commitment.projection is None
                or commitment.projection.risk_level
                in (RiskLevel.AT_RISK, RiskLevel.CRITICAL, RiskLevel.OVERDUE)
            ),
            pending_approvals=tuple(_approval_summary(item) for item in approvals),
            visible_failure_states=status.failure_states,
        )

    def _day_bounds(self, local_date: date, timezone: str) -> tuple[datetime, datetime]:
        zone = ZoneInfo(timezone)
        start = datetime.combine(local_date, time.min, tzinfo=zone)
        return start, start + timedelta(days=1)


def _approval_summary(value: Mapping[str, Any]) -> Mapping[str, Any]:
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
