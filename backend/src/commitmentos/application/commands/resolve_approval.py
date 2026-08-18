from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from commitmentos.application.dto import AuthenticatedActor, CommandResult, CommandStatus
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.domain.audit.models import ActivityEventFactory, ActivityEventType
from commitmentos.domain.commitments.models import OwnershipType
from commitmentos.domain.shared.errors import DomainError

VALID_DECISIONS = {"approve", "reject"}


class ResolveApproval:
    """Compare-and-set an approval decision and continue through an observation.

    One transaction resolves the decision, records the authoritative fact
    (confirmed effort for an effort approval), appends activity, and creates
    the `approval_resolved` observation. The reconciliation task is enqueued
    only after commit; a stale commitment revision supersedes the approval
    instead of applying old intent.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        observation_factory: ObservationFactory,
        observation_dispatcher: ObservationDispatcher,
        clock: Clock,
        activity_factory: ActivityEventFactory | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._observation_factory = observation_factory
        self._observation_dispatcher = observation_dispatcher
        self._clock = clock
        self._activity_factory = activity_factory or ActivityEventFactory()

    async def execute(
        self,
        actor: AuthenticatedActor,
        approval_id: str,
        decision: Mapping[str, Any],
        expected_revision: int,
        trace_id: str,
    ) -> CommandResult:
        self._validate_decision(decision)
        now = self._clock.now()

        async def _resolve(repositories: RepositorySet) -> CommandResult:
            approval = await repositories.approvals.get(approval_id)
            if approval is None:
                return CommandResult(
                    status=CommandStatus.TERMINAL_FAILURE,
                    identifiers={"approval_id": approval_id},
                    revision=None,
                    error_code="approval_not_found",
                )
            if approval.get("user_id") != actor.user_id:
                return CommandResult(
                    status=CommandStatus.TERMINAL_FAILURE,
                    identifiers={"approval_id": approval_id},
                    revision=None,
                    error_code="approval_forbidden",
                )
            if approval.get("status") != "pending":
                # Idempotent replay of an already-settled decision.
                return CommandResult(
                    status=CommandStatus.NO_OP,
                    identifiers={"approval_id": approval_id},
                    revision=approval.get("revision"),
                    error_code="approval_already_resolved",
                )
            expires_at = approval.get("expires_at")
            if expires_at is not None and expires_at < now:
                await repositories.approvals.resolve(
                    approval_id,
                    expected_revision,
                    {"status": "superseded", "reason": "expired", "actor": actor.user_id},
                )
                return CommandResult(
                    status=CommandStatus.NO_OP,
                    identifiers={"approval_id": approval_id},
                    revision=None,
                    error_code="approval_expired",
                )
            request_type = approval.get("request_type")
            commitment = None
            commitment_id = str(approval.get("commitment_id") or "")
            if commitment_id:
                commitment = await repositories.commitments.get(commitment_id)
            if request_type not in {
                "identity_confirmation",
                "deadline_required_confirmation",
            } and commitment is None:
                return CommandResult(
                    status=CommandStatus.TERMINAL_FAILURE,
                    identifiers={"approval_id": approval_id},
                    revision=None,
                    error_code="commitment_not_found",
                )
            if (
                commitment is not None
                and commitment.revision != approval.get("commitment_revision")
            ):
                # The commitment changed while the approval was pending; the
                # stale request is superseded and reconciliation recalculates.
                await repositories.approvals.resolve(
                    approval_id,
                    expected_revision,
                    {
                        "status": "superseded",
                        "reason": "commitment_revision_changed",
                        "actor": actor.user_id,
                    },
                )
                await repositories.activity.append(
                    self._activity_factory.create(
                        user_id=actor.user_id,
                        event_type=ActivityEventType.FAILURE_RECORDED,
                        trace_id=trace_id,
                        actor=actor.user_id,
                        summary="Approval superseded: the commitment changed while it was pending",
                        payload={
                            "approval_id": approval_id,
                            "approval_commitment_revision": approval.get("commitment_revision"),
                            "current_commitment_revision": commitment.revision,
                        },
                        created_at=now,
                    )
                )
                return CommandResult(
                    status=CommandStatus.NO_OP,
                    identifiers={"approval_id": approval_id},
                    revision=None,
                    error_code="approval_superseded",
                )
            if request_type == "identity_confirmation" and decision["decision"] == "approve":
                proposed = approval.get("payload", {}).get("proposal", {})
                confirmed_ownership = decision.get("ownership_type")
                if (
                    proposed.get("ownership_type") == OwnershipType.AMBIGUOUS.value
                    and confirmed_ownership is None
                ):
                    return CommandResult(
                        status=CommandStatus.TERMINAL_FAILURE,
                        identifiers={"approval_id": approval_id},
                        revision=approval.get("revision"),
                        error_code="confirmed_ownership_required",
                    )
            if (
                request_type == "effort_confirmation"
                and decision["decision"] == "approve"
                and "confirmed_minutes" not in decision
            ):
                return CommandResult(
                    status=CommandStatus.TERMINAL_FAILURE,
                    identifiers={"approval_id": approval_id},
                    revision=approval.get("revision"),
                    error_code="confirmed_minutes_required",
                )
            if (
                request_type == "deadline_required_confirmation"
                and decision["decision"] == "approve"
                and "deadline" not in decision
            ):
                return CommandResult(
                    status=CommandStatus.TERMINAL_FAILURE,
                    identifiers={"approval_id": approval_id},
                    revision=approval.get("revision"),
                    error_code="deadline_required",
                )
            if (
                request_type == "deadline_required_confirmation"
                and decision["decision"] == "approve"
                and datetime.fromisoformat(str(decision["deadline"])) <= now
            ):
                return CommandResult(
                    status=CommandStatus.TERMINAL_FAILURE,
                    identifiers={"approval_id": approval_id},
                    revision=approval.get("revision"),
                    error_code="deadline_must_be_future",
                )
            if (
                request_type
                in ("calendar_invalid_move_decision", "calendar_user_deleted_decision")
                and decision["decision"] == "approve"
            ):
                choice = decision.get("choice")
                options = tuple(approval.get("payload", {}).get("options", ()))
                if choice not in options:
                    return CommandResult(
                        status=CommandStatus.TERMINAL_FAILURE,
                        identifiers={"approval_id": approval_id},
                        revision=approval.get("revision"),
                        error_code="calendar_decision_choice_required",
                    )
                work_block_id = str(
                    approval.get("payload", {}).get("work_block_id") or ""
                )
                expected_work_block_revision = approval.get("payload", {}).get(
                    "expected_work_block_revision"
                )
                work_block = await repositories.work_blocks.get(work_block_id)
                if (
                    work_block is None
                    or work_block.revision != expected_work_block_revision
                ):
                    await repositories.approvals.resolve(
                        approval_id,
                        expected_revision,
                        {
                            "status": "superseded",
                            "reason": "work_block_revision_changed",
                            "actor": actor.user_id,
                        },
                    )
                    return CommandResult(
                        status=CommandStatus.NO_OP,
                        identifiers={"approval_id": approval_id},
                        revision=None,
                        error_code="approval_superseded",
                    )
            resolved = await repositories.approvals.resolve(
                approval_id,
                expected_revision,
                {
                    "status": "resolved",
                    "decision": decision["decision"],
                    "payload": dict(decision),
                    "actor": actor.user_id,
                    "resolved_at": now,
                },
            )
            new_commitment_revision = commitment.revision if commitment is not None else 0
            if (
                approval.get("request_type") == "effort_confirmation"
                and decision["decision"] == "approve"
            ):
                if commitment is None:
                    raise RuntimeError("effort approval requires a commitment")
                confirmed = commitment.confirm_effort(int(decision["confirmed_minutes"]), now)
                await repositories.commitments.save(confirmed, commitment.revision)
                new_commitment_revision = confirmed.revision
            await repositories.activity.append(
                self._activity_factory.create(
                    user_id=actor.user_id,
                    event_type=(
                        ActivityEventType.DEADLINE_CHANGE_CONFIRMATION
                        if approval.get("request_type")
                        == "deadline_change_confirmation"
                        else (
                            ActivityEventType.DEADLINE_REQUIRED_CONFIRMATION
                            if approval.get("request_type")
                            == "deadline_required_confirmation"
                            else (
                                ActivityEventType.RETRACTION_CONFIRMATION
                                if approval.get("request_type")
                                == "retraction_confirmation"
                                else ActivityEventType.CONFIRMATION_RECORDED
                            )
                        )
                    ),
                    trace_id=trace_id,
                    actor=actor.user_id,
                    summary=(
                        f"{approval.get('request_type')} "
                        f"{'approved' if decision['decision'] == 'approve' else 'rejected'}"
                    ),
                    payload={
                        "approval_id": approval_id,
                        "request_type": approval.get("request_type"),
                        "decision": decision["decision"],
                        "commitment_id": approval["commitment_id"],
                        "commitment_revision": new_commitment_revision,
                    },
                    created_at=now,
                )
            )
            observation = self._observation_factory.continuation(
                observation_type=ObservationType.APPROVAL_RESOLVED,
                user_id=actor.user_id,
                producer_id=approval_id,
                producer_version=str(resolved["revision"]),
                safe_metadata={
                    "approval_id": approval_id,
                    "request_type": approval.get("request_type"),
                    "decision": decision["decision"],
                    "commitment_id": approval["commitment_id"],
                },
                observed_at=now,
                trace_id=trace_id,
            )
            await repositories.observations.create(observation)
            return CommandResult(
                status=CommandStatus.COMPLETED,
                identifiers={
                    "approval_id": approval_id,
                    "observation_id": observation.observation_id,
                },
                revision=resolved["revision"],
                error_code=None,
            )

        try:
            result = await self._unit_of_work.run(_resolve)
        except DomainError as error:
            return CommandResult(
                status=CommandStatus.TERMINAL_FAILURE,
                identifiers={"approval_id": approval_id},
                revision=None,
                error_code=type(error).__name__,
            )
        if result.status == CommandStatus.COMPLETED:
            observation_id = result.identifiers.get("observation_id")
            if observation_id:
                try:
                    await self._observation_dispatcher.dispatch(observation_id)
                except Exception:  # noqa: BLE001 - dispatch is repairable
                    # The observation committed; the periodic dispatcher will
                    # recreate the same named task (write-before-enqueue gap).
                    pass
        return result

    def _validate_decision(self, decision: Mapping[str, Any]) -> None:
        if decision.get("decision") not in VALID_DECISIONS:
            raise ValueError("decision must be one of approve or reject")
        if decision.get("decision") == "approve" and "confirmed_minutes" in decision:
            minutes = decision["confirmed_minutes"]
            if (
                not isinstance(minutes, int)
                or isinstance(minutes, bool)
                or minutes < 30
                or minutes > 2400
                or minutes % 15 != 0
            ):
                raise ValueError(
                    "confirmed_minutes must be 30–2400 in 15-minute increments"
                )
        if decision.get("decision") == "approve" and "deadline" in decision:
            try:
                deadline = datetime.fromisoformat(str(decision["deadline"]))
            except ValueError as error:
                raise ValueError("deadline must be an ISO-8601 datetime") from error
            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise ValueError("deadline must include a timezone")
        if "ownership_type" in decision:
            try:
                OwnershipType(str(decision["ownership_type"]))
            except ValueError as error:
                raise ValueError("ownership_type is not supported") from error
