"""Phase 2 gate — checklist D2: Gmail bounded synchronization generations.

Every row of D2 runs against the production command stack over the in-memory
Firestore twin: a deterministic two-page Gmail fixture, staged in bounded
fenced commits, applied, and published exactly once, with worker death and
stale-fence rejection exercised explicitly.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from conftest import CONTROLLED_EMAIL, CONTROLLED_USER, Phase1App, restarted

from commitmentos.application.dto import CommandStatus, FencedLease
from commitmentos.contracts.observations import ReconciliationStatus
from commitmentos.contracts.synchronization import (
    SyncGenerationStatus,
    SyncIdFactory,
)
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType
from commitmentos.domain.shared.errors import InvalidTransitionError

THREAD = "thread_sync_fixture_001"
BASE_HISTORY = "5000"
CANDIDATE_HISTORY = "5210"


def seed_spike_cursor(app: Phase1App) -> None:
    """The deployed Phase 0 watch registration wrote this exact shape."""
    app.store.setdefault("sync_cursors", {})[f"gmail:{CONTROLLED_USER}"] = {
        "source": "gmail",
        "user_id": CONTROLLED_USER,
        "published_history_id": int(BASE_HISTORY),
        "watch_expiration": datetime(2026, 8, 19, 17, 0, tzinfo=timezone.utc),
        "watch_registered_at": datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    }


def script_two_page_history(app: Phase1App) -> list[str]:
    base = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
    message_ids = []
    for index in range(5):
        message_id = f"msg_sync_{index:03d}"
        app.gmail.add_message(
            message_id,
            THREAD,
            base + timedelta(minutes=index),
            subject=f"Fixture message {index}",
            body_text=f"Body of fixture message {index}.",
            label_ids=("INBOX",) if index % 2 == 0 else ("SENT",),
        )
        message_ids.append(message_id)
    # One irrelevant draft that normalization must filter out.
    app.gmail.add_message(
        "msg_sync_draft",
        THREAD,
        base + timedelta(minutes=30),
        subject="Draft",
        body_text="Unsent draft.",
        label_ids=("DRAFT",),
    )
    app.gmail.script_history(
        [
            (message_ids[:3] + ["msg_sync_draft"], "page-2-token", "5100"),
            (message_ids[3:], None, CANDIDATE_HISTORY),
        ]
    )
    return message_ids


def pubsub_envelope(history_id: int) -> dict:
    payload = json.dumps(
        {"emailAddress": CONTROLLED_EMAIL, "historyId": history_id}
    ).encode()
    return {"message": {"data": base64.b64encode(payload).decode()}}


async def signal_and_sync(app: Phase1App) -> None:
    await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
    await app.run_source_sync_tasks()


def generation_documents(app: Phase1App) -> dict[str, dict]:
    return dict(app.store.get("sync_generations", {}))


def cursor_document(app: Phase1App) -> dict:
    return app.store["sync_cursors"][f"gmail:{CONTROLLED_USER}"]


class TestBoundedGenerationProtocol:
    async def test_two_page_generation_stages_applies_and_publishes_once(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        message_ids = script_two_page_history(app)

        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        # The signal committed a coalesced request and one named bootstrap task.
        request = app.store["sync_requests"][f"gmail:{CONTROLLED_USER}"]
        assert request["status"] == "pending"
        assert request["latest_history_id"] == 5200
        assert len(app.task_dispatcher.source_sync_tasks) == 1

        results = await app.run_source_sync_tasks()
        # Bootstrap staged page 1 and enqueued the page-2 continuation.
        assert results[0].status == CommandStatus.ACCEPTED
        assert results[1].status == CommandStatus.COMPLETED

        generations = generation_documents(app)
        assert len(generations) == 1
        generation_id, generation = next(iter(generations.items()))

        # Generation derived from the adopted spike cursor at revision 0.
        assert generation["base_published_cursor_revision"] == 0
        assert generation["status"] == SyncGenerationStatus.PUBLISHED.value
        assert generation["page_count"] == 2
        # The draft was filtered; five relevant messages staged.
        assert generation["staged_item_count"] == 5
        assert generation["applied_item_count"] == 5
        assert generation["staged_manifest"] == generation["applied_manifest"]

        # Deterministic item identities.
        for message_id in message_ids:
            message = app.gmail.messages[message_id]
            item_id = SyncIdFactory.item_id(
                generation_id, message_id, message.payload_hash
            )
            item = app.store["sync_generation_items"][item_id]
            assert item["status"] == "applied"

        # Publication promoted the candidate cursor exactly once and cleared
        # the barrier.
        cursor = cursor_document(app)
        assert cursor["revision"] == 1
        assert cursor["published_cursor"] == CANDIDATE_HISTORY
        assert cursor["published_generation_id"] == generation_id
        assert cursor["publish_in_progress_generation_id"] is None

        # Released observations reached the reconciliation queue.
        observations = app.store["source_observations"]
        assert len(observations) == 5
        assert all(
            doc["reconciliation_status"] == ReconciliationStatus.QUEUED.value
            for doc in observations.values()
        )
        assert len(app.task_dispatcher.reconciliation_tasks) == 5
        assert request_status(app) == "published"

    async def test_vanished_message_in_history_is_skipped(
        self, app: Phase1App
    ) -> None:
        """History records can reference messages that no longer exist
        (discarded drafts, deletions). The worker skips them instead of
        crashing into a retry storm — the live-gate finding of 2026-08-13."""
        seed_spike_cursor(app)
        script_two_page_history(app)
        # Rewrite page 1 so it also references a message the mailbox no
        # longer has.
        page_one = app.gmail.history_pages[None]
        from commitmentos.application.ports.gmail_reader import (
            GmailHistoryChange,
            GmailHistoryPage,
        )

        app.gmail.history_pages[None] = GmailHistoryPage(
            changes=page_one.changes
            + (
                GmailHistoryChange(
                    history_id="5050",
                    message_ids=("msg_vanished_draft",),
                    label_ids=(),
                ),
            ),
            next_page_token=page_one.next_page_token,
            latest_history_id=page_one.latest_history_id,
        )
        await signal_and_sync(app)
        generation = next(iter(generation_documents(app).values()))
        assert generation["status"] == SyncGenerationStatus.PUBLISHED.value
        assert generation["staged_item_count"] == 5  # vanished message skipped
        assert cursor_document(app)["revision"] == 1

    async def test_published_cursor_unchanged_after_page_one(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        # Deliver only the bootstrap task (stages page 1, stops before page 2).
        results = await app.run_source_sync_tasks(limit=1)
        assert results[0].status == CommandStatus.ACCEPTED

        cursor = cursor_document(app)
        # Spike shape untouched except the reserved generation counter.
        assert cursor["revision"] == 0
        assert cursor["published_cursor"] == BASE_HISTORY

        generation = next(iter(generation_documents(app).values()))
        assert generation["status"] == SyncGenerationStatus.STAGING.value
        assert generation["current_page_sequence"] == 1
        assert generation["next_page_token"] == "page-2-token"
        # The candidate cursor is not stored until the final page.
        assert generation["candidate_next_cursor"] is None

    async def test_page_retry_reuses_deterministic_item_ids(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        await app.run_source_sync_tasks(limit=1)
        items_after_page_one = set(app.store["sync_generation_items"])
        generation_id = next(iter(generation_documents(app)))

        # Cloud Tasks redelivers page 1 (named task retry) on a fresh worker.
        replayed = restarted(app)
        result = await replayed.synchronize_source.execute(
            SourceSyncTaskV1(
                schema_version="task_v1",
                sync_request_id=f"gmail:{CONTROLLED_USER}",
                sync_generation_id=generation_id,
                page_sequence=1,
                source=SourceType.GMAIL,
                user_id=CONTROLLED_USER,
                trace_id="trace-retry",
            )
        )
        # The redelivered page finds its checkpoint committed, restages
        # nothing, and re-ensures the page-2 continuation task.
        assert result.status == CommandStatus.ACCEPTED
        assert set(app.store["sync_generation_items"]) == items_after_page_one
        generation = generation_documents(app)[generation_id]
        assert generation["staged_item_count"] == 3  # page 1 only, draft filtered

        # Draining the queue completes the generation with deterministic IDs.
        await replayed.run_source_sync_tasks()
        generation = generation_documents(app)[generation_id]
        assert generation["staged_item_count"] == 5
        assert items_after_page_one <= set(app.store["sync_generation_items"])

    async def test_stale_fencing_token_cannot_checkpoint_or_publish(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        await app.run_source_sync_tasks(limit=1)  # page 1 staged, mid-generation

        generation_id = next(iter(generation_documents(app)))
        generation = generation_documents(app)[generation_id]
        current_token = generation["source_fencing_token"]

        # A worker whose fence predates the recorded one must be unable to
        # checkpoint or publish the in-flight generation.
        stale_fence = FencedLease(
            lease_key=f"source-sync:gmail:{CONTROLLED_USER}",
            owner="dead-worker",
            fencing_token="0",
            expires_at=app.clock.now() + timedelta(seconds=300),
        )
        assert stale_fence.fencing_token != current_token

        async def _try_checkpoint(repositories) -> None:
            from commitmentos.contracts.synchronization import (
                MANIFEST_ALGORITHM_V1,
                SyncManifestHasher,
                SyncPageCheckpoint,
            )

            hasher = SyncManifestHasher()
            await repositories.sync_generations.record_page_checkpoint(
                SyncPageCheckpoint(
                    sync_generation_id=generation_id,
                    page_sequence=99,
                    staged_item_count=0,
                    committed_write_count=0,
                    estimated_commit_bytes=0,
                    page_manifest=hasher.empty(MANIFEST_ALGORITHM_V1),
                    aggregate_staged_manifest=hasher.empty(MANIFEST_ALGORITHM_V1),
                    next_page_token=None,
                    candidate_next_cursor=None,
                    final_provider_page=True,
                    committed_at=app.clock.now(),
                ),
                SyncGenerationStatus.STAGING,
                stale_fence,
            )

        import pytest

        with pytest.raises(InvalidTransitionError):
            await app.uow.run(_try_checkpoint)

        async def _try_publish(repositories) -> None:
            generation_record = await repositories.sync_generations.get(generation_id)
            await repositories.sync_cursors.publish_generation(
                generation_record,
                SyncGenerationStatus.READY_TO_PUBLISH,
                generation_record.staged_manifest,
                generation_record.applied_manifest,
                stale_fence,
                app.clock.now(),
            )

        with pytest.raises(InvalidTransitionError):
            await app.uow.run(_try_publish)

    async def test_worker_death_resumes_from_durable_checkpoint(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        await app.run_source_sync_tasks(limit=1)  # worker dies after page 1

        app.clock.advance(400)  # lease expires
        recovered = restarted(app)
        results = await recovered.run_source_sync_tasks()
        assert results[-1].status == CommandStatus.COMPLETED
        cursor = cursor_document(app)
        assert cursor["revision"] == 1
        assert cursor["published_cursor"] == CANDIDATE_HISTORY
        # One generation, published; no duplicate items from the takeover.
        generations = generation_documents(app)
        assert len(generations) == 1
        assert next(iter(generations.values()))["staged_item_count"] == 5

    async def test_coalesced_signal_cannot_start_second_generation(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        await app.run_source_sync_tasks(limit=1)  # generation active, mid-staging

        # A second signal lands while the generation is in flight; its
        # bootstrap task must adopt the active generation, never start a
        # second one from the same published cursor.
        await app.receive_gmail.execute(pubsub_envelope(5205), "trace-sync-test-2")
        second_bootstrap = app.task_dispatcher.source_sync_tasks[-1][1]
        assert second_bootstrap.page_sequence == 0
        result = await app.synchronize_source.execute(second_bootstrap)
        assert result.status == CommandStatus.COMPLETED
        assert len(generation_documents(app)) == 1
        assert cursor_document(app)["revision"] == 1

        # Remaining queued tasks (the page-2 continuation) converge without
        # further mutation.
        await app.run_source_sync_tasks()
        assert len(generation_documents(app)) == 1

    async def test_replayed_pubsub_signal_converges_on_one_named_task(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-a")
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-b")
        assert len(app.task_dispatcher.source_sync_tasks) == 1
        request = app.store["sync_requests"][f"gmail:{CONTROLLED_USER}"]
        assert request["signal_count"] == 2


class TestSyncFailureStates:
    async def test_auth_failure_records_reauth_required(self, app: Phase1App) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        app.gmail.raise_auth_error = True
        results = await app.run_source_sync_tasks()
        assert results[0].status == CommandStatus.TERMINAL_FAILURE
        assert results[0].error_code == "reauth_required"
        assert request_status(app) == "reauth_required"

    async def test_invalid_cursor_marks_full_resync_required(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        app.gmail.raise_cursor_invalid = True
        results = await app.run_source_sync_tasks()
        assert results[0].status == CommandStatus.TERMINAL_FAILURE
        assert results[0].error_code == "full_resync_required"
        assert cursor_document(app)["full_resync_required"] is True

    async def test_crash_gap_repaired_by_maintenance(self, app: Phase1App) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        # The request commits but the named task creation fails (B1 shape).
        app.task_dispatcher.fail_next_enqueues = 1
        import pytest

        with pytest.raises(ConnectionError):
            await app.receive_gmail.execute(pubsub_envelope(5200), "trace-sync-test")
        assert request_status(app) == "pending"
        assert len(app.task_dispatcher.source_sync_tasks) == 0

        result = await app.maintenance.dispatch_pending("trace-repair")
        assert result.identifiers["sync_requests_redispatched"] == "1"
        results = await app.run_source_sync_tasks()
        assert results[-1].status == CommandStatus.COMPLETED
        assert cursor_document(app)["revision"] == 1


class TestScheduledSyncMaintenance:
    async def test_watch_renewal_never_touches_published_cursor(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        before = dict(cursor_document(app))
        result = await app.maintenance.renew_watches("trace-renew")
        assert result.status == CommandStatus.COMPLETED
        assert app.gmail.watch_calls == 1
        after = cursor_document(app)
        assert after["published_history_id"] == before["published_history_id"]
        request = app.store["sync_requests"][f"gmail:{CONTROLLED_USER}"]
        assert "watch_expiration" in request

    async def test_quiet_window_catch_up_creates_and_dispatches_request(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        script_two_page_history(app)
        # Cursor activity is older than the quiet window.
        app.clock.advance(int(timedelta(hours=7).total_seconds()))
        result = await app.maintenance.recover_cursors("trace-catchup")
        assert result.status == CommandStatus.COMPLETED
        assert len(app.task_dispatcher.source_sync_tasks) == 1
        results = await app.run_source_sync_tasks()
        assert results[-1].status == CommandStatus.COMPLETED
        assert cursor_document(app)["revision"] == 1

    async def test_legacy_cursor_without_activity_timestamp_is_stale(
        self, app: Phase1App
    ) -> None:
        seed_spike_cursor(app)
        del app.store["sync_cursors"][f"gmail:{CONTROLLED_USER}"]["watch_registered_at"]
        script_two_page_history(app)

        result = await app.maintenance.recover_cursors("trace-unknown-cursor-age")

        assert result.status == CommandStatus.COMPLETED
        assert len(app.task_dispatcher.source_sync_tasks) == 1
        results = await app.run_source_sync_tasks()
        assert results[-1].status == CommandStatus.COMPLETED
        assert cursor_document(app)["revision"] == 1

    async def test_catch_up_skips_when_recent_activity(self, app: Phase1App) -> None:
        seed_spike_cursor(app)
        # Fresh cursor (updated at the fake clock's now minus nothing).
        app.store["sync_cursors"][f"gmail:{CONTROLLED_USER}"][
            "watch_registered_at"
        ] = app.clock.now()
        result = await app.maintenance.recover_cursors("trace-catchup")
        assert result.status == CommandStatus.NO_OP
        assert result.error_code == "recent_activity"


def request_status(app: Phase1App) -> str:
    return app.store["sync_requests"][f"gmail:{CONTROLLED_USER}"]["status"]
