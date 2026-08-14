from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from commitmentos.application.dto import AuthenticatedActor, CommandResult
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.task_dispatcher import TaskDispatcher
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.contracts.observations import ObservationFactory


@dataclass(frozen=True, slots=True)
class CompleteCommitmentRequest:
    commitment_id: str
    completed_at: datetime
    expected_revision: int


class CompleteCommitment:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        observation_factory: ObservationFactory,
        task_dispatcher: TaskDispatcher,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        ...

    async def execute(
        self,
        actor: AuthenticatedActor,
        request: CompleteCommitmentRequest,
        trace_id: str,
    ) -> CommandResult:
        ...

    async def _verify_terminal_transition(
        self,
        commitment_id: str,
        expected_revision: int,
    ) -> None:
        ...
