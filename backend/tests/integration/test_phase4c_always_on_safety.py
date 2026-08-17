from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta

from conftest import CALENDAR_ID, CONTROLLED_USER, TASK_SCHEMA_VERSION, Phase1App

from commitmentos.api.dependencies.calendar_channel import CalendarChannelVerifier
from commitmentos.api.routers.calendar_webhook import CalendarWebhookRouter
from commitmentos.application.commands.receive_calendar_signal import ReceiveCalendarSignal
from commitmentos.application.commands.run_maintenance import MaintenanceKind
from commitmentos.application.dto import CommandStatus
from commitmentos.application.queries.get_system_status import GetSystemStatus
from commitmentos.application.queries.get_today import GetToday
from commitmentos.contracts.observations import ObservationType, ReconciliationStatus
from commitmentos.domain.audit.models import ActivityEventType
from commitmentos.domain.commitments.models import (
    Commitment,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
    RiskLevel,
)
from commitmentos.domain.planning.calendar_state import CalendarSnapshotReducer
from commitmentos.domain.planning.models import PlannerRunStatus
from commitmentos.domain.progress.models import (
    UserEditState,
    WorkBlock,
    WorkBlockExecutionState,
)


def commitment(app: Phase1App, commitment_id: str, *, overdue: bool = False) -> Commitment:
    now = app.clock.now()
    return Commitment(
        commitment_id=commitment_id,
        user_id=CONTROLLED_USER,
        revision=1,
        source_thread_id=f"thread-{commitment_id}",
        semantic_fingerprint=f"fingerprint-{commitment_id}",
        title=f"Commitment {commitment_id}",
        description="",
        ownership_type=OwnershipType.MY_COMMITMENT,
        owner={"type": "user"},
        beneficiary={"display_name": "Reviewer"},
        deadline=Deadline(
            value=now - timedelta(minutes=1) if overdue else now + timedelta(days=2),
            timezone="UTC",
            confidence=1.0,
            evidence_id=f"evidence-{commitment_id}",
            source_expression="fixture deadline",
            rule_version="test",
        ),
        effort=Effort(60, 1.0, 60, now),
        lifecycle_status=LifecycleStatus.ACTIVE,
        completion_evidence_id=None,
        completed_at=None,
        plan_revision=1,
        projection=None,
        policy_profile="default_personal",
        created_at=now - timedelta(days=1),
        updated_at=now,
    )


def block(app: Phase1App, commitment_id: str) -> WorkBlock:
    now = app.clock.now()
    return WorkBlock(
        work_block_id=f"block-{commitment_id}",
        commitment_id=commitment_id,
        revision=1,
        calendar_id=CALENDAR_ID,
        calendar_event_id=f"event-{commitment_id}",
        calendar_snapshot_id=None,
        duration_minutes=30,
        execution_state=WorkBlockExecutionState.PLANNED,
        scheduled_start=now,
        scheduled_end=now + timedelta(minutes=30),
        verified_minutes=0,
        completion_evidence_id=None,
        user_edit_state=UserEditState.NONE,
        plan_revision=1,
    )


async def pending_approval(app: Phase1App, request_type: str):
    async def _load(repositories):  # noqa: ANN202
        return [
            value
            for value in await repositories.approvals.list_pending(CONTROLLED_USER)
            if value["request_type"] == request_type
        ]

    return (await app.uow.read(_load))[0]


async def create_live_plan(app: Phase1App) -> None:
    await app.seed_golden_observation()
    await app.run_reconciliation_tasks()
    effort = await pending_approval(app, "effort_confirmation")
    await app.resolve_approval.execute(
        app.actor(),
        effort["approval_id"],
        {"decision": "approve", "confirmed_minutes": 180},
        effort["revision"],
        "trace-4c-effort",
    )
    await app.run_reconciliation_tasks()
    plan = await pending_approval(app, "initial_plan_approval")
    await app.resolve_approval.execute(
        app.actor(),
        plan["approval_id"],
        {"decision": "approve"},
        plan["revision"],
        "trace-4c-plan",
    )
    await app.run_reconciliation_tasks()
    await app.run_calendar_action_tasks()
    await app.run_reconciliation_tasks()
    await app.synchronize_calendar_truth()


