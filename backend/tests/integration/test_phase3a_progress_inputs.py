"""Phase 3A — progress truth and bounded planning inputs, with no planning decisions."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import CONTROLLED_USER, Phase1App

from commitmentos.application.commands.record_work_check_in import WorkCheckInRequest
from commitmentos.application.dto import CommandStatus
from commitmentos.domain.commitments.models import (
    Commitment,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
)
from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.intervals import IntervalSet
from commitmentos.domain.planning.models import (
    CalendarBusyInterval,
    CommitmentDemand,
    PreservedWorkBlock,
    TimeInterval,
)
from commitmentos.domain.planning.preferences import default_user_planning_preferences
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)
from commitmentos.domain.progress.service import ProgressCalculator
from commitmentos.infrastructure.google.calendar_reader import GoogleCalendarReader

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc)


def commitment(*, lifecycle: LifecycleStatus = LifecycleStatus.ACTIVE) -> Commitment:
    return Commitment(
        commitment_id="commitment-3a",
        user_id=CONTROLLED_USER,
        revision=2,
        source_thread_id="thread-3a",
        semantic_fingerprint="my_commitment:phase-3a:none",
        title="Prepare the Phase 3A proof",
        description="",
        ownership_type=OwnershipType.MY_COMMITMENT,
        owner={"type": "user"},
        beneficiary={"display_name": "Reviewer"},
        deadline=Deadline(
            value=datetime(2026, 8, 15, 17, 0, tzinfo=LA),
            timezone="America/Los_Angeles",
            confidence=1.0,
            evidence_id="source-evidence",
            source_expression="Saturday 5 p.m.",
            rule_version="test",
        ),
        effort=Effort(
            proposed_minutes=180,
            confidence=1.0,
            confirmed_minutes=180,
            confirmed_at=NOW,
        ),
        lifecycle_status=lifecycle,
        completion_evidence_id="manual-completion" if lifecycle == LifecycleStatus.COMPLETED else None,
        completed_at=NOW if lifecycle == LifecycleStatus.COMPLETED else None,
        plan_revision=1,
        projection=None,
        policy_profile="default_personal",
        created_at=NOW,
        updated_at=NOW,
    )


def block(
    *,
    block_id: str = "block-3a",
    state: WorkBlockExecutionState = WorkBlockExecutionState.AWAITING_CHECK_IN,
    verified_minutes: int = 0,
    revision: int = 1,
) -> WorkBlock:
    return WorkBlock(
        work_block_id=block_id,
        commitment_id="commitment-3a",
        revision=revision,
        calendar_id="primary",
        calendar_event_id=f"event-{block_id}",
        calendar_snapshot_id=None,
        duration_minutes=60,
        execution_state=state,
        scheduled_start=NOW - timedelta(hours=1),
        scheduled_end=NOW,
        verified_minutes=verified_minutes,
        completion_evidence_id=None,
        user_edit_state=UserEditState.NONE,
        plan_revision=1,
    )


async def persist(app: Phase1App, item: Commitment, work_block: WorkBlock) -> None:
    async def _save(repositories) -> None:
        await repositories.commitments.save(item, None)
        await repositories.work_blocks.save(work_block, None)

    await app.uow.run(_save)


class TestProgressTruth:
    def test_elapsed_time_never_counts_as_progress(self) -> None:
        calculator = ProgressCalculator()
        elapsed = block(state=WorkBlockExecutionState.AWAITING_CHECK_IN)
        assert calculator.verified_minutes((elapsed,)) == 0
        assert calculator.remaining_minutes(180, (elapsed,)) == 180

    def test_partial_verification_reduces_only_the_verified_remainder(self) -> None:
        calculator = ProgressCalculator()
        partial = block(
            state=WorkBlockExecutionState.MISSED,
            verified_minutes=20,
            revision=2,
        )
        assert calculator.verified_minutes((partial,)) == 20
        assert calculator.remaining_minutes(180, (partial,)) == 160

    def test_manual_completion_is_terminal_without_fabricating_minutes(self) -> None:
        calculator = ProgressCalculator()
        partial = block(
            state=WorkBlockExecutionState.COMPLETED,
            verified_minutes=20,
            revision=2,
        )
        assert calculator.verified_minutes((partial,)) == 20
        assert calculator.active_remaining_minutes(
            commitment(lifecycle=LifecycleStatus.COMPLETED),
            (partial,),
        ) == 0

    async def test_check_in_is_bounded_atomic_and_idempotent(self, app: Phase1App) -> None:
        await persist(app, commitment(), block())
        request = WorkCheckInRequest(
            work_block_id="block-3a",
            idempotency_key="check-in-device-1",
            completed=False,
            verified_minutes=20,
            checked_in_at=NOW,
            expected_revision=1,
        )
        first = await app.record_work_check_in.execute(
            app.actor(), request, "trace-check-in"
        )
        assert first.status == CommandStatus.COMPLETED
        stored = app.store["work_blocks"]["block-3a"]
        assert stored["revision"] == 2
        assert stored["execution_state"] == WorkBlockExecutionState.MISSED.value
        assert stored["verified_minutes"] == 20
        assert len(app.store["evidence"]) == 1
        assert len(app.store["source_observations"]) == 1
        assert len(app.task_dispatcher.reconciliation_tasks) == 1

        replay = await app.record_work_check_in.execute(
            app.actor(), request, "trace-check-in-replay"
        )
        assert replay.status == CommandStatus.NO_OP
        assert replay.error_code == "check_in_already_recorded"
        assert len(app.store["evidence"]) == 1
        assert len(app.store["source_observations"]) == 1
        assert len(app.task_dispatcher.reconciliation_tasks) == 1

        conflicting = await app.record_work_check_in.execute(
            app.actor(),
            WorkCheckInRequest(
                work_block_id="block-3a",
                idempotency_key="check-in-device-1",
                completed=False,
                verified_minutes=30,
                checked_in_at=NOW,
                expected_revision=1,
            ),
            "trace-check-in-conflict",
        )
        assert conflicting.status == CommandStatus.TERMINAL_FAILURE
        assert conflicting.error_code == "idempotency_key_reused"

    async def test_check_in_cannot_exceed_block_duration(self, app: Phase1App) -> None:
        await persist(app, commitment(), block())
        result = await app.record_work_check_in.execute(
            app.actor(),
            WorkCheckInRequest(
                work_block_id="block-3a",
                idempotency_key="too-many-minutes",
                completed=True,
                verified_minutes=61,
                checked_in_at=NOW,
                expected_revision=1,
            ),
            "trace-check-in-bounds",
        )
        assert result.status == CommandStatus.TERMINAL_FAILURE
        assert app.store["work_blocks"]["block-3a"]["revision"] == 1
        assert app.store.get("evidence", {}) == {}


class TestIntervalsAndCandidates:
    def test_duration_and_subtraction_are_dst_safe(self) -> None:
        # Spring-forward skips 02:00; this is two elapsed hours, not three.
        available = TimeInterval(
            datetime(2026, 3, 8, 1, 0, tzinfo=LA),
            datetime(2026, 3, 8, 4, 0, tzinfo=LA),
        )
        assert available.duration_minutes() == 120
        busy = TimeInterval(
            datetime(2026, 3, 8, 1, 30, tzinfo=LA),
            datetime(2026, 3, 8, 3, 30, tzinfo=LA),
        )
        remaining = IntervalSet((available,)).subtract((busy,))
        assert [item.duration_minutes() for item in remaining] == [30, 30]
        assert IntervalSet(remaining).total_minutes() == 60

    def test_candidate_pool_is_grid_aligned_future_only_and_deterministic(self) -> None:
        preferences = default_user_planning_preferences("America/Los_Angeles")
        now = datetime(2026, 8, 13, 9, 7, tzinfo=LA)
        free = (
            TimeInterval(
                datetime(2026, 8, 13, 8, 0, tzinfo=LA),
                datetime(2026, 8, 13, 11, 0, tzinfo=LA),
            ),
        )
        demand = CommitmentDemand(
            commitment_id="c",
            commitment_revision=1,
            plan_revision=0,
            deadline=datetime(2026, 8, 13, 10, 30, tzinfo=LA),
            remaining_minutes=60,
            explicit_priority=0,
            created_at=now,
        )
        generator = CandidateSlotGenerator(grid_minutes=15)
        first = generator.generate(free, demand, preferences, now)
        second = generator.generate(free, demand, preferences, now)
        assert first == second
        assert first
        assert all(slot.interval.start.astimezone(LA).minute % 15 == 0 for slot in first)
        assert all(slot.interval.start >= now for slot in first)
        assert all(slot.interval.end <= demand.deadline for slot in first)
        assert {slot.interval.duration_minutes() for slot in first} == {30, 45, 60}
        assert all(slot.score == 0 for slot in first), "3A must not make scoring decisions"

    def test_shared_capacity_is_working_hours_minus_busy_and_preserved(self) -> None:
        preferences = default_user_planning_preferences("America/Los_Angeles")
        horizon = TimeInterval(
            datetime(2026, 8, 13, 0, 0, tzinfo=LA),
            datetime(2026, 8, 15, 0, 0, tzinfo=LA),
        )
        all_day = CalendarBusyInterval(
            snapshot_id="all-day",
            calendar_event_id="all-day",
            interval=TimeInterval(
                datetime(2026, 8, 13, 0, 0, tzinfo=LA),
                datetime(2026, 8, 14, 0, 0, tzinfo=LA),
            ),
            is_app_owned=False,
            work_block_id=None,
            source_revision=1,
        )
        preserved = PreservedWorkBlock(
            work_block_id="preserved",
            commitment_id="other",
            interval=TimeInterval(
                datetime(2026, 8, 14, 9, 0, tzinfo=LA),
                datetime(2026, 8, 14, 10, 0, tzinfo=LA),
            ),
            duration_minutes=60,
            plan_revision=1,
        )
        free = CandidateSlotGenerator(15).free_capacity(
            horizon,
            (all_day,),
            (preserved,),
            preferences,
        )
        assert free == (
            TimeInterval(
                datetime(2026, 8, 14, 10, 0, tzinfo=LA),
                datetime(2026, 8, 14, 17, 30, tzinfo=LA),
            ),
        )

    def test_hard_constraints_report_every_input_violation(self) -> None:
        preferences = default_user_planning_preferences("America/Los_Angeles")
        now = datetime(2026, 8, 13, 10, 0, tzinfo=LA)
        demand = CommitmentDemand(
            commitment_id="c",
            commitment_revision=1,
            plan_revision=0,
            deadline=datetime(2026, 8, 13, 17, 0, tzinfo=LA),
            remaining_minutes=120,
            explicit_priority=0,
            created_at=now,
        )
        candidate = TimeInterval(
            datetime(2026, 8, 13, 16, 45, tzinfo=LA),
            datetime(2026, 8, 13, 17, 15, tzinfo=LA),
        )
        occupied = (candidate,)
        focus = (
            TimeInterval(
                datetime(2026, 8, 13, 13, 0, tzinfo=LA),
                datetime(2026, 8, 13, 16, 0, tzinfo=LA),
            ),
        )
        codes = {
            violation.code
            for violation in ConstraintEvaluator().evaluate(
                candidate,
                demand,
                preferences,
                occupied,
                now,
                focus,
            )
        }
        assert codes == {"after_deadline", "overlap", "daily_focus_limit"}


class _Request:
    def __init__(self, response: dict) -> None:
        self._response = response

    def execute(self) -> dict:
        return self._response


class _Events:
    def __init__(self, pages: dict[str | None, dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def list(self, **kwargs) -> _Request:
        self.calls.append(kwargs)
        return _Request(self.pages[kwargs.get("pageToken")])


class _CalendarService:
    def __init__(self, pages: dict[str | None, dict]) -> None:
        self.event_resource = _Events(pages)

    def events(self) -> _Events:
        return self.event_resource


class TestCalendarAndPreferencesInputs:
    async def test_sync_reader_keeps_full_and_incremental_queries_token_eligible(
        self,
    ) -> None:
        service = _CalendarService(
            {
                None: {
                    "items": [],
                    "nextSyncToken": "calendar-sync-token",
                }
            }
        )
        reader = GoogleCalendarReader(
            credentials_provider=object(),  # type: ignore[arg-type]
            service_factory=lambda: service,
        )
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, tzinfo=timezone.utc)

        full = await reader.sync_events("primary", None, None, start, end)

        assert full.next_sync_token == "calendar-sync-token"
        full_call = service.event_resource.calls[-1]
        assert full_call["maxResults"] == 250
        assert full_call["timeMin"] == start.isoformat()
        assert full_call["timeMax"] == end.isoformat()
        assert "orderBy" not in full_call

        incremental = await reader.sync_events(
            "primary", "calendar-sync-token", None, start, end
        )
        assert incremental.next_sync_token == "calendar-sync-token"
        incremental_call = service.event_resource.calls[-1]
        assert incremental_call["syncToken"] == "calendar-sync-token"
        assert "orderBy" not in incremental_call
        assert "timeMin" not in incremental_call
        assert "timeMax" not in incremental_call

        gate_reader = GoogleCalendarReader(
            credentials_provider=object(),  # type: ignore[arg-type]
            service_factory=lambda: service,
            sync_page_size=10,
        )
        await gate_reader.sync_events("primary", None, None, start, end)
        assert service.event_resource.calls[-1]["maxResults"] == 10

    async def test_defaults_are_committed_once_as_authoritative_user_facts(
        self, app: Phase1App
    ) -> None:
        first = await app.planning_inputs.get_preferences(CONTROLLED_USER)
        stored_before = dict(app.store["users"][CONTROLLED_USER])
        second = await app.planning_inputs.get_preferences(CONTROLLED_USER)
        assert first == second
        assert first.working_day_start == time(9, 0)
        assert first.working_day_end == time(17, 30)
        assert first.minimum_block_minutes == 30
        assert first.maximum_block_minutes == 60
        assert first.daily_focus_limit_minutes == 180
        assert app.store["users"][CONTROLLED_USER] == stored_before

    async def test_user_horizon_work_block_read_returns_only_overlaps(
        self, app: Phase1App
    ) -> None:
        await persist(app, commitment(), block())

        async def _read(repositories):
            return await repositories.work_blocks.list_for_user_horizon(
                CONTROLLED_USER,
                NOW - timedelta(minutes=30),
                NOW + timedelta(hours=1),
            )

        rows = await app.uow.read(_read)
        assert [item.work_block_id for item in rows] == ["block-3a"]

    async def test_live_reader_expands_recurring_and_normalizes_all_day_busy_time(
        self,
    ) -> None:
        horizon = TimeInterval(
            datetime(2026, 8, 14, 0, 0, tzinfo=LA),
            datetime(2026, 8, 16, 0, 0, tzinfo=LA),
        )
        pages = {
            None: {
                "items": [
                    {
                        "id": "all-day",
                        "etag": '"a"',
                        "status": "confirmed",
                        "start": {"date": "2026-08-14"},
                        "end": {"date": "2026-08-15"},
                    },
                    {
                        "id": "free",
                        "etag": '"free"',
                        "status": "confirmed",
                        "transparency": "transparent",
                        "start": {"dateTime": "2026-08-14T09:00:00-07:00"},
                        "end": {"dateTime": "2026-08-14T10:00:00-07:00"},
                    },
                ],
                "nextPageToken": "page-2",
            },
            "page-2": {
                "items": [
                    {
                        "id": "recurring-instance-1",
                        "etag": '"r1"',
                        "status": "confirmed",
                        "recurringEventId": "weekly-team-meeting",
                        "originalStartTime": {
                            "dateTime": "2026-08-15T10:00:00-07:00"
                        },
                        "start": {"dateTime": "2026-08-15T10:00:00-07:00"},
                        "end": {"dateTime": "2026-08-15T11:00:00-07:00"},
                    }
                ]
            },
        }
        service = _CalendarService(pages)
        reader = GoogleCalendarReader(
            credentials_provider=object(),  # type: ignore[arg-type]
            service_factory=lambda: service,
        )
        busy = await reader.list_busy_intervals(
            "primary", horizon, "America/Los_Angeles"
        )
        assert [item.calendar_event_id for item in busy] == [
            "all-day",
            "recurring-instance-1",
        ]
        assert busy[0].interval.duration_minutes() == 24 * 60
        assert busy[1].interval.duration_minutes() == 60
        assert len(service.event_resource.calls) == 2
        assert all(call["singleEvents"] is True for call in service.event_resource.calls)
        assert all(call["orderBy"] == "startTime" for call in service.event_resource.calls)

    def test_naive_intervals_are_rejected_at_the_boundary(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            TimeInterval(datetime(2026, 1, 1), datetime(2026, 1, 2))
