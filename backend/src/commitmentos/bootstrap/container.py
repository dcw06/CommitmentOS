from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from commitmentos.api.dependencies.calendar_channel import CalendarChannelVerifier
from commitmentos.api.dependencies.controlled_session import ControlledSessionDependency
from commitmentos.api.dependencies.csrf import CsrfProtection
from commitmentos.api.dependencies.google_oidc import GoogleOidcDependency
from commitmentos.api.routers.auth import AuthRouter
from commitmentos.api.routers.calendar_webhook import CalendarWebhookRouter
from commitmentos.api.routers.dashboard import DashboardRouter
from commitmentos.api.routers.demo import DemoRouter
from commitmentos.application.commands.change_commitment import ChangeCommitment
from commitmentos.application.commands.change_system_control import ChangeSystemControl
from commitmentos.application.commands.complete_commitment import CompleteCommitment
from commitmentos.application.commands.execute_calendar_action import ExecuteCalendarAction
from commitmentos.application.commands.receive_calendar_signal import ReceiveCalendarSignal
from commitmentos.application.commands.receive_gmail_signal import ReceiveGmailSignal
from commitmentos.application.commands.reconcile_observation import ReconcileObservation
from commitmentos.application.commands.record_work_check_in import RecordWorkCheckIn
from commitmentos.application.commands.request_plan_undo import RequestPlanUndo
from commitmentos.application.commands.resolve_approval import ResolveApproval
from commitmentos.application.commands.run_maintenance import RunMaintenance
from commitmentos.application.commands.synchronize_source import SynchronizeSource
from commitmentos.application.queries.get_commitment import GetCommitment
from commitmentos.application.queries.get_system_status import GetSystemStatus
from commitmentos.application.queries.get_today import GetToday
from commitmentos.application.queries.list_activity import ListActivity
from commitmentos.application.queries.list_commitments import ListCommitments
from commitmentos.application.services.observation_dispatcher import ObservationDispatcher
from commitmentos.application.services.outbox_dispatcher import OutboxDispatcher
from commitmentos.application.services.planning_inputs import PlanningInputReader
from commitmentos.application.services.portfolio_planning import PortfolioPlanningService
from commitmentos.application.services.source_sync_dispatcher import SourceSyncDispatcher
from commitmentos.bootstrap.settings import Settings
from commitmentos.contracts.observations import ObservationFactory
from commitmentos.contracts.tasks import TaskNameFactory
from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.portfolio import PortfolioPlanner
from commitmentos.domain.planning.risk import RiskCalculator
from commitmentos.domain.planning.scoring import SlotScorer
from commitmentos.infrastructure.firestore.client import (
    FirestoreClientFactory,
    FirestoreTransactionRunner,
)
from commitmentos.infrastructure.firestore.serializers import SerializerRegistry
from commitmentos.infrastructure.firestore.unit_of_work import FirestoreUnitOfWork
from commitmentos.infrastructure.google.calendar_reader import GoogleCalendarReader
from commitmentos.infrastructure.google.calendar_writer import GoogleCalendarWriter
from commitmentos.infrastructure.google.credentials import ControlledCredentialsProvider
from commitmentos.infrastructure.google.gemini_interpreter import GeminiInterpreter
from commitmentos.infrastructure.google.gmail_reader import GoogleGmailReader
from commitmentos.infrastructure.google.oauth_client import GoogleOAuthClient
from commitmentos.infrastructure.google.oidc_verifier import GoogleIdentityVerifier
from commitmentos.infrastructure.messaging.cloud_tasks import CloudTasksDispatcher
from commitmentos.infrastructure.static_demo import StaticDemoReadModel
from commitmentos.workflows.reconciliation.graph import AdkReconciliationWorkflow
from commitmentos.workflows.reconciliation.phase1_workflow import DurableReconciliationWorkflow


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SecureIdGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(12)}"

    def new_token(self, byte_length: int) -> str:
        return secrets.token_urlsafe(byte_length)


@dataclass(frozen=True, slots=True)
class ReconciliationCapabilities:
    reconcile_observation: ReconcileObservation
    observation_dispatcher: ObservationDispatcher


@dataclass(frozen=True, slots=True)
class ExecutionCapabilities:
    execute_calendar_action: ExecuteCalendarAction
    outbox_dispatcher: OutboxDispatcher


@dataclass(frozen=True, slots=True)
class MaintenanceCapabilities:
    run_maintenance: RunMaintenance


@dataclass(frozen=True, slots=True)
class SynchronizationCapabilities:
    synchronize_source: SynchronizeSource
    receive_gmail_signal: ReceiveGmailSignal
    receive_calendar_signal: ReceiveCalendarSignal
    source_sync_dispatcher: SourceSyncDispatcher


