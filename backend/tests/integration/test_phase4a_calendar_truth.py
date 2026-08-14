from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from conftest import CALENDAR_ID, CONTROLLED_USER, TASK_SCHEMA_VERSION, Phase1App
from fakes import SourceCursorInvalidError

from commitmentos.api.dependencies.calendar_channel import CalendarChannelVerifier
from commitmentos.api.routers.calendar_webhook import CalendarWebhookRouter
from commitmentos.application.commands.receive_calendar_signal import ReceiveCalendarSignal
from commitmentos.application.commands.run_maintenance import RunMaintenance
from commitmentos.application.dto import CommandStatus
from commitmentos.application.ports.calendar_reader import (
    CalendarEventRecord,
    CalendarSyncPage,
)
from commitmentos.contracts.observations import ObservationType, ReconciliationStatus
from commitmentos.contracts.tasks import (
    ExecuteCalendarActionTaskV1,
    SourceSyncTaskV1,
    SourceType,
)
from commitmentos.domain.planning.calendar_state import CalendarSnapshotReducer
from commitmentos.domain.planning.models import TimeInterval
from commitmentos.domain.shared.errors import InvalidTransitionError


def calendar_task(request_suffix: str = "change") -> SourceSyncTaskV1:
    return SourceSyncTaskV1(
        schema_version=TASK_SCHEMA_VERSION,
        sync_request_id=f"calendar:{CONTROLLED_USER}:{request_suffix}",
        sync_generation_id=f"signal-{request_suffix}",
        page_sequence=0,
        source=SourceType.CALENDAR,
        user_id=CONTROLLED_USER,
        trace_id=f"trace-calendar-{request_suffix}",
    )


async def pending_approval(app: Phase1App, request_type: str):
    async def _load(repositories):
        return [
            approval
            for approval in await repositories.approvals.list_pending(CONTROLLED_USER)
            if approval["request_type"] == request_type
        ]

    return (await app.uow.read(_load))[0]


async def create_calendar_actions(app: Phase1App) -> None:
    await app.seed_golden_observation()
    await app.run_reconciliation_tasks()
    effort = await pending_approval(app, "effort_confirmation")
    await app.resolve_approval.execute(
        app.actor(),
        effort["approval_id"],
        {"decision": "approve", "confirmed_minutes": 180},
        effort["revision"],
        "trace-phase4-effort",
    )
    await app.run_reconciliation_tasks()
    plan = await pending_approval(app, "initial_plan_approval")
    await app.resolve_approval.execute(
        app.actor(),
        plan["approval_id"],
        {"decision": "approve"},
        plan["revision"],
        "trace-phase4-plan",
    )
    await app.run_reconciliation_tasks()


class TestCalendarSnapshotReducer:
    def test_all_day_dst_event_uses_exclusive_local_midnight_boundaries(self) -> None:
        reducer = CalendarSnapshotReducer()
        snapshot = reducer.reduce_change(
            None,
            {
                "user_id": CONTROLLED_USER,
                "calendar_id": CALENDAR_ID,
                "id": "all-day-dst",
                "status": "confirmed",
                "start": {"date": "2026-11-01", "timeZone": "America/Los_Angeles"},
                "end": {"date": "2026-11-02", "timeZone": "America/Los_Angeles"},
            },
            source_observation_id="source-1",
            calendar_state_revision=7,
            observed_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
        )
        assert snapshot.observed_all_day is True
        assert snapshot.observed_start is not None
        assert snapshot.observed_end is not None
        assert snapshot.observed_start.utcoffset() == timedelta(hours=-7)
        assert snapshot.observed_end.utcoffset() == timedelta(hours=-8)
        busy = reducer.busy_intervals(
            (snapshot,),
            TimeInterval(
                datetime(2026, 11, 1, tzinfo=timezone.utc),
                datetime(2026, 11, 3, tzinfo=timezone.utc),
            ),
        )
        assert len(busy) == 1
        assert busy[0].source_revision == 7


