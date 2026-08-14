from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, Mapping, Protocol, TypeVar

from commitmentos.contracts.auth import OAuthTransaction, OAuthTransactionStatus
from commitmentos.contracts.observations import (
    ObservationType,
    ObservationV1,
    ReconciliationStatus,
)
from commitmentos.contracts.synchronization import (
    SyncCursor,
    SyncGeneration,
    SyncGenerationItem,
    SyncGenerationItemStatus,
    SyncGenerationMode,
    SyncGenerationStatus,
    SyncManifestHash,
)
from commitmentos.contracts.tasks import SourceType
from commitmentos.domain.actions.models import (
    ActionOutbox,
    CalendarActionType,
    CalendarMutation,
    DispatchStatus,
    ExecutionStatus,
    MutationResponseEvidence,
)
from commitmentos.domain.audit.models import ActivityEvent, ActivityEventType
from commitmentos.domain.commitments.models import (
    BlockingStatus,
    Commitment,
    CommitmentProjection,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
    RiskLevel,
)
from commitmentos.domain.controls.models import ControlMode, SystemControls
from commitmentos.domain.planning.calendar_state import CalendarEventSnapshot
from commitmentos.domain.planning.models import (
    CommitmentAllocation,
    PlannedWorkBlock,
    PlannerRunStatus,
    PortfolioPlan,
    TimeInterval,
)
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)

T = TypeVar("T")


class DocumentSerializer(Protocol, Generic[T]):
    def to_document(self, value: T) -> Mapping[str, Any]:
        ...

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> T:
        ...


