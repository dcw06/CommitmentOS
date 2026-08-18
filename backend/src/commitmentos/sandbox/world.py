"""One isolated sandbox world: the real command stack over the twin.

`SandboxWorld` composes the same application commands, workflow, planner, and
executor that serve the controlled user in production, but every port that
would reach outside the process is the in-memory twin: Firestore is a dict,
Gmail is a scripted mailbox, Calendar is a dict of events, Cloud Tasks is a
list, and the clock is advanceable. Nothing here can read a live document or
call a Google API — the composition simply has no credential to do it with.

The one deliberately live edge is interpretation: the model interpreter is
injected, so the deployed sandbox can hand it the real Gemini client (see
`interpreter.py`, which caches per canned message so a public surface cannot
issue unbounded model calls).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from commitmentos.application.commands.change_commitment import ChangeCommitment
from commitmentos.application.commands.complete_commitment import CompleteCommitment
from commitmentos.application.commands.execute_calendar_action import (
    ExecuteCalendarAction,
)
from commitmentos.application.commands.receive_calendar_signal import (
    ReceiveCalendarSignal,
)
from commitmentos.application.commands.reconcile_observation import ReconcileObservation
from commitmentos.application.commands.record_work_check_in import RecordWorkCheckIn
from commitmentos.application.commands.resolve_approval import ResolveApproval
from commitmentos.application.commands.run_maintenance import (
    MaintenanceKind,
    RunMaintenance,
)
from commitmentos.application.commands.synchronize_source import SynchronizeSource
from commitmentos.application.dto import AuthenticatedActor
from commitmentos.application.ports.model_interpreter import ModelInterpreter
from commitmentos.application.services.observation_dispatcher import (
    ObservationDispatcher,
)
from commitmentos.application.services.outbox_dispatcher import OutboxDispatcher
from commitmentos.application.services.planning_inputs import PlanningInputReader
from commitmentos.application.services.portfolio_planning import (
    PortfolioPlanningService,
)
from commitmentos.application.services.source_sync_dispatcher import (
    SourceSyncDispatcher,
)
from commitmentos.contracts.observations import ObservationFactory, ObservationType
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType
from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.portfolio import PortfolioPlanner
from commitmentos.domain.planning.risk import RiskCalculator
from commitmentos.domain.planning.scoring import SlotScorer
from commitmentos.sandbox.twin import (
    FakeCalendar,
    FakeCalendarReader,
    FakeCalendarWriter,
    FakeClock,
    FakeGmailReader,
    FakeTaskDispatcher,
    InMemoryUnitOfWork,
    SequentialIdGenerator,
)
from commitmentos.workflows.reconciliation.phase1_workflow import (
    DurableReconciliationWorkflow,
)

SANDBOX_USER = "sandbox-user"
SANDBOX_EMAIL = "you@sandbox.invalid"
SANDBOX_CALENDAR_ID = "primary"
SANDBOX_TIMEZONE = "America/Los_Angeles"
WORKFLOW_VERSION = "reconciliation_workflow_v1"
TASK_SCHEMA_VERSION = "task_v1"


@dataclass
class SandboxWorld:
    """The real stack over one session's private twin."""

    interpreter: ModelInterpreter
    started_at: datetime
    store: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    _reconciliation_cursor: int = 0
    _calendar_action_cursor: int = 0
    _source_sync_cursor: int = 0

    def __post_init__(self) -> None:
        self.clock = FakeClock(self.started_at)
        self.task_dispatcher = FakeTaskDispatcher()
        self.calendar = FakeCalendar()
        self.gmail = FakeGmailReader()
        self.store.setdefault("sync_cursors", {}).setdefault(
            f"calendar:{SANDBOX_USER}",
            {
                "user_id": SANDBOX_USER,
                "source": SourceType.CALENDAR.value,
                "revision": 0,
                "published_cursor": "sandbox-calendar-sync-0",
                "published_generation_id": "sandbox-calendar-bootstrap",
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
            SANDBOX_CALENDAR_ID,
            SANDBOX_TIMEZONE,
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
        self.workflow = DurableReconciliationWorkflow(
            self.uow,
            self.outbox_dispatcher,
            self.clock,
            SANDBOX_CALENDAR_ID,
            gmail_reader=self.gmail,
            model_interpreter=self.interpreter,
            controlled_timezone=SANDBOX_TIMEZONE,
            controlled_email=SANDBOX_EMAIL,
            portfolio_planning=self.portfolio_planning,
        )
        self.reconcile = ReconcileObservation(self.uow, self.workflow, self.clock)
        self.resolve_approval = ResolveApproval(
            self.uow, self.observation_factory, self.observation_dispatcher, self.clock
        )
        self.record_work_check_in = RecordWorkCheckIn(
            self.uow, self.observation_factory, self.observation_dispatcher, self.clock
        )
        self.complete_commitment = CompleteCommitment(
            self.uow, self.observation_factory, self.observation_dispatcher, self.clock
        )
        self.change_commitment = ChangeCommitment(
            self.uow, self.observation_factory, self.observation_dispatcher, self.clock
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
            calendar_id=SANDBOX_CALENDAR_ID,
            controlled_timezone=SANDBOX_TIMEZONE,
        )
        self.receive_calendar_signal = ReceiveCalendarSignal(
            self.uow, self.task_dispatcher, self.clock, self.ids, TASK_SCHEMA_VERSION
        )
        self.maintenance = RunMaintenance(
            self.uow,
            self.observation_dispatcher,
            self.outbox_dispatcher,
            self.task_dispatcher,
            self.clock,
            SANDBOX_USER,
            TASK_SCHEMA_VERSION,
            50,
            source_sync_dispatcher=self.source_sync_dispatcher,
            gmail_reader=self.gmail,
            gmail_pubsub_topic="projects/sandbox/topics/gmail-watch",
        )

    def actor(self) -> AuthenticatedActor:
        return AuthenticatedActor(
            user_id=SANDBOX_USER,
            email=SANDBOX_EMAIL,
            session_id="sandbox-session",
            authenticated_at=self.clock.now(),
        )

    # ------------------------------------------------------------------
    # Task delivery: the sandbox plays the role of Cloud Tasks inline, so a
    # judge's click resolves to settled state before the response returns.
    # ------------------------------------------------------------------

    async def run_reconciliation_tasks(self, limit: int = 40) -> None:
        while self._reconciliation_cursor < len(self.task_dispatcher.reconciliation_tasks):
            if limit <= 0:
                return
            _, task = self.task_dispatcher.reconciliation_tasks[self._reconciliation_cursor]
            self._reconciliation_cursor += 1
            limit -= 1
            await self.reconcile.execute(task)

    async def run_calendar_action_tasks(self, limit: int = 40) -> None:
        while self._calendar_action_cursor < len(self.task_dispatcher.calendar_action_tasks):
            if limit <= 0:
                return
            _, task = self.task_dispatcher.calendar_action_tasks[self._calendar_action_cursor]
            self._calendar_action_cursor += 1
            limit -= 1
            await self.executor.execute(task)

    async def run_source_sync_tasks(self, limit: int = 40) -> None:
        while self._source_sync_cursor < len(self.task_dispatcher.source_sync_tasks):
            if limit <= 0:
                return
            _, task = self.task_dispatcher.source_sync_tasks[self._source_sync_cursor]
            self._source_sync_cursor += 1
            limit -= 1
            await self.synchronize_source.execute(task)

    async def drain(self, rounds: int = 12) -> None:
        for _ in range(rounds):
            before = (
                self._reconciliation_cursor,
                self._calendar_action_cursor,
                self._source_sync_cursor,
            )
            await self.run_reconciliation_tasks()
            await self.run_calendar_action_tasks()
            await self.run_source_sync_tasks()
            after = (
                self._reconciliation_cursor,
                self._calendar_action_cursor,
                self._source_sync_cursor,
            )
            if before == after:
                return

    async def synchronize_calendar_truth(self) -> None:
        await self.synchronize_source.execute(
            SourceSyncTaskV1(
                schema_version=TASK_SCHEMA_VERSION,
                sync_request_id=f"calendar:{SANDBOX_USER}:sandbox-signal",
                sync_generation_id=f"sandbox-calendar-{self.ids.new_id('gen')}",
                page_sequence=0,
                source=SourceType.CALENDAR,
                user_id=SANDBOX_USER,
                trace_id="trace-sandbox-calendar-sync",
            )
        )
        await self.drain()

    async def deliver_message(self, message_id: str, thread_id: str) -> str:
        """Ingest one canned message the way a Gmail push would."""
        observation = self.observation_factory.source_change(
            observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
            user_id=SANDBOX_USER,
            producer_id=f"{SANDBOX_USER}:{message_id}",
            producer_version="sandbox-1",
            source="gmail",
            external_id=message_id,
            external_version="sandbox-1",
            payload_hash=f"sandbox-{message_id}",
            source_reference={"thread_id": thread_id, "message_id": message_id},
            safe_metadata={},
            observed_at=self.clock.now(),
            trace_id=f"trace-sandbox-{message_id}",
        )

        async def _create(repositories):  # noqa: ANN001, ANN202
            await repositories.observations.create(observation)

        await self.uow.run(_create)
        await self.observation_dispatcher.dispatch(observation.observation_id)
        await self.drain()
        return observation.observation_id

    async def advance_to(self, moment: datetime) -> None:
        """Move the sandbox clock forward and run the safety reconciliation."""
        if self.clock.now() < moment:
            self.clock.current = moment
        await self.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-sandbox-elapse"
        )
        await self.drain()

    def add_busy_event(
        self, event_id: str, start: datetime, end: datetime, summary: str
    ) -> None:
        self.calendar.events[(SANDBOX_CALENDAR_ID, event_id)] = {
            "status": "confirmed",
            "start": start,
            "end": end,
            "etag": self.calendar.next_etag(),
            "private_properties": {},
            "summary": summary,
        }


def default_start() -> datetime:
    """A stable Monday 09:00 Pacific start so every session tells one story."""
    return datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)


def working_day_offset(start: datetime, days: int, hour_utc: int) -> datetime:
    return (start + timedelta(days=days)).replace(
        hour=hour_utc, minute=0, second=0, microsecond=0
    )
