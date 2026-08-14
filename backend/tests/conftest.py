from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from fakes import (  # noqa: E402
    FakeCalendar,
    FakeCalendarReader,
    FakeCalendarWriter,
    FakeClock,
    FakeGmailReader,
    FakeModelInterpreter,
    FakeTaskDispatcher,
    InMemoryUnitOfWork,
    SequentialIdGenerator,
)

from commitmentos.application.commands.change_system_control import (
    ChangeSystemControl,  # noqa: E402
)
from commitmentos.application.commands.execute_calendar_action import (
    ExecuteCalendarAction,  # noqa: E402
)
from commitmentos.application.commands.receive_gmail_signal import (
    ReceiveGmailSignal,  # noqa: E402
)
from commitmentos.application.commands.reconcile_observation import (
    ReconcileObservation,  # noqa: E402
)
from commitmentos.application.commands.record_work_check_in import (
    RecordWorkCheckIn,  # noqa: E402
)
from commitmentos.application.commands.request_plan_undo import RequestPlanUndo  # noqa: E402
from commitmentos.application.commands.resolve_approval import ResolveApproval  # noqa: E402
from commitmentos.application.commands.run_maintenance import RunMaintenance  # noqa: E402
from commitmentos.application.commands.synchronize_source import (
    SynchronizeSource,  # noqa: E402
)
from commitmentos.application.dto import AuthenticatedActor  # noqa: E402
from commitmentos.application.services.observation_dispatcher import (
    ObservationDispatcher,  # noqa: E402
)
from commitmentos.application.services.outbox_dispatcher import OutboxDispatcher  # noqa: E402
from commitmentos.application.services.planning_inputs import PlanningInputReader  # noqa: E402
from commitmentos.application.services.portfolio_planning import (  # noqa: E402
    PortfolioPlanningService,
)
from commitmentos.application.services.source_sync_dispatcher import (
    SourceSyncDispatcher,  # noqa: E402
)
from commitmentos.contracts.observations import ObservationFactory, ObservationType  # noqa: E402
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType  # noqa: E402
from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator  # noqa: E402
from commitmentos.domain.planning.constraints import ConstraintEvaluator  # noqa: E402
from commitmentos.domain.planning.portfolio import PortfolioPlanner  # noqa: E402
from commitmentos.domain.planning.risk import RiskCalculator  # noqa: E402
from commitmentos.domain.planning.scoring import SlotScorer  # noqa: E402
from commitmentos.workflows.reconciliation.phase1_workflow import (  # noqa: E402
    SeededReconciliationWorkflow,
)

CONTROLLED_EMAIL = "controlled@example.invalid"
CONTROLLED_TIMEZONE = "America/Los_Angeles"

CONTROLLED_USER = "user_fixture_controlled_001"
CALENDAR_ID = "primary"
WORKFLOW_VERSION = "reconciliation_workflow_v1"
TASK_SCHEMA_VERSION = "task_v1"

SEEDED_COMMITMENT = {
    "source_thread_id": "thread_fixture_golden_proposal_revision_001",
    "source_span_key": "message_fixture_golden_acceptance_002:span0",
    "title": "Send revised proposal to Professor Chen",
    "description": "Send the revised proposal to Professor Chen",
    "ownership_type": "my_commitment",
    "beneficiary": "Professor Chen",
    "deadline_value": "2026-08-16T16:00:00-07:00",
    "deadline_expression": "before our Friday 4 p.m. review",
    "deadline_confidence": 0.93,
    "timezone": "America/Los_Angeles",
    "proposed_effort_minutes": 180,
    "effort_confidence": 0.58,
    "semantic_fingerprint": "my_commitment:send-revised-proposal:professor-chen",
    "evidence_excerpt": "Yes—I'll have it back before our Friday 4 p.m. review.",
}


