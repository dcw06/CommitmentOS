from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from commitmentos.application.dto import ReconciliationOutcome, ReconciliationRequest
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.gmail_reader import GmailMessage, GmailReader
from commitmentos.application.ports.model_interpreter import (
    ModelInterpreter,
    ModelOutputParseError,
)
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.outbox_dispatcher import OutboxDispatcher
from commitmentos.application.services.portfolio_planning import PortfolioPlanningService
from commitmentos.application.services.projection_guard import (
    ProjectionGuard,
    projection_hash,
)
from commitmentos.contracts.model_output import (
    CommitmentSemanticProposalV1,
    ModelOutputValidator,
    ModelValidationDisposition,
    ProposalValidationResult,
    derive_semantic_fingerprint,
    derive_span_key,
    neutralize_untrusted_delimiters,
)
from commitmentos.contracts.observations import ObservationType, ObservationV1
from commitmentos.domain.actions.identity import (
    ActionIdempotencyKeyFactory,
    CalendarEventIdFactory,
)
from commitmentos.domain.actions.models import (
    ActionOutbox,
    CalendarActionType,
    CalendarMutation,
    DispatchStatus,
    ExecutionStatus,
)
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.commitments.identity import (
    CommitmentIdentityResolver,
    IdentityOperation,
    IdentityProposal,
    IdentityResolution,
)
from commitmentos.domain.commitments.models import (
    Commitment,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
    RiskLevel,
)
from commitmentos.domain.controls.models import ControlMode
from commitmentos.domain.planning.diff import PlanDiffer
from commitmentos.domain.planning.models import (
    PlanDiff,
    PlanMutation,
    PlanMutationType,
    PlannedWorkBlock,
    PlannerRunStatus,
    PortfolioPlan,
    TimeInterval,
)
from commitmentos.domain.planning.repair_policy import (
    RepairPolicyDecision,
    RepairPolicyDisposition,
    RepairPolicyEvaluator,
)
from commitmentos.domain.planning.risk import RiskCalculator
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)
from commitmentos.domain.progress.service import ProgressCalculator
from commitmentos.domain.shared.types import CanonicalEncoder


def derive_calendar_event_id(calendar_id: str, work_block_id: str) -> str:
    """Stable Calendar event identity from immutable work-block identity.

    Never contains a plan revision, action type, retry number, or scheduled
    time; plan revisions may change how the event is updated but never its
    identity (plan §9.4).
    """
    return CalendarEventIdFactory().derive(calendar_id, work_block_id)