def webhook(app: Phase1App) -> tuple[CalendarWebhookRouter, dict[str, str]]:
    token = "phase4c-channel-token"
    app.store.setdefault("calendar_channels", {})[CONTROLLED_USER] = {
        "user_id": CONTROLLED_USER,
        "calendar_id": CALENDAR_ID,
        "channel_id": "phase4c-channel",
        "resource_id": "phase4c-resource",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expiration": app.clock.now() + timedelta(days=1),
        "status": "active",
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
    return router, {
        "X-Goog-Channel-ID": "phase4c-channel",
        "X-Goog-Resource-ID": "phase4c-resource",
        "X-Goog-Channel-Token": token,
        "X-Goog-Resource-State": "exists",
    }


class TestPeriodicSafety:
    async def test_infeasible_plan_surfaces_portfolio_capacity_conflict(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        latest = max(
            app.store["planner_runs"].values(),
            key=lambda row: row["calculated_at"],
        )
        latest["feasible"] = False
        status = await GetSystemStatus(app.uow, app.clock).execute(CONTROLLED_USER)
        states = {item["state"] for item in status.failure_states}
        assert {"no_feasible_plan", "portfolio_capacity_conflict"}.issubset(states)

    async def test_scan_drives_real_block_lifecycle_without_inventing_progress(
        self, app: Phase1App
    ) -> None:
        item = commitment(app, "lifecycle")
        work = block(app, item.commitment_id)

        async def _save(repositories) -> None:  # noqa: ANN001
            await repositories.commitments.save(item, None)
            await repositories.work_blocks.save(work, None)

        await app.uow.run(_save)
        activated = await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-safety-activate"
        )
        assert activated.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()
        stored = app.store["work_blocks"][work.work_block_id]
        assert stored["execution_state"] == WorkBlockExecutionState.ACTIVE.value
        assert stored["verified_minutes"] == 0
        assert app.store["commitments"][item.commitment_id]["lifecycle_status"] == (
            LifecycleStatus.IN_PROGRESS.value
        )

        app.clock.advance(30 * 60)
        elapsed = await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-safety-elapsed"
        )
        assert elapsed.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()
        stored = app.store["work_blocks"][work.work_block_id]
        assert stored["execution_state"] == WorkBlockExecutionState.AWAITING_CHECK_IN.value
        assert stored["verified_minutes"] == 0
        assert any(
            row["event_type"] == ActivityEventType.WORK_CHECK_IN_REQUIRED.value
            and row["payload"]["failure_state"] == "work_check_in_required"
            for row in app.store["activity_events"].values()
        )

        observation_count = len(app.store["source_observations"])
        replay = await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-safety-replay"
        )
        assert replay.status == CommandStatus.NO_OP
        assert len(app.store["source_observations"]) == observation_count

    async def test_overdue_risk_is_refreshed_and_visible(self, app: Phase1App) -> None:
        item = commitment(app, "daf9a729-fixture", overdue=True)

        async def _save(repositories) -> None:  # noqa: ANN001
            await repositories.commitments.save(item, None)

        await app.uow.run(_save)
        result = await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-safety-overdue"
        )
        assert result.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()
        projection = app.store["commitments"][item.commitment_id]["projection"]
        assert projection["risk_level"] == RiskLevel.OVERDUE.value

        status_query = GetSystemStatus(app.uow, app.clock)
        status = await status_query.execute(CONTROLLED_USER)
        assert any(
            failure["state"] == "overdue"
            and failure["commitment_id"] == item.commitment_id
            for failure in status.failure_states
        )
        today = await GetToday(app.uow, app.clock, status_query).execute(
            CONTROLLED_USER, "UTC"
        )
        assert any(
            failure["state"] == "overdue"
            for failure in today.visible_failure_states
        )

    async def test_old_calculated_plan_is_cleaned_up_as_stale(
        self, app: Phase1App
    ) -> None:
        item = commitment(app, "stale-plan")

        async def _save(repositories) -> None:  # noqa: ANN001
            await repositories.commitments.save(item, None)

        await app.uow.run(_save)
        plan = await app.portfolio_planning.calculate(CONTROLLED_USER)

        async def _save_plan(repositories) -> None:  # noqa: ANN001
            await repositories.planner_runs.create(plan)

        await app.uow.run(_save_plan)
        app.clock.advance(6 * 60)
        await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-stale-plan"
        )
        await app.run_reconciliation_tasks()
        assert app.store["planner_runs"][plan.planner_run_id]["status"] == (
            PlannerRunStatus.STALE.value
        )

    async def test_snapshot_drift_requests_sync_and_stays_a_visible_fact(
        self, app: Phase1App
    ) -> None:
        item = commitment(app, "snapshot-drift")
        work = block(app, item.commitment_id)
        work = replace(
            work,
            scheduled_start=app.clock.now() + timedelta(hours=1),
            scheduled_end=app.clock.now() + timedelta(hours=1, minutes=30),
        )
        snapshot = CalendarSnapshotReducer().reduce_change(
            None,
            {
                "user_id": CONTROLLED_USER,
                "calendar_id": CALENDAR_ID,
                "id": work.calendar_event_id,
                "etag": '"drift-etag"',
                "status": "confirmed",
                "start": {
                    "dateTime": (app.clock.now() + timedelta(hours=2)).isoformat()
                },
                "end": {
                    "dateTime": (
                        app.clock.now() + timedelta(hours=2, minutes=30)
                    ).isoformat()
                },
                "extendedProperties": {
                    "private": {
                        "managed_by": "commitmentos",
                        "commitment_id": item.commitment_id,
                        "work_block_id": work.work_block_id,
                        "plan_revision": "1",
                    }
                },
            },
            "source-drift",
            1,
            app.clock.now(),
        )

        async def _save(repositories) -> None:  # noqa: ANN001
            await repositories.commitments.save(item, None)
            await repositories.work_blocks.save(work, None)
            await repositories.calendar_snapshots.save(snapshot)

        await app.uow.run(_save)
        result = await app.maintenance.execute(
            MaintenanceKind.SAFETY_RECONCILIATION, "trace-snapshot-drift"
        )
        assert result.status == CommandStatus.COMPLETED
        request = app.store["sync_requests"][f"calendar:{CONTROLLED_USER}"]
        assert request["reason"] == "desired_snapshot_drift"
        await app.run_reconciliation_tasks()
        assert any(
            row["payload"].get("failure_state") == "calendar_drift_detected"
            for row in app.store["activity_events"].values()
        )
        status = await GetSystemStatus(app.uow, app.clock).execute(CONTROLLED_USER)
        assert any(
            failure["state"] == "calendar_drift_detected"
            for failure in status.failure_states
        )

    async def test_reauthorization_and_full_resync_are_not_hidden(
        self, app: Phase1App
    ) -> None:
        app.store["sync_cursors"][f"calendar:{CONTROLLED_USER}"][
            "full_resync_required"
        ] = True

        async def _save(repositories) -> None:  # noqa: ANN001
            await repositories.sync_requests.upsert(
                f"gmail:{CONTROLLED_USER}",
                {
                    "source": "gmail",
                    "user_id": CONTROLLED_USER,
                    "status": "reauth_required",
                    "updated_at": app.clock.now(),
                },
            )

        await app.uow.run(_save)
        status = await GetSystemStatus(app.uow, app.clock).execute(CONTROLLED_USER)
        states = {failure["state"] for failure in status.failure_states}
        assert {"reauth_required", "full_resync_required"}.issubset(states)