@dataclass
class Phase1App:
    """A composition of the Phase 1 command stack over shared durable state.

    Rebuilding this object over the same `store` simulates a Cloud Run
    recycle: process memory is gone, Firestore state and queued named tasks
    survive.
    """

    store: dict[str, dict[str, dict[str, Any]]]
    clock: FakeClock
    task_dispatcher: FakeTaskDispatcher
    calendar: FakeCalendar
    gmail: FakeGmailReader = field(default_factory=FakeGmailReader)
    interpreter: FakeModelInterpreter = field(default_factory=FakeModelInterpreter)
    uow: InMemoryUnitOfWork = field(init=False)
    _reconciliation_cursor: int = 0
    _calendar_action_cursor: int = 0
    _source_sync_cursor: int = 0

    def __post_init__(self) -> None:
        self.store.setdefault("sync_cursors", {}).setdefault(
            f"calendar:{CONTROLLED_USER}",
            {
                "user_id": CONTROLLED_USER,
                "source": SourceType.CALENDAR.value,
                "revision": 0,
                "published_cursor": "fixture-calendar-sync-0",
                "published_generation_id": "fixture-calendar-bootstrap",
                "publish_in_progress_generation_id": None,
                "generation_counter": 0,
                "calendar_state_revision": 0,
                "full_resync_required": False,
                "updated_at": self.clock.now(),
            },
        )
        self.uow = InMemoryUnitOfWork(self.store, self.clock)
        self.ids = SequentialIdGenerator()
        self.observation_factory = ObservationFactory()
        self.observation_dispatcher = ObservationDispatcher(
            self.uow, self.task_dispatcher, WORKFLOW_VERSION, TASK_SCHEMA_VERSION
        )
        self.outbox_dispatcher = OutboxDispatcher(
            self.uow, self.task_dispatcher, TASK_SCHEMA_VERSION, self.clock
        )
        self.calendar_reader = FakeCalendarReader(self.calendar)
        self.calendar_writer = FakeCalendarWriter(self.calendar)
        self.planning_inputs = PlanningInputReader(
            self.uow,
            self.calendar_reader,
            self.clock,
            CALENDAR_ID,
            CONTROLLED_TIMEZONE,
        )
        constraint_evaluator = ConstraintEvaluator()
        self.portfolio_planning = PortfolioPlanningService(
            self.uow,
            self.planning_inputs,
            PortfolioPlanner(
                CandidateSlotGenerator(15),
                constraint_evaluator,
                SlotScorer("stable-slot-score-v1"),
                RiskCalculator("portfolio-risk-v1"),
            ),
            self.clock,
            constraint_evaluator,
        )
        self.workflow = SeededReconciliationWorkflow(
            self.uow,
            self.outbox_dispatcher,
            self.clock,
            CALENDAR_ID,
            gmail_reader=self.gmail,
            model_interpreter=self.interpreter,
            controlled_timezone=CONTROLLED_TIMEZONE,
            controlled_email=CONTROLLED_EMAIL,
            portfolio_planning=self.portfolio_planning,
        )
        self.reconcile = ReconcileObservation(self.uow, self.workflow, self.clock)
        self.resolve_approval = ResolveApproval(
            self.uow, self.observation_factory, self.observation_dispatcher, self.clock
        )
        self.change_control = ChangeSystemControl(
            self.uow, self.observation_factory, self.observation_dispatcher, self.clock
        )
        self.record_work_check_in = RecordWorkCheckIn(
            self.uow,
            self.observation_factory,
            self.observation_dispatcher,
            self.clock,
        )
        self.request_plan_undo = RequestPlanUndo(
            self.uow,
            self.observation_factory,
            self.observation_dispatcher,
            self.clock,
        )
        self.source_sync_dispatcher = SourceSyncDispatcher(
            self.uow, self.task_dispatcher, TASK_SCHEMA_VERSION
        )
        self.executor = ExecuteCalendarAction(
            self.uow,
            self.calendar_reader,
            self.calendar_writer,
            self.observation_factory,
            self.observation_dispatcher,
            self.clock,
            self.ids,
            source_sync_dispatcher=self.source_sync_dispatcher,
        )
        self.synchronize_source = SynchronizeSource(
            self.uow,
            self.gmail,
            self.observation_factory,
            self.observation_dispatcher,
            self.task_dispatcher,
            self.clock,
            self.ids,
            TASK_SCHEMA_VERSION,
            max_transaction_writes=400,
            max_estimated_transaction_bytes=8 * 1024 * 1024,
            max_items_per_apply_chunk=25,
            calendar_reader=self.calendar_reader,
            calendar_id="primary",
            controlled_timezone="America/New_York",
        )
        self.receive_gmail = ReceiveGmailSignal(
            self.uow,
            self.task_dispatcher,
            self.clock,
            CONTROLLED_USER,
            CONTROLLED_EMAIL,
            TASK_SCHEMA_VERSION,
        )
        self.maintenance = RunMaintenance(
            self.uow,
            self.observation_dispatcher,
            self.outbox_dispatcher,
            self.task_dispatcher,
            self.clock,
            CONTROLLED_USER,
            TASK_SCHEMA_VERSION,
            50,
            source_sync_dispatcher=self.source_sync_dispatcher,
            gmail_reader=self.gmail,
            gmail_pubsub_topic="projects/test/topics/gmail-watch",
        )

    def actor(self) -> AuthenticatedActor:
        return AuthenticatedActor(
            user_id=CONTROLLED_USER,
            email="controlled@example.invalid",
            session_id="session-1",
            authenticated_at=self.clock.now(),
        )

    async def seed_golden_observation(self) -> str:
        observation = self.observation_factory.source_change(
            observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
            user_id=CONTROLLED_USER,
            producer_id=f"{CONTROLLED_USER}:message_fixture_golden_acceptance_002",
            producer_version="payload-hash-1",
            source="gmail",
            external_id="message_fixture_golden_acceptance_002",
            external_version="payload-hash-1",
            payload_hash="payload-hash-1",
            source_reference={
                "thread_id": SEEDED_COMMITMENT["source_thread_id"],
                "message_id": "message_fixture_golden_acceptance_002",
            },
            safe_metadata={"seeded_commitment": SEEDED_COMMITMENT},
            observed_at=self.clock.now(),
            trace_id="trace-seeded-001",
        )

        async def _create(repositories):
            await repositories.observations.create(observation)
            return observation.observation_id

        observation_id = await self.uow.run(_create)
        await self.observation_dispatcher.dispatch(observation_id)
        return observation_id

    async def run_reconciliation_tasks(self, limit: int = 20) -> list[Any]:
        """Deliver queued reconciliation tasks like Cloud Tasks would."""
        results = []
        while self._reconciliation_cursor < len(self.task_dispatcher.reconciliation_tasks):
            if len(results) >= limit:
                break
            _, task = self.task_dispatcher.reconciliation_tasks[self._reconciliation_cursor]
            self._reconciliation_cursor += 1
            results.append(await self.reconcile.execute(task))
        return results

    async def run_calendar_action_tasks(self, limit: int = 20) -> list[Any]:
        results = []
        while self._calendar_action_cursor < len(self.task_dispatcher.calendar_action_tasks):
            if len(results) >= limit:
                break
            _, task = self.task_dispatcher.calendar_action_tasks[self._calendar_action_cursor]
            self._calendar_action_cursor += 1
            results.append(await self.executor.execute(task))
        return results

    async def run_source_sync_tasks(self, limit: int = 20) -> list[Any]:
        """Deliver queued source-sync tasks like Cloud Tasks would."""
        results = []
        while self._source_sync_cursor < len(self.task_dispatcher.source_sync_tasks):
            if len(results) >= limit:
                break
            _, task = self.task_dispatcher.source_sync_tasks[self._source_sync_cursor]
            self._source_sync_cursor += 1
            results.append(await self.synchronize_source.execute(task))
        return results

    async def synchronize_calendar_truth(self) -> Any:
        return await self.synchronize_source.execute(
            SourceSyncTaskV1(
                schema_version=TASK_SCHEMA_VERSION,
                sync_request_id=f"calendar:{CONTROLLED_USER}:fixture-signal",
                sync_generation_id="fixture-calendar-signal",
                page_sequence=0,
                source=SourceType.CALENDAR,
                user_id=CONTROLLED_USER,
                trace_id="trace-fixture-calendar-sync",
            )
        )

    async def drain(self, rounds: int = 10) -> None:
        for _ in range(rounds):
            reconciled = await self.run_reconciliation_tasks()
            executed = await self.run_calendar_action_tasks()
            if not reconciled and not executed:
                return


@pytest.fixture
def app() -> Phase1App:
    return Phase1App(
        store={},
        clock=FakeClock(),
        task_dispatcher=FakeTaskDispatcher(),
        calendar=FakeCalendar(),
    )


def restarted(app: Phase1App) -> Phase1App:
    """A new process over the same durable state and task queue."""
    fresh = Phase1App(
        store=app.store,
        clock=app.clock,
        task_dispatcher=app.task_dispatcher,
        calendar=app.calendar,
        gmail=app.gmail,
        interpreter=app.interpreter,
    )
    fresh._reconciliation_cursor = app._reconciliation_cursor
    fresh._calendar_action_cursor = app._calendar_action_cursor
    fresh._source_sync_cursor = app._source_sync_cursor
    return fresh
