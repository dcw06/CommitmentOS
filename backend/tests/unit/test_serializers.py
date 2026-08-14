"""Firestore serialization round trips (§16.2 contract row)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from test_domain_models import make_commitment, make_outbox

from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.commitments.models import (
    BlockingStatus,
    CommitmentProjection,
    RiskLevel,
)
from commitmentos.domain.controls.models import initial_system_controls
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)
from commitmentos.infrastructure.firestore.serializers import SerializerRegistry

NOW = datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc)
REGISTRY = SerializerRegistry()


class TestRoundTrips:
    def test_commitment_with_projection(self) -> None:
        commitment = make_commitment(
            projection=CommitmentProjection(
                verified_completed_minutes=60,
                remaining_minutes=120,
                risk_level=RiskLevel.ON_TRACK,
                blocking_status=BlockingStatus.CLEAR,
                source_commitment_revision=1,
                source_work_block_revision_hash="sha256:abc",
                planner_run_id="planner-1",
                calculator_version="portfolio-risk-v1",
                computed_at=NOW,
            )
        )
        document = REGISTRY.commitments.to_document(commitment)
        restored = REGISTRY.commitments.from_document(commitment.commitment_id, document)
        assert restored == commitment

    def test_commitment_without_projection(self) -> None:
        commitment = make_commitment()
        document = REGISTRY.commitments.to_document(commitment)
        assert REGISTRY.commitments.from_document(commitment.commitment_id, document) == commitment

    def test_outbox_with_mutation_response(self) -> None:
        from commitmentos.domain.actions.models import MutationResponseEvidence

        action = make_outbox().claim("tok", NOW + timedelta(minutes=2), NOW).start_external_io(NOW)
        action = action.succeed(
            MutationResponseEvidence(
                mutation_response_etag='"etag-9"',
                mutation_response_payload_hash="hash",
                mutation_response_status="applied",
                mutation_response_received_at=NOW,
            ),
            NOW,
        )
        document = REGISTRY.outbox.to_document(action)
        assert REGISTRY.outbox.from_document(action.outbox_id, document) == action

    def test_observation(self) -> None:
        observation = ObservationFactory().source_change(
            observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
            user_id="u1",
            producer_id="u1:m1",
            producer_version="v1",
            source="gmail",
            external_id="m1",
            external_version="v1",
            payload_hash="h",
            source_reference={"thread_id": "t1"},
            safe_metadata={"seeded": True},
            observed_at=NOW,
            trace_id="trace",
        )
        document = REGISTRY.observations.to_document(observation)
        assert (
            REGISTRY.observations.from_document(observation.observation_id, document)
            == observation
        )

    def test_work_block(self) -> None:
        block = WorkBlock(
            work_block_id="b1",
            commitment_id="c1",
            revision=2,
            calendar_id="primary",
            calendar_event_id="evt",
            calendar_snapshot_id=None,
            duration_minutes=60,
            execution_state=WorkBlockExecutionState.PLANNED,
            scheduled_start=NOW,
            scheduled_end=NOW + timedelta(hours=1),
            verified_minutes=0,
            completion_evidence_id=None,
            user_edit_state=UserEditState.NONE,
            plan_revision=1,
        )
        document = REGISTRY.work_blocks.to_document(block)
        assert REGISTRY.work_blocks.from_document(block.work_block_id, document) == block

    def test_system_controls(self) -> None:
        controls = initial_system_controls("u1", NOW)
        document = REGISTRY.system_controls.to_document(controls)
        assert REGISTRY.system_controls.from_document("u1", document) == controls

    def test_activity_event(self) -> None:
        event = ActivityEventFactory().create(
            user_id="u1",
            event_type=ActivityEventType.OUTBOX_WRITTEN,
            trace_id="trace",
            actor="reconciliation",
            summary="s",
            payload={"outbox_ids": ["a", "b"]},
            created_at=NOW,
        )
        document = REGISTRY.activity.to_document(event)
        assert REGISTRY.activity.from_document(event.activity_event_id, document) == event
