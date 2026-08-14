from __future__ import annotations

from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.id_generator import IdGenerator
from commitmentos.application.ports.unit_of_work import UnitOfWork
from commitmentos.domain.actions.identity import ActionIdempotencyKeyFactory, CalendarEventIdFactory
from commitmentos.domain.actions.models import ActionOutbox
from commitmentos.workflows.reconciliation.state import ReconciliationStateV1


class WriteActionOutboxNode:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        calendar_event_id_factory: CalendarEventIdFactory,
        idempotency_key_factory: ActionIdempotencyKeyFactory,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        ...

    async def execute(self, state: ReconciliationStateV1) -> ReconciliationStateV1:
        ...

    def _build_actions(self, state: ReconciliationStateV1) -> tuple[ActionOutbox, ...]:
        ...

    def _validate_expected_observed_etags(self, actions: tuple[ActionOutbox, ...]) -> None:
        ...
