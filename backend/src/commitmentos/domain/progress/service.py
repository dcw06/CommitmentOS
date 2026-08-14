from __future__ import annotations

from typing import Sequence

from commitmentos.domain.commitments.models import Commitment, LifecycleStatus
from commitmentos.domain.progress.models import WorkBlock
from commitmentos.domain.shared.types import CanonicalEncoder


class ProgressCalculator:
    def verified_minutes(self, work_blocks: Sequence[WorkBlock]) -> int:
        return sum(block.verified_minutes for block in work_blocks)

    def remaining_minutes(self, confirmed_effort: int, work_blocks: Sequence[WorkBlock]) -> int:
        if confirmed_effort < 0:
            raise ValueError("confirmed effort cannot be negative")
        return max(confirmed_effort - self.verified_minutes(work_blocks), 0)

    def active_remaining_minutes(
        self,
        commitment: Commitment,
        work_blocks: Sequence[WorkBlock],
    ) -> int:
        """Return schedulable remaining work without inventing progress.

        Explicit completion is terminal even when verified minutes are below
        the original estimate. Returning zero here closes the scheduling
        demand; `verified_minutes()` remains the unmodified authoritative fact.
        """
        if commitment.lifecycle_status == LifecycleStatus.COMPLETED:
            return 0
        confirmed = commitment.effort.confirmed_minutes
        if confirmed is None:
            raise ValueError("remaining effort requires confirmed effort")
        return self.remaining_minutes(confirmed, work_blocks)

    def work_block_revision_hash(self, work_blocks: Sequence[WorkBlock]) -> str:
        parts: list[str | int] = ["work-block-revisions:v1"]
        for block in sorted(work_blocks, key=lambda item: item.work_block_id):
            parts.extend(
                [
                    block.work_block_id,
                    str(block.revision),
                    block.execution_state.value,
                    str(block.verified_minutes),
                    block.completion_evidence_id or "",
                ]
            )
        return CanonicalEncoder.hash(parts)
