from __future__ import annotations

from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.domain.commitments.identity import CommitmentIdentityResolver, IdentityProposal
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class ResolveCommitmentIdentityNode:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        identity_resolver: CommitmentIdentityResolver,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _to_identity_proposal(self, state: ReconciliationStateV1) -> tuple[IdentityProposal, ...]:
        ...
