from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.task_dispatcher import TaskDispatcher, TaskDispatchResult
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.tasks import ExecuteCalendarActionTaskV1
from commitmentos.domain.actions.models import (
    ActionOutbox,
    DispatchStatus,
    ExecutionStatus,
)
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.shared.types import CanonicalEncoder


@dataclass(frozen=True, slots=True)
class OutboxDispatchSummary:
    scanned: int
    queued: int
    held: int
    superseded: int


class OutboxDispatcher:
    """Releases committed outbox intent to the Calendar-actions queue.

    Dispatch never releases work while automatic actions are paused; a paused
    record is durably held without losing intent. Task creation happens after
    the dispatch-status commit and stays repairable through
    `dispatch_pending` (the periodic write-before-enqueue repair path).
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        task_dispatcher: TaskDispatcher,
        task_schema_version: str,
        clock: Clock,
        activity_factory: ActivityEventFactory | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._task_dispatcher = task_dispatcher
        self._task_schema_version = task_schema_version
        self._clock = clock
        self._activity_factory = activity_factory or ActivityEventFactory()

    async def dispatch(self, outbox_id: str) -> TaskDispatchResult | None:
        async def _mark(repositories: RepositorySet) -> ActionOutbox | None:
            action = await repositories.outbox.get(outbox_id)
            if action is None:
                return None
            if action.dispatch_status == DispatchStatus.QUEUED:
                # Repair path: the named task is idempotent, recreate it.
                return action
            if action.dispatch_status != DispatchStatus.PENDING:
                return None
            controls = await repositories.system_controls.get(action.user_id)
            now = self._clock.now()
            if not controls.allows_automatic_actions():
                held = action.hold(controls.control_epoch, now)
                await repositories.outbox.save(held, action.updated_at)
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=action.user_id,
                        event_type=ActivityEventType.POLICY_DECIDED,
                        trace_id=action.outbox_id,
                        actor="outbox_dispatcher",
                        summary="Calendar action held: automatic actions are paused",
                        payload={
                            "outbox_id": action.outbox_id,
                            "held_at_control_epoch": controls.control_epoch,
                        },
                        created_at=now,
                    )
                )
                return None
            queued = action.mark_queued(now)
            await repositories.outbox.save(queued, action.updated_at)
            return queued

        action = await self._unit_of_work.run(_mark)
        if action is None:
            return None
        return await self._task_dispatcher.enqueue_calendar_action(self._task_for(action))

    async def dispatch_pending(self, user_id: str, limit: int) -> OutboxDispatchSummary:
        async def _list(repositories: RepositorySet) -> list[ActionOutbox]:
            return list(await repositories.outbox.list_pending_dispatch(user_id, limit))

        pending = await self._unit_of_work.read(_list)
        queued = 0
        held = 0
        for action in pending:
            result = await self.dispatch(action.outbox_id)
            if result is not None:
                queued += 1
            else:
                held += 1
        return OutboxDispatchSummary(scanned=len(pending), queued=queued, held=held, superseded=0)

    async def release_held(self, user_id: str, limit: int) -> OutboxDispatchSummary:
        """Revalidate held actions after an automatic-action resume.

        Held records are never released blindly. Each is superseded; when the
        underlying commitment and plan revisions are still current, a fresh
        intent carrying the new control epoch is written and dispatched.
        """

        async def _list(repositories: RepositorySet) -> list[ActionOutbox]:
            return list(await repositories.outbox.list_held(user_id, limit))

        held_actions = await self._unit_of_work.read(_list)
        superseded = 0
        requeued = 0
        replacements: list[str] = []
        for held in held_actions:

            async def _revalidate(
                repositories: RepositorySet,
                outbox_id: str = held.outbox_id,
            ) -> str | None:
                action = await repositories.outbox.get(outbox_id)
                if action is None or action.execution_status != ExecutionStatus.HELD_BY_CONTROL:
                    return None
                controls = await repositories.system_controls.get(action.user_id)
                if not controls.allows_automatic_actions():
                    return None
                commitment = await repositories.commitments.get(action.commitment_id)
                now = self._clock.now()
                still_valid = (
                    commitment is not None
                    and commitment.revision == action.expected_commitment_revision
                    and commitment.plan_revision == action.expected_plan_revision
                )
                superseded_action = action.supersede(now)
                await repositories.outbox.save(superseded_action, action.updated_at)
                if not still_valid:
                    await repositories.activity.append(
                        self._activity_factory.create(
                            user_id=action.user_id,
                            event_type=ActivityEventType.POLICY_DECIDED,
                            trace_id=action.outbox_id,
                            actor="outbox_dispatcher",
                            summary="Held Calendar action superseded: state changed while paused",
                            payload={"outbox_id": action.outbox_id},
                            created_at=now,
                        )
                    )
                    return None
                replacement_key = f"{action.action_idempotency_key}:epoch{controls.control_epoch}"
                replacement_id = CanonicalEncoder.hash(["outbox:v1", replacement_key])
                replacement = replace(
                    action,
                    outbox_id=replacement_id,
                    action_idempotency_key=replacement_key,
                    expected_control_epoch=controls.control_epoch,
                    dispatch_status=DispatchStatus.PENDING,
                    execution_status=ExecutionStatus.PENDING,
                    claim_token=None,
                    claim_lease_expires_at=None,
                    attempts=0,
                    mutation_response=None,
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
                created = await repositories.outbox.create(replacement)
                if not created:
                    return None
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=action.user_id,
                        event_type=ActivityEventType.OUTBOX_WRITTEN,
                        trace_id=action.outbox_id,
                        actor="outbox_dispatcher",
                        summary="Held Calendar action revalidated and reissued after resume",
                        payload={
                            "superseded_outbox_id": action.outbox_id,
                            "replacement_outbox_id": replacement_id,
                            "control_epoch": controls.control_epoch,
                        },
                        created_at=now,
                    )
                )
                return replacement_id

            replacement_id = await self._unit_of_work.run(_revalidate)
            superseded += 1
            if replacement_id is not None:
                replacements.append(replacement_id)
        for replacement_id in replacements:
            result = await self.dispatch(replacement_id)
            if result is not None:
                requeued += 1
        return OutboxDispatchSummary(
            scanned=len(held_actions),
            queued=requeued,
            held=0,
            superseded=superseded,
        )

    def _is_dispatch_eligible(self, action: ActionOutbox) -> bool:
        return (
            action.dispatch_status == DispatchStatus.PENDING
            and action.execution_status == ExecutionStatus.PENDING
        )

    def _task_for(self, action: ActionOutbox) -> ExecuteCalendarActionTaskV1:
        return ExecuteCalendarActionTaskV1(
            schema_version=self._task_schema_version,
            outbox_id=action.outbox_id,
            action_idempotency_key=action.action_idempotency_key,
            trace_id=action.outbox_id,
        )

    @staticmethod
    def _now_or(value: datetime | None, fallback: datetime) -> datetime:
        return value if value is not None else fallback
