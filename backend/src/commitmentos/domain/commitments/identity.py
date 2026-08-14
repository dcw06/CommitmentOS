from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from commitmentos.domain.commitments.models import (
    Commitment,
    LifecycleStatus,
    OwnershipType,
)


class IdentityOperation(StrEnum):
    CREATE = "create"
    UPDATE_EXISTING = "update_existing"
    SUPERSEDE = "supersede"
    IGNORE = "ignore"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class IdentityProposal:
    operation: IdentityOperation
    target_commitment_id: str | None
    source_span_key: str
    semantic_fingerprint: str
    ownership_type: OwnershipType
    confidence: float


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    operation: IdentityOperation
    target_commitment_id: str | None
    candidate_ids: tuple[str, ...]
    reason: str


# Lifecycle states an update/supersede may target. Completed and canceled
# commitments are terminal; a "new" promise on a completed thread is a new
# commitment, never a resurrection.
_UPDATABLE_STATUSES = frozenset(
    {
        LifecycleStatus.CANDIDATE,
        LifecycleStatus.AWAITING_CONFIRMATION,
        LifecycleStatus.ACTIVE,
        LifecycleStatus.IN_PROGRESS,
        LifecycleStatus.COMPLETION_CANDIDATE,
        LifecycleStatus.PAUSED,
    }
)


class CommitmentIdentityResolver:
    """Deterministic validation of a model-proposed identity operation
    against the thread-linked candidate set (plan §9.6 steps 4–5).

    The model proposes; this resolver decides. An invalid or unverifiable
    proposal is downgraded to `ambiguous` (routed to confirmation) or
    `ignore` — model text can never mint authority over a commitment the
    deterministic rules cannot verify.
    """

    def resolve(
        self,
        proposal: IdentityProposal,
        candidates: Sequence[Commitment],
        dismissed_span_keys: frozenset[str] = frozenset(),
    ) -> IdentityResolution:
        candidate_ids = tuple(c.commitment_id for c in candidates)

        if proposal.operation == IdentityOperation.IGNORE:
            return IdentityResolution(
                IdentityOperation.IGNORE, None, candidate_ids, "model_proposed_ignore"
            )
        if proposal.source_span_key in dismissed_span_keys:
            # Durable user rejection is authoritative regardless of how the
            # model classifies the same unchanged span on a later thread run.
            return IdentityResolution(
                IdentityOperation.IGNORE,
                None,
                candidate_ids,
                "dismissed_span_resurfaced",
            )
        if proposal.operation == IdentityOperation.AMBIGUOUS:
            return IdentityResolution(
                IdentityOperation.AMBIGUOUS, None, candidate_ids, "model_uncertain"
            )

        if proposal.operation == IdentityOperation.CREATE:
            for candidate in candidates:
                if (
                    candidate.lifecycle_status in _UPDATABLE_STATUSES
                    and candidate.semantic_fingerprint == proposal.semantic_fingerprint
                ):
                    # Restatement of an existing commitment: converge instead
                    # of duplicating.
                    return IdentityResolution(
                        IdentityOperation.UPDATE_EXISTING,
                        candidate.commitment_id,
                        candidate_ids,
                        "fingerprint_matches_existing",
                    )
            if proposal.ownership_type == OwnershipType.MY_COMMITMENT:
                # Acceptance convergence: the controlled user promising in a
                # thread with exactly one open request candidate is that
                # request becoming their commitment, not a second record.
                open_requests = [
                    candidate
                    for candidate in candidates
                    if candidate.lifecycle_status in _UPDATABLE_STATUSES
                    and candidate.ownership_type == OwnershipType.REQUEST_TO_ME
                ]
                if len(open_requests) == 1:
                    return IdentityResolution(
                        IdentityOperation.UPDATE_EXISTING,
                        open_requests[0].commitment_id,
                        candidate_ids,
                        "request_accepted_in_thread",
                    )
                if len(open_requests) > 1:
                    return IdentityResolution(
                        IdentityOperation.AMBIGUOUS,
                        None,
                        candidate_ids,
                        "multiple_open_requests",
                    )
            return IdentityResolution(
                IdentityOperation.CREATE, None, candidate_ids, "no_matching_candidate"
            )

        # update_existing / supersede require a verifiable in-thread target.
        target = next(
            (
                c
                for c in candidates
                if c.commitment_id == proposal.target_commitment_id
            ),
            None,
        )
        if target is None:
            return IdentityResolution(
                IdentityOperation.AMBIGUOUS,
                None,
                candidate_ids,
                "target_not_in_candidate_set",
            )
        if target.lifecycle_status not in _UPDATABLE_STATUSES:
            return IdentityResolution(
                IdentityOperation.AMBIGUOUS,
                None,
                candidate_ids,
                f"target_terminal:{target.lifecycle_status.value}",
            )
        if not self._ownership_compatible(target.ownership_type, proposal.ownership_type):
            return IdentityResolution(
                IdentityOperation.AMBIGUOUS,
                None,
                candidate_ids,
                "ownership_incompatible",
            )
        return IdentityResolution(
            proposal.operation,
            target.commitment_id,
            candidate_ids,
            "target_verified",
        )

    @staticmethod
    def _ownership_compatible(current: OwnershipType, proposed: OwnershipType) -> bool:
        if current == proposed:
            return True
        # A request the controlled user has now accepted becomes their
        # commitment; every other ownership flip needs confirmation.
        return (
            current == OwnershipType.REQUEST_TO_ME
            and proposed == OwnershipType.MY_COMMITMENT
        )