class DurableReconciliationWorkflow:
    """Bounded, transaction-aware reconciliation controller.

    Production invokes this controller once inside the two-stage ADK boundary
    in ``graph.py``. The controller owns the real interpretation, identity,
    portfolio, policy, projection, and durable-outbox stages because their
    fencing and transaction boundaries span those steps. Calendar I/O remains
    in the separate executor. Seeded observations are only a test/demo input.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        outbox_dispatcher: OutboxDispatcher,
        clock: Clock,
        calendar_id: str,
        activity_factory: ActivityEventFactory | None = None,
        gmail_reader: GmailReader | None = None,
        model_interpreter: ModelInterpreter | None = None,
        output_validator: ModelOutputValidator | None = None,
        identity_resolver: CommitmentIdentityResolver | None = None,
        controlled_timezone: str = "UTC",
        controlled_email: str = "",
        portfolio_planning: PortfolioPlanningService | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._outbox_dispatcher = outbox_dispatcher
        self._clock = clock
        self._calendar_id = calendar_id
        self._activity_factory = activity_factory or ActivityEventFactory()
        self._gmail_reader = gmail_reader
        self._model_interpreter = model_interpreter
        self._output_validator = output_validator or ModelOutputValidator()
        self._identity_resolver = identity_resolver or CommitmentIdentityResolver()
        self._controlled_timezone = controlled_timezone
        self._controlled_email = controlled_email
        self._portfolio_planning = portfolio_planning
        self._event_ids = CalendarEventIdFactory()
        self._action_keys = ActionIdempotencyKeyFactory()
        self._plan_differ = PlanDiffer()
        self._repair_policy = RepairPolicyEvaluator()
        self._projection_guard = ProjectionGuard(
            ProgressCalculator(),
            RiskCalculator("portfolio-risk-v1"),
            "portfolio-risk-v1",
        )

    async def execute(self, request: ReconciliationRequest) -> ReconciliationOutcome:
        async def _load(repositories: RepositorySet) -> ObservationV1 | None:
            return await repositories.observations.get(request.observation_id)

        observation = await self._unit_of_work.read(_load)
        if observation is None:
            return self._outcome(request, "rejected", (), "observation_not_found")
        if observation.observation_type == ObservationType.GMAIL_MESSAGE_CHANGED:
            if isinstance(observation.safe_metadata.get("seeded_commitment"), Mapping):
                return await self._handle_seeded_gmail(request, observation)
            return await self._handle_gmail_observation(request, observation)
        if observation.observation_type == ObservationType.APPROVAL_RESOLVED:
            return await self._handle_approval_resolved(request, observation)
        if observation.observation_type == ObservationType.SYSTEM_CONTROL_CHANGED:
            return await self._handle_control_changed(request, observation)
        if observation.observation_type == ObservationType.WORK_CHECK_IN_RECORDED:
            return await self._handle_portfolio_replan(
                request,
                observation,
                "progress_changed",
            )
        if observation.observation_type == ObservationType.PLAN_UNDO_REQUESTED:
            return await self._handle_portfolio_replan(
                request,
                observation,
                "undo_requested",
            )
        if observation.observation_type == ObservationType.COMPLETION_CONFIRMED:
            return await self._handle_completion_confirmed(request, observation)
        if observation.observation_type in (
            ObservationType.COMMITMENT_PAUSED,
            ObservationType.COMMITMENT_DISMISSED,
        ):
            return await self._handle_commitment_suspended(request, observation)
        if observation.observation_type in (
            ObservationType.COMMITMENT_REOPENED,
            ObservationType.COMMITMENT_RESUMED,
            ObservationType.COMMITMENT_PRIORITY_CHANGED,
        ):
            lifecycle_reentry = observation.observation_type in (
                ObservationType.COMMITMENT_REOPENED,
                ObservationType.COMMITMENT_RESUMED,
            )
            repaired = await self._handle_calendar_repair(
                request,
                observation,
                previous_plan=(
                    await self._latest_published_plan(request.user_id)
                    if lifecycle_reentry
                    else None
                ),
                policy_override=lifecycle_reentry,
            )
            if repaired.error_code != "previous_plan_not_found":
                return repaired
            return await self._handle_portfolio_replan(
                request,
                observation,
                observation.observation_type.value,
            )
        if observation.observation_type == ObservationType.ACTION_RESULT:
            return await self._handle_action_result(request, observation)
        if observation.observation_type == ObservationType.CALENDAR_USER_MOVE_VALID:
            return await self._handle_valid_calendar_move(request, observation)
        if observation.observation_type == ObservationType.CALENDAR_USER_MOVE_INVALID:
            return await self._record_calendar_decision(
                request,
                observation,
                request_type="calendar_invalid_move_decision",
                options=("restore_approved_slot", "reschedule_safely", "pause_commitment"),
                event_type=ActivityEventType.USER_MOVE_REQUIRES_DECISION,
                summary="Calendar move violates hard constraints; a user choice is required",
            )
        if observation.observation_type == ObservationType.CALENDAR_USER_DELETION:
            return await self._record_calendar_decision(
                request,
                observation,
                request_type="calendar_user_deleted_decision",
                options=("reschedule_unfinished", "record_completed", "pause_commitment"),
                event_type=ActivityEventType.USER_DELETION_REQUIRES_DECISION,
                summary="Deleted work block requires one explicit user decision",
            )
        if observation.observation_type in (
            ObservationType.CALENDAR_ENVIRONMENTAL_DISRUPTION,
            ObservationType.CALENDAR_EVENT_CHANGED,
        ):
            return await self._handle_calendar_repair(request, observation)
        if observation.observation_type == ObservationType.CALENDAR_APP_ECHO:
            return self._outcome(request, "ignored", (), None)
        if observation.observation_type == ObservationType.PERIODIC_SAFETY:
            return await self._handle_periodic_safety(request, observation)
        return self._outcome(request, "ignored", (), None)

    async def _handle_periodic_safety(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        kind = str(observation.safe_metadata.get("kind") or "")
        if kind == "portfolio_refresh":
            return await self._handle_portfolio_replan(
                request,
                observation,
                str(observation.safe_metadata.get("reason") or "periodic_safety"),
            )
        if kind == "stale_planner_cleanup":
            planner_run_id = str(
                observation.safe_metadata.get("planner_run_id") or ""
            )
            now = self._clock.now()

            async def _stale(repositories: RepositorySet) -> tuple[str, ...]:
                plan = await repositories.planner_runs.get(planner_run_id)
                if plan is None:
                    return ()
                if plan.status == PlannerRunStatus.STALE:
                    return (planner_run_id,)
                if plan.status != PlannerRunStatus.CALCULATED:
                    return (planner_run_id,)
                stale = replace(
                    plan,
                    status=PlannerRunStatus.STALE,
                    stale_reason="periodic_unpublished_run_cleanup",
                )
                await repositories.planner_runs.save(
                    stale, PlannerRunStatus.CALCULATED.value
                )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.FAILURE_RECORDED,
                        trace_id=request.trace_id,
                        actor="periodic_safety",
                        summary="Unpublished planner run marked stale",
                        payload={
                            "failure_state": "projection_stale",
                            "planner_run_id": planner_run_id,
                            "stale_reason": stale.stale_reason,
                        },
                        created_at=now,
                    )
                )
                return (observation.observation_id, planner_run_id)

            durable = await self._unit_of_work.run_fenced(
                request.processing_fence, _stale
            )
            return self._outcome(request, "processed", durable, None)
        if kind == "calendar_drift_detected":
            now = self._clock.now()
            work_block_id = str(
                observation.safe_metadata.get("work_block_id") or ""
            )

            async def _record_drift(
                repositories: RepositorySet,
            ) -> tuple[str, ...]:
                block = await repositories.work_blocks.get(work_block_id)
                if (
                    block is None
                    or block.user_edit_state != UserEditState.NONE
                    or block.execution_state != WorkBlockExecutionState.PLANNED
                ):
                    return (work_block_id,) if block is not None else ()
                snapshot = await repositories.calendar_snapshots.get(
                    block.calendar_id, block.calendar_event_id
                )
                if snapshot is None or (
                    not snapshot.is_tombstone
                    and snapshot.observed_start == block.scheduled_start
                    and snapshot.observed_end == block.scheduled_end
                ):
                    return (work_block_id,)
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.FAILURE_RECORDED,
                        trace_id=request.trace_id,
                        actor="periodic_safety",
                        summary="Calendar snapshot differs from desired work-block state",
                        payload={
                            "failure_state": "calendar_drift_detected",
                            "source_observation_id": observation.observation_id,
                            "work_block_id": work_block_id,
                            "commitment_id": block.commitment_id,
                            "calendar_event_id": block.calendar_event_id,
                            "snapshot_tombstone": snapshot.is_tombstone,
                            "classification_owned_by": "calendar_sync_publication",
                            "calendar_sync_requested": True,
                        },
                        created_at=now,
                    )
                )
                return (observation.observation_id, work_block_id)

            durable = await self._unit_of_work.run_fenced(
                request.processing_fence, _record_drift
            )
            return self._outcome(request, "processed", durable, None)
        if kind != "work_block_lifecycle":
            return self._outcome(request, "ignored", (), "unknown_safety_kind")

        now = self._clock.now()
        work_block_id = str(observation.safe_metadata.get("work_block_id") or "")
        target_value = str(
            observation.safe_metadata.get("target_execution_state") or ""
        )
        try:
            target = WorkBlockExecutionState(target_value)
        except ValueError:
            return self._outcome(request, "rejected", (), "invalid_lifecycle_target")

        async def _transition(repositories: RepositorySet) -> tuple[str, ...]:
            block = await repositories.work_blocks.get(work_block_id)
            if block is None:
                return ()
            if block.execution_state == target:
                return (block.work_block_id,)
            if target == WorkBlockExecutionState.ACTIVE:
                if (
                    block.execution_state != WorkBlockExecutionState.PLANNED
                    or not (block.scheduled_start <= now < block.scheduled_end)
                ):
                    return (block.work_block_id,)
                updated = block.activate(now)
                event_type = ActivityEventType.WORK_BLOCK_ACTIVATED
                summary = "Scheduled work block is now active"
            elif target == WorkBlockExecutionState.AWAITING_CHECK_IN:
                if (
                    block.execution_state
                    not in (
                        WorkBlockExecutionState.PLANNED,
                        WorkBlockExecutionState.ACTIVE,
                    )
                    or now < block.scheduled_end
                ):
                    return (block.work_block_id,)
                updated = block.request_check_in(now)
                event_type = ActivityEventType.WORK_CHECK_IN_REQUIRED
                summary = "Elapsed work block needs a verified-minute check-in"
            else:
                return ()
            await repositories.work_blocks.save(updated, block.revision)

            commitment = await repositories.commitments.get(block.commitment_id)
            if (
                commitment is not None
                and commitment.lifecycle_status == LifecycleStatus.ACTIVE
            ):
                advanced = commitment.transition(LifecycleStatus.IN_PROGRESS, now)
                await repositories.commitments.save(advanced, commitment.revision)

            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=event_type,
                    trace_id=request.trace_id,
                    actor="periodic_safety",
                    summary=summary,
                    payload={
                        "failure_state": (
                            "work_check_in_required"
                            if target == WorkBlockExecutionState.AWAITING_CHECK_IN
                            else None
                        ),
                        "source_observation_id": observation.observation_id,
                        "work_block_id": updated.work_block_id,
                        "commitment_id": updated.commitment_id,
                        "previous_execution_state": block.execution_state.value,
                        "execution_state": updated.execution_state.value,
                        "verified_minutes": updated.verified_minutes,
                        "scheduled_start": updated.scheduled_start.isoformat(),
                        "scheduled_end": updated.scheduled_end.isoformat(),
                    },
                    created_at=now,
                )
            )
            return (observation.observation_id, updated.work_block_id)

        durable = await self._unit_of_work.run_fenced(
            request.processing_fence, _transition
        )
        if not durable:
            return self._outcome(request, "ignored", (), "lifecycle_fact_no_longer_due")
        if self._portfolio_planning is None:
            return self._outcome(request, "processed", durable, None)
        replanned = await self._handle_portfolio_replan(
            request, observation, "work_block_lifecycle_changed"
        )
        if replanned.status != "processed":
            return self._outcome(
                request,
                "processed",
                durable,
                replanned.error_code or "projection_refresh_deferred",
            )
        return self._outcome(
            request,
            replanned.status,
            tuple(dict.fromkeys((*durable, *replanned.durable_outcome_ids))),
            replanned.error_code,
        )

    # ------------------------------------------------------------------
    # Seeded Gmail observation -> commitment candidate + effort approval
    # ------------------------------------------------------------------

    async def _handle_seeded_gmail(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        seed = observation.safe_metadata.get("seeded_commitment")
        if not isinstance(seed, Mapping):
            return self._outcome(request, "ignored", (), "no_seeded_payload")
        now = self._clock.now()
        commitment_id = CanonicalEncoder.hash(
            ["commitment:v1", request.user_id, seed["source_thread_id"], seed["source_span_key"]]
        )

        async def _apply(repositories: RepositorySet) -> tuple[str, ...]:
            existing = await repositories.commitments.get(commitment_id)
            if existing is not None:
                # Replayed observation: converge without duplication.
                return (commitment_id,)
            deadline_value = datetime.fromisoformat(seed["deadline_value"])
            evidence_id = CanonicalEncoder.hash(
                ["evidence:v1", observation.observation_id, seed["source_span_key"]]
            )
            commitment = Commitment(
                commitment_id=commitment_id,
                user_id=request.user_id,
                revision=1,
                source_thread_id=seed["source_thread_id"],
                semantic_fingerprint=seed.get("semantic_fingerprint", ""),
                title=seed["title"],
                description=seed.get("description", ""),
                ownership_type=OwnershipType(seed["ownership_type"]),
                owner={"type": "user", "display_name": "Me"},
                beneficiary={"display_name": seed.get("beneficiary", "")},
                deadline=Deadline(
                    value=deadline_value,
                    timezone=seed["timezone"],
                    confidence=float(seed.get("deadline_confidence", 1.0)),
                    evidence_id=evidence_id,
                    source_expression=seed.get("deadline_expression", ""),
                    rule_version="seeded_v1",
                ),
                effort=Effort(
                    proposed_minutes=int(seed["proposed_effort_minutes"]),
                    confidence=float(seed.get("effort_confidence", 0.5)),
                    confirmed_minutes=None,
                    confirmed_at=None,
                ),
                lifecycle_status=LifecycleStatus.AWAITING_CONFIRMATION,
                completion_evidence_id=None,
                completed_at=None,
                plan_revision=0,
                projection=None,
                policy_profile="default_personal",
                created_at=now,
                updated_at=now,
                last_reconciled_at=now,
            )
            await repositories.commitments.save(commitment, None)
            await repositories.evidence.create(
                {
                    "evidence_id": evidence_id,
                    "commitment_id": commitment_id,
                    "source_reference": dict(observation.source_reference),
                    "excerpt": seed.get("evidence_excerpt", ""),
                    "confidence": float(seed.get("deadline_confidence", 1.0)),
                    "model_version": "seeded",
                    "schema_version": "seeded_v1",
                    "created_at": now,
                }
            )
            approval_id = self._approval_id(commitment_id, "effort_confirmation", 1)
            await repositories.approvals.create(
                {
                    "approval_id": approval_id,
                    "user_id": request.user_id,
                    "commitment_id": commitment_id,
                    "commitment_revision": 1,
                    "request_type": "effort_confirmation",
                    "payload": {"proposed_minutes": int(seed["proposed_effort_minutes"])},
                    "continuation_type": "effort_confirmation",
                    "policy_reason": "effort_must_be_confirmed_before_first_plan",
                    "status": "pending",
                    "revision": 1,
                    "created_at": now,
                    "expires_at": now + timedelta(days=7),
                }
            )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.INTERPRETATION_CREATED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary=f"Commitment candidate created: {seed['title']}",
                    payload={
                        "commitment_id": commitment_id,
                        "observation_id": observation.observation_id,
                        "ownership_type": seed["ownership_type"],
                    },
                    created_at=now,
                )
            )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.CONFIRMATION_RECORDED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Effort confirmation requested",
                    payload={"approval_id": approval_id, "commitment_id": commitment_id},
                    created_at=now,
                )
            )
            return (commitment_id, evidence_id, approval_id)

        durable_ids = await self._unit_of_work.run_fenced(request.processing_fence, _apply)
        return self._outcome(request, "processed", durable_ids, None)

    # ------------------------------------------------------------------
    # Real Gmail observation -> interpret / validate / resolve identity /
    # upsert evidence and commitment facts (Phase 2)
    # ------------------------------------------------------------------

    async def _handle_gmail_observation(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        if self._model_interpreter is None or self._gmail_reader is None:
            return self._outcome(request, "rejected", (), "interpretation_unavailable")
        thread_id = observation.source_reference.get("thread_id", "")
        if not thread_id:
            return self._outcome(request, "rejected", (), "missing_thread_reference")

        # Transient source context (§8.1): fetched per invocation, held in
        # memory only, never serialized into state, logs, or activity. A
        # failed fetch raises and surfaces as a visible retryable failure.
        messages = list(await self._gmail_reader.get_thread(request.user_id, thread_id))
        if not messages:
            return self._outcome(request, "rejected", (), "source_context_unavailable")

        async def _load_candidates(
            repositories: RepositorySet,
        ) -> tuple[list[Commitment], frozenset[str]]:
            candidates = list(
                await repositories.commitments.list_for_thread(request.user_id, thread_id)
            )
            dismissed: set[str] = set()
            for candidate in candidates:
                if candidate.lifecycle_status != LifecycleStatus.DISMISSED:
                    continue
                for evidence in await repositories.evidence.list_for_commitment(
                    candidate.commitment_id
                ):
                    span_key = evidence.get("span_key")
                    if not span_key:
                        message_id = (evidence.get("source_reference") or {}).get("message_id", "")
                        excerpt = evidence.get("excerpt", "")
                        if message_id and excerpt:
                            span_key = derive_span_key(message_id, excerpt)
                    if span_key:
                        dismissed.add(span_key)
            for dismissal in await repositories.source_span_dismissals.list_for_thread(
                request.user_id,
                thread_id,
            ):
                span_key = dismissal.get("source_span_key")
                if span_key:
                    dismissed.add(str(span_key))
            return candidates, frozenset(dismissed)

        candidates, dismissed_span_keys = await self._unit_of_work.read(_load_candidates)

        source_text = self._render_untrusted_thread(messages)
        candidate_context = [
            {
                "commitment_id": candidate.commitment_id,
                "ownership_type": candidate.ownership_type.value,
                "lifecycle_status": candidate.lifecycle_status.value,
                "deadline": candidate.deadline.value.isoformat(),
                "normalized_outcome": candidate.title,
            }
            for candidate in candidates
        ]
        try:
            result = await self._model_interpreter.interpret_commitment(
                source_text,
                {"timezone": self._controlled_timezone, "thread_id": thread_id},
                candidate_context,
            )
        except ModelOutputParseError as error:
            await self._record_interpretation_rejected(request, observation, error.error_codes)
            return self._outcome(request, "rejected", (), "model_output_schema_rejected")

        validation = self._output_validator.validate(
            result.interpretation,
            {message.message_id: message.body_text for message in messages},
            frozenset(candidate.commitment_id for candidate in candidates),
            deadline_timezone=self._controlled_timezone,
        )
        if validation.disposition == ModelValidationDisposition.REJECTED:
            await self._record_interpretation_rejected(request, observation, validation.error_codes)
            return self._outcome(request, "rejected", (), "interpretation_rejected")

        now = self._clock.now()
        metadata = result.metadata

        async def _apply(repositories: RepositorySet) -> tuple[str, ...]:
            durable: list[str] = []
            audit_operations: list[Mapping[str, Any]] = []
            for proposal_result in validation.proposals:
                proposal = proposal_result.proposal
                resolution = self._identity_resolver.resolve(
                    IdentityProposal(
                        operation=proposal.proposed_identity_operation,
                        target_commitment_id=proposal.target_commitment_id,
                        source_span_key=proposal.source_span_key,
                        semantic_fingerprint=derive_semantic_fingerprint(
                            proposal.ownership_type,
                            proposal.normalized_outcome,
                            proposal.beneficiary_display_name,
                        ),
                        ownership_type=proposal.ownership_type,
                        confidence=proposal.confidence,
                    ),
                    candidates,
                    dismissed_span_keys,
                )
                ids = await self._apply_identity_resolution(
                    repositories,
                    request,
                    observation,
                    proposal_result,
                    resolution,
                    now,
                )
                durable.extend(ids)
                audit_operations.append(
                    {
                        "source_span_key": proposal.source_span_key,
                        "proposed_operation": proposal.proposed_identity_operation.value,
                        "final_operation": resolution.operation.value,
                        "target_commitment_id": resolution.target_commitment_id,
                        "reason": resolution.reason,
                        "disposition": proposal_result.disposition.value,
                    }
                )
            # §9.6 step 6: candidate set, proposed and final operations, and
            # reasons all land in the audit timeline; model audit metadata is
            # sanitized (no source text).
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.INTERPRETATION_CREATED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary=(
                        f"Interpreted thread activity: {len(validation.proposals)} "
                        "proposal(s) resolved"
                    ),
                    payload={
                        "observation_id": observation.observation_id,
                        "thread_id": thread_id,
                        "candidate_ids": [c.commitment_id for c in candidates],
                        "operations": audit_operations,
                        "model_id": metadata.model_id,
                        "prompt_version": metadata.prompt_version,
                        "schema_version": metadata.schema_version,
                        "latency_ms": metadata.latency_ms,
                        "input_tokens": metadata.input_tokens,
                        "output_tokens": metadata.output_tokens,
                    },
                    created_at=now,
                )
            )
            return tuple(durable)

        durable_ids = await self._unit_of_work.run_fenced(request.processing_fence, _apply)
        return self._outcome(request, "processed", durable_ids, None)

    async def _apply_identity_resolution(
        self,
        repositories: RepositorySet,
        request: ReconciliationRequest,
        observation: ObservationV1,
        proposal_result: ProposalValidationResult,
        resolution: IdentityResolution,
        now: datetime,
    ) -> tuple[str, ...]:
        proposal = proposal_result.proposal
        # Deterministic dismissal suppression outranks model confidence and
        # ambiguity. An unchanged rejected span must never open another
        # confirmation request after later thread activity.
        if resolution.operation == IdentityOperation.IGNORE:
            return ()
        needs_confirmation = (
            proposal_result.disposition == ModelValidationDisposition.CONFIRMATION_REQUIRED
            or resolution.operation == IdentityOperation.AMBIGUOUS
        )
        if needs_confirmation:
            return await self._record_identity_confirmation(
                repositories, request, observation, proposal, resolution, now
            )
        if resolution.operation == IdentityOperation.CREATE:
            return await self._create_commitment_from_proposal(
                repositories, request, observation, proposal, now
            )
        if resolution.operation == IdentityOperation.UPDATE_EXISTING:
            return await self._update_commitment_from_proposal(
                repositories, request, observation, proposal, resolution, now
            )
        if resolution.operation == IdentityOperation.SUPERSEDE:
            canceled = await self._cancel_superseded_target(repositories, request, resolution, now)
            created = await self._create_commitment_from_proposal(
                repositories, request, observation, proposal, now
            )
            return canceled + created
        return ()

    async def _create_commitment_from_proposal(
        self,
        repositories: RepositorySet,
        request: ReconciliationRequest,
        observation: ObservationV1,
        proposal: CommitmentSemanticProposalV1,
        now: datetime,
    ) -> tuple[str, ...]:
        thread_id = observation.source_reference.get("thread_id", "")
        commitment_id = CanonicalEncoder.hash(
            ["commitment:v1", request.user_id, thread_id, proposal.source_span_key]
        )
        if await repositories.commitments.get(commitment_id) is not None:
            # Replayed observation: converge without duplication.
            return (commitment_id,)
        if proposal.deadline is None or proposal.deadline.proposed_value is None:
            # Accepted proposals always carry a deadline value; anything else
            # was already routed to confirmation by the validator.
            return await self._record_identity_confirmation(
                repositories,
                request,
                observation,
                proposal,
                IdentityResolution(IdentityOperation.AMBIGUOUS, None, (), "missing_deadline"),
                now,
            )
        evidence_ids = await self._upsert_evidence(
            repositories, observation, commitment_id, proposal, now
        )
        actionable = proposal.ownership_type == OwnershipType.MY_COMMITMENT
        commitment = Commitment(
            commitment_id=commitment_id,
            user_id=request.user_id,
            revision=1,
            source_thread_id=thread_id,
            semantic_fingerprint=derive_semantic_fingerprint(
                proposal.ownership_type,
                proposal.normalized_outcome,
                proposal.beneficiary_display_name,
            ),
            title=proposal.normalized_outcome,
            description=proposal.description,
            ownership_type=proposal.ownership_type,
            owner={
                "type": "user" if actionable else "counterparty",
                "display_name": proposal.owner_display_name or "Me",
            },
            beneficiary={"display_name": proposal.beneficiary_display_name or ""},
            deadline=Deadline(
                value=datetime.fromisoformat(proposal.deadline.proposed_value),
                timezone=self._controlled_timezone,
                confidence=proposal.deadline.confidence,
                evidence_id=evidence_ids[0],
                source_expression=proposal.deadline.source_expression,
                rule_version="deadline_normalization_v1",
            ),
            effort=Effort(
                proposed_minutes=proposal.proposed_effort_minutes,
                confidence=proposal.confidence,
                confirmed_minutes=None,
                confirmed_at=None,
            ),
            lifecycle_status=(
                LifecycleStatus.AWAITING_CONFIRMATION if actionable else LifecycleStatus.CANDIDATE
            ),
            completion_evidence_id=None,
            completed_at=None,
            plan_revision=0,
            projection=None,
            policy_profile="default_personal",
            created_at=now,
            updated_at=now,
            last_reconciled_at=now,
        )
        await repositories.commitments.save(commitment, None)
        durable: list[str] = [commitment_id, *evidence_ids]
        await repositories.activity.append(
            self._activity_factory.create(
                user_id=request.user_id,
                event_type=ActivityEventType.INTERPRETATION_CREATED,
                trace_id=request.trace_id,
                actor="reconciliation",
                summary=f"Commitment candidate created: {proposal.normalized_outcome}",
                payload={
                    "commitment_id": commitment_id,
                    "observation_id": observation.observation_id,
                    "ownership_type": proposal.ownership_type.value,
                    "source_span_key": proposal.source_span_key,
                },
                created_at=now,
            )
        )
        if actionable:
            approval_id = self._approval_id(commitment_id, "effort_confirmation", 1)
            if await repositories.approvals.get(approval_id) is None:
                await repositories.approvals.create(
                    {
                        "approval_id": approval_id,
                        "user_id": request.user_id,
                        "commitment_id": commitment_id,
                        "commitment_revision": 1,
                        "request_type": "effort_confirmation",
                        "payload": {"proposed_minutes": proposal.proposed_effort_minutes},
                        "continuation_type": "effort_confirmation",
                        "policy_reason": "effort_must_be_confirmed_before_first_plan",
                        "status": "pending",
                        "revision": 1,
                        "created_at": now,
                        "expires_at": now + timedelta(days=7),
                    }
                )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.CONFIRMATION_RECORDED,
                        trace_id=request.trace_id,
                        actor="reconciliation",
                        summary="Effort confirmation requested",
                        payload={
                            "approval_id": approval_id,
                            "commitment_id": commitment_id,
                        },
                        created_at=now,
                    )
                )
                durable.append(approval_id)
        return tuple(durable)

    async def _update_commitment_from_proposal(
        self,
        repositories: RepositorySet,
        request: ReconciliationRequest,
        observation: ObservationV1,
        proposal: CommitmentSemanticProposalV1,
        resolution: IdentityResolution,
        now: datetime,
    ) -> tuple[str, ...]:
        target = await repositories.commitments.get(resolution.target_commitment_id or "")
        if target is None:
            return ()
        evidence_ids = await self._upsert_evidence(
            repositories, observation, target.commitment_id, proposal, now
        )
        changes: dict[str, Any] = {}
        updated = target
        if proposal.deadline is not None and proposal.deadline.proposed_value is not None:
            proposed_value = datetime.fromisoformat(proposal.deadline.proposed_value)
            if proposed_value != target.deadline.value:
                changes["deadline"] = {
                    "previous": target.deadline.value.isoformat(),
                    "revised": proposed_value.isoformat(),
                    "source_expression": proposal.deadline.source_expression,
                }
                updated = replace(
                    updated,
                    deadline=Deadline(
                        value=proposed_value,
                        timezone=self._controlled_timezone,
                        confidence=proposal.deadline.confidence,
                        evidence_id=evidence_ids[0],
                        source_expression=proposal.deadline.source_expression,
                        rule_version="deadline_normalization_v1",
                    ),
                )
        if (
            target.ownership_type == OwnershipType.REQUEST_TO_ME
            and proposal.ownership_type == OwnershipType.MY_COMMITMENT
        ):
            changes["ownership_type"] = {
                "previous": target.ownership_type.value,
                "revised": proposal.ownership_type.value,
            }
            updated = replace(updated, ownership_type=proposal.ownership_type)
            if target.lifecycle_status == LifecycleStatus.CANDIDATE:
                updated = replace(updated, lifecycle_status=LifecycleStatus.AWAITING_CONFIRMATION)
            if (
                updated.effort.proposed_minutes is None
                and proposal.proposed_effort_minutes is not None
            ):
                updated = replace(
                    updated,
                    effort=Effort(
                        proposed_minutes=proposal.proposed_effort_minutes,
                        confidence=proposal.confidence,
                        confirmed_minutes=None,
                        confirmed_at=None,
                    ),
                )
        if not changes:
            # A pure restatement adds evidence but changes no authoritative
            # fact; it still restores the pending-confirmation invariant so a
            # superseded effort request cannot leave the commitment stuck.
            reissued = await self._ensure_effort_confirmation_requested(
                repositories, request, target, now
            )
            return (target.commitment_id, *evidence_ids, *reissued)
        updated = replace(
            updated,
            revision=target.revision + 1,
            updated_at=now,
            last_reconciled_at=now,
        )
        await repositories.commitments.save(updated, target.revision)
        durable: list[str] = [target.commitment_id, *evidence_ids]
        await repositories.activity.append(
            self._activity_factory.create(
                user_id=request.user_id,
                event_type=ActivityEventType.INTERPRETATION_CREATED,
                trace_id=request.trace_id,
                actor="reconciliation",
                summary=f"Commitment revised from new evidence: {target.title}",
                payload={
                    "commitment_id": target.commitment_id,
                    "observation_id": observation.observation_id,
                    "operation": "update_existing",
                    "changes": changes,
                    "revision": updated.revision,
                },
                created_at=now,
            )
        )
        durable.extend(
            await self._ensure_effort_confirmation_requested(repositories, request, updated, now)
        )
        return tuple(durable)

    async def _ensure_effort_confirmation_requested(
        self,
        repositories: RepositorySet,
        request: ReconciliationRequest,
        commitment: Commitment,
        now: datetime,
    ) -> tuple[str, ...]:
        """Restore the awaiting-confirmation invariant (golden audit step 3).

        A my_commitment awaiting confirmation with unconfirmed effort must
        hold exactly one pending effort_confirmation approval carrying the
        current commitment revision. A commitment revision therefore
        supersedes stale pending requests and reissues against the new
        revision; a later observation restores the invariant if a stale
        request was superseded outside reconciliation.
        """
        if (
            commitment.ownership_type != OwnershipType.MY_COMMITMENT
            or commitment.lifecycle_status != LifecycleStatus.AWAITING_CONFIRMATION
            or commitment.effort.confirmed_minutes is not None
        ):
            return ()
        durable: list[str] = []
        current_id = self._approval_id(
            commitment.commitment_id, "effort_confirmation", commitment.revision
        )
        for stale_revision in range(1, commitment.revision):
            stale_id = self._approval_id(
                commitment.commitment_id, "effort_confirmation", stale_revision
            )
            if stale_id == current_id:
                continue
            stale = await repositories.approvals.get(stale_id)
            if stale is None or stale.get("status") != "pending":
                continue
            await repositories.approvals.resolve(
                stale_id,
                stale["revision"],
                {
                    "status": "superseded",
                    "reason": "commitment_revision_changed",
                    "actor": "reconciliation",
                },
            )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.CONFIRMATION_RECORDED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary=(
                        "Effort confirmation superseded; reissued against the revised commitment"
                    ),
                    payload={
                        "approval_id": stale_id,
                        "commitment_id": commitment.commitment_id,
                        "stale_commitment_revision": stale.get("commitment_revision"),
                        "current_commitment_revision": commitment.revision,
                    },
                    created_at=now,
                )
            )
            durable.append(stale_id)
        if await repositories.approvals.get(current_id) is None:
            await repositories.approvals.create(
                {
                    "approval_id": current_id,
                    "user_id": request.user_id,
                    "commitment_id": commitment.commitment_id,
                    "commitment_revision": commitment.revision,
                    "request_type": "effort_confirmation",
                    "payload": {"proposed_minutes": commitment.effort.proposed_minutes},
                    "continuation_type": "effort_confirmation",
                    "policy_reason": "effort_must_be_confirmed_before_first_plan",
                    "status": "pending",
                    "revision": 1,
                    "created_at": now,
                    "expires_at": now + timedelta(days=7),
                }
            )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.CONFIRMATION_RECORDED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Effort confirmation requested",
                    payload={
                        "approval_id": current_id,
                        "commitment_id": commitment.commitment_id,
                    },
                    created_at=now,
                )
            )
            durable.append(current_id)
        return tuple(durable)

    async def _cancel_superseded_target(
        self,
        repositories: RepositorySet,
        request: ReconciliationRequest,
        resolution: IdentityResolution,
        now: datetime,
    ) -> tuple[str, ...]:
        target = await repositories.commitments.get(resolution.target_commitment_id or "")
        if target is None or target.lifecycle_status == LifecycleStatus.CANCELED:
            return ()
        updated = replace(
            target,
            lifecycle_status=LifecycleStatus.CANCELED,
            revision=target.revision + 1,
            updated_at=now,
        )
        await repositories.commitments.save(updated, target.revision)
        await repositories.activity.append(
            self._activity_factory.create(
                user_id=request.user_id,
                event_type=ActivityEventType.INTERPRETATION_CREATED,
                trace_id=request.trace_id,
                actor="reconciliation",
                summary=f"Commitment superseded: {target.title}",
                payload={
                    "commitment_id": target.commitment_id,
                    "operation": "supersede",
                    "reason": resolution.reason,
                },
                created_at=now,
            )
        )
        return (target.commitment_id,)

    async def _record_identity_confirmation(
        self,
        repositories: RepositorySet,
        request: ReconciliationRequest,
        observation: ObservationV1,
        proposal: CommitmentSemanticProposalV1,
        resolution: IdentityResolution,
        now: datetime,
    ) -> tuple[str, ...]:
        approval_id = CanonicalEncoder.hash(
            [
                "approval:v1",
                observation.observation_id,
                "identity_confirmation",
                proposal.source_span_key,
            ]
        )
        if await repositories.approvals.get(approval_id) is not None:
            return (approval_id,)
        target = None
        if resolution.target_commitment_id:
            target = await repositories.commitments.get(resolution.target_commitment_id)
        await repositories.approvals.create(
            {
                "approval_id": approval_id,
                "user_id": request.user_id,
                "commitment_id": resolution.target_commitment_id or "",
                "commitment_revision": target.revision if target is not None else 0,
                "request_type": "identity_confirmation",
                "payload": {
                    # Keep the compact Phase 2 dashboard fields while also
                    # storing the full safe proposal needed by this durable
                    # continuation.
                    "source_span_key": proposal.source_span_key,
                    "normalized_outcome": proposal.normalized_outcome,
                    "ownership_type": proposal.ownership_type.value,
                    "proposed_operation": proposal.proposed_identity_operation.value,
                    "reason": resolution.reason,
                    "source_observation_id": observation.observation_id,
                    "source_reference": dict(observation.source_reference),
                    "resolution_reason": resolution.reason,
                    "resolution_target_commitment_id": resolution.target_commitment_id,
                    "proposal": self._proposal_to_document(proposal),
                },
                "continuation_type": "identity_confirmation",
                "policy_reason": "uncertain_interpretation_requires_confirmation",
                "status": "pending",
                "revision": 1,
                "created_at": now,
                "expires_at": now + timedelta(days=7),
            }
        )
        await repositories.activity.append(
            self._activity_factory.create(
                user_id=request.user_id,
                event_type=ActivityEventType.CONFIRMATION_RECORDED,
                trace_id=request.trace_id,
                actor="reconciliation",
                summary="Interpretation requires confirmation",
                payload={
                    "approval_id": approval_id,
                    "observation_id": observation.observation_id,
                    "reason": resolution.reason,
                },
                created_at=now,
            )
        )
        return (approval_id,)

    async def _upsert_evidence(
        self,
        repositories: RepositorySet,
        observation: ObservationV1,
        commitment_id: str,
        proposal: CommitmentSemanticProposalV1,
        now: datetime,
    ) -> tuple[str, ...]:
        evidence_ids: list[str] = []
        for span in proposal.evidence:
            evidence_id = CanonicalEncoder.hash(
                ["evidence:v1", observation.observation_id, span.span_key]
            )
            if await repositories.evidence.get(evidence_id) is None:
                await repositories.evidence.create(
                    {
                        "evidence_id": evidence_id,
                        "commitment_id": commitment_id,
                        "source_reference": {
                            "thread_id": observation.source_reference.get("thread_id", ""),
                            "message_id": span.message_id,
                        },
                        "span_key": span.span_key,
                        "excerpt": span.excerpt,
                        "confidence": proposal.confidence,
                        "model_version": "extraction_v2",
                        "schema_version": "extraction_v2",
                        "created_at": now,
                    }
                )
            evidence_ids.append(evidence_id)
        return tuple(evidence_ids)

    async def _record_interpretation_rejected(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
        error_codes: tuple[str, ...],
    ) -> None:
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> None:
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.INTERPRETATION_REJECTED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Model output rejected by deterministic validation",
                    payload={
                        "observation_id": observation.observation_id,
                        "error_codes": list(error_codes)[:20],
                    },
                    created_at=now,
                )
            )

        await self._unit_of_work.run_fenced(request.processing_fence, _record)

    def _render_untrusted_thread(self, messages: list[GmailMessage]) -> str:
        rendered = []
        for message in messages:
            direction = "outbound" if "SENT" in message.label_ids else "inbound"
            sender = neutralize_untrusted_delimiters(message.headers.get("from", ""))
            if self._controlled_email and self._controlled_email in sender:
                sender = "Controlled User"
            subject = neutralize_untrusted_delimiters(message.headers.get("subject", ""))
            body = neutralize_untrusted_delimiters(message.body_text)
            rendered.append(
                f'<message id="{message.message_id}" direction="{direction}" '
                f'from="{sender}" sent_at="{message.internal_date.isoformat()}" '
                f'subject="{subject}">\n'
                f"{body}\n</message>"
            )
        return "\n".join(rendered)

    # ------------------------------------------------------------------
    # Calendar truth -> stable repair or one explicit decision
    # ------------------------------------------------------------------

    async def _previous_portfolio_plan(self, user_id: str) -> PortfolioPlan | None:
        async def _load(repositories: RepositorySet) -> PortfolioPlan | None:
            candidates: dict[str, PortfolioPlan] = {}
            for commitment in await repositories.commitments.list_active(user_id):
                planner_run_id = (
                    commitment.projection.planner_run_id
                    if commitment.projection is not None
                    else ""
                )
                if not planner_run_id or planner_run_id in candidates:
                    continue
                plan = await repositories.planner_runs.get(planner_run_id)
                if plan is not None and plan.status == PlannerRunStatus.PUBLISHED:
                    candidates[planner_run_id] = plan
            return max(
                candidates.values(),
                key=lambda plan: (plan.calculated_at, plan.planner_run_id),
                default=None,
            )

        return await self._unit_of_work.read(_load)

    async def _latest_published_plan(self, user_id: str) -> PortfolioPlan | None:
        async def _load(repositories: RepositorySet) -> PortfolioPlan | None:
            plans = await repositories.planner_runs.list_for_user(
                user_id,
                PlannerRunStatus.PUBLISHED.value,
                25,
            )
            return max(
                plans,
                key=lambda plan: (plan.calculated_at, plan.planner_run_id),
                default=None,
            )

        return await self._unit_of_work.read(_load)

    async def _handle_valid_calendar_move(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        work_block_id = str(observation.safe_metadata.get("work_block_id") or "")
        commitment_id = str(observation.safe_metadata.get("commitment_id") or "")
        start_value = str(observation.safe_metadata.get("observed_start") or "")
        end_value = str(observation.safe_metadata.get("observed_end") or "")
        if not work_block_id or not commitment_id or not start_value or not end_value:
            return self._outcome(request, "rejected", (), "calendar_move_payload_invalid")
        previous_plan = await self._previous_portfolio_plan(request.user_id)
        if previous_plan is None:
            return self._outcome(request, "rejected", (), "previous_plan_not_found")
        start = datetime.fromisoformat(start_value)
        end = datetime.fromisoformat(end_value)
        now = self._clock.now()

        async def _adopt(repositories: RepositorySet) -> tuple[str, ...]:
            block = await repositories.work_blocks.get(work_block_id)
            commitment = await repositories.commitments.get(commitment_id)
            if block is None or commitment is None or commitment.user_id != request.user_id:
                return ()
            if (
                block.user_edit_state == UserEditState.ADOPTED
                and block.scheduled_start == start
                and block.scheduled_end == end
            ):
                return (work_block_id, commitment_id)
            adopted = block.adopt_manual_move(start, end, now)
            updated_commitment = replace(
                commitment,
                revision=commitment.revision + 1,
                plan_revision=commitment.plan_revision + 1,
                updated_at=now,
            )
            adopted = replace(adopted, plan_revision=updated_commitment.plan_revision)
            await repositories.work_blocks.save(adopted, block.revision)
            await repositories.commitments.save(updated_commitment, commitment.revision)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.USER_MOVE_ADOPTED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Valid Calendar move adopted as authoritative plan truth",
                    payload={
                        "observation_id": observation.observation_id,
                        "commitment_id": commitment_id,
                        "work_block_id": work_block_id,
                        "observed_start": start.isoformat(),
                        "observed_end": end.isoformat(),
                        "plan_revision": updated_commitment.plan_revision,
                    },
                    created_at=now,
                )
            )
            return (work_block_id, commitment_id)

        adopted_ids = await self._unit_of_work.run_fenced(
            request.processing_fence,
            _adopt,
        )
        if not adopted_ids:
            return self._outcome(request, "rejected", (), "calendar_move_target_not_found")
        return await self._handle_calendar_repair(
            request,
            observation,
            previous_plan=previous_plan,
            adopted_work_block_ids=(work_block_id,),
            policy_override=True,
        )

    async def _record_calendar_decision(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
        *,
        request_type: str,
        options: tuple[str, ...],
        event_type: ActivityEventType,
        summary: str,
    ) -> ReconciliationOutcome:
        work_block_id = str(observation.safe_metadata.get("work_block_id") or "")
        commitment_id = str(observation.safe_metadata.get("commitment_id") or "")
        if not work_block_id or not commitment_id:
            return self._outcome(request, "rejected", (), "calendar_decision_target_missing")
        approval_id = CanonicalEncoder.hash(
            ["calendar-decision:v1", request_type, observation.observation_id]
        )
        now = self._clock.now()

        async def _record(repositories: RepositorySet) -> tuple[str, ...]:
            existing_approval = await repositories.approvals.get(approval_id)
            if existing_approval is not None:
                return (work_block_id, approval_id)
            block = await repositories.work_blocks.get(work_block_id)
            commitment = await repositories.commitments.get(commitment_id)
            if block is None or commitment is None or commitment.user_id != request.user_id:
                return ()
            target_state = (
                UserEditState.INVALID_MOVE
                if request_type == "calendar_invalid_move_decision"
                else UserEditState.USER_DELETED
            )
            updated_block = block
            if block.user_edit_state != target_state:
                updated_block = (
                    block.mark_invalid_move(now)
                    if target_state == UserEditState.INVALID_MOVE
                    else block.mark_user_deleted(now)
                )
                await repositories.work_blocks.save(updated_block, block.revision)
            await repositories.approvals.create(
                {
                    "approval_id": approval_id,
                    "user_id": request.user_id,
                    "commitment_id": commitment_id,
                    "commitment_revision": commitment.revision,
                    "request_type": request_type,
                    "payload": {
                        "source_observation_id": observation.observation_id,
                        "work_block_id": work_block_id,
                        "expected_work_block_revision": updated_block.revision,
                        "previous_start": observation.safe_metadata.get("previous_start", ""),
                        "previous_end": observation.safe_metadata.get("previous_end", ""),
                        "observed_start": observation.safe_metadata.get("observed_start", ""),
                        "observed_end": observation.safe_metadata.get("observed_end", ""),
                        "options": list(options),
                    },
                    "continuation_type": request_type,
                    "policy_reason": "explicit_calendar_edit_requires_choice",
                    "status": "pending",
                    "revision": 1,
                    "created_at": now,
                    "expires_at": now + timedelta(days=7),
                }
            )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=event_type,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary=summary,
                    payload={
                        "observation_id": observation.observation_id,
                        "approval_id": approval_id,
                        "commitment_id": commitment_id,
                        "work_block_id": work_block_id,
                        "options": list(options),
                        "calendar_action_created": False,
                    },
                    created_at=now,
                )
            )
            return (work_block_id, approval_id)

        durable = await self._unit_of_work.run_fenced(request.processing_fence, _record)
        if not durable:
            return self._outcome(request, "rejected", (), "calendar_decision_target_not_found")
        return self._outcome(request, "processed", durable, None)

    async def _handle_calendar_repair(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
        *,
        previous_plan: PortfolioPlan | None = None,
        adopted_work_block_ids: tuple[str, ...] = (),
        recreate_work_block_ids: tuple[str, ...] = (),
        policy_override: bool = False,
    ) -> ReconciliationOutcome:
        planning = self._portfolio_planning
        if planning is None:
            return self._outcome(request, "rejected", (), "portfolio_planner_unavailable")
        previous = previous_plan or await self._previous_portfolio_plan(request.user_id)
        if previous is None:
            return self._outcome(request, "ignored", (), "previous_plan_not_found")
        include_id = str(observation.safe_metadata.get("commitment_id") or "")
        for _attempt in range(3):
            repaired, preferences = await planning.calculate_repair(
                request.user_id,
                previous,
                (include_id,) if include_id else (),
            )
            if not await planning.calendar_inputs_match(repaired):
                continue
            plan_diff = self._plan_differ.diff(previous, repaired)
            if adopted_work_block_ids:
                plan_diff = self._filter_plan_diff(
                    plan_diff,
                    set(adopted_work_block_ids),
                )
            plan_diff, stale_resume = await self._add_snapshot_resume_mutation(
                observation,
                repaired,
                plan_diff,
            )
            if recreate_work_block_ids:
                plan_diff = self._add_recreate_mutations(
                    repaired,
                    plan_diff,
                    set(recreate_work_block_ids),
                )
            ownership_valid = self._plan_differ.validate_owned_targets(
                plan_diff,
                {block.work_block_id for block in previous.work_blocks},
            )
            policy = self._repair_policy.evaluate(
                plan_diff,
                repaired,
                preferences,
                ownership_valid=ownership_valid,
            )
            if (
                policy.disposition != RepairPolicyDisposition.FORBIDDEN
                and (policy_override or stale_resume)
            ):
                policy = replace(
                    policy,
                    disposition=RepairPolicyDisposition.AUTOMATIC,
                    reason_codes=(),
                )
            result = await self._publish_or_apply_repair(
                request,
                observation,
                previous,
                repaired,
                plan_diff,
                policy,
                adopted_work_block_ids,
            )
            if result.error_code != "repair_inputs_stale":
                return result
        return self._outcome(request, "rejected", (), "repair_inputs_kept_changing")

    async def _resume_approved_calendar_repair(
        self,
        request: ReconciliationRequest,
        approval: Mapping[str, Any],
    ) -> ReconciliationOutcome:
        source_observation_id = str(
            approval.get("payload", {}).get("source_observation_id") or ""
        )

        async def _load(repositories: RepositorySet) -> ObservationV1 | None:
            return await repositories.observations.get(source_observation_id)

        source = await self._unit_of_work.read(_load)
        if source is None:
            return self._outcome(request, "rejected", (), "repair_observation_not_found")
        return await self._handle_calendar_repair(
            request,
            source,
            policy_override=True,
        )

    async def _apply_calendar_decision(
        self,
        request: ReconciliationRequest,
        approval: Mapping[str, Any],
    ) -> ReconciliationOutcome:
        payload = approval.get("payload", {})
        decision_payload = approval.get("decision", {}).get("payload", {})
        choice = str(decision_payload.get("choice") or "")
        options = tuple(str(item) for item in payload.get("options", ()))
        if choice not in options:
            return self._outcome(request, "rejected", (), "calendar_decision_choice_invalid")
        source_observation_id = str(payload.get("source_observation_id") or "")
        work_block_id = str(payload.get("work_block_id") or "")
        commitment_id = str(approval.get("commitment_id") or "")
        now = self._clock.now()
        if choice == "pause_commitment":
            async def _pause(repositories: RepositorySet) -> tuple[str, ...]:
                commitment = await repositories.commitments.get(commitment_id)
                if commitment is None:
                    return ()
                paused = commitment.transition(LifecycleStatus.PAUSED, now)
                await repositories.commitments.save(paused, commitment.revision)
                return (commitment_id, work_block_id)

            durable = await self._unit_of_work.run_fenced(request.processing_fence, _pause)
            return self._outcome(request, "processed", durable, None)
        if choice == "record_completed":
            async def _request_check_in(repositories: RepositorySet) -> tuple[str, ...]:
                block = await repositories.work_blocks.get(work_block_id)
                if block is None:
                    return ()
                awaiting = block.request_check_in(now)
                await repositories.work_blocks.save(awaiting, block.revision)
                return (work_block_id,)

            durable = await self._unit_of_work.run_fenced(
                request.processing_fence,
                _request_check_in,
            )
            return self._outcome(request, "processed", durable, None)

        async def _load(repositories: RepositorySet) -> ObservationV1 | None:
            return await repositories.observations.get(source_observation_id)

        source = await self._unit_of_work.read(_load)
        if source is None:
            return self._outcome(request, "rejected", (), "calendar_decision_source_missing")
        if choice == "restore_approved_slot":
            return await self._restore_approved_calendar_slot(
                request,
                source,
                work_block_id,
                commitment_id,
            )
        return await self._handle_calendar_repair(
            request,
            source,
            recreate_work_block_ids=(
                (work_block_id,)
                if approval.get("request_type") == "calendar_user_deleted_decision"
                else ()
            ),
            policy_override=True,
        )

    async def _restore_approved_calendar_slot(
        self,
        request: ReconciliationRequest,
        source: ObservationV1,
        work_block_id: str,
        commitment_id: str,
    ) -> ReconciliationOutcome:
        """Turn an explicit restore choice into one fenced conditional patch."""
        now = self._clock.now()

        async def _restore(repositories: RepositorySet) -> tuple[str, ...]:
            block = await repositories.work_blocks.get(work_block_id)
            commitment = await repositories.commitments.get(commitment_id)
            if (
                block is None
                or commitment is None
                or commitment.user_id != request.user_id
                or block.execution_state != WorkBlockExecutionState.PLANNED
            ):
                return ()
            snapshot = await repositories.calendar_snapshots.get(
                block.calendar_id, block.calendar_event_id
            )
            if snapshot is None or snapshot.observed_event_etag is None:
                return ()
            projection = commitment.projection
            if projection is None:
                return ()
            plan = await repositories.planner_runs.get(projection.planner_run_id)
            if plan is None or plan.status != PlannerRunStatus.PUBLISHED:
                return ()

            updated_commitment = replace(
                commitment,
                revision=commitment.revision + 1,
                plan_revision=commitment.plan_revision + 1,
                updated_at=now,
            )
            restored_block = replace(
                block,
                revision=block.revision + 1,
                user_edit_state=UserEditState.NONE,
                plan_revision=updated_commitment.plan_revision,
            )
            blocks = list(await repositories.work_blocks.list_for_commitment(commitment_id))
            resulting_blocks = tuple(
                restored_block if item.work_block_id == work_block_id else item
                for item in blocks
            )
            rebuilt = self._projection_guard.rebuild(
                updated_commitment,
                resulting_blocks,
                plan,
                now,
            )
            updated_commitment = updated_commitment.with_projection(rebuilt, now)
            await repositories.work_blocks.save(restored_block, block.revision)
            await repositories.commitments.save(updated_commitment, commitment.revision)

            controls = await repositories.system_controls.get(request.user_id)
            action_key = self._action_keys.derive(
                commitment_id,
                updated_commitment.plan_revision,
                CalendarActionType.PATCH,
                work_block_id,
            )
            outbox_id = CanonicalEncoder.hash(["outbox:v1", action_key])
            outbox = ActionOutbox(
                outbox_id=outbox_id,
                user_id=request.user_id,
                commitment_id=commitment_id,
                work_block_id=work_block_id,
                action_idempotency_key=action_key,
                expected_commitment_revision=updated_commitment.revision,
                expected_plan_revision=updated_commitment.plan_revision,
                expected_projection_hash=projection_hash(updated_commitment),
                expected_control_epoch=controls.control_epoch,
                before_state={
                    "scheduled_start": snapshot.observed_start.isoformat()
                    if snapshot.observed_start is not None
                    else None,
                    "scheduled_end": snapshot.observed_end.isoformat()
                    if snapshot.observed_end is not None
                    else None,
                    "calendar_event_id": block.calendar_event_id,
                    "observed_event_etag": snapshot.observed_event_etag,
                },
                mutation=CalendarMutation(
                    action_type=CalendarActionType.PATCH,
                    calendar_id=block.calendar_id,
                    calendar_event_id=block.calendar_event_id,
                    work_block_id=work_block_id,
                    desired_start=block.scheduled_start,
                    desired_end=block.scheduled_end,
                    expected_observed_event_etag=snapshot.observed_event_etag,
                    private_properties={
                        "managed_by": "commitmentos",
                        "commitment_id": commitment_id,
                        "work_block_id": work_block_id,
                        "plan_revision": str(updated_commitment.plan_revision),
                    },
                ),
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
            await repositories.outbox.create(outbox)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.OUTBOX_WRITTEN,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Approved Calendar slot restoration written to outbox",
                    payload={
                        "source_observation_id": source.observation_id,
                        "commitment_id": commitment_id,
                        "work_block_id": work_block_id,
                        "outbox_id": outbox_id,
                        "choice": "restore_approved_slot",
                    },
                    created_at=now,
                )
            )
            return (work_block_id, commitment_id, outbox_id)

        durable = await self._unit_of_work.run_fenced(
            request.processing_fence,
            _restore,
        )
        if not durable:
            return self._outcome(
                request,
                "rejected",
                (),
                "calendar_restore_target_not_found",
            )
        outbox_id = durable[-1]
        try:
            await self._outbox_dispatcher.dispatch(outbox_id)
        except Exception:  # noqa: BLE001 - durable outbox repairs the enqueue gap
            pass
        return self._outcome(request, "processed", durable, None)

    # ------------------------------------------------------------------
    # Approval continuations
    # ------------------------------------------------------------------

    async def _handle_approval_resolved(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        approval_id = observation.safe_metadata.get("approval_id")
        if not approval_id:
            return self._outcome(request, "rejected", (), "missing_approval_reference")

        async def _load(repositories: RepositorySet) -> Mapping[str, Any] | None:
            return await repositories.approvals.get(approval_id)

        approval = await self._unit_of_work.read(_load)
        if approval is None:
            return self._outcome(request, "rejected", (), "approval_not_found")
        if approval.get("status") != "resolved":
            return self._outcome(request, "ignored", (), "approval_not_resolved")
        decision = approval.get("decision", {})
        if approval.get("request_type") == "identity_confirmation":
            return await self._apply_identity_confirmation(request, approval)
        if decision.get("decision") != "approve":
            return self._outcome(request, "processed", (), None)
        if approval.get("request_type") == "effort_confirmation":
            return await self._propose_initial_plan(request, approval)
        if approval.get("request_type") == "initial_plan_approval":
            return await self._commit_initial_plan(request, approval)
        if approval.get("request_type") == "action_approval":
            return await self._resume_approved_calendar_repair(request, approval)
        if approval.get("request_type") in (
            "calendar_invalid_move_decision",
            "calendar_user_deleted_decision",
        ):
            return await self._apply_calendar_decision(request, approval)
        return self._outcome(request, "ignored", (), "unknown_approval_type")

    async def _apply_identity_confirmation(
        self,
        request: ReconciliationRequest,
        approval: Mapping[str, Any],
    ) -> ReconciliationOutcome:
        now = self._clock.now()
        payload = approval.get("payload", {})
        proposal_document = payload.get("proposal")
        source_observation_id = payload.get("source_observation_id")
        if not isinstance(proposal_document, Mapping) or not source_observation_id:
            return self._outcome(request, "rejected", (), "identity_payload_invalid")
        proposal = self._proposal_from_document(proposal_document)
        resolved_decision = approval.get("decision", {})
        decision_payload = resolved_decision.get("payload", {})
        decision = resolved_decision.get("decision")

        async def _apply(repositories: RepositorySet) -> tuple[str, ...]:
            observation = await repositories.observations.get(str(source_observation_id))
            if observation is None:
                return ()
            if decision == "reject":
                dismissal_id = CanonicalEncoder.hash(
                    ["source-span-dismissal:v1", request.user_id, proposal.source_span_key]
                )
                await repositories.source_span_dismissals.create(
                    {
                        "dismissal_id": dismissal_id,
                        "user_id": request.user_id,
                        "thread_id": observation.source_reference.get("thread_id", ""),
                        "source_span_key": proposal.source_span_key,
                        "source_observation_id": observation.observation_id,
                        "approval_id": approval["approval_id"],
                        "reason": decision_payload.get("reason", "user_rejected"),
                        "dismissed_by": request.user_id,
                        "dismissed_at": now,
                    }
                )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.CONFIRMATION_RECORDED,
                        trace_id=request.trace_id,
                        actor=request.user_id,
                        summary="Source interpretation dismissed",
                        payload={
                            "approval_id": approval["approval_id"],
                            "source_span_key": proposal.source_span_key,
                            "dismissal_id": dismissal_id,
                        },
                        created_at=now,
                    )
                )
                return (dismissal_id,)
            if decision != "approve":
                return ()
            ownership_value = decision_payload.get("ownership_type")
            if ownership_value is not None:
                confirmed_ownership = OwnershipType(str(ownership_value))
                proposal_to_apply = replace(
                    proposal,
                    ownership_type=confirmed_ownership,
                )
            else:
                proposal_to_apply = proposal
            if proposal_to_apply.ownership_type == OwnershipType.AMBIGUOUS:
                return ()
            return await self._create_commitment_from_proposal(
                repositories,
                request,
                observation,
                proposal_to_apply,
                now,
            )

        durable_ids = await self._unit_of_work.run_fenced(request.processing_fence, _apply)
        if not durable_ids:
            return self._outcome(request, "rejected", (), "identity_application_failed")
        return self._outcome(request, "processed", durable_ids, None)

    @staticmethod
    def _proposal_to_document(
        proposal: CommitmentSemanticProposalV1,
    ) -> Mapping[str, Any]:
        return {
            "source_span_key": proposal.source_span_key,
            "normalized_outcome": proposal.normalized_outcome,
            "description": proposal.description,
            "ownership_type": proposal.ownership_type.value,
            "owner_display_name": proposal.owner_display_name,
            "beneficiary_display_name": proposal.beneficiary_display_name,
            "deadline": (
                {
                    "source_expression": proposal.deadline.source_expression,
                    "proposed_value": proposal.deadline.proposed_value,
                    "confidence": proposal.deadline.confidence,
                }
                if proposal.deadline is not None
                else None
            ),
            "proposed_effort_minutes": proposal.proposed_effort_minutes,
            "proposed_identity_operation": proposal.proposed_identity_operation.value,
            "target_commitment_id": proposal.target_commitment_id,
            "evidence": [
                {
                    "span_key": span.span_key,
                    "message_id": span.message_id,
                    "excerpt": span.excerpt,
                }
                for span in proposal.evidence
            ],
            "confidence": proposal.confidence,
        }

    @staticmethod
    def _proposal_from_document(
        document: Mapping[str, Any],
    ) -> CommitmentSemanticProposalV1:
        from commitmentos.contracts.model_output import (
            DeadlineProposalV1,
            EvidenceSpanProposalV1,
        )

        deadline_document = document.get("deadline")
        deadline = None
        if isinstance(deadline_document, Mapping):
            deadline = DeadlineProposalV1(
                source_expression=str(deadline_document.get("source_expression") or ""),
                proposed_value=(
                    str(deadline_document["proposed_value"])
                    if deadline_document.get("proposed_value") is not None
                    else None
                ),
                confidence=float(deadline_document.get("confidence", 0)),
            )
        return CommitmentSemanticProposalV1(
            source_span_key=str(document["source_span_key"]),
            normalized_outcome=str(document["normalized_outcome"]),
            description=str(document.get("description") or ""),
            ownership_type=OwnershipType(str(document["ownership_type"])),
            owner_display_name=(
                str(document["owner_display_name"])
                if document.get("owner_display_name") is not None
                else None
            ),
            beneficiary_display_name=(
                str(document["beneficiary_display_name"])
                if document.get("beneficiary_display_name") is not None
                else None
            ),
            deadline=deadline,
            proposed_effort_minutes=(
                int(document["proposed_effort_minutes"])
                if document.get("proposed_effort_minutes") is not None
                else None
            ),
            proposed_identity_operation=IdentityOperation(
                str(document["proposed_identity_operation"])
            ),
            target_commitment_id=(
                str(document["target_commitment_id"])
                if document.get("target_commitment_id") is not None
                else None
            ),
            evidence=tuple(
                EvidenceSpanProposalV1(
                    span_key=str(item["span_key"]),
                    message_id=str(item["message_id"]),
                    excerpt=str(item["excerpt"]),
                )
                for item in document.get("evidence", ())
            ),
            confidence=float(document.get("confidence", 0)),
        )

    async def _propose_initial_plan(
        self,
        request: ReconciliationRequest,
        approval: Mapping[str, Any],
    ) -> ReconciliationOutcome:
        commitment_id = approval["commitment_id"]
        planning = self._portfolio_planning
        if planning is None:
            return self._outcome(request, "rejected", (), "portfolio_planner_unavailable")
        for _attempt in range(3):
            plan = await planning.calculate(
                request.user_id,
                (commitment_id,),
            )
            if not await planning.calendar_inputs_match(plan):
                await self._record_stale_planner_run(
                    request,
                    plan,
                    "calendar_changed_before_publication",
                    create=True,
                )
                continue
            now = self._clock.now()

            async def _publish(
                repositories: RepositorySet,
            ) -> tuple[str, tuple[str, ...]]:
                if not await planning.expected_revisions_match(
                    repositories,
                    plan,
                ):
                    stale = replace(
                        plan,
                        status=PlannerRunStatus.STALE,
                        stale_reason="authoritative_revision_changed",
                    )
                    await repositories.planner_runs.create(stale)
                    return ("stale", (plan.planner_run_id,))
                commitments: dict[str, Commitment] = {}
                work_blocks: dict[str, tuple[WorkBlock, ...]] = {}
                for planned_commitment_id in plan.commitment_order:
                    current = await repositories.commitments.get(planned_commitment_id)
                    if current is None:
                        return ("stale", ())
                    commitments[planned_commitment_id] = current
                    work_blocks[planned_commitment_id] = tuple(
                        await repositories.work_blocks.list_for_commitment(planned_commitment_id)
                    )
                commitment = commitments.get(commitment_id)
                if commitment is None or commitment.effort.confirmed_minutes is None:
                    return ("invalid", ())
                approval_id = CanonicalEncoder.hash(
                    [
                        "approval:v1",
                        commitment_id,
                        "initial_plan_approval",
                        commitment.revision,
                        plan.planner_run_id,
                    ]
                )
                existing = await repositories.approvals.get(approval_id)
                if existing is not None:
                    return ("published", (plan.planner_run_id, approval_id))
                published = replace(
                    plan,
                    status=PlannerRunStatus.PUBLISHED,
                    published_at=now,
                )
                stored_run = await repositories.planner_runs.get(plan.planner_run_id)
                if stored_run is None:
                    await repositories.planner_runs.create(published)
                elif stored_run.status == PlannerRunStatus.PUBLISHED:
                    published = stored_run
                else:
                    await repositories.planner_runs.save(
                        published,
                        stored_run.status.value,
                    )
                for planned_commitment_id in plan.commitment_order:
                    current = commitments[planned_commitment_id]
                    projection = self._projection_guard.rebuild(
                        current,
                        work_blocks[planned_commitment_id],
                        published,
                        now,
                    )
                    await repositories.commitments.save(
                        current.with_projection(projection, now),
                        current.revision,
                    )
                    allocation = next(
                        item
                        for item in published.allocations
                        if item.commitment_id == planned_commitment_id
                    )
                    await repositories.activity.append(
                        self._activity_factory.create(
                            user_id=request.user_id,
                            event_type=ActivityEventType.RISK_CHANGED,
                            trace_id=request.trace_id,
                            actor="portfolio_planner",
                            summary=(f"Portfolio risk calculated: {allocation.risk_level.value}"),
                            payload=dict(published.risk_audit.get(planned_commitment_id, {}))
                            | {
                                "commitment_id": planned_commitment_id,
                                "planner_run_id": published.planner_run_id,
                                "commitment_order": list(published.commitment_order),
                            },
                            created_at=now,
                        )
                    )
                proposed_blocks = [
                    {
                        "work_block_id": block.work_block_id,
                        "commitment_id": block.commitment_id,
                        "start": block.interval.start.isoformat(),
                        "end": block.interval.end.isoformat(),
                        "preserved": block.preserved,
                        "calendar_event_id": block.calendar_event_id,
                    }
                    for block in published.work_blocks
                ]
                await repositories.approvals.create(
                    {
                        "approval_id": approval_id,
                        "user_id": request.user_id,
                        "commitment_id": commitment_id,
                        "commitment_revision": commitment.revision,
                        "request_type": "initial_plan_approval",
                        "payload": {
                            "planner_run_id": published.planner_run_id,
                            "expected_revisions": dict(published.expected_revisions),
                            "commitment_order": list(published.commitment_order),
                            "allocations": self._allocation_documents(published),
                            "proposed_blocks": proposed_blocks,
                        },
                        "continuation_type": "initial_plan_approval",
                        "policy_reason": "first_plan_requires_approval",
                        "status": "pending",
                        "revision": 1,
                        "created_at": now,
                        "expires_at": now + timedelta(days=7),
                    }
                )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.PLAN_PROPOSED,
                        trace_id=request.trace_id,
                        actor="portfolio_planner",
                        summary=(
                            f"Constraint-safe portfolio plan proposed: "
                            f"{len(proposed_blocks)} work blocks"
                        ),
                        payload={
                            "commitment_id": commitment_id,
                            "approval_id": approval_id,
                            "planner_run_id": published.planner_run_id,
                            "commitment_order": list(published.commitment_order),
                            "proposed_blocks": proposed_blocks,
                            "feasible": published.feasible,
                        },
                        created_at=now,
                    )
                )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.POLICY_DECIDED,
                        trace_id=request.trace_id,
                        actor="reconciliation",
                        summary="Policy: the first Calendar plan requires approval",
                        payload={
                            "commitment_id": commitment_id,
                            "planner_run_id": published.planner_run_id,
                            "policy_reason": "first_plan_requires_approval",
                            "disposition": "approval_required",
                        },
                        created_at=now,
                    )
                )
                return ("published", (published.planner_run_id, approval_id))

            disposition, durable_ids = await self._unit_of_work.run_fenced(
                request.processing_fence,
                _publish,
            )
            if disposition == "published":
                return self._outcome(request, "processed", durable_ids, None)
            if disposition != "stale":
                break
        return self._outcome(
            request,
            "rejected",
            (),
            "plan_proposal_preconditions_failed",
        )

    async def _commit_initial_plan(
        self,
        request: ReconciliationRequest,
        approval: Mapping[str, Any],
    ) -> ReconciliationOutcome:
        commitment_id = approval["commitment_id"]
        planning = self._portfolio_planning
        if planning is None:
            return self._outcome(request, "rejected", (), "portfolio_planner_unavailable")
        planner_run_id = str(approval.get("payload", {}).get("planner_run_id") or "")
        if not planner_run_id:
            return self._outcome(request, "rejected", (), "planner_run_reference_missing")

        async def _load_plan(repositories: RepositorySet) -> PortfolioPlan | None:
            return await repositories.planner_runs.get(planner_run_id)

        plan = await self._unit_of_work.read(_load_plan)
        if plan is None or plan.status != PlannerRunStatus.PUBLISHED:
            return self._outcome(request, "rejected", (), "planner_run_not_publishable")
        if not await planning.calendar_inputs_match(plan):
            await self._record_stale_planner_run(
                request,
                plan,
                "calendar_changed_before_plan_commit",
                create=False,
            )
            return await self._propose_initial_plan(request, approval)
        now = self._clock.now()

        async def _apply(
            repositories: RepositorySet,
        ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
            if not await planning.expected_revisions_match(
                repositories,
                plan,
            ):
                return ("stale", (), ())
            commitments: dict[str, Commitment] = {}
            current_blocks: dict[str, WorkBlock] = {}
            blocks_by_commitment: dict[str, list[WorkBlock]] = {}
            for planned_commitment_id in plan.commitment_order:
                current = await repositories.commitments.get(planned_commitment_id)
                if current is None:
                    return ("stale", (), ())
                commitments[planned_commitment_id] = current
                blocks = list(
                    await repositories.work_blocks.list_for_commitment(planned_commitment_id)
                )
                blocks_by_commitment[planned_commitment_id] = blocks
                current_blocks.update({item.work_block_id: item for item in blocks})
            commitment = commitments.get(commitment_id)
            if commitment is None or commitment.revision != approval.get("commitment_revision"):
                return ("stale", (), ())
            controls = await repositories.system_controls.get(request.user_id)
            previous_blocks = tuple(
                PlannedWorkBlock(
                    work_block_id=block.work_block_id,
                    commitment_id=block.commitment_id,
                    interval=TimeInterval(block.scheduled_start, block.scheduled_end),
                    duration_minutes=block.duration_minutes,
                    preserved=True,
                    calendar_event_id=block.calendar_event_id,
                )
                for block in current_blocks.values()
                if block.execution_state
                in (WorkBlockExecutionState.PLANNED, WorkBlockExecutionState.ACTIVE)
                and block.scheduled_end.astimezone(timezone.utc) > now.astimezone(timezone.utc)
            )
            previous = replace(
                plan,
                planner_run_id=f"previous:{plan.planner_run_id}",
                work_blocks=previous_blocks,
            )
            plan_diff = self._plan_differ.diff(previous, plan)
            if not self._plan_differ.validate_owned_targets(
                plan_diff,
                set(current_blocks),
            ):
                return ("invalid", (), ())
            snapshots = {}
            for mutation in plan_diff.mutations:
                if mutation.mutation_type in (
                    PlanMutationType.MOVE,
                    PlanMutationType.CANCEL,
                ):
                    existing = current_blocks[mutation.work_block_id]
                    snapshots[mutation.work_block_id] = await repositories.calendar_snapshots.get(
                        existing.calendar_id,
                        existing.calendar_event_id,
                    )

            affected = {item.commitment_id for item in plan_diff.mutations}
            affected.add(commitment_id)
            updated_commitments: dict[str, Commitment] = {}
            plan_revisions: dict[str, int] = {}
            for affected_id in affected:
                current = commitments[affected_id]
                target_lifecycle = (
                    LifecycleStatus.ACTIVE
                    if affected_id == commitment_id
                    and current.lifecycle_status == LifecycleStatus.AWAITING_CONFIRMATION
                    else current.lifecycle_status
                )
                updated = replace(
                    current,
                    plan_revision=current.plan_revision + 1,
                    revision=current.revision + 1,
                    lifecycle_status=target_lifecycle,
                    updated_at=now,
                )
                updated_commitments[affected_id] = updated
                plan_revisions[affected_id] = updated.plan_revision

            resulting_blocks = dict(current_blocks)
            changed_blocks: dict[str, WorkBlock] = {}
            for mutation in plan_diff.mutations:
                plan_revision = plan_revisions[mutation.commitment_id]
                if mutation.mutation_type == PlanMutationType.CREATE:
                    if mutation.after is None:
                        return ("invalid", (), ())
                    calendar_event_id = self._event_ids.derive(
                        self._calendar_id,
                        mutation.work_block_id,
                    )
                    changed = WorkBlock(
                        work_block_id=mutation.work_block_id,
                        commitment_id=mutation.commitment_id,
                        revision=1,
                        calendar_id=self._calendar_id,
                        calendar_event_id=calendar_event_id,
                        calendar_snapshot_id=None,
                        duration_minutes=mutation.after.duration_minutes(),
                        execution_state=WorkBlockExecutionState.PLANNED,
                        scheduled_start=mutation.after.start,
                        scheduled_end=mutation.after.end,
                        verified_minutes=0,
                        completion_evidence_id=None,
                        user_edit_state=UserEditState.NONE,
                        plan_revision=plan_revision,
                    )
                else:
                    existing = current_blocks[mutation.work_block_id]
                    if mutation.mutation_type == PlanMutationType.MOVE:
                        if mutation.after is None:
                            return ("invalid", (), ())
                        changed = replace(
                            existing,
                            scheduled_start=mutation.after.start,
                            scheduled_end=mutation.after.end,
                            duration_minutes=mutation.after.duration_minutes(),
                            revision=existing.revision + 1,
                            plan_revision=plan_revision,
                        )
                    else:
                        changed = replace(
                            existing,
                            execution_state=WorkBlockExecutionState.CANCELED,
                            revision=existing.revision + 1,
                            plan_revision=plan_revision,
                        )
                changed_blocks[changed.work_block_id] = changed
                resulting_blocks[changed.work_block_id] = changed

            for affected_id, updated in tuple(updated_commitments.items()):
                post_blocks = [
                    block
                    for block in resulting_blocks.values()
                    if block.commitment_id == affected_id
                ]
                projection = self._projection_guard.rebuild(
                    updated,
                    post_blocks,
                    plan,
                    now,
                )
                updated_commitments[affected_id] = updated.with_projection(
                    projection,
                    now,
                )

            for affected_id, updated in updated_commitments.items():
                await repositories.commitments.save(
                    updated,
                    commitments[affected_id].revision,
                )
            for work_block_id, changed in changed_blocks.items():
                existing_block = current_blocks.get(work_block_id)
                await repositories.work_blocks.save(
                    changed,
                    existing_block.revision if existing_block is not None else None,
                )

            durable: list[str] = []
            outbox_ids: list[str] = []
            for mutation in plan_diff.mutations:
                work_block = resulting_blocks[mutation.work_block_id]
                action_type = {
                    PlanMutationType.CREATE: CalendarActionType.INSERT,
                    PlanMutationType.MOVE: CalendarActionType.PATCH,
                    PlanMutationType.ADOPT: CalendarActionType.ADOPT,
                    PlanMutationType.CANCEL: CalendarActionType.CANCEL,
                }[mutation.mutation_type]
                plan_revision = plan_revisions[mutation.commitment_id]
                action_key = self._action_keys.derive(
                    mutation.commitment_id,
                    plan_revision,
                    action_type,
                    mutation.work_block_id,
                )
                outbox_id = CanonicalEncoder.hash(["outbox:v1", action_key])
                snapshot = snapshots.get(mutation.work_block_id)
                target_commitment = updated_commitments[mutation.commitment_id]
                outbox = ActionOutbox(
                    outbox_id=outbox_id,
                    user_id=request.user_id,
                    commitment_id=mutation.commitment_id,
                    work_block_id=mutation.work_block_id,
                    action_idempotency_key=action_key,
                    expected_commitment_revision=target_commitment.revision,
                    expected_plan_revision=plan_revision,
                    expected_projection_hash=projection_hash(target_commitment),
                    expected_control_epoch=controls.control_epoch,
                    before_state={
                        "scheduled_start": mutation.before.start.isoformat()
                        if mutation.before is not None
                        else None,
                        "scheduled_end": mutation.before.end.isoformat()
                        if mutation.before is not None
                        else None,
                        "calendar_event_id": work_block.calendar_event_id,
                        "observed_event_etag": (
                            snapshot.observed_event_etag
                            if snapshot is not None
                            else None
                        ),
                    },
                    mutation=CalendarMutation(
                        action_type=action_type,
                        calendar_id=self._calendar_id,
                        calendar_event_id=work_block.calendar_event_id,
                        work_block_id=mutation.work_block_id,
                        desired_start=(
                            mutation.after.start if mutation.after is not None else None
                        ),
                        desired_end=(mutation.after.end if mutation.after is not None else None),
                        expected_observed_event_etag=(
                            snapshot.observed_event_etag if snapshot is not None else None
                        ),
                        private_properties={
                            "managed_by": "commitmentos",
                            "commitment_id": mutation.commitment_id,
                            "work_block_id": mutation.work_block_id,
                            "plan_revision": str(plan_revision),
                        },
                    ),
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
                await repositories.outbox.create(outbox)
                durable.extend([mutation.work_block_id, outbox_id])
                outbox_ids.append(outbox_id)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.OUTBOX_WRITTEN,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary=(
                        f"Plan approved: {len(plan_diff.mutations)} "
                        "Calendar actions written to the outbox"
                    ),
                    payload={
                        "commitment_id": commitment_id,
                        "planner_run_id": plan.planner_run_id,
                        "commitment_order": list(plan.commitment_order),
                        "expected_revisions": dict(plan.expected_revisions),
                        "plan_revisions": plan_revisions,
                        "outbox_ids": outbox_ids,
                        "control_epoch": controls.control_epoch,
                    },
                    created_at=now,
                )
            )
            durable.append(plan.planner_run_id)
            return ("committed", tuple(durable), tuple(outbox_ids))

        disposition, durable_ids, outbox_ids = await self._unit_of_work.run_fenced(
            request.processing_fence, _apply
        )
        if disposition == "stale":
            await self._record_stale_planner_run(
                request,
                plan,
                "authoritative_revision_changed_before_plan_commit",
                create=False,
            )
            return await self._propose_initial_plan(request, approval)
        if disposition != "committed" or not durable_ids:
            return self._outcome(request, "rejected", (), "plan_commit_preconditions_failed")
        # Dispatch after commit; the periodic dispatcher repairs any gap.
        for outbox_id in outbox_ids:
            try:
                await self._outbox_dispatcher.dispatch(outbox_id)
            except Exception:  # noqa: BLE001 - dispatch is repairable
                pass
        return self._outcome(request, "processed", durable_ids, None)

    @staticmethod
    def _filter_plan_diff(plan_diff: PlanDiff, excluded_ids: set[str]) -> PlanDiff:
        mutations = tuple(
            mutation
            for mutation in plan_diff.mutations
            if mutation.work_block_id not in excluded_ids
        )
        return replace(
            plan_diff,
            mutations=mutations,
            preserved_work_block_ids=tuple(
                sorted(set(plan_diff.preserved_work_block_ids) | excluded_ids)
            ),
            moved_block_count=sum(
                mutation.mutation_type == PlanMutationType.MOVE
                for mutation in mutations
            ),
            total_displacement_minutes=sum(
                abs(
                    int(
                        (
                            mutation.after.start.astimezone(timezone.utc)
                            - mutation.before.start.astimezone(timezone.utc)
                        ).total_seconds()
                        // 60
                    )
                )
                for mutation in mutations
                if mutation.mutation_type == PlanMutationType.MOVE
                and mutation.before is not None
                and mutation.after is not None
            ),
        )

    async def _add_snapshot_resume_mutation(
        self,
        observation: ObservationV1,
        repaired: PortfolioPlan,
        plan_diff: PlanDiff,
    ) -> tuple[PlanDiff, bool]:
        work_block_id = str(observation.safe_metadata.get("work_block_id") or "")
        if (
            observation.observation_type != ObservationType.CALENDAR_EVENT_CHANGED
            or not work_block_id
            or any(item.work_block_id == work_block_id for item in plan_diff.mutations)
        ):
            return plan_diff, False

        async def _load(repositories: RepositorySet):  # noqa: ANN202 - local tuple shape
            block = await repositories.work_blocks.get(work_block_id)
            if block is None:
                return None, None, False
            snapshot = await repositories.calendar_snapshots.get(
                block.calendar_id,
                block.calendar_event_id,
            )
            actions = await repositories.outbox.list_for_work_block(
                repaired.user_id,
                work_block_id,
                20,
            )
            stale = any(
                action.execution_status == ExecutionStatus.STALE_PRECONDITION
                for action in actions
            )
            return block, snapshot, stale

        block, snapshot, stale = await self._unit_of_work.read(_load)
        if (
            block is None
            or snapshot is None
            or snapshot.observed_start is None
            or snapshot.observed_end is None
            or not stale
        ):
            return plan_diff, False
        before = TimeInterval(snapshot.observed_start, snapshot.observed_end)
        after = TimeInterval(block.scheduled_start, block.scheduled_end)
        if before == after:
            return plan_diff, False
        mutation = PlanMutation(
            mutation_type=PlanMutationType.MOVE,
            work_block_id=block.work_block_id,
            commitment_id=block.commitment_id,
            before=before,
            after=after,
            reason="resume_after_stale_precondition_resync",
            calendar_event_id=block.calendar_event_id,
        )
        return (
            replace(
                plan_diff,
                mutations=tuple(
                    sorted(
                        plan_diff.mutations + (mutation,),
                        key=lambda item: item.work_block_id,
                    )
                ),
                moved_block_count=plan_diff.moved_block_count + 1,
                total_displacement_minutes=(
                    plan_diff.total_displacement_minutes
                    + abs(
                        int(
                            (
                                after.start.astimezone(timezone.utc)
                                - before.start.astimezone(timezone.utc)
                            ).total_seconds()
                            // 60
                        )
                    )
                ),
                metadata=dict(plan_diff.metadata)
                | {"resume_reason": "stale_precondition_resynchronized"},
            ),
            True,
        )

    @staticmethod
    def _add_recreate_mutations(
        repaired: PortfolioPlan,
        plan_diff: PlanDiff,
        recreate_ids: set[str],
    ) -> PlanDiff:
        existing = {mutation.work_block_id for mutation in plan_diff.mutations}
        desired = {block.work_block_id: block for block in repaired.work_blocks}
        additions = tuple(
            PlanMutation(
                mutation_type=PlanMutationType.ADOPT,
                work_block_id=work_block_id,
                commitment_id=desired[work_block_id].commitment_id,
                before=None,
                after=desired[work_block_id].interval,
                reason="user_approved_recreation_after_deletion",
                calendar_event_id=desired[work_block_id].calendar_event_id,
            )
            for work_block_id in sorted(recreate_ids - existing)
            if work_block_id in desired
        )
        return replace(
            plan_diff,
            mutations=tuple(
                sorted(
                    plan_diff.mutations + additions,
                    key=lambda item: item.work_block_id,
                )
            ),
        )

    @staticmethod
    def _repair_blocking_infeasibility(repaired: PortfolioPlan) -> bool:
        """Escalate only when the repair itself failed (§12.4 rule 8).

        Shortfall on a commitment whose deadline has already passed is
        structurally unrepairable by any Calendar mutation: no eligible slot
        can exist before a past deadline, so that shortfall persists in every
        plan until the user acts. It stays visible as `overdue` risk and the
        portfolio-level `no_feasible_plan` state, but it must not convert an
        unrelated in-policy repair into an approval — otherwise one overdue
        commitment permanently disables automatic repair for the portfolio.
        """
        repair_audit = repaired.risk_audit.get("_repair", {})
        if str(repair_audit.get("unplaced_work_block_ids") or ""):
            return True
        if str(repair_audit.get("immutable_conflict") or "") == "true":
            return True
        return any(
            allocation.shortfall_minutes > 0
            and allocation.risk_level is not RiskLevel.OVERDUE
            for allocation in repaired.allocations
        )

    async def _publish_or_apply_repair(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
        previous: PortfolioPlan,
        repaired: PortfolioPlan,
        plan_diff: PlanDiff,
        policy: RepairPolicyDecision,
        adopted_work_block_ids: tuple[str, ...],
    ) -> ReconciliationOutcome:
        planning = self._portfolio_planning
        if planning is None:
            return self._outcome(request, "rejected", (), "portfolio_planner_unavailable")
        now = self._clock.now()
        if policy.disposition == RepairPolicyDisposition.FORBIDDEN:
            async def _record_forbidden(repositories: RepositorySet) -> None:
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.POLICY_DECIDED,
                        trace_id=request.trace_id,
                        actor="reconciliation",
                        summary="Calendar mutation refused by ownership policy",
                        payload={
                            "source_observation_id": observation.observation_id,
                            "disposition": RepairPolicyDisposition.FORBIDDEN.value,
                            "reason_codes": list(policy.reason_codes),
                            "threshold_version": policy.threshold_version,
                            "calendar_mutations_written": 0,
                        },
                        created_at=now,
                    )
                )

            await self._unit_of_work.run_fenced(
                request.processing_fence,
                _record_forbidden,
            )
            return self._outcome(request, "rejected", (), "policy_forbidden")
        repair_blocked = self._repair_blocking_infeasibility(repaired)
        approval_required = (
            repair_blocked
            or policy.disposition == RepairPolicyDisposition.APPROVAL_REQUIRED
        )
        explanation = await self._explain_repair_decision(
            observation,
            plan_diff,
            repaired,
            policy,
            approval_required,
        )

        if approval_required:
            async def _propose(repositories: RepositorySet) -> tuple[str, ...]:
                if not await planning.expected_revisions_match(repositories, repaired):
                    return ()
                published = replace(
                    repaired,
                    status=PlannerRunStatus.PUBLISHED,
                    published_at=now,
                )
                stored = await repositories.planner_runs.get(published.planner_run_id)
                if stored is None:
                    await repositories.planner_runs.create(published)
                commitment_id = str(
                    observation.safe_metadata.get("commitment_id")
                    or (published.commitment_order[0] if published.commitment_order else "")
                )
                commitment = await repositories.commitments.get(commitment_id)
                if commitment is None:
                    return ()
                approval_id = CanonicalEncoder.hash(
                    ["action-approval:v1", published.planner_run_id]
                )
                existing_approval = await repositories.approvals.get(approval_id)
                if existing_approval is not None:
                    return (published.planner_run_id, approval_id)
                await repositories.approvals.create(
                    {
                        "approval_id": approval_id,
                        "user_id": request.user_id,
                        "commitment_id": commitment_id,
                        "commitment_revision": commitment.revision,
                        "request_type": "action_approval",
                        "payload": {
                            "source_observation_id": observation.observation_id,
                            "previous_planner_run_id": previous.planner_run_id,
                            "planner_run_id": published.planner_run_id,
                            "expected_revisions": dict(published.expected_revisions),
                            "feasible": published.feasible,
                            "reason_codes": list(policy.reason_codes),
                            "moved_block_count": policy.changed_block_count,
                            "maximum_shift_minutes": policy.maximum_shift_minutes,
                            "total_displacement_minutes": (
                                policy.total_displacement_minutes
                            ),
                            "mutations": self._plan_mutation_documents(plan_diff),
                            "risk_arc": self._risk_arc(published),
                            "explanation": explanation,
                        },
                        "continuation_type": "action_approval",
                        "policy_reason": (
                            "repair_infeasible"
                            if repair_blocked
                            else "policy_threshold_breached"
                        ),
                        "status": "pending",
                        "revision": 1,
                        "created_at": now,
                        "expires_at": now + timedelta(days=7),
                    }
                )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.POLICY_DECIDED,
                        trace_id=request.trace_id,
                        actor="reconciliation",
                        summary="Repair requires explicit approval before Calendar mutation",
                        payload={
                            "approval_id": approval_id,
                            "planner_run_id": published.planner_run_id,
                            "disposition": RepairPolicyDisposition.APPROVAL_REQUIRED.value,
                            "threshold_version": policy.threshold_version,
                            "reason_codes": list(policy.reason_codes),
                            "feasible": published.feasible,
                            "calendar_mutations_written": 0,
                        },
                        created_at=now,
                    )
                )
                return (published.planner_run_id, approval_id)

            durable = await self._unit_of_work.run_fenced(
                request.processing_fence,
                _propose,
            )
            if not durable:
                return self._outcome(request, "rejected", (), "repair_inputs_stale")
            return self._outcome(request, "processed", durable, None)

        async def _apply(
            repositories: RepositorySet,
        ) -> tuple[tuple[str, ...], tuple[str, ...]]:
            if not await planning.expected_revisions_match(repositories, repaired):
                return (), ()
            commitments: dict[str, Commitment] = {}
            blocks: dict[str, WorkBlock] = {}
            for commitment_id in repaired.commitment_order:
                commitment = await repositories.commitments.get(commitment_id)
                if commitment is None:
                    return (), ()
                commitments[commitment_id] = commitment
                for block in await repositories.work_blocks.list_for_commitment(commitment_id):
                    blocks[block.work_block_id] = block
            if not self._plan_differ.validate_owned_targets(plan_diff, set(blocks)):
                return (), ()

            snapshots = {}
            for mutation in plan_diff.mutations:
                if mutation.mutation_type in (PlanMutationType.MOVE, PlanMutationType.CANCEL):
                    target_block = blocks.get(mutation.work_block_id)
                    if target_block is None:
                        return (), ()
                    snapshot = await repositories.calendar_snapshots.get(
                        target_block.calendar_id,
                        target_block.calendar_event_id,
                    )
                    if snapshot is None or snapshot.observed_event_etag is None:
                        return (), ()
                    snapshots[mutation.work_block_id] = snapshot

            affected_commitments = {
                mutation.commitment_id for mutation in plan_diff.mutations
            }
            updated_commitments: dict[str, Commitment] = {}
            plan_revisions: dict[str, int] = {}
            for commitment_id in affected_commitments:
                current = commitments[commitment_id]
                updated = replace(
                    current,
                    revision=current.revision + 1,
                    plan_revision=current.plan_revision + 1,
                    updated_at=now,
                )
                updated_commitments[commitment_id] = updated
                plan_revisions[commitment_id] = updated.plan_revision

            resulting_blocks = dict(blocks)
            changed_blocks: dict[str, WorkBlock] = {}
            for mutation in plan_diff.mutations:
                plan_revision = plan_revisions[mutation.commitment_id]
                existing = blocks.get(mutation.work_block_id)
                if mutation.mutation_type == PlanMutationType.CREATE:
                    if mutation.after is None or existing is not None:
                        return (), ()
                    changed = WorkBlock(
                        work_block_id=mutation.work_block_id,
                        commitment_id=mutation.commitment_id,
                        revision=1,
                        calendar_id=self._calendar_id,
                        calendar_event_id=self._event_ids.derive(
                            self._calendar_id,
                            mutation.work_block_id,
                        ),
                        calendar_snapshot_id=None,
                        duration_minutes=mutation.after.duration_minutes(),
                        execution_state=WorkBlockExecutionState.PLANNED,
                        scheduled_start=mutation.after.start,
                        scheduled_end=mutation.after.end,
                        verified_minutes=0,
                        completion_evidence_id=None,
                        user_edit_state=UserEditState.NONE,
                        plan_revision=plan_revision,
                    )
                elif existing is None:
                    return (), ()
                elif mutation.mutation_type == PlanMutationType.MOVE:
                    if mutation.after is None:
                        return (), ()
                    changed = replace(
                        existing,
                        scheduled_start=mutation.after.start,
                        scheduled_end=mutation.after.end,
                        duration_minutes=mutation.after.duration_minutes(),
                        revision=existing.revision + 1,
                        user_edit_state=UserEditState.NONE,
                        plan_revision=plan_revision,
                    )
                elif mutation.mutation_type == PlanMutationType.CANCEL:
                    changed = replace(
                        existing,
                        execution_state=WorkBlockExecutionState.CANCELED,
                        revision=existing.revision + 1,
                        user_edit_state=UserEditState.NONE,
                        plan_revision=plan_revision,
                    )
                else:
                    changed = replace(
                        existing,
                        revision=existing.revision + 1,
                        user_edit_state=UserEditState.NONE,
                        plan_revision=plan_revision,
                    )
                changed_blocks[changed.work_block_id] = changed
                resulting_blocks[changed.work_block_id] = changed

            published = replace(
                repaired,
                status=PlannerRunStatus.PUBLISHED,
                published_at=now,
            )
            stored = await repositories.planner_runs.get(published.planner_run_id)
            if stored is None:
                await repositories.planner_runs.create(published)
            for work_block_id, changed in changed_blocks.items():
                existing = blocks.get(work_block_id)
                await repositories.work_blocks.save(
                    changed,
                    existing.revision if existing is not None else None,
                )

            projected_commitments: dict[str, Commitment] = {}
            for commitment_id in repaired.commitment_order:
                commitment = updated_commitments.get(
                    commitment_id,
                    commitments[commitment_id],
                )
                projection = self._projection_guard.rebuild(
                    commitment,
                    tuple(
                        block
                        for block in resulting_blocks.values()
                        if block.commitment_id == commitment_id
                    ),
                    published,
                    now,
                )
                projected = commitment.with_projection(projection, now)
                projected_commitments[commitment_id] = projected
                await repositories.commitments.save(
                    projected,
                    commitments[commitment_id].revision,
                )

            controls = await repositories.system_controls.get(request.user_id)
            outbox_ids: list[str] = []
            durable: list[str] = [observation.observation_id, published.planner_run_id]
            for mutation in plan_diff.mutations:
                work_block = resulting_blocks[mutation.work_block_id]
                action_type = {
                    PlanMutationType.CREATE: CalendarActionType.INSERT,
                    PlanMutationType.MOVE: CalendarActionType.PATCH,
                    PlanMutationType.ADOPT: CalendarActionType.ADOPT,
                    PlanMutationType.CANCEL: CalendarActionType.CANCEL,
                }[mutation.mutation_type]
                plan_revision = plan_revisions[mutation.commitment_id]
                action_key = self._action_keys.derive(
                    mutation.commitment_id,
                    plan_revision,
                    action_type,
                    mutation.work_block_id,
                )
                outbox_id = CanonicalEncoder.hash(["outbox:v1", action_key])
                snapshot = snapshots.get(mutation.work_block_id)
                target_commitment = projected_commitments[mutation.commitment_id]
                outbox = ActionOutbox(
                    outbox_id=outbox_id,
                    user_id=request.user_id,
                    commitment_id=mutation.commitment_id,
                    work_block_id=mutation.work_block_id,
                    action_idempotency_key=action_key,
                    expected_commitment_revision=target_commitment.revision,
                    expected_plan_revision=plan_revision,
                    expected_projection_hash=projection_hash(target_commitment),
                    expected_control_epoch=controls.control_epoch,
                    before_state={
                        "scheduled_start": mutation.before.start.isoformat()
                        if mutation.before is not None
                        else None,
                        "scheduled_end": mutation.before.end.isoformat()
                        if mutation.before is not None
                        else None,
                        "calendar_event_id": work_block.calendar_event_id,
                        "observed_event_etag": (
                            snapshot.observed_event_etag
                            if snapshot is not None
                            else None
                        ),
                    },
                    mutation=CalendarMutation(
                        action_type=action_type,
                        calendar_id=work_block.calendar_id,
                        calendar_event_id=work_block.calendar_event_id,
                        work_block_id=mutation.work_block_id,
                        desired_start=(
                            mutation.after.start if mutation.after is not None else None
                        ),
                        desired_end=(
                            mutation.after.end if mutation.after is not None else None
                        ),
                        expected_observed_event_etag=(
                            snapshot.observed_event_etag if snapshot is not None else None
                        ),
                        private_properties={
                            "managed_by": "commitmentos",
                            "commitment_id": mutation.commitment_id,
                            "work_block_id": mutation.work_block_id,
                            "plan_revision": str(plan_revision),
                        },
                    ),
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
                await repositories.outbox.create(outbox)
                outbox_ids.append(outbox_id)
                durable.extend((mutation.work_block_id, outbox_id))

            risk_arc = self._risk_arc(published)
            for commitment_id in published.commitment_order:
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.RISK_CHANGED,
                        trace_id=request.trace_id,
                        actor="stable_plan_repairer",
                        summary="Repair risk arc recorded from disruption through recovery",
                        payload={
                            "commitment_id": commitment_id,
                            "planner_run_id": published.planner_run_id,
                            **risk_arc.get(commitment_id, {}),
                        },
                        created_at=now,
                    )
                )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.PLAN_REPAIRED,
                    trace_id=request.trace_id,
                    actor="stable_plan_repairer",
                    summary=(
                        f"Minimal repair moved {plan_diff.moved_block_count} block(s) "
                        f"and preserved {len(plan_diff.preserved_work_block_ids)}"
                    ),
                    payload={
                        "source_observation_id": observation.observation_id,
                        "source_observation_type": observation.observation_type.value,
                        "detected_at": observation.observed_at.isoformat(),
                        "decided_at": now.isoformat(),
                        "decision_latency_ms": max(
                            0,
                            int((now - observation.observed_at).total_seconds() * 1000),
                        ),
                        "warmed_budget_ms": 15_000,
                        "operational_budget_ms": 60_000,
                        "planner_run_id": published.planner_run_id,
                        "previous_planner_run_id": previous.planner_run_id,
                        "mutations": self._plan_mutation_documents(plan_diff),
                        "moved_block_count": plan_diff.moved_block_count,
                        "total_displacement_minutes": (
                            plan_diff.total_displacement_minutes
                        ),
                        "preserved_work_block_ids": list(
                            plan_diff.preserved_work_block_ids
                        ),
                        "commitment_order": list(published.commitment_order),
                        "allocations": self._allocation_documents(published),
                        "risk_arc": risk_arc,
                        "outbox_ids": outbox_ids,
                        "undo_available": True,
                        "explanation": explanation,
                    },
                    created_at=now,
                )
            )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.POLICY_DECIDED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Repair remained within policy and was applied automatically",
                    payload={
                        "source_observation_id": observation.observation_id,
                        "planner_run_id": published.planner_run_id,
                        "previous_planner_run_id": previous.planner_run_id,
                        "disposition": RepairPolicyDisposition.AUTOMATIC.value,
                        "threshold_version": policy.threshold_version,
                        "moved_block_count": policy.changed_block_count,
                        "maximum_shift_minutes": policy.maximum_shift_minutes,
                        "total_displacement_minutes": (
                            policy.total_displacement_minutes
                        ),
                        "undo_available": True,
                        "outbox_ids": outbox_ids,
                        "adopted_work_block_ids": list(adopted_work_block_ids),
                    },
                    created_at=now,
                )
            )
            return tuple(durable), tuple(outbox_ids)

        durable, outbox_ids = await self._unit_of_work.run_fenced(
            request.processing_fence,
            _apply,
        )
        if not durable:
            return self._outcome(request, "rejected", (), "repair_inputs_stale")
        for outbox_id in outbox_ids:
            try:
                await self._outbox_dispatcher.dispatch(outbox_id)
            except Exception:  # noqa: BLE001 - dispatch gap is repairable
                pass
        return self._outcome(request, "processed", durable, None)

    async def _explain_repair_decision(
        self,
        observation: ObservationV1,
        plan_diff: PlanDiff,
        repaired: PortfolioPlan,
        policy: RepairPolicyDecision,
        approval_required: bool,
    ) -> Mapping[str, Any]:
        fallback = (
            f"The calendar change affected {plan_diff.moved_block_count} work block(s). "
            + (
                "The proposed repair is waiting for approval."
                if approval_required
                else "CommitmentOS applied the smallest policy-permitted repair."
            )
            + f" {len(plan_diff.preserved_work_block_ids)} block(s) stayed unchanged."
        )
        if self._model_interpreter is None:
            return {"text": fallback, "source": "deterministic_fallback"}
        decision = {
            "observation_type": observation.observation_type.value,
            "classification": str(
                observation.safe_metadata.get(
                    "classification", observation.observation_type.value
                )
            ),
            "approval_required": approval_required,
            "policy_disposition": policy.disposition.value,
            "policy_reason_codes": list(policy.reason_codes),
            "moved_block_count": plan_diff.moved_block_count,
            "total_displacement_minutes": plan_diff.total_displacement_minutes,
            "preserved_block_count": len(plan_diff.preserved_work_block_ids),
            "portfolio_feasible": repaired.feasible,
            "fallback_explanation": fallback,
        }
        evidence = tuple(
            {
                key: "" if value is None else str(value)
                for key, value in document.items()
            }
            for document in self._plan_mutation_documents(plan_diff)
        )
        try:
            text, metadata = await self._model_interpreter.explain_decision(
                decision, evidence
            )
        except Exception:  # noqa: BLE001 - explanation is non-authoritative
            return {"text": fallback, "source": "deterministic_fallback"}
        return {
            "text": text,
            "source": "gemini",
            "model_id": metadata.model_id,
            "prompt_version": metadata.prompt_version,
            "schema_version": metadata.schema_version,
            "latency_ms": metadata.latency_ms,
        }

    @staticmethod
    def _plan_mutation_documents(plan_diff: PlanDiff) -> list[dict[str, Any]]:
        return [
            {
                "mutation_type": mutation.mutation_type.value,
                "work_block_id": mutation.work_block_id,
                "commitment_id": mutation.commitment_id,
                "before_start": mutation.before.start.isoformat() if mutation.before else None,
                "before_end": mutation.before.end.isoformat() if mutation.before else None,
                "after_start": mutation.after.start.isoformat() if mutation.after else None,
                "after_end": mutation.after.end.isoformat() if mutation.after else None,
                "reason": mutation.reason,
                "calendar_event_id": mutation.calendar_event_id,
            }
            for mutation in plan_diff.mutations
        ]

    @staticmethod
    def _risk_arc(plan: PortfolioPlan) -> dict[str, dict[str, Any]]:
        return {
            commitment_id: {
                key: value
                for key, value in audit.items()
                if key
                in {
                    "detected_allocation_deficit",
                    "shortfall_before_repair",
                    "shortfall_after_repair",
                    "risk_before_repair",
                    "risk_after_repair",
                }
            }
            for commitment_id, audit in plan.risk_audit.items()
            if commitment_id != "_repair"
        }

    # ------------------------------------------------------------------
    # Control change and action results
    # ------------------------------------------------------------------

    async def _handle_commitment_suspended(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        """Release future Calendar capacity after an explicit pause or dismissal."""

        now = self._clock.now()
        commitment_id = str(observation.safe_metadata.get("commitment_id") or "")
        expected_status = (
            LifecycleStatus.PAUSED
            if observation.observation_type == ObservationType.COMMITMENT_PAUSED
            else LifecycleStatus.DISMISSED
        )
        outbox_ids: list[str] = []

        async def _cancel(repositories: RepositorySet) -> tuple[str, ...]:
            commitment = await repositories.commitments.get(commitment_id)
            if commitment is None or commitment.lifecycle_status != expected_status:
                return ()
            controls = await repositories.system_controls.get(request.user_id)
            canceled_ids: list[str] = []
            missing_snapshot_ids: list[str] = []
            for block in await repositories.work_blocks.list_for_commitment(
                commitment_id
            ):
                if block.execution_state != WorkBlockExecutionState.PLANNED:
                    continue
                canceled = block.cancel(now)
                await repositories.work_blocks.save(canceled, block.revision)
                canceled_ids.append(block.work_block_id)
                snapshot = await repositories.calendar_snapshots.get(
                    block.calendar_id,
                    block.calendar_event_id,
                )
                if (
                    snapshot is None
                    or snapshot.is_tombstone
                    or snapshot.observed_event_etag is None
                ):
                    missing_snapshot_ids.append(block.work_block_id)
                    continue
                action_key = self._action_keys.derive(
                    commitment_id,
                    commitment.plan_revision,
                    CalendarActionType.CANCEL,
                    block.work_block_id,
                )
                outbox_id = CanonicalEncoder.hash(["outbox:v1", action_key])
                if await repositories.outbox.get(outbox_id) is None:
                    await repositories.outbox.create(
                        ActionOutbox(
                            outbox_id=outbox_id,
                            user_id=request.user_id,
                            commitment_id=commitment_id,
                            work_block_id=block.work_block_id,
                            action_idempotency_key=action_key,
                            expected_commitment_revision=commitment.revision,
                            expected_plan_revision=commitment.plan_revision,
                            expected_projection_hash="",
                            expected_control_epoch=controls.control_epoch,
                            before_state={
                                "scheduled_start": block.scheduled_start.isoformat(),
                                "scheduled_end": block.scheduled_end.isoformat(),
                                "calendar_event_id": block.calendar_event_id,
                                "observed_event_etag": snapshot.observed_event_etag,
                            },
                            mutation=CalendarMutation(
                                action_type=CalendarActionType.CANCEL,
                                calendar_id=block.calendar_id,
                                calendar_event_id=block.calendar_event_id,
                                work_block_id=block.work_block_id,
                                desired_start=block.scheduled_start,
                                desired_end=block.scheduled_end,
                                expected_observed_event_etag=(
                                    snapshot.observed_event_etag
                                ),
                                private_properties={
                                    "managed_by": "commitmentos",
                                    "commitment_id": commitment_id,
                                    "work_block_id": block.work_block_id,
                                    "plan_revision": str(commitment.plan_revision),
                                },
                            ),
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
                    )
                outbox_ids.append(outbox_id)
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=(
                        ActivityEventType.COMMITMENT_PAUSED
                        if expected_status == LifecycleStatus.PAUSED
                        else ActivityEventType.COMMITMENT_DISMISSED
                    ),
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Future work blocks released from the active portfolio",
                    payload={
                        "source_observation_id": observation.observation_id,
                        "commitment_id": commitment_id,
                        "target_status": expected_status.value,
                        "future_blocks_canceled": canceled_ids,
                        "cancel_outbox_ids": list(outbox_ids),
                        "blocks_waiting_for_calendar_truth": missing_snapshot_ids,
                    },
                    created_at=now,
                )
            )
            return (observation.observation_id, *canceled_ids)

        durable = await self._unit_of_work.run_fenced(
            request.processing_fence,
            _cancel,
        )
        if not durable:
            return self._outcome(request, "ignored", (), "suspension_target_changed")
        for outbox_id in outbox_ids:
            try:
                await self._outbox_dispatcher.dispatch(outbox_id)
            except Exception:  # noqa: BLE001 - durable outbox repairs enqueue gaps
                pass
        if self._portfolio_planning is None:
            return self._outcome(request, "processed", durable, None)
        replanned = await self._handle_portfolio_replan(
            request,
            observation,
            observation.observation_type.value,
        )
        return self._outcome(
            request,
            "processed",
            tuple(dict.fromkeys((*durable, *replanned.durable_outcome_ids))),
            replanned.error_code,
        )

    async def _handle_completion_confirmed(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        """Continuation for explicit completion (plan §4.5, golden audit step 17).

        Pending check-in requests close under the terminal state without any
        verified minute changing; leftover future blocks release their
        Calendar time through guarded cancel intents; the portfolio replan
        removes the completed commitment from the demand set. The commitment
        itself is never mutated here — completion is already terminal.
        """
        now = self._clock.now()
        commitment_id = str(observation.safe_metadata.get("commitment_id") or "")
        outbox_ids: list[str] = []

        async def _close(repositories: RepositorySet) -> tuple[str, ...]:
            commitment = await repositories.commitments.get(commitment_id)
            if commitment is None:
                return ()
            if commitment.lifecycle_status != LifecycleStatus.COMPLETED:
                # A replayed completion observation after an explicit reopen
                # must not re-close anything; only current truth acts.
                return ()
            blocks = tuple(
                await repositories.work_blocks.list_for_commitment(commitment_id)
            )
            closed_check_ins: list[str] = []
            canceled_blocks: list[str] = []
            events_without_snapshot: list[str] = []
            controls = None
            for original in blocks:
                block = original
                if block.execution_state == WorkBlockExecutionState.ACTIVE:
                    block = block.request_check_in(now)
                if block.execution_state == WorkBlockExecutionState.AWAITING_CHECK_IN:
                    updated = block.mark_missed(now)
                    await repositories.work_blocks.save(updated, original.revision)
                    closed_check_ins.append(updated.work_block_id)
                    continue
                if block.execution_state != WorkBlockExecutionState.PLANNED:
                    continue
                canceled = block.cancel(now)
                await repositories.work_blocks.save(canceled, block.revision)
                canceled_blocks.append(canceled.work_block_id)
                snapshot = await repositories.calendar_snapshots.get(
                    block.calendar_id, block.calendar_event_id
                )
                if (
                    snapshot is None
                    or snapshot.is_tombstone
                    or snapshot.observed_event_etag is None
                ):
                    # The 4C drift detector surfaces the leftover event; the
                    # terminal state must not depend on Calendar truth.
                    events_without_snapshot.append(block.calendar_event_id)
                    continue
                if controls is None:
                    controls = await repositories.system_controls.get(request.user_id)
                action_key = self._action_keys.derive(
                    commitment_id,
                    commitment.plan_revision,
                    CalendarActionType.CANCEL,
                    block.work_block_id,
                )
                outbox_id = CanonicalEncoder.hash(["outbox:v1", action_key])
                if await repositories.outbox.get(outbox_id) is not None:
                    outbox_ids.append(outbox_id)
                    continue
                outbox = ActionOutbox(
                    outbox_id=outbox_id,
                    user_id=request.user_id,
                    commitment_id=commitment_id,
                    work_block_id=block.work_block_id,
                    action_idempotency_key=action_key,
                    expected_commitment_revision=commitment.revision,
                    expected_plan_revision=commitment.plan_revision,
                    # Completed commitments carry no current-provenance
                    # projection by design; revision, epoch, and etag guards
                    # still protect this cancel.
                    expected_projection_hash="",
                    expected_control_epoch=controls.control_epoch,
                    before_state={
                        "scheduled_start": block.scheduled_start.isoformat(),
                        "scheduled_end": block.scheduled_end.isoformat(),
                        "calendar_event_id": block.calendar_event_id,
                        "observed_event_etag": snapshot.observed_event_etag,
                    },
                    mutation=CalendarMutation(
                        action_type=CalendarActionType.CANCEL,
                        calendar_id=block.calendar_id,
                        calendar_event_id=block.calendar_event_id,
                        work_block_id=block.work_block_id,
                        desired_start=block.scheduled_start,
                        desired_end=block.scheduled_end,
                        expected_observed_event_etag=snapshot.observed_event_etag,
                        private_properties={
                            "managed_by": "commitmentos",
                            "commitment_id": commitment_id,
                            "work_block_id": block.work_block_id,
                            "plan_revision": str(commitment.plan_revision),
                        },
                    ),
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
                await repositories.outbox.create(outbox)
                outbox_ids.append(outbox_id)
            if closed_check_ins or canceled_blocks:
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.COMPLETION_RECORDED,
                        trace_id=request.trace_id,
                        actor="reconciliation",
                        summary=(
                            "Completion closed pending check-in requests; "
                            "verified minutes unchanged"
                        ),
                        payload={
                            "source_observation_id": observation.observation_id,
                            "commitment_id": commitment_id,
                            "check_in_requests_closed": closed_check_ins,
                            "future_blocks_canceled": canceled_blocks,
                            "cancel_outbox_ids": list(outbox_ids),
                            "events_without_snapshot": events_without_snapshot,
                        },
                        created_at=now,
                    )
                )
            return (
                observation.observation_id,
                *closed_check_ins,
                *canceled_blocks,
            )

        durable = await self._unit_of_work.run_fenced(request.processing_fence, _close)
        if not durable:
            return self._outcome(request, "ignored", (), "completion_target_not_terminal")
        for outbox_id in outbox_ids:
            try:
                await self._outbox_dispatcher.dispatch(outbox_id)
            except Exception:  # noqa: BLE001 - durable outbox repairs the enqueue gap
                pass
        if self._portfolio_planning is None:
            return self._outcome(request, "processed", durable, None)
        replanned = await self._handle_portfolio_replan(
            request, observation, "commitment_completed"
        )
        if replanned.status != "processed":
            return self._outcome(
                request,
                "processed",
                durable,
                replanned.error_code or "projection_refresh_deferred",
            )
        return self._outcome(
            request,
            "processed",
            tuple(dict.fromkeys((*durable, *replanned.durable_outcome_ids))),
            replanned.error_code,
        )

    async def _handle_portfolio_replan(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
        reason: str,
    ) -> ReconciliationOutcome:
        """Recalculate from current facts without reversing prior state.

        Progress and undo inputs publish a fresh planner run and projections.
        Calendar mutations remain governed by the plan-diff/policy path; an
        undo request is therefore compensating reconciliation, never a blind
        restoration of an old document or event body.
        """
        planning = self._portfolio_planning
        if planning is None:
            return self._outcome(request, "rejected", (), "portfolio_planner_unavailable")
        for _attempt in range(3):
            plan = await planning.calculate(request.user_id)
            if not await planning.calendar_inputs_match(plan):
                await self._record_stale_planner_run(
                    request,
                    plan,
                    "calendar_changed_before_replan_publication",
                    create=True,
                )
                continue
            now = self._clock.now()

            async def _publish(repositories: RepositorySet) -> tuple[str, ...]:
                if not await planning.expected_revisions_match(
                    repositories,
                    plan,
                ):
                    stale = replace(
                        plan,
                        status=PlannerRunStatus.STALE,
                        stale_reason="authoritative_revision_changed",
                    )
                    await repositories.planner_runs.create(stale)
                    return ()
                published = replace(
                    plan,
                    status=PlannerRunStatus.PUBLISHED,
                    published_at=now,
                )
                stored = await repositories.planner_runs.get(plan.planner_run_id)
                if stored is None:
                    await repositories.planner_runs.create(published)
                elif stored.status != PlannerRunStatus.PUBLISHED:
                    await repositories.planner_runs.save(
                        published,
                        stored.status.value,
                    )
                else:
                    published = stored
                for commitment_id in published.commitment_order:
                    commitment = await repositories.commitments.get(commitment_id)
                    if commitment is None:
                        return ()
                    blocks = tuple(
                        await repositories.work_blocks.list_for_commitment(commitment_id)
                    )
                    projection = self._projection_guard.rebuild(
                        commitment,
                        blocks,
                        published,
                        now,
                    )
                    await repositories.commitments.save(
                        commitment.with_projection(projection, now),
                        commitment.revision,
                    )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=(
                            ActivityEventType.PLAN_UNDO_REQUESTED
                            if reason == "undo_requested"
                            else ActivityEventType.PLAN_PROPOSED
                        ),
                        trace_id=request.trace_id,
                        actor="portfolio_planner",
                        summary=(
                            "Undo reconciled by replanning from current facts"
                            if reason == "undo_requested"
                            else "Portfolio replanned after verified progress changed"
                        ),
                        payload={
                            "source_observation_id": observation.observation_id,
                            "source_planner_run_id": observation.safe_metadata.get(
                                "planner_run_id"
                            ),
                            "planner_run_id": published.planner_run_id,
                            "commitment_order": list(published.commitment_order),
                            "allocations": self._allocation_documents(published),
                            "reason": reason,
                            "mode": "replan_from_current_facts",
                            "calendar_mutations_written": 0,
                        },
                        created_at=now,
                    )
                )
                return (observation.observation_id, published.planner_run_id)

            durable = await self._unit_of_work.run_fenced(
                request.processing_fence,
                _publish,
            )
            if durable:
                return self._outcome(request, "processed", durable, None)
        return self._outcome(request, "rejected", (), "replan_inputs_kept_changing")

    async def _handle_control_changed(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        if (
            observation.safe_metadata.get("control_name") == "automatic_actions"
            and observation.safe_metadata.get("target_mode") == ControlMode.ENABLED.value
        ):
            summary = await self._outbox_dispatcher.release_held(request.user_id, 50)
            now = self._clock.now()

            async def _record(repositories: RepositorySet) -> None:
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=request.user_id,
                        event_type=ActivityEventType.CONTROL_CHANGED,
                        trace_id=request.trace_id,
                        actor="outbox_dispatcher",
                        summary="Held Calendar actions revalidated after resume",
                        payload={
                            "control_name": "automatic_actions",
                            "control_epoch": observation.safe_metadata.get(
                                "control_epoch"
                            ),
                            "held_actions_scanned": summary.scanned,
                            "actions_reissued": summary.queued,
                            "actions_superseded": summary.superseded,
                            "resume_revalidation_result": (
                                "reissued_current_intent"
                                if summary.queued
                                else "no_current_intent_to_release"
                            ),
                        },
                        created_at=now,
                    )
                )

            await self._unit_of_work.run_fenced(request.processing_fence, _record)
        return self._outcome(request, "processed", (observation.observation_id,), None)

    async def _handle_action_result(
        self,
        request: ReconciliationRequest,
        observation: ObservationV1,
    ) -> ReconciliationOutcome:
        now = self._clock.now()
        work_block_id = observation.safe_metadata.get("work_block_id", "")

        async def _apply(repositories: RepositorySet) -> tuple[str, ...]:
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.OBSERVATION_RECEIVED,
                    trace_id=request.trace_id,
                    actor="reconciliation",
                    summary="Action result observed; desired and actual state verified",
                    payload={
                        "observation_id": observation.observation_id,
                        "work_block_id": work_block_id,
                        "execution_status": observation.safe_metadata.get("execution_status"),
                    },
                    created_at=now,
                )
            )
            return (observation.observation_id,)

        durable_ids = await self._unit_of_work.run_fenced(request.processing_fence, _apply)
        return self._outcome(request, "processed", durable_ids, None)

    # ------------------------------------------------------------------

    async def _record_stale_planner_run(
        self,
        request: ReconciliationRequest,
        plan: PortfolioPlan,
        reason: str,
        *,
        create: bool,
    ) -> None:
        now = self._clock.now()
        stale = replace(
            plan,
            status=PlannerRunStatus.STALE,
            stale_reason=reason,
            published_at=plan.published_at,
        )

        async def _record(repositories: RepositorySet) -> None:
            if create:
                await repositories.planner_runs.create(stale)
            else:
                current = await repositories.planner_runs.get(plan.planner_run_id)
                if current is not None and current.status == PlannerRunStatus.PUBLISHED:
                    await repositories.planner_runs.save(
                        stale,
                        PlannerRunStatus.PUBLISHED.value,
                    )
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=request.user_id,
                    event_type=ActivityEventType.FAILURE_RECORDED,
                    trace_id=request.trace_id,
                    actor="portfolio_planner",
                    summary="Planner run superseded because authoritative inputs changed",
                    payload={
                        "planner_run_id": plan.planner_run_id,
                        "reason": reason,
                        "recalculation_required": True,
                    },
                    created_at=now,
                )
            )

        await self._unit_of_work.run_fenced(request.processing_fence, _record)

    @staticmethod
    def _approval_id(commitment_id: str, request_type: str, commitment_revision: int) -> str:
        return CanonicalEncoder.hash(
            ["approval:v1", commitment_id, request_type, str(commitment_revision)]
        )

    @staticmethod
    def _allocation_documents(plan: PortfolioPlan) -> list[dict[str, Any]]:
        return [
            {
                "commitment_id": allocation.commitment_id,
                "allocated_work_minutes": allocation.allocated_work_minutes,
                "shortfall_minutes": allocation.shortfall_minutes,
                "shared_buffer_minutes": allocation.portfolio_slack_minutes,
                "projected_finish": (
                    allocation.projected_finish.isoformat()
                    if allocation.projected_finish is not None
                    else None
                ),
                "risk_level": allocation.risk_level.value,
            }
            for allocation in plan.allocations
        ]

    @staticmethod
    def _outcome(
        request: ReconciliationRequest,
        status: str,
        durable_outcome_ids: tuple[str, ...],
        error_code: str | None,
    ) -> ReconciliationOutcome:
        return ReconciliationOutcome(
            run_id=request.run_id,
            observation_id=request.observation_id,
            status=status,
            durable_outcome_ids=tuple(durable_outcome_ids),
            error_code=error_code,
            retryable=False,
            processing_fencing_token=request.processing_fence.fencing_token,
        )
