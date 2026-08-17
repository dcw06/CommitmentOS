from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.observations import ReconciliationStatus
from commitmentos.contracts.tasks import SourceType
from commitmentos.domain.actions.models import ExecutionStatus
from commitmentos.domain.audit.models import ActivityEventType
from commitmentos.domain.commitments.models import RiskLevel
from commitmentos.domain.controls.models import ControlMode, SystemControls
from commitmentos.domain.planning.models import PlannerRunStatus
from commitmentos.domain.progress.models import WorkBlockExecutionState
from commitmentos.domain.progress.service import ProgressCalculator

STATUS_SCAN_LIMIT = 250


@dataclass(frozen=True, slots=True)
class SystemStatusView:
    user_id: str
    controls: SystemControls
    gmail_watch_expires_at: datetime | None
    calendar_watch_expires_at: datetime | None
    last_successful_sync: Mapping[str, datetime | None]
    pending_observation_count: int
    held_observation_count: int
    pending_action_count: int
    held_action_count: int
    in_flight_action_count: int
    pending_decision_count: int
    stale_planner_run_count: int
    failure_states: tuple[Mapping[str, Any], ...]
    generated_at: datetime


class GetSystemStatus:
    def __init__(self, unit_of_work: UnitOfWork, clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._progress = ProgressCalculator()

    async def execute(self, user_id: str) -> SystemStatusView:
        now = self._clock.now()

        async def _load(repositories: RepositorySet) -> SystemStatusView:
            controls = await repositories.system_controls.get(user_id)
            pending_observations = tuple(
                await repositories.observations.list_pending(user_id, STATUS_SCAN_LIMIT)
            )
            held_observations = tuple(
                await repositories.observations.list_held(user_id, STATUS_SCAN_LIMIT)
            )
            retrying_observations = tuple(
                await repositories.observations.list_for_statuses(
                    user_id,
                    (
                        ReconciliationStatus.RETRYABLE_FAILED.value,
                    ),
                    STATUS_SCAN_LIMIT,
                )
            )
            pending_actions = tuple(
                await repositories.outbox.list_for_user_statuses(
                    user_id,
                    (
                        ExecutionStatus.PENDING.value,
                        ExecutionStatus.CLAIMED.value,
                        ExecutionStatus.ACTION_IN_FLIGHT.value,
                        ExecutionStatus.RETRYABLE_FAILED.value,
                        ExecutionStatus.EXTERNAL_VERIFICATION_PENDING.value,
                    ),
                    STATUS_SCAN_LIMIT,
                )
            )
            held_actions = tuple(
                await repositories.outbox.list_held(user_id, STATUS_SCAN_LIMIT)
            )
            in_flight = tuple(
                action
                for action in pending_actions
                if action.execution_status
                in (
                    ExecutionStatus.CLAIMED,
                    ExecutionStatus.ACTION_IN_FLIGHT,
                    ExecutionStatus.EXTERNAL_VERIFICATION_PENDING,
                )
            )
            action_failures = tuple(
                await repositories.outbox.list_for_user_statuses(
                    user_id,
                    (
                        ExecutionStatus.ACTION_IN_FLIGHT.value,
                        ExecutionStatus.RETRYABLE_FAILED.value,
                        ExecutionStatus.TERMINAL_FAILED.value,
                        ExecutionStatus.STALE.value,
                        ExecutionStatus.STALE_PRECONDITION.value,
                    ),
                    STATUS_SCAN_LIMIT,
                )
            )
            approvals = tuple(await repositories.approvals.list_pending(user_id))
            requests = tuple(
                await repositories.sync_requests.list_for_user(
                    user_id, STATUS_SCAN_LIMIT
                )
            )
            gmail_cursor = await repositories.sync_cursors.get(
                user_id, SourceType.GMAIL
            )
            calendar_cursor = await repositories.sync_cursors.get(
                user_id, SourceType.CALENDAR
            )
            gmail_generation = await repositories.sync_generations.get_active(
                user_id, SourceType.GMAIL
            )
            calendar_generation = await repositories.sync_generations.get_active(
                user_id, SourceType.CALENDAR
            )
            channel = await repositories.calendar_channels.get(user_id)
            stale_runs = tuple(
                await repositories.planner_runs.list_for_user(
                    user_id, PlannerRunStatus.STALE.value, STATUS_SCAN_LIMIT
                )
            )
            recent_plans = tuple(
                await repositories.planner_runs.list_for_user(
                    user_id, None, STATUS_SCAN_LIMIT
                )
            )
            commitments = tuple(await repositories.commitments.list_active(user_id))
            recent_activity = tuple(
                await repositories.activity.list_for_user(
                    user_id, None, STATUS_SCAN_LIMIT
                )
            )

            failures: list[Mapping[str, Any]] = []

            def add(state: str, **details: Any) -> None:
                failures.append({"state": state, **details})

            if controls.observation_mode == ControlMode.PAUSED:
                add("monitoring_paused", control_epoch=controls.control_epoch)
            if controls.automatic_action_mode == ControlMode.PAUSED:
                add("automatic_actions_paused", control_epoch=controls.control_epoch)
            for request in requests:
                status = str(request.get("status") or "")
                if status == "reauth_required":
                    add(
                        "reauth_required",
                        source=request.get("source"),
                        sync_request_id=request.get("sync_request_id"),
                    )
                if request.get("failure_state") == "source_sync_delayed":
                    add(
                        "source_sync_delayed",
                        source=request.get("source"),
                        sync_request_id=request.get("sync_request_id"),
                        delayed_since=_iso(request.get("delayed_since")),
                    )
                if status == "pending":
                    add(
                        "source_sync_in_progress",
                        source=request.get("source"),
                        sync_request_id=request.get("sync_request_id"),
                        reason=request.get("reason"),
                    )
            for source, cursor, generation in (
                (SourceType.GMAIL, gmail_cursor, gmail_generation),
                (SourceType.CALENDAR, calendar_cursor, calendar_generation),
            ):
                if cursor is not None and cursor.full_resync_required:
                    add("full_resync_required", source=source.value)
                if generation is not None or (
                    cursor is not None
                    and cursor.publish_in_progress_generation_id is not None
                ):
                    active_generation_id = (
                        generation.sync_generation_id
                        if generation is not None
                        else (
                            cursor.publish_in_progress_generation_id
                            if cursor is not None
                            else None
                        )
                    )
                    add(
                        "source_sync_in_progress",
                        source=source.value,
                        sync_generation_id=active_generation_id,
                    )
            for observation in held_observations:
                add(
                    "reconciliation_held_by_control",
                    observation_id=observation.observation_id,
                )
            for observation in retrying_observations:
                add(
                    "reconciliation_retrying",
                    observation_id=observation.observation_id,
                    processing_attempt=observation.processing_attempt,
                    reconciliation_status=observation.reconciliation_status.value,
                )
            for action in held_actions:
                add(
                    "action_held_by_control",
                    outbox_id=action.outbox_id,
                    work_block_id=action.work_block_id,
                )
            for action in action_failures:
                state = {
                    ExecutionStatus.ACTION_IN_FLIGHT: "action_in_flight",
                    ExecutionStatus.RETRYABLE_FAILED: "calendar_action_retrying",
                    ExecutionStatus.TERMINAL_FAILED: "calendar_action_failed",
                    ExecutionStatus.STALE: "action_stale",
                    ExecutionStatus.STALE_PRECONDITION: "calendar_precondition_stale",
                }[action.execution_status]
                add(
                    state,
                    outbox_id=action.outbox_id,
                    work_block_id=action.work_block_id,
                    error=dict(action.error or {}),
                    attempts=action.attempts,
                )
            if any(
                event.event_type == ActivityEventType.INTERPRETATION_REJECTED
                for event in recent_activity
            ):
                latest_rejection = next(
                    event
                    for event in recent_activity
                    if event.event_type == ActivityEventType.INTERPRETATION_REJECTED
                )
                add(
                    "model_output_rejected",
                    activity_event_id=latest_rejection.activity_event_id,
                    occurred_at=latest_rejection.created_at.isoformat(),
                )
            for approval in approvals:
                request_type = str(approval.get("request_type") or "")
                add(
                    "pending_decision",
                    approval_id=approval.get("approval_id"),
                    request_type=request_type,
                    commitment_id=approval.get("commitment_id"),
                )
                if request_type in (
                    "calendar_invalid_move_decision",
                    "calendar_user_deleted_decision",
                ):
                    add(
                        "user_block_decision_required",
                        approval_id=approval.get("approval_id"),
                        request_type=request_type,
                    )
            for run in stale_runs:
                add(
                    "projection_stale",
                    planner_run_id=run.planner_run_id,
                    reason=run.stale_reason,
                )
            if recent_plans and not recent_plans[0].feasible:
                add(
                    "no_feasible_plan",
                    planner_run_id=recent_plans[0].planner_run_id,
                )
                add(
                    "portfolio_capacity_conflict",
                    planner_run_id=recent_plans[0].planner_run_id,
                )
            for commitment in commitments:
                blocks = tuple(
                    await repositories.work_blocks.list_for_commitment(
                        commitment.commitment_id
                    )
                )
                for block in blocks:
                    if block.execution_state == WorkBlockExecutionState.AWAITING_CHECK_IN:
                        add(
                            "work_check_in_required",
                            commitment_id=commitment.commitment_id,
                            work_block_id=block.work_block_id,
                            verified_minutes=block.verified_minutes,
                        )
                    if (
                        block.execution_state == WorkBlockExecutionState.PLANNED
                        and block.user_edit_state.value == "none"
                    ):
                        snapshot = await repositories.calendar_snapshots.get(
                            block.calendar_id, block.calendar_event_id
                        )
                        if snapshot is not None and (
                            snapshot.is_tombstone
                            or snapshot.observed_start != block.scheduled_start
                            or snapshot.observed_end != block.scheduled_end
                        ):
                            add(
                                "calendar_drift_detected",
                                commitment_id=commitment.commitment_id,
                                work_block_id=block.work_block_id,
                                calendar_event_id=block.calendar_event_id,
                            )
                projection = commitment.projection
                revision_hash = self._progress.work_block_revision_hash(blocks)
                if projection is None or not projection.matches(
                    commitment.revision, revision_hash
                ):
                    add(
                        "projection_stale",
                        commitment_id=commitment.commitment_id,
                    )
                elif projection.risk_level == RiskLevel.OVERDUE:
                    add(
                        "overdue",
                        commitment_id=commitment.commitment_id,
                        deadline=commitment.deadline.value.isoformat(),
                    )

            gmail_request = next(
                (item for item in requests if item.get("source") == SourceType.GMAIL.value),
                None,
            )
            unique_failures = {
                repr(sorted(item.items(), key=lambda pair: pair[0])): item
                for item in failures
            }
            return SystemStatusView(
                user_id=user_id,
                controls=controls,
                gmail_watch_expires_at=(
                    gmail_request.get("watch_expiration") if gmail_request else None
                ),
                calendar_watch_expires_at=(
                    channel.get("expiration") if channel is not None else None
                ),
                last_successful_sync={
                    SourceType.GMAIL.value: (
                        gmail_cursor.updated_at if gmail_cursor is not None else None
                    ),
                    SourceType.CALENDAR.value: (
                        calendar_cursor.updated_at if calendar_cursor is not None else None
                    ),
                },
                pending_observation_count=len(pending_observations),
                held_observation_count=len(held_observations),
                pending_action_count=len(pending_actions),
                held_action_count=len(held_actions),
                in_flight_action_count=len(in_flight),
                pending_decision_count=len(approvals),
                stale_planner_run_count=len(stale_runs),
                failure_states=tuple(unique_failures.values()),
                generated_at=now,
            )

        return await self._unit_of_work.read(_load)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value
