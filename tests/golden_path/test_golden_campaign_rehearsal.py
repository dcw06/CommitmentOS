"""Local rehearsal of the Phase 5B golden campaign (seeded mode).

Mirrors `scripts/run_golden_path.py`'s run sequence over the in-memory twin
before any live run: portfolio context, golden commitment, effort 180, plan
approval with a verified constraint envelope, real-machinery conflict repair
through the webhook path, verified check-ins, elapse, explicit completion,
replay convergence, and the frozen audit-order contract. The rehearsal is
stricter than the live driver in one respect: block elapse here uses the
real periodic safety scan under the controllable clock, not the driver
stand-in.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta

from commitmentos.api.dependencies.calendar_channel import CalendarChannelVerifier
from commitmentos.api.routers.calendar_webhook import CalendarWebhookRouter
from commitmentos.application.commands.complete_commitment import (
    CompleteCommitmentRequest,
)
from commitmentos.application.commands.receive_calendar_signal import (
    ReceiveCalendarSignal,
)
from commitmentos.application.commands.run_maintenance import MaintenanceKind
from commitmentos.application.dto import CommandStatus
from commitmentos.contracts.observations import ObservationType
from commitmentos.domain.shared.types import CanonicalEncoder
from tests.fault_injection.harness import harness

CONTROLLED_USER = harness.CONTROLLED_USER
CALENDAR_ID = harness.CALENDAR_ID
TASK_SCHEMA_VERSION = harness.TASK_SCHEMA_VERSION

# Mirrors GoldenPathRunner.AUDIT_ORDER (scripts/run_golden_path.py).
AUDIT_ORDER = (
    "interpretation_created",
    "confirmation_recorded",
    "plan_proposed",
    "outbox_written",
    "calendar_action_result",
    "work_check_in_recorded",
    "plan_repaired",
    "work_check_in_recorded",
    "work_check_in_required",
    "completion_recorded",
)


def _payload(app, *, thread: str, span: str, title: str, beneficiary: str,  # noqa: ANN001
             effort: int, deadline_days: int, deadline_hour: int, fingerprint: str) -> dict:
    deadline = (app.clock.now() + timedelta(days=deadline_days)).replace(
        hour=deadline_hour, minute=0, second=0, microsecond=0
    )
    return {
        "source_thread_id": thread,
        "source_span_key": span,
        "title": title,
        "description": "golden campaign rehearsal",
        "ownership_type": "my_commitment",
        "beneficiary": beneficiary,
        "deadline_value": deadline.isoformat(),
        "deadline_expression": "rehearsal deadline",
        "deadline_confidence": 0.93,
        "timezone": harness.CONTROLLED_TIMEZONE,
        "proposed_effort_minutes": effort,
        "effort_confidence": 0.58,
        "semantic_fingerprint": fingerprint,
        "evidence_excerpt": "rehearsal evidence excerpt",
    }


async def _seed(app, payload: dict, label: str) -> str:  # noqa: ANN001
    observation = app.observation_factory.source_change(
        observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
        user_id=CONTROLLED_USER,
        producer_id=f"{CONTROLLED_USER}:{label}",
        producer_version="rehearsal-1",
        source="gmail",
        external_id=label,
        external_version="rehearsal-1",
        payload_hash="rehearsal-1",
        source_reference={
            "thread_id": payload["source_thread_id"],
            "message_id": label,
        },
        safe_metadata={"seeded_commitment": payload},
        observed_at=app.clock.now(),
        trace_id=f"trace-rehearsal-{label}",
    )

    async def _create(repositories):  # noqa: ANN001, ANN202
        await repositories.observations.create(observation)

    await app.uow.run(_create)
    await app.observation_dispatcher.dispatch(observation.observation_id)
    return observation.observation_id


async def _pending(app, request_type: str, commitment_id: str) -> dict:  # noqa: ANN001
    async def _load(repositories):  # noqa: ANN001, ANN202
        return [
            value
            for value in await repositories.approvals.list_pending(CONTROLLED_USER)
            if value["request_type"] == request_type
            and value["commitment_id"] == commitment_id
        ]

    rows = await app.uow.read(_load)
    assert rows, f"no pending {request_type} for {commitment_id[:12]}"
    return rows[0]


async def _approve(app, approval: dict, **extra) -> None:  # noqa: ANN001
    result = await app.resolve_approval.execute(
        app.actor(),
        approval["approval_id"],
        {"decision": "approve", **extra},
        approval["revision"],
        "trace-rehearsal-approve",
    )
    assert result.status == CommandStatus.COMPLETED, result.error_code


def _blocks(app, commitment_id: str) -> list[tuple[str, dict]]:  # noqa: ANN001
    return sorted(
        (
            (block_id, row)
            for block_id, row in app.store["work_blocks"].items()
            if row["commitment_id"] == commitment_id
        ),
        key=lambda pair: pair[1]["scheduled_start"],
    )


def _webhook(app):  # noqa: ANN001, ANN202
    token = "rehearsal-channel-token"
    app.store.setdefault("calendar_channels", {})[CONTROLLED_USER] = {
        "user_id": CONTROLLED_USER,
        "calendar_id": CALENDAR_ID,
        "channel_id": "rehearsal-channel",
        "resource_id": "rehearsal-resource",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expiration": app.clock.now() + timedelta(days=7),
    }
    router = CalendarWebhookRouter(
        CalendarChannelVerifier(app.uow, app.clock, 20, 60),
        ReceiveCalendarSignal(
            app.uow, app.task_dispatcher, app.clock, app.ids, TASK_SCHEMA_VERSION
        ),
        "/webhooks/calendar",
    )
    headers = {
        "X-Goog-Channel-ID": "rehearsal-channel",
        "X-Goog-Resource-ID": "rehearsal-resource",
        "X-Goog-Channel-Token": token,
        "X-Goog-Resource-State": "exists",
    }
    return router, headers


async def _elapse_via_safety(app, until: datetime) -> None:  # noqa: ANN001
    if app.clock.now() < until:
        app.clock.current = until
    await app.maintenance.execute(
        MaintenanceKind.SAFETY_RECONCILIATION, "trace-rehearsal-elapse"
    )
    await app.drain()


async def _check_in(app, block_id: str, minutes: int, key: str) -> None:  # noqa: ANN001
    from commitmentos.application.commands.record_work_check_in import (
        WorkCheckInRequest,
    )

    row = app.store["work_blocks"][block_id]
    result = await app.record_work_check_in.execute(
        app.actor(),
        WorkCheckInRequest(
            work_block_id=block_id,
            idempotency_key=key,
            completed=True,
            verified_minutes=minutes,
            checked_in_at=app.clock.now(),
            expected_revision=row["revision"],
        ),
        "trace-rehearsal-check-in",
    )
    assert result.status == CommandStatus.COMPLETED, result.error_code
    await app.drain()


class TestGoldenCampaignRehearsal:
    async def test_full_campaign_run_sequence(self, app) -> None:  # noqa: ANN001
        run_start = app.clock.now()

        # 1. Portfolio context: the second commitment, planned first.
        second_payload = _payload(
            app,
            thread="thread_rehearsal_data_summary",
            span="message_rehearsal_data_summary:span0",
            title="Prepare Q3 data summary for Sam",
            beneficiary="Sam",
            effort=120,
            deadline_days=6,
            deadline_hour=12,
            fingerprint="my_commitment:q3-data-summary:sam",
        )
        second_id = CanonicalEncoder.hash(
            [
                "commitment:v1",
                CONTROLLED_USER,
                second_payload["source_thread_id"],
                second_payload["source_span_key"],
            ]
        )
        await _seed(app, second_payload, "message_rehearsal_data_summary")
        await app.run_reconciliation_tasks()
        await _approve(
            app, await _pending(app, "effort_confirmation", second_id), confirmed_minutes=120
        )
        await app.run_reconciliation_tasks()
        await _approve(app, await _pending(app, "initial_plan_approval", second_id))
        await app.drain()
        await app.synchronize_calendar_truth()
        second_before = copy.deepcopy(
            [(i, d["scheduled_start"], d["calendar_event_id"]) for i, d in _blocks(app, second_id)]
        )
        assert second_before, "second commitment has no blocks"

        # 2. The golden commitment (seeded mode).
        golden_payload = _payload(
            app,
            thread="thread_rehearsal_golden_proposal",
            span="message_rehearsal_golden_proposal:span0",
            title="Send revised proposal to Professor Chen",
            beneficiary="Professor Chen",
            effort=180,
            deadline_days=3,
            deadline_hour=23,
            fingerprint="my_commitment:send-revised-proposal:professor-chen",
        )
        golden_id = CanonicalEncoder.hash(
            [
                "commitment:v1",
                CONTROLLED_USER,
                golden_payload["source_thread_id"],
                golden_payload["source_span_key"],
            ]
        )
        await _seed(app, golden_payload, "message_rehearsal_golden_proposal")
        await app.run_reconciliation_tasks()

        # 3. Effort confirmed at exactly 180; 4. plan approved.
        await _approve(
            app, await _pending(app, "effort_confirmation", golden_id), confirmed_minutes=180
        )
        await app.run_reconciliation_tasks()
        await _approve(app, await _pending(app, "initial_plan_approval", golden_id))
        await app.drain()
        await app.synchronize_calendar_truth()
        await app.run_reconciliation_tasks()

        golden_blocks = _blocks(app, golden_id)
        deadline = datetime.fromisoformat(golden_payload["deadline_value"])
        total = sum(d["duration_minutes"] for _, d in golden_blocks)
        assert total == 180
        intervals = [
            (d["scheduled_start"], d["scheduled_end"])
            for _, d in golden_blocks + _blocks(app, second_id)
        ]
        for index, (a_start, a_end) in enumerate(intervals):
            for b_start, b_end in intervals[index + 1 :]:
                assert not (a_start < b_end and b_start < a_end), "double allocation"
        for _, row in golden_blocks:
            assert row["scheduled_end"] <= deadline
            assert row["calendar_event_id"]

        # 5. First verified check-in (60 minutes) after real elapse.
        first_id, first_row = golden_blocks[0]
        await _elapse_via_safety(
            app, first_row["scheduled_end"] + timedelta(minutes=5)
        )
        assert app.store["work_blocks"][first_id]["execution_state"] == "awaiting_check_in"
        await _check_in(app, first_id, 60, "rehearsal-check-in-1")

        # 6. Conflict over the next planned golden block → automatic repair.
        remaining = [
            (i, d) for i, d in _blocks(app, golden_id) if d["execution_state"] == "planned"
        ]
        target_id, target_row = remaining[0]
        original_start = target_row["scheduled_start"]
        unaffected_before = {
            i: (d["scheduled_start"], d["calendar_event_id"])
            for i, d in _blocks(app, golden_id)
            if i != target_id
        }
        app.calendar.events[(CALENDAR_ID, "rehearsal-conflict-meeting")] = {
            "status": "confirmed",
            "start": target_row["scheduled_start"],
            "end": target_row["scheduled_end"],
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        router, headers = _webhook(app)
        headers["X-Goog-Message-Number"] = "901"
        response = await router.receive("POST", headers, b"", "trace-rehearsal-conflict")
        assert response.status_code == 204
        await app.run_source_sync_tasks()
        await app.drain()

        moved = app.store["work_blocks"][target_id]
        assert moved["scheduled_start"] != original_start, "conflict was not repaired"
        assert moved["calendar_event_id"] == target_row["calendar_event_id"]
        unaffected_after = {
            i: (d["scheduled_start"], d["calendar_event_id"])
            for i, d in _blocks(app, golden_id)
            if i != target_id
        }
        assert unaffected_after == unaffected_before
        assert [
            (i, d["scheduled_start"], d["calendar_event_id"]) for i, d in _blocks(app, second_id)
        ] == second_before
        assert (CALENDAR_ID, "rehearsal-conflict-meeting") in app.calendar.events

        # Echo suppression: the repair's own change signal starts no loop.
        outbox_count = len(app.store["action_outbox"])
        headers["X-Goog-Message-Number"] = "902"
        response = await router.receive("POST", headers, b"", "trace-rehearsal-echo")
        assert response.status_code == 204
        await app.run_source_sync_tasks()
        await app.drain()
        assert len(app.store["action_outbox"]) == outbox_count

        # 7. Check in the moved block (total 120 verified).
        moved_row = app.store["work_blocks"][target_id]
        await _elapse_via_safety(app, moved_row["scheduled_end"] + timedelta(minutes=5))
        await _check_in(app, target_id, 60, "rehearsal-check-in-2")

        # 8. The final block elapses into the durable check-in request.
        final_rows = [
            (i, d) for i, d in _blocks(app, golden_id) if d["execution_state"] == "planned"
        ]
        latest_end = max(d["scheduled_end"] for _, d in final_rows)
        await _elapse_via_safety(app, latest_end + timedelta(minutes=5))
        for block_id, _ in final_rows:
            assert app.store["work_blocks"][block_id]["execution_state"] == (
                "awaiting_check_in"
            )

        # 9. Explicit completion; verified minutes stay 120 (§4.5).
        result = await app.complete_commitment.execute(
            app.actor(),
            CompleteCommitmentRequest(
                commitment_id=golden_id,
                idempotency_key="rehearsal-complete",
                completed_at=app.clock.now(),
                expected_revision=app.store["commitments"][golden_id]["revision"],
                note="Sent the revised proposal ahead of the Thursday review.",
            ),
            "trace-rehearsal-complete",
        )
        assert result.status == CommandStatus.COMPLETED
        await app.drain()
        stored = app.store["commitments"][golden_id]
        assert stored["lifecycle_status"] == "completed"
        verified = sum(d["verified_minutes"] for _, d in _blocks(app, golden_id))
        assert verified == 120
        assert not [
            i
            for i, d in _blocks(app, golden_id)
            if d["execution_state"] in ("awaiting_check_in", "active")
        ]

        # 10. Frozen audit order (campaign form).
        events = sorted(
            (
                {**row, "_id": event_id}
                for event_id, row in app.store["activity_events"].items()
                if row["user_id"] == CONTROLLED_USER
                and row["created_at"] >= run_start
            ),
            key=lambda row: (row["created_at"], row["_id"]),
        )
        sequence = [row["event_type"] for row in events]
        cursor = 0
        for expected in AUDIT_ORDER:
            assert expected in sequence[cursor:], f"audit order missing {expected}"
            cursor = sequence.index(expected, cursor) + 1

        # 11. Replay convergence: every task redelivered, state identical.
        state_before = copy.deepcopy(app.store)
        app._reconciliation_cursor = 0  # noqa: SLF001 - test redelivery control
        app._calendar_action_cursor = 0  # noqa: SLF001
        app._source_sync_cursor = 0  # noqa: SLF001
        await app.run_source_sync_tasks()
        await app.drain()
        assert app.store == state_before
        assert app.store["commitments"][golden_id]["lifecycle_status"] == "completed"
