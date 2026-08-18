"""Deterministic in-memory twin of the durable and Google surfaces.

`InMemoryContext` duck-types `FirestoreTransactionContext`, so the production
repository implementations run unmodified against a dict store. External
adapters (task dispatcher, Calendar writer/reader, clock, IDs) are scriptable
in-memory doubles that mirror live semantics (etags, cancelled-corpse
revival, thread ordering, named-task dedup).

Two consumers share this module: the backend test suite (via the
`backend/tests/fakes.py` re-export) and the judge-facing sandbox
(`commitmentos.sandbox`), which runs the real command stack over an isolated
twin per session. Behavior changes here must keep both green.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from commitmentos.application.dto import FencedLease
from commitmentos.application.ports.calendar_reader import (
    CalendarEventRecord,
    CalendarSyncPage,
    CalendarWatch,
)
from commitmentos.application.ports.calendar_writer import (
    CalendarMutationOutcome,
    CalendarMutationOutcomeType,
)
from commitmentos.application.ports.gmail_reader import (
    GmailHistoryChange,
    GmailHistoryPage,
    GmailMailboxPage,
    GmailMessage,
    GmailWatch,
    SourceAuthorizationError,
    SourceCursorInvalidError,
)
from commitmentos.application.ports.model_interpreter import (
    InterpretationResult,
    ModelInvocationMetadata,
    ModelOutputParseError,
)
from commitmentos.application.ports.task_dispatcher import TaskDispatchResult
from commitmentos.application.ports.unit_of_work import RepositorySet
from commitmentos.contracts.model_output import CommitmentInterpretationV1
from commitmentos.contracts.tasks import (
    ExecuteCalendarActionTaskV1,
    ReconcileObservationTaskV1,
    SourceSyncTaskV1,
    TaskNameFactory,
)
from commitmentos.domain.actions.models import CalendarMutation
from commitmentos.domain.planning.models import CalendarBusyInterval, TimeInterval
from commitmentos.domain.shared.types import CanonicalEncoder
from commitmentos.infrastructure.firestore.repositories.implementations import (
    FirestoreRepositorySet,
)
from commitmentos.infrastructure.firestore.serializers import SerializerRegistry
from commitmentos.infrastructure.google.gmail_reader import message_payload_hash

T = TypeVar("T")


class InMemoryContext:
    def __init__(self, store: dict[str, dict[str, dict[str, Any]]], transactional: bool) -> None:
        self._store = store
        self._transactional = transactional
        self._staged: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._created: set[tuple[str, str]] = set()
        self._order: list[tuple[str, str]] = []

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        key = (collection, document_id)
        if key in self._staged:
            staged = self._staged[key]
            return dict(staged) if staged is not None else None
        document = self._store.get(collection, {}).get(document_id)
        return dict(document) if document is not None else None

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]],
        order_by: tuple[str, str] | None = None,
        limit: int | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        results: list[tuple[str, dict[str, Any]]] = []
        for document_id, document in self._store.get(collection, {}).items():
            if all(self._matches(document, field, op, value) for field, op, value in filters):
                results.append((document_id, dict(document)))
        if order_by is not None:
            field, direction = order_by
            results.sort(
                key=lambda row: (row[1].get(field) is None, row[1].get(field)),
                reverse=direction == "DESCENDING",
            )
        if limit is not None:
            results = results[:limit]
        return results

    @staticmethod
    def _matches(document: dict[str, Any], field: str, op: str, value: Any) -> bool:
        current = document.get(field)
        if op == "==":
            return current == value
        if op == "in":
            return current in value
        if op == "<":
            return current is not None and current < value
        if op == ">":
            return current is not None and current > value
        raise ValueError(f"unsupported operator {op}")

    def _stage(self, collection: str, document_id: str, value: dict[str, Any] | None) -> None:
        key = (collection, document_id)
        if key not in self._staged:
            self._order.append(key)
        self._staged[key] = value

    def stage_set(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        self._stage(collection, document_id, dict(value))

    def stage_create(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        self._stage(collection, document_id, dict(value))
        self._created.add((collection, document_id))

    def stage_delete(self, collection: str, document_id: str) -> None:
        self._stage(collection, document_id, None)

    def has_staged_writes(self) -> bool:
        return bool(self._order)

    def flush(self) -> None:
        if self._order and not self._transactional:
            raise RuntimeError("read-only context cannot stage writes")
        for key in self._order:
            collection, document_id = key
            value = self._staged[key]
            bucket = self._store.setdefault(collection, {})
            if value is None:
                bucket.pop(document_id, None)
            elif key in self._created and document_id in bucket:
                raise RuntimeError(f"create conflict: {collection}/{document_id} already exists")
            else:
                bucket[document_id] = dict(value)
        self._order.clear()
        self._staged.clear()
        self._created.clear()


class InMemoryUnitOfWork:
    def __init__(
        self,
        store: dict[str, dict[str, dict[str, Any]]] | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self.store: dict[str, dict[str, dict[str, Any]]] = store if store is not None else {}
        self._serializers = SerializerRegistry()
        self._clock = clock

    async def run(self, operation: Callable[[RepositorySet], Awaitable[T]]) -> T:
        context = InMemoryContext(self.store, transactional=True)
        repositories = FirestoreRepositorySet(context, self._serializers)  # type: ignore[arg-type]
        result = await operation(repositories)
        context.flush()
        return result

    async def read(self, operation: Callable[[RepositorySet], Awaitable[T]]) -> T:
        context = InMemoryContext(self.store, transactional=False)
        repositories = FirestoreRepositorySet(context, self._serializers)  # type: ignore[arg-type]
        result = await operation(repositories)
        if context.has_staged_writes():
            raise RuntimeError("read-only unit of work cannot stage writes")
        return result

    async def run_fenced(
        self,
        fence: FencedLease,
        operation: Callable[[RepositorySet], Awaitable[T]],
    ) -> T:
        async def _fenced(repositories: RepositorySet) -> T:
            # Verification must use the injected test clock: leases are acquired
            # at FakeClock time, so verifying against wall-clock time makes every
            # fenced write "expire" once real time passes the pinned fixture date.
            now = self._clock.now() if self._clock is not None else datetime.now(timezone.utc)
            await repositories.processing_leases.verify(fence, now)
            return await operation(repositories)

        return await self.run(_fenced)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class SequentialIdGenerator:
    def __init__(self) -> None:
        self._counter = 0

    def new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:06d}"

    def new_token(self, byte_length: int) -> str:
        self._counter += 1
        return f"token-{self._counter:06d}"


class FakeTaskDispatcher:
    """Records named tasks instead of calling Cloud Tasks.

    Set `fail_next_enqueues` to simulate the write-before-enqueue crash gap:
    the durable record commits but task creation raises.
    """

    def __init__(self) -> None:
        self._names = TaskNameFactory()
        self.source_sync_tasks: list[tuple[str, SourceSyncTaskV1]] = []
        self.reconciliation_tasks: list[tuple[str, ReconcileObservationTaskV1]] = []
        self.calendar_action_tasks: list[tuple[str, ExecuteCalendarActionTaskV1]] = []
        self.fail_next_enqueues = 0

    def _maybe_fail(self) -> None:
        if self.fail_next_enqueues > 0:
            self.fail_next_enqueues -= 1
            raise ConnectionError("simulated Cloud Tasks outage")

    async def enqueue_source_sync(self, task: SourceSyncTaskV1) -> TaskDispatchResult:
        self._maybe_fail()
        name = self._names.source_sync(task)
        created = all(existing != name for existing, _ in self.source_sync_tasks)
        if created:
            self.source_sync_tasks.append((name, task))
        return TaskDispatchResult(task_name=name, created=created, scheduled_for=None)

    async def enqueue_reconciliation(self, task: ReconcileObservationTaskV1) -> TaskDispatchResult:
        self._maybe_fail()
        name = self._names.reconciliation(task)
        created = all(existing != name for existing, _ in self.reconciliation_tasks)
        if created:
            self.reconciliation_tasks.append((name, task))
        return TaskDispatchResult(task_name=name, created=created, scheduled_for=None)

    async def enqueue_calendar_action(
        self,
        task: ExecuteCalendarActionTaskV1,
        schedule_at: datetime | None = None,
    ) -> TaskDispatchResult:
        self._maybe_fail()
        name = self._names.calendar_action(task)
        created = all(existing != name for existing, _ in self.calendar_action_tasks)
        if created:
            self.calendar_action_tasks.append((name, task))
        return TaskDispatchResult(task_name=name, created=created, scheduled_for=schedule_at)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()


class FakeCalendar:
    """Shared in-memory Calendar store for the fake reader and writer."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self._etag_counter = 0
        self.mutation_log: list[tuple[str, str]] = []

    def live_events(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Events excluding cancelled corpses (Google reserves the ID of a
        cancelled event; it stays retrievable rather than vanishing)."""
        return {
            key: event
            for key, event in self.events.items()
            if event.get("status") != "cancelled"
        }

    def next_etag(self) -> str:
        self._etag_counter += 1
        return f'"etag-{self._etag_counter}"'

    def record(self, calendar_id: str, event_id: str) -> CalendarEventRecord:
        event = self.events[(calendar_id, event_id)]
        return CalendarEventRecord(
            calendar_id=calendar_id,
            event_id=event_id,
            etag=event["etag"],
            status=event["status"],
            payload=dict(event),
            payload_hash=_payload_hash(event),
        )


class FakeCalendarReader:
    def __init__(self, calendar: FakeCalendar) -> None:
        self._calendar = calendar
        self.sync_pages: list[CalendarSyncPage | Exception] = []
        self.sync_calls: list[dict[str, Any]] = []
        self.watches: list[CalendarWatch] = []
        self.stopped_watches: list[tuple[str, str]] = []

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEventRecord | None:
        if (calendar_id, event_id) not in self._calendar.events:
            return None
        return self._calendar.record(calendar_id, event_id)

    async def list_busy_intervals(
        self,
        calendar_id: str,
        planning_horizon: TimeInterval,
        timezone_name: str,
    ) -> tuple[CalendarBusyInterval, ...]:
        del timezone_name
        busy: list[CalendarBusyInterval] = []
        for (event_calendar_id, event_id), event in self._calendar.events.items():
            if event_calendar_id != calendar_id:
                continue
            if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                continue
            interval = TimeInterval(event["start"], event["end"])
            if not interval.overlaps(planning_horizon):
                continue
            properties = event.get("private_properties", {})
            is_owned = properties.get("managed_by") == "commitmentos"
            busy.append(
                CalendarBusyInterval(
                    snapshot_id=CanonicalEncoder.hash(
                        ["fake-calendar-busy:v1", event_id, event["etag"]]
                    ),
                    calendar_event_id=event_id,
                    interval=interval,
                    is_app_owned=is_owned,
                    work_block_id=properties.get("work_block_id") if is_owned else None,
                    source_revision=1,
                )
            )
        return tuple(sorted(busy, key=lambda item: (item.interval.start, item.calendar_event_id)))

    async def create_events_watch(
        self,
        calendar_id: str,
        callback_url: str,
        channel_id: str,
        channel_token: str,
        expiration: datetime,
    ) -> CalendarWatch:
        del calendar_id, callback_url
        watch = CalendarWatch(
            channel_id=channel_id,
            resource_id=f"resource-{len(self.watches) + 1}",
            resource_uri="https://calendar.invalid/events",
            expiration=expiration,
            token_hash=hashlib.sha256(channel_token.encode()).hexdigest(),
        )
        self.watches.append(watch)
        return watch

    async def stop_watch(self, channel_id: str, resource_id: str) -> None:
        self.stopped_watches.append((channel_id, resource_id))

    async def sync_events(
        self,
        calendar_id: str,
        sync_token: str | None,
        page_token: str | None,
        time_min: datetime | None,
        time_max: datetime | None,
    ) -> CalendarSyncPage:
        self.sync_calls.append(
            {
                "calendar_id": calendar_id,
                "sync_token": sync_token,
                "page_token": page_token,
                "time_min": time_min,
                "time_max": time_max,
            }
        )
        if self.sync_pages:
            scripted = self.sync_pages.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        records: list[CalendarEventRecord] = []
        for (event_calendar_id, event_id), event in self._calendar.events.items():
            if event_calendar_id != calendar_id:
                continue
            payload = {
                "id": event_id,
                "etag": event.get("etag"),
                "status": event.get("status", "confirmed"),
                "start": {"dateTime": event["start"].isoformat()},
                "end": {"dateTime": event["end"].isoformat()},
                "extendedProperties": {
                    "private": dict(event.get("private_properties", {}))
                },
            }
            records.append(
                CalendarEventRecord(
                    calendar_id=calendar_id,
                    event_id=event_id,
                    etag=event.get("etag"),
                    status=str(event.get("status", "confirmed")),
                    payload=payload,
                    payload_hash=_payload_hash(payload),
                )
            )
        return CalendarSyncPage(
            events=tuple(records),
            next_page_token=None,
            next_sync_token=f"sync-{len(self.sync_calls)}",
        )


class FakeCalendarWriter:
    def __init__(self, calendar: FakeCalendar) -> None:
        self._calendar = calendar
        self.retryable_failures_remaining = 0

    def _maybe_retryable(self) -> CalendarMutationOutcome | None:
        if self.retryable_failures_remaining > 0:
            self.retryable_failures_remaining -= 1
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.RETRYABLE_FAILURE,
                event=None,
                error={"error_code": "simulated_backend_error"},
            )
        return None

    async def insert_or_adopt_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        retryable = self._maybe_retryable()
        if retryable is not None:
            return retryable
        key = (mutation.calendar_id, mutation.calendar_event_id)
        existing = self._calendar.events.get(key)
        if existing is not None:
            if existing.get("private_properties", {}).get("work_block_id") != mutation.work_block_id:
                return CalendarMutationOutcome(
                    outcome_type=CalendarMutationOutcomeType.TERMINAL_FAILURE,
                    event=None,
                    error={"error_code": "ownership_mismatch"},
                )
            if existing.get("status") == "cancelled":
                # Mirror the real writer: a cancelled owned event is a
                # reserved corpse; a create for the same work block revives
                # it with the desired state instead of blessing the corpse.
                existing["status"] = "confirmed"
                existing["start"] = mutation.desired_start
                existing["end"] = mutation.desired_end
                existing["private_properties"] = dict(mutation.private_properties)
                existing["etag"] = self._calendar.next_etag()
                self._calendar.mutation_log.append(("revive", mutation.calendar_event_id))
                return CalendarMutationOutcome(
                    outcome_type=CalendarMutationOutcomeType.APPLIED,
                    event=self._calendar.record(*key),
                    error=None,
                )
            self._calendar.mutation_log.append(("adopt", mutation.calendar_event_id))
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.ALREADY_APPLIED,
                event=self._calendar.record(*key),
                error=None,
            )
        self._calendar.events[key] = {
            "status": "confirmed",
            "start": mutation.desired_start,
            "end": mutation.desired_end,
            "etag": self._calendar.next_etag(),
            "private_properties": dict(mutation.private_properties),
        }
        self._calendar.mutation_log.append(("insert", mutation.calendar_event_id))
        return CalendarMutationOutcome(
            outcome_type=CalendarMutationOutcomeType.APPLIED,
            event=self._calendar.record(*key),
            error=None,
        )

    async def patch_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        retryable = self._maybe_retryable()
        if retryable is not None:
            return retryable
        key = (mutation.calendar_id, mutation.calendar_event_id)
        existing = self._calendar.events.get(key)
        if existing is None:
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.TERMINAL_FAILURE,
                event=None,
                error={"error_code": "not_found"},
            )
        if existing["etag"] != mutation.expected_observed_event_etag:
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.PRECONDITION_FAILED,
                event=None,
                error={"error_code": "failedPrecondition", "http_status": "412"},
            )
        existing["start"] = mutation.desired_start
        existing["end"] = mutation.desired_end
        existing["etag"] = self._calendar.next_etag()
        self._calendar.mutation_log.append(("patch", mutation.calendar_event_id))
        return CalendarMutationOutcome(
            outcome_type=CalendarMutationOutcomeType.APPLIED,
            event=self._calendar.record(*key),
            error=None,
        )

    async def cancel_owned(self, mutation: CalendarMutation) -> CalendarMutationOutcome:
        retryable = self._maybe_retryable()
        if retryable is not None:
            return retryable
        key = (mutation.calendar_id, mutation.calendar_event_id)
        existing = self._calendar.events.get(key)
        if existing is None:
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.TERMINAL_FAILURE,
                event=None,
                error={"error_code": "not_found"},
            )
        if existing["etag"] != mutation.expected_observed_event_etag:
            return CalendarMutationOutcome(
                outcome_type=CalendarMutationOutcomeType.PRECONDITION_FAILED,
                event=None,
                error={"error_code": "failedPrecondition", "http_status": "412"},
            )
        # Google reserves a cancelled event's ID: keep the corpse retrievable
        # (status "cancelled") instead of deleting the entry outright.
        existing["status"] = "cancelled"
        existing["etag"] = self._calendar.next_etag()
        self._calendar.mutation_log.append(("cancel", mutation.calendar_event_id))
        return CalendarMutationOutcome(
            outcome_type=CalendarMutationOutcomeType.APPLIED,
            event=None,
            error=None,
        )


class FakeGmailReader:
    """Scripted Gmail provider: history pages keyed by page token plus a
    message store. Failures are injectable for auth/cursor tests."""

    def __init__(self) -> None:
        self.messages: dict[str, GmailMessage] = {}
        self.threads: dict[str, list[str]] = {}
        # page key: None for the first page, then the page token string.
        self.history_pages: dict[str | None, GmailHistoryPage] = {}
        self.mailbox_pages: dict[str | None, GmailMailboxPage] = {}
        self.watch = GmailWatch(
            history_id="1000",
            expiration=datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc),
        )
        self.raise_auth_error = False
        self.raise_cursor_invalid = False
        self.history_calls: list[tuple[str, str | None]] = []
        self.mailbox_calls: list[str | None] = []
        self.watch_calls = 0

    def add_message(
        self,
        message_id: str,
        thread_id: str,
        internal_date: datetime,
        subject: str,
        body_text: str,
        label_ids: tuple[str, ...],
        headers: dict[str, str] | None = None,
    ) -> GmailMessage:
        payload_hash = message_payload_hash(message_id, internal_date, subject, body_text)
        message = GmailMessage(
            message_id=message_id,
            thread_id=thread_id,
            internal_date=internal_date,
            label_ids=tuple(sorted(label_ids)),
            headers={"subject": subject, **(headers or {})},
            body_text=body_text,
            payload_hash=payload_hash,
        )
        self.messages[message_id] = message
        self.threads.setdefault(thread_id, [])
        if message_id not in self.threads[thread_id]:
            self.threads[thread_id].append(message_id)
        return message

    def script_history(
        self,
        pages: list[tuple[list[str], str | None, str]],
    ) -> None:
        """pages: [(message_ids, next_page_token, latest_history_id)] — the
        first page is served for token None, later pages for their token."""
        self.history_pages.clear()
        token: str | None = None
        for message_ids, next_token, latest in pages:
            self.history_pages[token] = GmailHistoryPage(
                changes=tuple(
                    GmailHistoryChange(
                        history_id=latest,
                        message_ids=(message_id,),
                        label_ids=self.messages[message_id].label_ids
                        if message_id in self.messages
                        else (),
                    )
                    for message_id in message_ids
                ),
                next_page_token=next_token,
                latest_history_id=latest,
            )
            token = next_token

    async def create_watch(self, user_id, topic_name, label_ids) -> GmailWatch:
        if self.raise_auth_error:
            raise SourceAuthorizationError("scripted auth failure")
        self.watch_calls += 1
        return self.watch

    async def list_history(self, user_id, start_history_id, page_token):
        if self.raise_auth_error:
            raise SourceAuthorizationError("scripted auth failure")
        if self.raise_cursor_invalid:
            raise SourceCursorInvalidError("scripted invalid cursor")
        self.history_calls.append((start_history_id, page_token))
        page = self.history_pages.get(page_token)
        if page is None:
            return GmailHistoryPage(
                changes=(), next_page_token=None, latest_history_id=start_history_id
            )
        return page

    async def list_messages(self, user_id, page_token):
        if self.raise_auth_error:
            raise SourceAuthorizationError("scripted auth failure")
        self.mailbox_calls.append(page_token)
        page = self.mailbox_pages.get(page_token)
        if page is not None:
            return page
        return GmailMailboxPage(
            message_ids=tuple(sorted(self.messages)),
            next_page_token=None,
            latest_history_id=self.watch.history_id,
        )

    async def get_message(self, user_id, message_id) -> GmailMessage | None:
        # None mirrors the live adapter for vanished messages (deleted or
        # discarded drafts referenced by an old history record).
        return self.messages.get(message_id)

    async def get_thread(self, user_id, thread_id):
        ordered = sorted(
            (self.messages[m] for m in self.threads.get(thread_id, [])),
            key=lambda m: (m.internal_date, m.message_id),
        )
        return list(ordered)


class FakeModelInterpreter:
    """Scripted interpreter: queue interpretations per call, record prompts."""

    def __init__(self) -> None:
        self.scripted: list[CommitmentInterpretationV1] = []
        self.calls: list[dict[str, Any]] = []
        self.raise_parse_error = False

    def script(self, interpretation: CommitmentInterpretationV1) -> None:
        self.scripted.append(interpretation)

    async def interpret_commitment(self, source_text, source_metadata, candidate_commitments):
        if self.raise_parse_error:
            raise ModelOutputParseError(("scripted_parse_failure",))
        self.calls.append(
            {
                "source_text": source_text,
                "source_metadata": dict(source_metadata),
                "candidates": [dict(c) for c in candidate_commitments],
            }
        )
        if not self.scripted:
            raise AssertionError("FakeModelInterpreter has no scripted interpretation")
        interpretation = self.scripted.pop(0)
        return InterpretationResult(
            interpretation=interpretation,
            metadata=ModelInvocationMetadata(
                model_id="fake-gemini",
                prompt_version="commitment_interpretation_v2",
                schema_version="extraction_v2",
                thinking_level="low",
                latency_ms=7,
                input_tokens=100,
                output_tokens=50,
            ),
        )

    async def explain_decision(self, decision, evidence):
        del evidence
        return (
            str(decision.get("fallback_explanation", "The plan was updated.")),
            ModelInvocationMetadata(
                model_id="fake-gemini",
                prompt_version="explanation_v1",
                schema_version="explanation_v1",
                thinking_level="low",
                latency_ms=5,
                input_tokens=30,
                output_tokens=15,
            ),
        )