class TestCalendarGenerationPublication:
    async def test_default_off_probe_delay_can_hold_the_real_publication_barrier(
        self, app: Phase1App, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from commitmentos.application.commands import synchronize_source as sync_module

        observed_delays: list[float] = []

        async def record_delay(seconds: float) -> None:
            cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
            assert cursor["publish_in_progress_generation_id"] is not None
            observed_delays.append(seconds)

        monkeypatch.setattr(sync_module.asyncio, "sleep", record_delay)
        app.synchronize_source._publication_barrier_probe_delay_seconds = 0.125
        app.calendar.events[(CALENDAR_ID, "probe-delay-event")] = {
            "status": "confirmed",
            "start": app.clock.now() + timedelta(hours=1),
            "end": app.clock.now() + timedelta(hours=2),
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }

        result = await app.synchronize_source.execute(calendar_task("probe-delay"))

        assert result.status == CommandStatus.COMPLETED
        assert observed_delays == [0.125]
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        assert cursor["publish_in_progress_generation_id"] is None

    async def test_two_pages_hold_candidate_token_until_final_publication(
        self, app: Phase1App
    ) -> None:
        def record(event_id: str, etag: str) -> CalendarEventRecord:
            payload = {
                "id": event_id,
                "etag": etag,
                "status": "confirmed",
                "start": {"dateTime": (app.clock.now() + timedelta(hours=1)).isoformat()},
                "end": {"dateTime": (app.clock.now() + timedelta(hours=2)).isoformat()},
            }
            return CalendarEventRecord(
                calendar_id=CALENDAR_ID,
                event_id=event_id,
                etag=etag,
                status="confirmed",
                payload=payload,
                payload_hash=f"payload-{event_id}",
            )

        app.calendar_reader.sync_pages.extend(
            [
                CalendarSyncPage((record("page-1", '"etag-1"'),), "page-2", None),
                CalendarSyncPage((record("page-2", '"etag-2"'),), None, "sync-final"),
            ]
        )
        first = await app.synchronize_source.execute(calendar_task("two-page"))
        assert first.status == CommandStatus.ACCEPTED
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        assert cursor["published_cursor"] == "fixture-calendar-sync-0"
        assert cursor["calendar_state_revision"] == 0
        generation = next(iter(app.store["sync_generations"].values()))
        assert generation["candidate_next_cursor"] is None
        assert generation["status"] == "staging"

        continuation_results = await app.run_source_sync_tasks()
        assert continuation_results[-1].status == CommandStatus.COMPLETED
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        assert cursor["published_cursor"] == "sync-final"
        assert cursor["calendar_state_revision"] == 1
        assert len(app.store["calendar_event_snapshots"]) == 2

    async def test_incremental_snapshot_and_token_publish_exactly_once(
        self, app: Phase1App
    ) -> None:
        app.calendar.events[(CALENDAR_ID, "meeting-1")] = {
            "status": "confirmed",
            "start": app.clock.now() + timedelta(hours=1),
            "end": app.clock.now() + timedelta(hours=2),
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        task = calendar_task("incremental")
        first = await app.synchronize_source.execute(task)
        assert first.status == CommandStatus.COMPLETED
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        assert cursor["published_cursor"] == "sync-1"
        assert cursor["revision"] == 1
        assert cursor["calendar_state_revision"] == 1
        assert cursor["publish_in_progress_generation_id"] is None
        snapshots = list(app.store["calendar_event_snapshots"].values())
        assert len(snapshots) == 1
        assert snapshots[0]["calendar_state_revision"] == 1
        observations = list(app.store["source_observations"].values())
        assert observations[0]["observation_type"] == (
            ObservationType.CALENDAR_ENVIRONMENTAL_DISRUPTION.value
        )

        replay = await app.synchronize_source.execute(task)
        assert replay.status == CommandStatus.COMPLETED
        assert cursor["revision"] == 1
        assert cursor["calendar_state_revision"] == 1

    async def test_full_resync_tombstones_events_missing_from_provider(
        self, app: Phase1App
    ) -> None:
        app.calendar.events[(CALENDAR_ID, "removed-event")] = {
            "status": "confirmed",
            "start": app.clock.now() + timedelta(hours=1),
            "end": app.clock.now() + timedelta(hours=2),
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        await app.synchronize_source.execute(calendar_task("seed-event"))
        del app.calendar.events[(CALENDAR_ID, "removed-event")]
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        cursor["full_resync_required"] = True

        result = await app.synchronize_source.execute(calendar_task("full-resync"))
        assert result.status == CommandStatus.COMPLETED
        snapshot = next(iter(app.store["calendar_event_snapshots"].values()))
        assert snapshot["is_tombstone"] is True
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        assert cursor["calendar_state_revision"] == 2
        generation = next(
            value
            for value in app.store["sync_generations"].values()
            if value["mode"] == "full_resync"
        )
        assert generation["mode"] == "full_resync"
        assert generation["full_sync_tombstones_complete"] is True
        assert generation["staged_manifest"] == generation["applied_manifest"]

    async def test_410_abandons_generation_and_queues_full_resync(
        self, app: Phase1App
    ) -> None:
        app.calendar_reader.sync_pages.append(
            SourceCursorInvalidError("expired calendar sync token")
        )
        result = await app.synchronize_source.execute(calendar_task("expired-token"))
        assert result.status == CommandStatus.ACCEPTED
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        assert cursor["full_resync_required"] is True
        generation = next(iter(app.store["sync_generations"].values()))
        assert generation["status"] == "abandoned"
        assert any(
            task.source == SourceType.CALENDAR
            and task.sync_request_id.endswith(":full-resync")
            for _, task in app.task_dispatcher.source_sync_tasks
        )


class TestCalendarWebhookIngress:
    async def test_sync_handshake_bootstraps_missing_canonical_snapshot(
        self, app: Phase1App
    ) -> None:
        token = "calendar-channel-secret"
        app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"] = {
            "source": "calendar",
            "user_id": CONTROLLED_USER,
            "published_sync_token": "legacy-calendar-token",
        }
        app.store.setdefault("calendar_channels", {})[CONTROLLED_USER] = {
            "user_id": CONTROLLED_USER,
            "calendar_id": CALENDAR_ID,
            "channel_id": "channel-bootstrap",
            "resource_id": "resource-bootstrap",
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        }
        router = CalendarWebhookRouter(
            CalendarChannelVerifier(app.uow, app.clock, 20, 60),
            ReceiveCalendarSignal(
                app.uow,
                app.task_dispatcher,
                app.clock,
                app.ids,
                TASK_SCHEMA_VERSION,
            ),
            "/webhooks/calendar",
        )
        response = await router.receive(
            "POST",
            {
                "X-Goog-Channel-ID": "channel-bootstrap",
                "X-Goog-Resource-ID": "resource-bootstrap",
                "X-Goog-Channel-Token": token,
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "sync",
            },
            b"",
            "trace-calendar-bootstrap",
        )

        assert response.status_code == 204
        assert len(app.task_dispatcher.source_sync_tasks) == 1
        request = app.store["sync_requests"][f"calendar:{CONTROLLED_USER}"]
        assert request["initial_snapshot_required"] is True
        results = await app.run_source_sync_tasks()
        assert results[-1].status == CommandStatus.COMPLETED
        generation_id, generation = next(iter(app.store["sync_generations"].items()))
        assert generation["mode"] == "full_resync"
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        assert cursor["published_generation_id"] == generation_id

    async def test_sync_handshake_is_no_op_after_snapshot_publication(
        self, app: Phase1App
    ) -> None:
        command = ReceiveCalendarSignal(
            app.uow,
            app.task_dispatcher,
            app.clock,
            app.ids,
            TASK_SCHEMA_VERSION,
        )
        result = await command.execute(
            {
                "X-Goog-Channel-ID": "channel-existing",
                "X-Goog-Resource-ID": "resource-existing",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "sync",
            },
            "trace-existing-snapshot",
            CONTROLLED_USER,
            CALENDAR_ID,
        )

        assert result.status == CommandStatus.COMPLETED
        assert result.identifiers["handshake"] == "true"
        assert app.task_dispatcher.source_sync_tasks == []

    async def test_verified_signal_coalesces_and_enqueues_one_named_task(
        self, app: Phase1App
    ) -> None:
        token = "calendar-channel-secret"
        app.store.setdefault("calendar_channels", {})[CONTROLLED_USER] = {
            "user_id": CONTROLLED_USER,
            "calendar_id": CALENDAR_ID,
            "channel_id": "channel-1",
            "resource_id": "resource-1",
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        }
        router = CalendarWebhookRouter(
            CalendarChannelVerifier(app.uow, app.clock, 20, 60),
            ReceiveCalendarSignal(
                app.uow,
                app.task_dispatcher,
                app.clock,
                app.ids,
                TASK_SCHEMA_VERSION,
            ),
            "/webhooks/calendar",
        )
        headers = {
            "X-Goog-Channel-ID": "channel-1",
            "X-Goog-Resource-ID": "resource-1",
            "X-Goog-Channel-Token": token,
            "X-Goog-Message-Number": "42",
            "X-Goog-Resource-State": "exists",
        }
        first = await router.receive("POST", headers, b"", "trace-webhook")
        second = await router.receive("POST", headers, b"", "trace-webhook-replay")
        assert first.status_code == second.status_code == 204
        request = app.store["sync_requests"][f"calendar:{CONTROLLED_USER}"]
        assert request["signal_count"] == 2
        assert len(app.task_dispatcher.source_sync_tasks) == 1

    async def test_invalid_channel_token_writes_nothing(self, app: Phase1App) -> None:
        app.store.setdefault("calendar_channels", {})[CONTROLLED_USER] = {
            "user_id": CONTROLLED_USER,
            "calendar_id": CALENDAR_ID,
            "channel_id": "channel-1",
            "resource_id": "resource-1",
            "token_hash": hashlib.sha256(b"expected").hexdigest(),
        }
        verifier = CalendarChannelVerifier(app.uow, app.clock, 20, 60)
        with pytest.raises(ValueError):
            await verifier.verify(
                "POST",
                {
                    "X-Goog-Channel-ID": "channel-1",
                    "X-Goog-Resource-ID": "resource-1",
                    "X-Goog-Channel-Token": "wrong",
                    "X-Goog-Message-Number": "1",
                    "X-Goog-Resource-State": "exists",
                },
                b"",
            )
        assert "calendar_channel_rate_limits" not in app.store
        assert "sync_requests" not in app.store

    async def test_maintenance_renews_channel_with_overlap_metadata(
        self, app: Phase1App
    ) -> None:
        maintenance = RunMaintenance(
            app.uow,
            app.observation_dispatcher,
            app.outbox_dispatcher,
            app.task_dispatcher,
            app.clock,
            CONTROLLED_USER,
            TASK_SCHEMA_VERSION,
            50,
            source_sync_dispatcher=app.source_sync_dispatcher,
            calendar_reader=app.calendar_reader,
            calendar_id=CALENDAR_ID,
            calendar_callback_url="https://service.invalid/webhooks/calendar",
            calendar_channel_token_provider=lambda: "calendar-channel-secret",
            id_generator=app.ids,
        )
        first = await maintenance.renew_watches("trace-renew-1")
        second = await maintenance.renew_watches("trace-renew-2")
        assert first.status == second.status == CommandStatus.COMPLETED
        channel = app.store["calendar_channels"][CONTROLLED_USER]
        assert channel["previous_channel_id"] == "calendar-channel-000001"
        assert channel["channel_id"] == "calendar-channel-000002"
        assert app.calendar_reader.stopped_watches == [
            ("calendar-channel-000001", "resource-1")
        ]


class TestPublicationBarrierAndEchoes:
    async def test_planner_and_executor_are_ineligible_while_apply_barrier_is_held(
        self, app: Phase1App
    ) -> None:
        await create_calendar_actions(app)
        cursor = app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"]
        cursor["publish_in_progress_generation_id"] = "calendar-generation-applying"
        with pytest.raises(InvalidTransitionError):
            await app.portfolio_planning.calculate(CONTROLLED_USER)

        _, task = app.task_dispatcher.calendar_action_tasks[0]
        result = await app.executor.execute(
            ExecuteCalendarActionTaskV1(
                schema_version=task.schema_version,
                outbox_id=task.outbox_id,
                action_idempotency_key=task.action_idempotency_key,
                trace_id=task.trace_id,
            )
        )
        assert result.status == CommandStatus.RETRYABLE_FAILURE
        assert result.error_code == "calendar_truth_ineligible"
        assert app.calendar.events == {}

    async def test_completed_outbox_echo_is_typed_and_suppressed(
        self, app: Phase1App
    ) -> None:
        await create_calendar_actions(app)
        results = await app.run_calendar_action_tasks()
        assert all(result.status == CommandStatus.COMPLETED for result in results)
        await app.synchronize_source.execute(calendar_task("app-echo"))
        echoes = [
            observation
            for observation in app.store["source_observations"].values()
            if observation["observation_type"] == ObservationType.CALENDAR_APP_ECHO.value
        ]
        assert len(echoes) == 3
        assert all(
            observation["reconciliation_status"] == ReconciliationStatus.IGNORED.value
            for observation in echoes
        )

    async def test_owned_event_move_validity_and_user_deletion_are_typed(
        self, app: Phase1App
    ) -> None:
        await create_calendar_actions(app)
        await app.run_calendar_action_tasks()
        await app.synchronize_source.execute(calendar_task("owned-baseline"))
        event_key = sorted(app.calendar.events)[0]
        event = app.calendar.events[event_key]

        event["start"] = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)
        event["end"] = datetime(2026, 8, 15, 17, 0, tzinfo=timezone.utc)
        event["etag"] = app.calendar.next_etag()
        await app.synchronize_source.execute(calendar_task("valid-user-move"))
        observation_types = {
            value["observation_type"]
            for value in app.store["source_observations"].values()
        }
        assert any(
            value["observation_type"]
            == ObservationType.CALENDAR_USER_MOVE_VALID.value
            for value in app.store["source_observations"].values()
        ), (
            observation_types,
            event["start"].isoformat(),
            event["end"].isoformat(),
            [
                (value["start"].isoformat(), value["end"].isoformat())
                for value in app.calendar.events.values()
            ],
        )

        event["start"] = app.clock.now() - timedelta(hours=1)
        event["end"] = app.clock.now()
        event["etag"] = app.calendar.next_etag()
        await app.synchronize_source.execute(calendar_task("invalid-user-move"))
        assert any(
            value["observation_type"]
            == ObservationType.CALENDAR_USER_MOVE_INVALID.value
            for value in app.store["source_observations"].values()
        )

        event_id = event_key[1]
        app.calendar_reader.sync_pages.append(
            CalendarSyncPage(
                events=(
                    CalendarEventRecord(
                        calendar_id=CALENDAR_ID,
                        event_id=event_id,
                        etag='"deleted-etag"',
                        status="cancelled",
                        payload={
                            "id": event_id,
                            "etag": '"deleted-etag"',
                            "status": "cancelled",
                        },
                        payload_hash="deleted-payload",
                    ),
                ),
                next_page_token=None,
                next_sync_token="sync-after-user-delete",
            )
        )
        await app.synchronize_source.execute(calendar_task("user-deletion"))
        assert any(
            value["observation_type"] == ObservationType.CALENDAR_USER_DELETION.value
            for value in app.store["source_observations"].values()
        )
