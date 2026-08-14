from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.domain.audit.models import ActivityEvent
from commitmentos.domain.commitments.models import Commitment
from commitmentos.domain.progress.models import WorkBlock

ACTIVITY_SCAN_LIMIT = 100


@dataclass(frozen=True, slots=True)
class CommitmentDetail:
    """The source evidence view: the commitment, its minimal evidence
    excerpts with source references, work blocks, and related audit trail."""

    commitment: Commitment
    evidence: tuple[Mapping[str, Any], ...]
    work_blocks: tuple[WorkBlock, ...]
    activity: tuple[ActivityEvent, ...]


class GetCommitment:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def execute(self, user_id: str, commitment_id: str) -> CommitmentDetail | None:
        async def _load(repositories: RepositorySet) -> CommitmentDetail | None:
            commitment = await repositories.commitments.get(commitment_id)
            if commitment is None or commitment.user_id != user_id:
                # Absent and not-owned are indistinguishable to the caller.
                return None
            evidence = tuple(
                dict(item)
                for item in await repositories.evidence.list_for_commitment(commitment_id)
            )
            work_blocks = tuple(
                await repositories.work_blocks.list_for_commitment(commitment_id)
            )
            related_activity = tuple(
                event
                for event in await repositories.activity.list_for_user(
                    user_id, None, ACTIVITY_SCAN_LIMIT
                )
                if event.payload.get("commitment_id") == commitment_id
            )
            return CommitmentDetail(
                commitment=commitment,
                evidence=evidence,
                work_blocks=work_blocks,
                activity=related_activity,
            )

        return await self._unit_of_work.read(_load)
