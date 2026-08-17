from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta
from typing import Any, Mapping

from commitmentos.application.dto import CommandResult, CommandStatus
from commitmentos.application.ports.clock import Clock
from commitmentos.application.ports.task_dispatcher import TaskDispatcher
from commitmentos.application.ports.unit_of_work import RepositorySet, UnitOfWork
from commitmentos.contracts.tasks import SourceSyncTaskV1, SourceType


class MalformedSignalError(ValueError):
    """The Pub/Sub envelope does not decode to a Gmail notification."""


class UnexpectedMailboxError(ValueError):
    """The notification names a mailbox other than the controlled account."""


class SignalRateLimitError(ValueError):
    """A valid delivery exceeded the bounded change-signal fetch budget."""


def bootstrap_generation_marker(latest_history_id: int) -> str:
    """Placeholder generation id for signal-triggered tasks.

    A real generation does not exist until the sync worker creates one against
    the published cursor, but the named task must still differ per signal so
    Cloud Tasks' 24-hour completed-name retention cannot swallow a later
    notification. The worker resolves the active generation itself and treats
    this marker as opaque.
    """
    return f"signal-{latest_history_id}"


class ReceiveGmailSignal:
    """Authenticated Pub/Sub ingress: durable coalesced sync request, then a
    named source-sync task (plan §9.1 steps 3–4).

    Pub/Sub delivery is at-least-once and unordered; the request document
    coalesces on `max(latest_history_id)` and the queue/lease pair downstream
    serializes actual cursor work. Pub/Sub's job ends here — every durable
    step after ingress travels through Cloud Tasks.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        task_dispatcher: TaskDispatcher,
        clock: Clock,
        controlled_user_id: str,
        controlled_email: str,
        task_schema_version: str,
        maximum_signals_per_window: int = 60,
        rate_limit_window_seconds: int = 60,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._task_dispatcher = task_dispatcher
        self._clock = clock
        self._controlled_user_id = controlled_user_id
        self._controlled_email = controlled_email
        self._task_schema_version = task_schema_version
        self._maximum_signals_per_window = maximum_signals_per_window
        self._rate_limit_window_seconds = rate_limit_window_seconds

    async def execute(
        self,
        envelope: Mapping[str, Any],
        trace_id: str,
    ) -> CommandResult:
        notification = self._decode_notification(envelope)
        if notification["email_address"] != self._controlled_email:
            raise UnexpectedMailboxError("notification for an unexpected mailbox")
        history_id = int(notification["history_id"])
        now = self._clock.now()
        request_id = self._sync_request_id(self._controlled_user_id)

        async def _coalesce(repositories: RepositorySet) -> int | None:
            current = await repositories.sync_requests.get(request_id)
            window_started_at = (
                current.get("rate_window_started_at") if current is not None else None
            )
            if not isinstance(window_started_at, datetime) or (
                now - window_started_at
                >= timedelta(seconds=self._rate_limit_window_seconds)
            ):
                window_started_at = now
                rate_count = 0
            else:
                rate_count = int(current.get("rate_window_signal_count", 0))
            if rate_count >= self._maximum_signals_per_window:
                return None
            latest = history_id
            if current is not None:
                latest = max(int(current.get("latest_history_id", 0)), history_id)
            await repositories.sync_requests.upsert(
                request_id,
                {
                    "source": SourceType.GMAIL.value,
                    "user_id": self._controlled_user_id,
                    "latest_history_id": latest,
                    "status": "pending",
                    "rate_window_started_at": window_started_at,
                    "rate_window_signal_count": rate_count + 1,
                    "updated_at": now,
                },
            )
            return latest

        latest_history_id = await self._unit_of_work.run(_coalesce)
        if latest_history_id is None:
            raise SignalRateLimitError("gmail change signal rate limited")

        # Task creation strictly after the durable commit; a failed enqueue
        # stays repairable through maintenance dispatch_pending.
        result = await self._task_dispatcher.enqueue_source_sync(
            SourceSyncTaskV1(
                schema_version=self._task_schema_version,
                sync_request_id=request_id,
                sync_generation_id=bootstrap_generation_marker(latest_history_id),
                page_sequence=0,
                source=SourceType.GMAIL,
                user_id=self._controlled_user_id,
                trace_id=trace_id,
            )
        )
        return CommandResult(
            status=CommandStatus.ACCEPTED,
            identifiers={
                "sync_request_id": request_id,
                "latest_history_id": str(latest_history_id),
                "task_name": result.task_name,
            },
            revision=None,
            error_code=None,
        )

    @staticmethod
    def _decode_notification(envelope: Mapping[str, Any]) -> Mapping[str, str]:
        message = envelope.get("message")
        if not isinstance(message, Mapping):
            raise MalformedSignalError("missing pubsub message")
        data = message.get("data", "")
        try:
            payload = json.loads(base64.b64decode(data).decode("utf-8"))
            return {
                "email_address": payload["emailAddress"],
                "history_id": str(int(payload["historyId"])),
            }
        except (KeyError, ValueError, TypeError, json.JSONDecodeError, binascii.Error) as error:
            raise MalformedSignalError("malformed gmail notification") from error

    @staticmethod
    def _sync_request_id(user_id: str) -> str:
        # One coalescing document per user and source, matching the spike's
        # `gmail:{user}` so the deployed document is adopted in place.
        return f"{SourceType.GMAIL.value}:{user_id}"
