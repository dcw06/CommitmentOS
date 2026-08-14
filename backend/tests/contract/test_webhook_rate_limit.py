"""D4 webhook hardening — the durable per-channel rate limit under exceedance.

The limit shipped with Phase 4A (Firestore-backed sliding window). These are
the deferred D4 negatives: over-limit valid signals return 429 with zero
side effects, the limit survives a process restart, and the window recovers.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import timedelta

from conftest import CALENDAR_ID, CONTROLLED_USER, TASK_SCHEMA_VERSION, Phase1App, restarted
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commitmentos.api.dependencies.calendar_channel import CalendarChannelVerifier
from commitmentos.api.middleware.request_context import RequestContextMiddleware
from commitmentos.api.routers.calendar_webhook import CalendarWebhookRouter
from commitmentos.application.commands.receive_calendar_signal import ReceiveCalendarSignal

WINDOW_LIMIT = 20
CHANNEL_TOKEN = "rate-limit-channel-token"


def _client(app: Phase1App) -> tuple[TestClient, dict[str, str]]:
    app.store.setdefault("calendar_channels", {})[CONTROLLED_USER] = {
        "user_id": CONTROLLED_USER,
        "calendar_id": CALENDAR_ID,
        "channel_id": "rate-limit-channel",
        "resource_id": "rate-limit-resource",
        "token_hash": hashlib.sha256(CHANNEL_TOKEN.encode()).hexdigest(),
        "expiration": app.clock.now() + timedelta(days=1),
    }
    router = CalendarWebhookRouter(
        CalendarChannelVerifier(app.uow, app.clock, WINDOW_LIMIT, 60),
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
        "X-Goog-Channel-ID": "rate-limit-channel",
        "X-Goog-Resource-ID": "rate-limit-resource",
        "X-Goog-Channel-Token": CHANNEL_TOKEN,
        "X-Goog-Resource-State": "exists",
    }
    fastapi_app = FastAPI()
    fastapi_app.add_middleware(RequestContextMiddleware)
    fastapi_app.include_router(router.build())
    return TestClient(fastapi_app, raise_server_exceptions=False), headers


def _signal(client: TestClient, headers: dict[str, str], message_number: int):
    return client.post(
        "/webhooks/calendar",
        headers={**headers, "X-Goog-Message-Number": str(message_number)},
    )


class TestWebhookRateLimitExceedance:
    def test_over_limit_valid_signals_are_rejected_with_zero_side_effects(
        self, app: Phase1App
    ) -> None:
        client, headers = _client(app)
        for number in range(1, WINDOW_LIMIT + 1):
            assert _signal(client, headers, number).status_code == 204

        sync_requests_before = copy.deepcopy(app.store.get("sync_requests", {}))
        tasks_before = len(app.task_dispatcher.source_sync_tasks)

        for number in range(WINDOW_LIMIT + 1, WINDOW_LIMIT + 4):
            response = _signal(client, headers, number)
            assert response.status_code == 429
        assert app.store.get("sync_requests", {}) == sync_requests_before
        assert len(app.task_dispatcher.source_sync_tasks) == tasks_before

    def test_rate_limit_state_is_durable_across_process_restart(
        self, app: Phase1App
    ) -> None:
        client, headers = _client(app)
        for number in range(1, WINDOW_LIMIT + 1):
            assert _signal(client, headers, number).status_code == 204

        # A new process over the same durable store: the Firestore-backed
        # window still applies — this is what distinguishes the durable limit
        # from the Phase 0 per-instance in-memory limiter.
        fresh_client, fresh_headers = _client(restarted(app))
        response = _signal(fresh_client, fresh_headers, WINDOW_LIMIT + 1)
        assert response.status_code == 429

    def test_window_recovers_after_expiry(self, app: Phase1App) -> None:
        client, headers = _client(app)
        for number in range(1, WINDOW_LIMIT + 1):
            assert _signal(client, headers, number).status_code == 204
        assert _signal(client, headers, WINDOW_LIMIT + 1).status_code == 429

        app.clock.advance(61)
        recovered = _signal(client, headers, WINDOW_LIMIT + 2)
        assert recovered.status_code == 204

    def test_invalid_token_is_rejected_before_the_rate_limit_is_consumed(
        self, app: Phase1App
    ) -> None:
        client, headers = _client(app)
        bad = {**headers, "X-Goog-Channel-Token": "wrong-token"}
        for number in range(1, WINDOW_LIMIT + 5):
            assert _signal(client, bad, number).status_code == 403
        # Invalid probes cannot exhaust the valid-signal budget.
        assert app.store.get("calendar_channel_rate_limits", {}) == {}
        assert _signal(client, headers, 999).status_code == 204