@dataclass(frozen=True, slots=True)
class QueryCapabilities:
    get_commitment: GetCommitment
    list_commitments: ListCommitments
    get_today: GetToday
    list_activity: ListActivity
    get_system_status: GetSystemStatus


@dataclass(frozen=True, slots=True)
class ControlledCommandCapabilities:
    resolve_approval: ResolveApproval
    change_system_control: ChangeSystemControl
    record_work_check_in: RecordWorkCheckIn
    request_plan_undo: RequestPlanUndo
    complete_commitment: CompleteCommitment
    change_commitment: ChangeCommitment


@dataclass(frozen=True, slots=True)
class PlanningInputCapabilities:
    planning_input_reader: PlanningInputReader


@dataclass(frozen=True, slots=True)
class IdentityDependencies:
    session: ControlledSessionDependency
    csrf: CsrfProtection
    pubsub_oidc: GoogleOidcDependency
    tasks_oidc: GoogleOidcDependency
    scheduler_oidc: GoogleOidcDependency


class ApplicationContainer:
    """Phase 1 composition root.

    Constructs production adapters once per Cloud Run instance, including
    source synchronization, planning/repair, safety, and dashboard reads.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.clock = SystemClock()
        self.id_generator = SecureIdGenerator()
        self._runner = FirestoreTransactionRunner(
            FirestoreClientFactory(
                settings.google_cloud_project, settings.firestore_database_id
            ).create(),
            maximum_attempts=5,
        )
        self._unit_of_work = FirestoreUnitOfWork(self._runner, SerializerRegistry())
        base_url = str(settings.service_base_url).rstrip("/")
        self._task_dispatcher = CloudTasksDispatcher(
            client=None,
            project_id=settings.google_cloud_project,
            region=settings.google_cloud_region,
            queue_names={
                "source_sync": settings.source_sync_queue,
                "reconciliation": settings.reconciliation_queue,
                "calendar_actions": settings.calendar_actions_queue,
            },
            handler_urls={
                "source_sync": f"{base_url}/internal/tasks/source-sync",
                "reconciliation": f"{base_url}/internal/tasks/reconcile-observation",
                "calendar_actions": f"{base_url}/internal/tasks/execute-calendar-action",
            },
            service_account_email=settings.tasks_service_account,
            oidc_audience=settings.tasks_oidc_audience,
            task_name_factory=TaskNameFactory(),
        )
        self._observation_factory = ObservationFactory()
        self._observation_dispatcher = ObservationDispatcher(
            self._unit_of_work,
            self._task_dispatcher,
            settings.workflow_version,
            settings.task_schema_version,
        )
        self._outbox_dispatcher = OutboxDispatcher(
            self._unit_of_work,
            self._task_dispatcher,
            settings.task_schema_version,
            self.clock,
        )
        self._credentials_provider = ControlledCredentialsProvider(
            settings.oauth_client_secret_ref,
            settings.controlled_refresh_token_secret_ref,
        )
        self._calendar_reader = GoogleCalendarReader(
            self._credentials_provider,
            sync_page_size=settings.calendar_sync_page_size,
        )
        self._calendar_writer = GoogleCalendarWriter(self._credentials_provider)
        self._planning_input_reader = PlanningInputReader(
            self._unit_of_work,
            self._calendar_reader,
            self.clock,
            settings.calendar_id,
            settings.controlled_timezone,
        )
        constraint_evaluator = ConstraintEvaluator()
        self._portfolio_planning = PortfolioPlanningService(
            self._unit_of_work,
            self._planning_input_reader,
            PortfolioPlanner(
                CandidateSlotGenerator(15),
                constraint_evaluator,
                SlotScorer("stable-slot-score-v1"),
                RiskCalculator(settings.risk_version),
            ),
            self.clock,
            constraint_evaluator,
            settings.planner_version,
        )
        self._gmail_reader = GoogleGmailReader(self._credentials_provider)
        self._model_interpreter = GeminiInterpreter(
            client_factory=self._gemini_client,
            model_id=settings.gemini_model_id,
            prompt_version=settings.prompt_version,
            schema_version=settings.extraction_schema_version,
            thinking_level=settings.gemini_thinking_level,
            controlled_display_name="Controlled User",
        )
        self._source_sync_dispatcher = SourceSyncDispatcher(
            self._unit_of_work,
            self._task_dispatcher,
            settings.task_schema_version,
        )
        inner_workflow = DurableReconciliationWorkflow(
            self._unit_of_work,
            self._outbox_dispatcher,
            self.clock,
            settings.calendar_id,
            gmail_reader=self._gmail_reader,
            model_interpreter=self._model_interpreter,
            controlled_timezone=settings.controlled_timezone,
            controlled_email=settings.controlled_email,
            portfolio_planning=self._portfolio_planning,
        )
        # Production reconciliation runs through the ADK Workflow graph; the
        # inner route object is what integration tests execute directly.
        self._workflow = AdkReconciliationWorkflow(inner_workflow)
        self._identity_verifier = GoogleIdentityVerifier()

    def _gemini_client(self):  # noqa: ANN202 - lazy third-party client
        from google import genai
        from google.cloud import secretmanager

        api_key = (
            secretmanager.SecretManagerServiceClient()
            .access_secret_version(name=self._settings.gemini_api_key_secret_ref)
            .payload.data.decode("utf-8")
        )
        return genai.Client(api_key=api_key)

    def _calendar_channel_token(self) -> str:
        from google.cloud import secretmanager

        return (
            secretmanager.SecretManagerServiceClient()
            .access_secret_version(name=self._settings.calendar_channel_secret_ref)
            .payload.data.decode("utf-8")
        )

    def _oauth_client_config(self) -> dict:
        import json

        from google.cloud import secretmanager

        payload = (
            secretmanager.SecretManagerServiceClient()
            .access_secret_version(name=self._settings.oauth_client_secret_ref)
            .payload.data.decode("utf-8")
        )
        return json.loads(payload)["web"]

    @classmethod
    def build(cls, settings: Settings) -> ApplicationContainer:
        return cls(settings)

    def unit_of_work(self) -> FirestoreUnitOfWork:
        return self._unit_of_work

    def calendar_writer(self) -> GoogleCalendarWriter:
        return self._calendar_writer

    def firestore_client(self):  # noqa: ANN201 - third-party client type
        return self._runner.client

    @property
    def observation_factory(self) -> ObservationFactory:
        return self._observation_factory

    @property
    def observation_dispatcher(self) -> ObservationDispatcher:
        return self._observation_dispatcher

    def reconciliation(self) -> ReconciliationCapabilities:
        return ReconciliationCapabilities(
            reconcile_observation=ReconcileObservation(
                self._unit_of_work, self._workflow, self.clock
            ),
            observation_dispatcher=self._observation_dispatcher,
        )

    def execution(self) -> ExecutionCapabilities:
        return ExecutionCapabilities(
            execute_calendar_action=ExecuteCalendarAction(
                self._unit_of_work,
                self._calendar_reader,
                self._calendar_writer,
                self._observation_factory,
                self._observation_dispatcher,
                self.clock,
                self.id_generator,
                source_sync_dispatcher=self._source_sync_dispatcher,
            ),
            outbox_dispatcher=self._outbox_dispatcher,
        )

    def maintenance(self) -> MaintenanceCapabilities:
        return MaintenanceCapabilities(
            run_maintenance=RunMaintenance(
                self._unit_of_work,
                self._observation_dispatcher,
                self._outbox_dispatcher,
                self._task_dispatcher,
                self.clock,
                self._settings.controlled_user_id,
                self._settings.task_schema_version,
                self._settings.maintenance_scan_limit,
                source_sync_dispatcher=self._source_sync_dispatcher,
                gmail_reader=self._gmail_reader,
                gmail_pubsub_topic=self._gmail_topic_path(),
                calendar_reader=self._calendar_reader,
                calendar_id=self._settings.calendar_id,
                calendar_callback_url=self._settings.calendar_webhook_url(),
                calendar_channel_token_provider=self._calendar_channel_token,
                id_generator=self.id_generator,
            )
        )

    def _gmail_topic_path(self) -> str:
        topic = self._settings.gmail_pubsub_topic
        if topic.startswith("projects/"):
            return topic
        return (
            f"projects/{self._settings.google_cloud_project}/topics/{topic}"
        )

    def synchronization(self) -> SynchronizationCapabilities:
        receive_calendar_signal = ReceiveCalendarSignal(
            self._unit_of_work,
            self._task_dispatcher,
            self.clock,
            self.id_generator,
            self._settings.task_schema_version,
        )
        return SynchronizationCapabilities(
            synchronize_source=SynchronizeSource(
                self._unit_of_work,
                self._gmail_reader,
                self._observation_factory,
                self._observation_dispatcher,
                self._task_dispatcher,
                self.clock,
                self.id_generator,
                self._settings.task_schema_version,
                self._settings.maximum_sync_transaction_writes,
                self._settings.maximum_sync_transaction_bytes,
                self._settings.maximum_sync_apply_items_per_chunk,
                calendar_reader=self._calendar_reader,
                calendar_id=self._settings.calendar_id,
                controlled_timezone=self._settings.controlled_timezone,
                publication_barrier_probe_delay_seconds=(
                    self._settings.sync_publication_barrier_probe_delay_seconds
                ),
            ),
            receive_gmail_signal=ReceiveGmailSignal(
                self._unit_of_work,
                self._task_dispatcher,
                self.clock,
                self._settings.controlled_user_id,
                self._settings.controlled_email,
                self._settings.task_schema_version,
            ),
            receive_calendar_signal=receive_calendar_signal,
            source_sync_dispatcher=self._source_sync_dispatcher,
        )

    def calendar_webhook_router(
        self,
        receive_calendar_signal: ReceiveCalendarSignal,
    ) -> CalendarWebhookRouter:
        return CalendarWebhookRouter(
            CalendarChannelVerifier(
                self._unit_of_work,
                self.clock,
                maximum_signals_per_window=20,
                rate_limit_window_seconds=60,
            ),
            receive_calendar_signal,
            self._settings.calendar_webhook_path,
        )

    def controlled_commands(self) -> ControlledCommandCapabilities:
        return ControlledCommandCapabilities(
            resolve_approval=ResolveApproval(
                self._unit_of_work,
                self._observation_factory,
                self._observation_dispatcher,
                self.clock,
            ),
            change_system_control=ChangeSystemControl(
                self._unit_of_work,
                self._observation_factory,
                self._observation_dispatcher,
                self.clock,
            ),
            record_work_check_in=RecordWorkCheckIn(
                self._unit_of_work,
                self._observation_factory,
                self._observation_dispatcher,
                self.clock,
            ),
            request_plan_undo=RequestPlanUndo(
                self._unit_of_work,
                self._observation_factory,
                self._observation_dispatcher,
                self.clock,
            ),
            complete_commitment=CompleteCommitment(
                self._unit_of_work,
                self._observation_factory,
                self._observation_dispatcher,
                self.clock,
            ),
            change_commitment=ChangeCommitment(
                self._unit_of_work,
                self._observation_factory,
                self._observation_dispatcher,
                self.clock,
            ),
        )

    def planning_inputs(self) -> PlanningInputCapabilities:
        return PlanningInputCapabilities(
            planning_input_reader=self._planning_input_reader
        )

    def queries(self) -> QueryCapabilities:
        system_status = GetSystemStatus(self._unit_of_work, self.clock)
        return QueryCapabilities(
            get_commitment=GetCommitment(self._unit_of_work),
            list_commitments=ListCommitments(self._unit_of_work),
            get_today=GetToday(self._unit_of_work, self.clock, system_status),
            list_activity=ListActivity(self._unit_of_work),
            get_system_status=system_status,
        )

    def dashboard_router(
        self,
        queries: QueryCapabilities,
        session: ControlledSessionDependency,
    ) -> DashboardRouter:
        return DashboardRouter(
            queries.get_today,
            queries.list_activity,
            queries.get_system_status,
            session,
            self._settings.controlled_timezone,
        )

    def auth_router(self, identity: IdentityDependencies) -> AuthRouter:
        import httpx

        client_config = self._oauth_client_config()
        oauth_client = GoogleOAuthClient(
            client_config,
            str(self._settings.oauth_redirect_uri),
            # Login requests only basic identity scopes; the mailbox/Calendar
            # grant is a separate, already-stored consent (scope_set_v1).
            ("openid", "email"),
            httpx.AsyncClient(timeout=30),
        )
        return AuthRouter(
            oauth_client,
            self._identity_verifier,
            self._unit_of_work,
            identity.session,
            identity.csrf,
            self.clock,
            self.id_generator,
            self._settings.controlled_user_id,
            self._settings.controlled_email,
            client_config["client_id"],
        )

    def demo_router(self) -> DemoRouter:
        return DemoRouter(StaticDemoReadModel(self._settings.demo_data_directory))

    def identity_dependencies(self) -> IdentityDependencies:
        settings = self._settings
        return IdentityDependencies(
            session=ControlledSessionDependency(
                self._unit_of_work, self.clock, settings.controlled_user_id
            ),
            csrf=CsrfProtection(),
            pubsub_oidc=GoogleOidcDependency(
                self._identity_verifier,
                settings.pubsub_oidc_audience,
                settings.pubsub_service_account,
                "pubsub",
            ),
            tasks_oidc=GoogleOidcDependency(
                self._identity_verifier,
                settings.tasks_oidc_audience,
                settings.tasks_service_account,
                "tasks",
            ),
            scheduler_oidc=GoogleOidcDependency(
                self._identity_verifier,
                settings.scheduler_oidc_audience,
                settings.scheduler_service_account,
                "scheduler",
            ),
        )