class TestRealWatchEchoAndLatency:
    async def test_repair_watch_echo_starts_no_second_repair_and_records_budget(
        self, app: Phase1App
    ) -> None:
        await create_live_plan(app)
        blocks = sorted(
            (
                dict(value) | {"work_block_id": key}
                for key, value in app.store["work_blocks"].items()
            ),
            key=lambda value: (value["scheduled_start"], value["work_block_id"]),
        )
        target = blocks[1]
        app.calendar.events[(CALENDAR_ID, "phase4c-real-meeting")] = {
            "status": "confirmed",
            "start": target["scheduled_start"],
            "end": target["scheduled_end"],
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        router, headers = webhook(app)
        headers["X-Goog-Message-Number"] = "501"
        response = await router.receive("POST", headers, b"", "trace-4c-watch-conflict")
        assert response.status_code == 204
        await app.run_source_sync_tasks()
        await app.run_reconciliation_tasks()
        patch_count_before = sum(
            mutation == "patch" for mutation, _ in app.calendar.mutation_log
        )
        await app.run_calendar_action_tasks()
        patch_count_after = sum(
            mutation == "patch" for mutation, _ in app.calendar.mutation_log
        )
        assert patch_count_after == patch_count_before + 1
        outbox_count = len(app.store["action_outbox"])
        echo_count_before = sum(
            row["observation_type"] == ObservationType.CALENDAR_APP_ECHO.value
            and row["source_reference"].get("work_block_id")
            == target["work_block_id"]
            for row in app.store["source_observations"].values()
        )

        headers["X-Goog-Message-Number"] = "502"
        response = await router.receive("POST", headers, b"", "trace-4c-watch-echo")
        assert response.status_code == 204
        await app.run_source_sync_tasks()
        echo = [
            row
            for row in app.store["source_observations"].values()
            if row["observation_type"] == ObservationType.CALENDAR_APP_ECHO.value
            and row["source_reference"].get("work_block_id")
            == target["work_block_id"]
        ]
        assert len(echo) == echo_count_before + 1
        assert echo[-1]["reconciliation_status"] == ReconciliationStatus.IGNORED.value
        assert len(app.store["action_outbox"]) == outbox_count
        assert sum(mutation == "patch" for mutation, _ in app.calendar.mutation_log) == (
            patch_count_after
        )

        explanation = next(
            row
            for row in app.store["activity_events"].values()
            if row["event_type"] == ActivityEventType.PLAN_REPAIRED.value
        )
        assert explanation["payload"]["moved_block_count"] == 1
        assert explanation["payload"]["mutations"][0]["before_start"]
        assert explanation["payload"]["mutations"][0]["after_start"]
        assert explanation["payload"]["risk_arc"]
        calendar_result = next(
            row
            for row in app.store["activity_events"].values()
            if row["event_type"] == ActivityEventType.CALENDAR_ACTION_RESULT.value
            and row["payload"].get("source_observation_id")
        )
        assert calendar_result["payload"]["repair_latency_ms"] < 15_000
        assert calendar_result["payload"]["warmed_repair_under_15_seconds"] is True