def _utc(value: Any) -> datetime | None:
    """Normalize Firestore timestamp values to timezone-aware UTC datetimes."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, found {type(value)!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_utc(value: Any) -> datetime:
    result = _utc(value)
    if result is None:
        raise ValueError("required timestamp is missing")
    return result


class CommitmentSerializer:
    def to_document(self, value: Commitment) -> Mapping[str, Any]:
        document: dict[str, Any] = {
            "user_id": value.user_id,
            "revision": value.revision,
            "source_thread_id": value.source_thread_id,
            "semantic_fingerprint": value.semantic_fingerprint,
            "title": value.title,
            "description": value.description,
            "ownership_type": value.ownership_type.value,
            "owner": dict(value.owner),
            "beneficiary": dict(value.beneficiary),
            "deadline": {
                "value": value.deadline.value,
                "timezone": value.deadline.timezone,
                "confidence": value.deadline.confidence,
                "evidence_id": value.deadline.evidence_id,
                "source_expression": value.deadline.source_expression,
                "rule_version": value.deadline.rule_version,
            },
            "effort": {
                "proposed_minutes": value.effort.proposed_minutes,
                "confidence": value.effort.confidence,
                "confirmed_minutes": value.effort.confirmed_minutes,
                "confirmed_at": value.effort.confirmed_at,
            },
            "lifecycle_status": value.lifecycle_status.value,
            "completion_evidence_id": value.completion_evidence_id,
            "completed_at": value.completed_at,
            "plan_revision": value.plan_revision,
            "projection": None,
            "policy_profile": value.policy_profile,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }
        if value.projection is not None:
            document["projection"] = {
                "verified_completed_minutes": value.projection.verified_completed_minutes,
                "remaining_minutes": value.projection.remaining_minutes,
                "risk_level": value.projection.risk_level.value,
                "blocking_status": value.projection.blocking_status.value,
                "source_commitment_revision": value.projection.source_commitment_revision,
                "source_work_block_revision_hash": value.projection.source_work_block_revision_hash,
                "planner_run_id": value.projection.planner_run_id,
                "calculator_version": value.projection.calculator_version,
                "computed_at": value.projection.computed_at,
            }
        return document

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> Commitment:
        projection_data = value.get("projection")
        projection = None
        if projection_data is not None:
            projection = CommitmentProjection(
                verified_completed_minutes=projection_data["verified_completed_minutes"],
                remaining_minutes=projection_data["remaining_minutes"],
                risk_level=RiskLevel(projection_data["risk_level"]),
                blocking_status=BlockingStatus(projection_data["blocking_status"]),
                source_commitment_revision=projection_data["source_commitment_revision"],
                source_work_block_revision_hash=projection_data["source_work_block_revision_hash"],
                planner_run_id=projection_data["planner_run_id"],
                calculator_version=projection_data["calculator_version"],
                computed_at=_require_utc(projection_data["computed_at"]),
            )
        deadline_data = value["deadline"]
        effort_data = value["effort"]
        return Commitment(
            commitment_id=document_id,
            user_id=value["user_id"],
            revision=value["revision"],
            source_thread_id=value["source_thread_id"],
            semantic_fingerprint=value["semantic_fingerprint"],
            title=value["title"],
            description=value["description"],
            ownership_type=OwnershipType(value["ownership_type"]),
            owner=dict(value["owner"]),
            beneficiary=dict(value["beneficiary"]),
            deadline=Deadline(
                value=_require_utc(deadline_data["value"]),
                timezone=deadline_data["timezone"],
                confidence=deadline_data["confidence"],
                evidence_id=deadline_data["evidence_id"],
                source_expression=deadline_data["source_expression"],
                rule_version=deadline_data["rule_version"],
            ),
            effort=Effort(
                proposed_minutes=effort_data["proposed_minutes"],
                confidence=effort_data["confidence"],
                confirmed_minutes=effort_data["confirmed_minutes"],
                confirmed_at=_utc(effort_data["confirmed_at"]),
            ),
            lifecycle_status=LifecycleStatus(value["lifecycle_status"]),
            completion_evidence_id=value["completion_evidence_id"],
            completed_at=_utc(value["completed_at"]),
            plan_revision=value["plan_revision"],
            projection=projection,
            policy_profile=value["policy_profile"],
            created_at=_require_utc(value["created_at"]),
            updated_at=_require_utc(value["updated_at"]),
        )


class WorkBlockSerializer:
    def to_document(self, value: WorkBlock) -> Mapping[str, Any]:
        return {
            "commitment_id": value.commitment_id,
            "revision": value.revision,
            "calendar_id": value.calendar_id,
            "calendar_event_id": value.calendar_event_id,
            "calendar_snapshot_id": value.calendar_snapshot_id,
            "duration_minutes": value.duration_minutes,
            "execution_state": value.execution_state.value,
            "scheduled_start": value.scheduled_start,
            "scheduled_end": value.scheduled_end,
            "verified_minutes": value.verified_minutes,
            "completion_evidence_id": value.completion_evidence_id,
            "user_edit_state": value.user_edit_state.value,
            "plan_revision": value.plan_revision,
        }

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> WorkBlock:
        return WorkBlock(
            work_block_id=document_id,
            commitment_id=value["commitment_id"],
            revision=value["revision"],
            calendar_id=value["calendar_id"],
            calendar_event_id=value["calendar_event_id"],
            calendar_snapshot_id=value["calendar_snapshot_id"],
            duration_minutes=value["duration_minutes"],
            execution_state=WorkBlockExecutionState(value["execution_state"]),
            scheduled_start=_require_utc(value["scheduled_start"]),
            scheduled_end=_require_utc(value["scheduled_end"]),
            verified_minutes=value["verified_minutes"],
            completion_evidence_id=value["completion_evidence_id"],
            user_edit_state=UserEditState(value["user_edit_state"]),
            plan_revision=value["plan_revision"],
        )


class ObservationSerializer:
    def to_document(self, value: ObservationV1) -> Mapping[str, Any]:
        return {
            "observation_type": value.observation_type.value,
            "user_id": value.user_id,
            "producer_id": value.producer_id,
            "producer_version": value.producer_version,
            "source": value.source,
            "external_id": value.external_id,
            "external_version": value.external_version,
            "observed_at": value.observed_at,
            "payload_hash": value.payload_hash,
            "source_reference": dict(value.source_reference),
            "safe_metadata": dict(value.safe_metadata),
            "reconciliation_status": value.reconciliation_status.value,
            "dispatch_generation": value.dispatch_generation,
            "held_control_epoch": value.held_control_epoch,
            "claimed_control_epoch": value.claimed_control_epoch,
            "processing_lease_key": value.processing_lease_key,
            "processing_lease_owner": value.processing_lease_owner,
            "processing_fencing_token": value.processing_fencing_token,
            "processing_lease_expires_at": value.processing_lease_expires_at,
            "processing_attempt": value.processing_attempt,
            "trace_id": value.trace_id,
        }

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> ObservationV1:
        return ObservationV1(
            observation_id=document_id,
            observation_type=ObservationType(value["observation_type"]),
            user_id=value["user_id"],
            producer_id=value["producer_id"],
            producer_version=value["producer_version"],
            source=value["source"],
            external_id=value["external_id"],
            external_version=value["external_version"],
            observed_at=_require_utc(value["observed_at"]),
            payload_hash=value["payload_hash"],
            source_reference=dict(value["source_reference"]),
            safe_metadata=dict(value["safe_metadata"]),
            reconciliation_status=ReconciliationStatus(value["reconciliation_status"]),
            dispatch_generation=value["dispatch_generation"],
            held_control_epoch=value["held_control_epoch"],
            claimed_control_epoch=value["claimed_control_epoch"],
            processing_lease_key=value["processing_lease_key"],
            processing_lease_owner=value["processing_lease_owner"],
            processing_fencing_token=value["processing_fencing_token"],
            processing_lease_expires_at=_utc(value["processing_lease_expires_at"]),
            processing_attempt=value["processing_attempt"],
            trace_id=value["trace_id"],
        )


class ActionOutboxSerializer:
    def to_document(self, value: ActionOutbox) -> Mapping[str, Any]:
        document: dict[str, Any] = {
            "user_id": value.user_id,
            "commitment_id": value.commitment_id,
            "work_block_id": value.work_block_id,
            "action_idempotency_key": value.action_idempotency_key,
            "expected_commitment_revision": value.expected_commitment_revision,
            "expected_plan_revision": value.expected_plan_revision,
            "expected_projection_hash": value.expected_projection_hash,
            "expected_control_epoch": value.expected_control_epoch,
            "mutation": {
                "action_type": value.mutation.action_type.value,
                "calendar_id": value.mutation.calendar_id,
                "calendar_event_id": value.mutation.calendar_event_id,
                "work_block_id": value.mutation.work_block_id,
                "desired_start": value.mutation.desired_start,
                "desired_end": value.mutation.desired_end,
                "expected_observed_event_etag": value.mutation.expected_observed_event_etag,
                "private_properties": dict(value.mutation.private_properties),
            },
            "dispatch_status": value.dispatch_status.value,
            "execution_status": value.execution_status.value,
            "claim_token": value.claim_token,
            "claim_lease_expires_at": value.claim_lease_expires_at,
            "attempts": value.attempts,
            "mutation_response": None,
            "error": dict(value.error) if value.error is not None else None,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }
        if value.mutation_response is not None:
            document["mutation_response"] = {
                "mutation_response_etag": value.mutation_response.mutation_response_etag,
                "mutation_response_payload_hash": value.mutation_response.mutation_response_payload_hash,
                "mutation_response_status": value.mutation_response.mutation_response_status,
                "mutation_response_received_at": value.mutation_response.mutation_response_received_at,
            }
        return document

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> ActionOutbox:
        mutation_data = value["mutation"]
        response_data = value.get("mutation_response")
        response = None
        if response_data is not None:
            response = MutationResponseEvidence(
                mutation_response_etag=response_data["mutation_response_etag"],
                mutation_response_payload_hash=response_data["mutation_response_payload_hash"],
                mutation_response_status=response_data["mutation_response_status"],
                mutation_response_received_at=_require_utc(
                    response_data["mutation_response_received_at"]
                ),
            )
        return ActionOutbox(
            outbox_id=document_id,
            user_id=value["user_id"],
            commitment_id=value["commitment_id"],
            work_block_id=value["work_block_id"],
            action_idempotency_key=value["action_idempotency_key"],
            expected_commitment_revision=value["expected_commitment_revision"],
            expected_plan_revision=value["expected_plan_revision"],
            expected_projection_hash=value["expected_projection_hash"],
            expected_control_epoch=value["expected_control_epoch"],
            mutation=CalendarMutation(
                action_type=CalendarActionType(mutation_data["action_type"]),
                calendar_id=mutation_data["calendar_id"],
                calendar_event_id=mutation_data["calendar_event_id"],
                work_block_id=mutation_data["work_block_id"],
                desired_start=_utc(mutation_data["desired_start"]),
                desired_end=_utc(mutation_data["desired_end"]),
                expected_observed_event_etag=mutation_data["expected_observed_event_etag"],
                private_properties=dict(mutation_data["private_properties"]),
            ),
            dispatch_status=DispatchStatus(value["dispatch_status"]),
            execution_status=ExecutionStatus(value["execution_status"]),
            claim_token=value["claim_token"],
            claim_lease_expires_at=_utc(value["claim_lease_expires_at"]),
            attempts=value["attempts"],
            mutation_response=response,
            error=dict(value["error"]) if value.get("error") is not None else None,
            created_at=_require_utc(value["created_at"]),
            updated_at=_require_utc(value["updated_at"]),
        )


class ActivityEventSerializer:
    def to_document(self, value: ActivityEvent) -> Mapping[str, Any]:
        return {
            "user_id": value.user_id,
            "event_type": value.event_type.value,
            "trace_id": value.trace_id,
            "actor": value.actor,
            "summary": value.summary,
            "payload": dict(value.payload),
            "created_at": value.created_at,
        }

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> ActivityEvent:
        return ActivityEvent(
            activity_event_id=document_id,
            user_id=value["user_id"],
            event_type=ActivityEventType(value["event_type"]),
            trace_id=value["trace_id"],
            actor=value["actor"],
            summary=value["summary"],
            payload=dict(value["payload"]),
            created_at=_require_utc(value["created_at"]),
        )


class SystemControlsSerializer:
    def to_document(self, value: SystemControls) -> Mapping[str, Any]:
        return {
            "user_id": value.user_id,
            "observation_mode": value.observation_mode.value,
            "automatic_action_mode": value.automatic_action_mode.value,
            "control_epoch": value.control_epoch,
            "actor": value.actor,
            "reason": value.reason,
            "updated_at": value.updated_at,
        }

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> SystemControls:
        return SystemControls(
            user_id=value["user_id"],
            observation_mode=ControlMode(value["observation_mode"]),
            automatic_action_mode=ControlMode(value["automatic_action_mode"]),
            control_epoch=value["control_epoch"],
            actor=value["actor"],
            reason=value["reason"],
            updated_at=_require_utc(value["updated_at"]),
        )


class CalendarEventSnapshotSerializer:
    def to_document(self, value: CalendarEventSnapshot) -> Mapping[str, Any]:
        return {
            "calendar_snapshot_id": value.calendar_snapshot_id,
            "user_id": value.user_id,
            "calendar_id": value.calendar_id,
            "calendar_event_id": value.calendar_event_id,
            "calendar_state_revision": value.calendar_state_revision,
            "observed_event_etag": value.observed_event_etag,
            "observed_status": value.observed_status,
            "observed_start": value.observed_start,
            "observed_end": value.observed_end,
            "observed_timezone": value.observed_timezone,
            "observed_all_day": value.observed_all_day,
            "observed_transparency": value.observed_transparency,
            "observed_recurring_event_id": value.observed_recurring_event_id,
            "observed_original_start_time": value.observed_original_start_time,
            "observed_managed_by": value.observed_managed_by,
            "observed_commitment_id": value.observed_commitment_id,
            "observed_work_block_id": value.observed_work_block_id,
            "observed_plan_revision": value.observed_plan_revision,
            "observed_payload_hash": value.observed_payload_hash,
            "source_observation_id": value.source_observation_id,
            "observed_at": value.observed_at,
            "is_tombstone": value.is_tombstone,
            "deleted_at": value.deleted_at,
        }

    def from_document(
        self,
        document_id: str,
        value: Mapping[str, Any],
    ) -> CalendarEventSnapshot:
        return CalendarEventSnapshot(
            calendar_snapshot_id=document_id,
            user_id=value["user_id"],
            calendar_id=value["calendar_id"],
            calendar_event_id=value["calendar_event_id"],
            calendar_state_revision=value["calendar_state_revision"],
            observed_event_etag=value["observed_event_etag"],
            observed_status=value["observed_status"],
            observed_start=_utc(value["observed_start"]),
            observed_end=_utc(value["observed_end"]),
            observed_timezone=value["observed_timezone"],
            observed_all_day=value["observed_all_day"],
            observed_transparency=value["observed_transparency"],
            observed_recurring_event_id=value["observed_recurring_event_id"],
            observed_original_start_time=_utc(value["observed_original_start_time"]),
            observed_managed_by=value["observed_managed_by"],
            observed_commitment_id=value["observed_commitment_id"],
            observed_work_block_id=value["observed_work_block_id"],
            observed_plan_revision=value["observed_plan_revision"],
            observed_payload_hash=value["observed_payload_hash"],
            source_observation_id=value["source_observation_id"],
            observed_at=_require_utc(value["observed_at"]),
            is_tombstone=value["is_tombstone"],
            deleted_at=_utc(value["deleted_at"]),
        )


class OAuthTransactionSerializer:
    def to_document(self, value: OAuthTransaction) -> Mapping[str, Any]:
        return {
            "state_hash": value.state_hash,
            "nonce": value.nonce,
            "code_verifier": value.code_verifier,
            "return_to": value.return_to,
            "status": value.status.value,
            "revision": value.revision,
            "created_at": value.created_at,
            "expires_at": value.expires_at,
            "consumed_at": value.consumed_at,
        }

    def from_document(
        self,
        document_id: str,
        value: Mapping[str, Any],
    ) -> OAuthTransaction:
        return OAuthTransaction(
            transaction_id=document_id,
            state_hash=value["state_hash"],
            nonce=value["nonce"],
            code_verifier=value["code_verifier"],
            return_to=value["return_to"],
            status=OAuthTransactionStatus(value["status"]),
            revision=value["revision"],
            created_at=_require_utc(value["created_at"]),
            expires_at=_require_utc(value["expires_at"]),
            consumed_at=_utc(value["consumed_at"]),
        )


class PortfolioPlanSerializer:
    def to_document(self, value: PortfolioPlan) -> Mapping[str, Any]:
        return {
            "user_id": value.user_id,
            "planner_version": value.planner_version,
            "constraint_version": value.constraint_version,
            "score_version": value.score_version,
            "threshold_version": value.threshold_version,
            "calendar_state_revision": value.calendar_state_revision,
            "calendar_snapshot_hash": value.calendar_snapshot_hash,
            "expected_revisions": dict(value.expected_revisions),
            "commitment_order": list(value.commitment_order),
            "work_blocks": [
                {
                    "work_block_id": block.work_block_id,
                    "commitment_id": block.commitment_id,
                    "start": block.interval.start,
                    "end": block.interval.end,
                    "duration_minutes": block.duration_minutes,
                    "preserved": block.preserved,
                    "calendar_event_id": block.calendar_event_id,
                }
                for block in value.work_blocks
            ],
            "allocations": [
                {
                    "commitment_id": allocation.commitment_id,
                    "preserved_reserved_minutes": allocation.preserved_reserved_minutes,
                    "allocation_deficit": allocation.allocation_deficit,
                    "newly_allocated_minutes": allocation.newly_allocated_minutes,
                    "allocated_work_minutes": allocation.allocated_work_minutes,
                    "shortfall_minutes": allocation.shortfall_minutes,
                    "portfolio_slack_minutes": allocation.portfolio_slack_minutes,
                    "projected_finish": allocation.projected_finish,
                    "risk_level": allocation.risk_level.value,
                }
                for allocation in value.allocations
            ],
            "unallocated_intervals": [
                {"start": interval.start, "end": interval.end}
                for interval in value.unallocated_intervals
            ],
            "feasible": value.feasible,
            "calculated_at": value.calculated_at,
            "planning_horizon": {
                "start": value.planning_horizon.start,
                "end": value.planning_horizon.end,
            },
            "risk_audit": {
                key: dict(item) for key, item in value.risk_audit.items()
            },
            "status": value.status.value,
            "stale_reason": value.stale_reason,
            "published_at": value.published_at,
        }

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> PortfolioPlan:
        return PortfolioPlan(
            planner_run_id=document_id,
            planner_version=str(value["planner_version"]),
            calendar_state_revision=int(value["calendar_state_revision"]),
            calendar_snapshot_hash=str(value["calendar_snapshot_hash"]),
            expected_revisions={
                str(key): int(revision)
                for key, revision in value["expected_revisions"].items()
            },
            commitment_order=tuple(str(item) for item in value["commitment_order"]),
            work_blocks=tuple(
                PlannedWorkBlock(
                    work_block_id=str(item["work_block_id"]),
                    commitment_id=str(item["commitment_id"]),
                    interval=TimeInterval(
                        _require_utc(item["start"]),
                        _require_utc(item["end"]),
                    ),
                    duration_minutes=int(item["duration_minutes"]),
                    preserved=bool(item["preserved"]),
                    calendar_event_id=(
                        str(item["calendar_event_id"])
                        if item.get("calendar_event_id") is not None
                        else None
                    ),
                )
                for item in value["work_blocks"]
            ),
            allocations=tuple(
                CommitmentAllocation(
                    commitment_id=str(item["commitment_id"]),
                    preserved_reserved_minutes=int(item["preserved_reserved_minutes"]),
                    allocation_deficit=int(item["allocation_deficit"]),
                    newly_allocated_minutes=int(item["newly_allocated_minutes"]),
                    allocated_work_minutes=int(item["allocated_work_minutes"]),
                    shortfall_minutes=int(item["shortfall_minutes"]),
                    portfolio_slack_minutes=int(item["portfolio_slack_minutes"]),
                    projected_finish=_utc(item.get("projected_finish")),
                    risk_level=RiskLevel(str(item["risk_level"])),
                )
                for item in value["allocations"]
            ),
            unallocated_intervals=tuple(
                TimeInterval(
                    _require_utc(item["start"]),
                    _require_utc(item["end"]),
                )
                for item in value["unallocated_intervals"]
            ),
            feasible=bool(value["feasible"]),
            user_id=str(value["user_id"]),
            constraint_version=str(value["constraint_version"]),
            score_version=str(value["score_version"]),
            threshold_version=str(value["threshold_version"]),
            calculated_at=_require_utc(value["calculated_at"]),
            planning_horizon=TimeInterval(
                _require_utc(value["planning_horizon"]["start"]),
                _require_utc(value["planning_horizon"]["end"]),
            ),
            risk_audit={
                str(key): dict(item) for key, item in value.get("risk_audit", {}).items()
            },
            status=PlannerRunStatus(str(value["status"])),
            stale_reason=(
                str(value["stale_reason"])
                if value.get("stale_reason") is not None
                else None
            ),
            published_at=_utc(value.get("published_at")),
        )


def _manifest_to_document(value: SyncManifestHash) -> dict[str, Any]:
    return {
        "algorithm_version": value.algorithm_version,
        "item_count": value.item_count,
        "digest": value.digest,
    }


def _manifest_from_document(value: Mapping[str, Any]) -> SyncManifestHash:
    return SyncManifestHash(
        algorithm_version=value["algorithm_version"],
        item_count=int(value["item_count"]),
        digest=value["digest"],
    )


class SyncGenerationSerializer:
    def to_document(self, value: SyncGeneration) -> Mapping[str, Any]:
        return {
            "sync_request_id": value.sync_request_id,
            "user_id": value.user_id,
            "source": value.source.value,
            "mode": value.mode.value,
            "status": value.status.value,
            "base_published_cursor_revision": value.base_published_cursor_revision,
            "provider_request_parameters": dict(value.provider_request_parameters),
            "current_page_sequence": value.current_page_sequence,
            "next_page_token": value.next_page_token,
            "candidate_next_cursor": value.candidate_next_cursor,
            "staged_manifest": _manifest_to_document(value.staged_manifest),
            "applied_manifest": _manifest_to_document(value.applied_manifest),
            "page_count": value.page_count,
            "staged_item_count": value.staged_item_count,
            "applied_item_count": value.applied_item_count,
            "outstanding_chunk_id": value.outstanding_chunk_id,
            "full_sync_tombstones_complete": value.full_sync_tombstones_complete,
            "source_lease_key": value.source_lease_key,
            "source_lease_owner": value.source_lease_owner,
            "source_fencing_token": value.source_fencing_token,
            "source_lease_expires_at": value.source_lease_expires_at,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
            "published_at": value.published_at,
        }

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> SyncGeneration:
        return SyncGeneration(
            sync_generation_id=document_id,
            sync_request_id=value["sync_request_id"],
            user_id=value["user_id"],
            source=SourceType(value["source"]),
            mode=SyncGenerationMode(value["mode"]),
            status=SyncGenerationStatus(value["status"]),
            base_published_cursor_revision=int(value["base_published_cursor_revision"]),
            provider_request_parameters=dict(value.get("provider_request_parameters", {})),
            current_page_sequence=int(value["current_page_sequence"]),
            next_page_token=value.get("next_page_token"),
            candidate_next_cursor=value.get("candidate_next_cursor"),
            staged_manifest=_manifest_from_document(value["staged_manifest"]),
            applied_manifest=_manifest_from_document(value["applied_manifest"]),
            page_count=int(value["page_count"]),
            staged_item_count=int(value["staged_item_count"]),
            applied_item_count=int(value["applied_item_count"]),
            outstanding_chunk_id=value.get("outstanding_chunk_id"),
            full_sync_tombstones_complete=bool(value["full_sync_tombstones_complete"]),
            source_lease_key=value["source_lease_key"],
            source_lease_owner=value["source_lease_owner"],
            source_fencing_token=value["source_fencing_token"],
            source_lease_expires_at=_require_utc(value["source_lease_expires_at"]),
            created_at=_require_utc(value["created_at"]),
            updated_at=_require_utc(value["updated_at"]),
            published_at=_utc(value.get("published_at")),
        )


class SyncGenerationItemSerializer:
    def to_document(self, value: SyncGenerationItem) -> Mapping[str, Any]:
        return {
            "sync_generation_item_id": value.sync_generation_item_id,
            "sync_generation_id": value.sync_generation_id,
            "page_sequence": value.page_sequence,
            "chunk_sequence": value.chunk_sequence,
            "source": value.source.value,
            "external_id": value.external_id,
            "external_version": value.external_version,
            "payload_hash": value.payload_hash,
            "normalized_kind": value.normalized_kind,
            "normalized_payload": dict(value.normalized_payload),
            "status": value.status.value,
            "staged_at": value.staged_at,
            "applied_at": value.applied_at,
        }

    def from_document(
        self,
        document_id: str,
        value: Mapping[str, Any],
    ) -> SyncGenerationItem:
        return SyncGenerationItem(
            sync_generation_item_id=document_id,
            sync_generation_id=value["sync_generation_id"],
            page_sequence=int(value["page_sequence"]),
            chunk_sequence=int(value["chunk_sequence"]),
            source=SourceType(value["source"]),
            external_id=value["external_id"],
            external_version=value["external_version"],
            payload_hash=value["payload_hash"],
            normalized_kind=value["normalized_kind"],
            normalized_payload=dict(value.get("normalized_payload", {})),
            status=SyncGenerationItemStatus(value["status"]),
            staged_at=_require_utc(value["staged_at"]),
            applied_at=_utc(value.get("applied_at")),
        )


class SyncCursorSerializer:
    def to_document(self, value: SyncCursor) -> Mapping[str, Any]:
        return {
            "user_id": value.user_id,
            "source": value.source.value,
            "revision": value.revision,
            "published_cursor": value.published_cursor,
            "published_generation_id": value.published_generation_id,
            "publish_in_progress_generation_id": value.publish_in_progress_generation_id,
            "generation_counter": value.generation_counter,
            "calendar_state_revision": value.calendar_state_revision,
            "full_resync_required": value.full_resync_required,
            "updated_at": value.updated_at,
        }

    def from_document(self, document_id: str, value: Mapping[str, Any]) -> SyncCursor:
        return SyncCursor(
            user_id=value["user_id"],
            source=SourceType(value["source"]),
            revision=int(value["revision"]),
            published_cursor=value.get("published_cursor"),
            published_generation_id=value.get("published_generation_id"),
            publish_in_progress_generation_id=value.get("publish_in_progress_generation_id"),
            generation_counter=int(value.get("generation_counter", 0)),
            calendar_state_revision=int(value.get("calendar_state_revision", 0)),
            full_resync_required=bool(value.get("full_resync_required", False)),
            updated_at=_require_utc(value["updated_at"]),
        )


class SerializerRegistry:
    """One instance of every document serializer, shared by the repository set."""

    def __init__(self) -> None:
        self.commitments = CommitmentSerializer()
        self.work_blocks = WorkBlockSerializer()
        self.observations = ObservationSerializer()
        self.outbox = ActionOutboxSerializer()
        self.activity = ActivityEventSerializer()
        self.system_controls = SystemControlsSerializer()
        self.calendar_snapshots = CalendarEventSnapshotSerializer()
        self.oauth_transactions = OAuthTransactionSerializer()
        self.portfolio_plans = PortfolioPlanSerializer()
        self.sync_generations = SyncGenerationSerializer()
        self.sync_generation_items = SyncGenerationItemSerializer()
        self.sync_cursors = SyncCursorSerializer()
