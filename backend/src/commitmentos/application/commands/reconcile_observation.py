from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from commitmentos.application.dto import (
    CommandResult,
    CommandStatus,
    ReconciliationClaim,
    ReconciliationOutcome,
    ReconciliationRequest,
)
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.reconciliation_workflow import ReconciliationWorkflow
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.observations import (
    TERMINAL_RECONCILIATION_STATUSES,
    ReconciliationStatus,
)
from commitmentos.contracts.tasks import ReconcileObservationTaskV1
from commitmentos.domain.shared.types import CanonicalEncoder

PROCESSING_LEASE_SECONDS = 300

logger = logging.getLogger("commitmentos.reconciliation")


class ReconcileObservation:
    """Claim-or-hold envelope around one bounded reconciliation run.

    The claim transaction is the start boundary for monitoring pause: it reads
    `system_controls`, verifies the dispatch generation, and either moves the
    observation to `processing` with a fenced lease or durably holds it. No
    workflow code runs for a held observation.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        workflow: ReconciliationWorkflow,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._workflow = workflow
        self._clock = clock

    async def execute(self, task: ReconcileObservationTaskV1) -> CommandResult:
        claim_or_result = await self._claim_or_hold(task)
        if isinstance(claim_or_result, CommandResult):
            return claim_or_result
        claim = claim_or_result
        request = self._workflow_request(task, claim)
        try:
            outcome = await self._workflow.execute(request)
        except Exception as error:
            locations: list[str] = []
            traceback_cursor = error.__traceback__
            while traceback_cursor is not None:
                frame = traceback_cursor.tb_frame
                locations.append(f"{frame.f_code.co_name}:{traceback_cursor.tb_lineno}")
                traceback_cursor = traceback_cursor.tb_next
            logger.exception(
                "reconciliation workflow exception",
                extra={
                    "safe_payload": {
                        "error_type": type(error).__name__,
                        "location": " > ".join(locations[-8:]),
                    }
                },
            )
            await self._record_retryable_failure(claim, "workflow_exception")
            return CommandResult(
                status=CommandStatus.RETRYABLE_FAILURE,
                identifiers={"observation_id": claim.observation_id, "run_id": claim.run_id},
                revision=None,
                error_code="workflow_exception",
            )
        if outcome.retryable:
            await self._record_retryable_failure(claim, outcome.error_code or "retryable_failure")
            return CommandResult(
                status=CommandStatus.RETRYABLE_FAILURE,
                identifiers={"observation_id": claim.observation_id, "run_id": claim.run_id},
                revision=None,
                error_code=outcome.error_code,
            )
        await self._record_terminal_outcome(claim, outcome, task.workflow_version)
        return CommandResult(
            status=CommandStatus.COMPLETED,
            identifiers={
                "observation_id": claim.observation_id,
                "run_id": claim.run_id,
                "outcome_status": outcome.status,
            },
            revision=None,
            error_code=outcome.error_code,
        )

    async def _claim_or_hold(
        self,
        task: ReconcileObservationTaskV1,
    ) -> ReconciliationClaim | CommandResult:
        now = self._clock.now()
        worker_owner = f"reconcile-worker-{uuid.uuid4().hex[:12]}"

        async def _claim(repositories: RepositorySet) -> ReconciliationClaim | CommandResult:
            observation = await repositories.observations.get(task.observation_id)
            if observation is None:
                return CommandResult(
                    status=CommandStatus.TERMINAL_FAILURE,
                    identifiers={"observation_id": task.observation_id},
                    revision=None,
                    error_code="observation_not_found",
                )
            if observation.reconciliation_status in TERMINAL_RECONCILIATION_STATUSES:
                return CommandResult(
                    status=CommandStatus.NO_OP,
                    identifiers={"observation_id": task.observation_id},
                    revision=None,
                    error_code=None,
                )
            if task.dispatch_generation != observation.dispatch_generation:
                # A stale pre-pause task name; the current generation owns the work.
                return CommandResult(
                    status=CommandStatus.NO_OP,
                    identifiers={"observation_id": task.observation_id},
                    revision=None,
                    error_code="stale_dispatch_generation",
                )
            if observation.reconciliation_status == ReconciliationStatus.PROCESSING:
                lease_expiry = observation.processing_lease_expires_at
                if lease_expiry is not None and lease_expiry > now:
                    return CommandResult(
                        status=CommandStatus.RETRYABLE_FAILURE,
                        identifiers={"observation_id": task.observation_id},
                        revision=None,
                        error_code="processing_lease_active",
                    )
            controls = await repositories.system_controls.get(observation.user_id)
            if not controls.allows_observation_processing():
                if observation.reconciliation_status != ReconciliationStatus.HELD_BY_CONTROL:
                    await repositories.observations.set_reconciliation_status(
                        task.observation_id,
                        {
                            ReconciliationStatus.PENDING,
                            ReconciliationStatus.QUEUED,
                            ReconciliationStatus.RETRYABLE_FAILED,
                            ReconciliationStatus.PROCESSING,
                        },
                        ReconciliationStatus.HELD_BY_CONTROL,
                        observation.dispatch_generation,
                        controls.control_epoch,
                        None,
                        None,
                    )
                return CommandResult(
                    status=CommandStatus.HELD,
                    identifiers={"observation_id": task.observation_id},
                    revision=None,
                    error_code=None,
                )
            lease_key = f"reconciliation:{observation.user_id}"
            fence = await repositories.processing_leases.acquire(
                lease_key,
                worker_owner,
                now,
                now + timedelta(seconds=PROCESSING_LEASE_SECONDS),
            )
            if fence is None:
                return CommandResult(
                    status=CommandStatus.RETRYABLE_FAILURE,
                    identifiers={"observation_id": task.observation_id},
                    revision=None,
                    error_code="reconciliation_lease_unavailable",
                )
            claimed = await repositories.observations.set_reconciliation_status(
                task.observation_id,
                {
                    ReconciliationStatus.PENDING,
                    ReconciliationStatus.QUEUED,
                    ReconciliationStatus.RETRYABLE_FAILED,
                    ReconciliationStatus.PROCESSING,
                },
                ReconciliationStatus.PROCESSING,
                observation.dispatch_generation,
                controls.control_epoch,
                observation.processing_fencing_token,
                fence,
            )
            run_id = CanonicalEncoder.hash(
                ["reconciliation:v1", task.observation_id, task.workflow_version]
            )
            return ReconciliationClaim(
                observation_id=task.observation_id,
                user_id=claimed.user_id,
                run_id=run_id,
                dispatch_generation=claimed.dispatch_generation,
                claimed_control_epoch=controls.control_epoch,
                processing_fence=fence,
            )

        return await self._unit_of_work.run(_claim)

    def _workflow_request(
        self,
        task: ReconcileObservationTaskV1,
        claim: ReconciliationClaim,
    ) -> ReconciliationRequest:
        return ReconciliationRequest(
            run_id=claim.run_id,
            observation_id=claim.observation_id,
            user_id=claim.user_id,
            trace_id=task.trace_id,
            workflow_version=task.workflow_version,
            dispatch_generation=claim.dispatch_generation,
            claimed_control_epoch=claim.claimed_control_epoch,
            processing_fence=claim.processing_fence,
        )

    async def _record_retryable_failure(
        self,
        claim: ReconciliationClaim,
        error_code: str,
    ) -> None:
        async def _record(repositories: RepositorySet) -> None:
            await repositories.observations.set_reconciliation_status(
                claim.observation_id,
                {ReconciliationStatus.PROCESSING},
                ReconciliationStatus.RETRYABLE_FAILED,
                claim.dispatch_generation,
                None,
                claim.processing_fence.fencing_token,
                None,
            )
            await repositories.processing_leases.release(
                claim.processing_fence.lease_key,
                claim.processing_fence.owner,
                claim.processing_fence.fencing_token,
            )

        await self._unit_of_work.run(_record)

    async def _record_terminal_outcome(
        self,
        claim: ReconciliationClaim,
        outcome: ReconciliationOutcome,
        workflow_version: str,
    ) -> None:
        target = ReconciliationStatus(outcome.status)
        if target not in TERMINAL_RECONCILIATION_STATUSES:
            raise ValueError(f"workflow outcome status {outcome.status} is not terminal")

        async def _record(repositories: RepositorySet) -> None:
            await repositories.observations.set_reconciliation_status(
                claim.observation_id,
                {ReconciliationStatus.PROCESSING},
                target,
                claim.dispatch_generation,
                None,
                outcome.processing_fencing_token,
                None,
            )
            await repositories.reconciliation_runs.save(
                claim.run_id,
                {
                    "observation_id": claim.observation_id,
                    "user_id": claim.user_id,
                    "workflow_version": workflow_version,
                    "status": outcome.status,
                    "durable_outcome_ids": list(outcome.durable_outcome_ids),
                    "error_code": outcome.error_code,
                    "dispatch_generation": claim.dispatch_generation,
                    "claimed_control_epoch": claim.claimed_control_epoch,
                    "terminated_at": self._clock.now(),
                },
                outcome.processing_fencing_token,
            )
            await repositories.processing_leases.release(
                claim.processing_fence.lease_key,
                claim.processing_fence.owner,
                claim.processing_fence.fencing_token,
            )

        await self._unit_of_work.run(_record)
