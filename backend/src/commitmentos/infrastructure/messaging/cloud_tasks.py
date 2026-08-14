from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Mapping

from google.api_core import exceptions as google_exceptions
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from commitmentos.application.ports.task_dispatcher import TaskDispatchResult
from commitmentos.contracts.tasks import (
    ExecuteCalendarActionTaskV1,
    ReconcileObservationTaskV1,
    SourceSyncTaskV1,
    TaskNameFactory,
)


class CloudTasksDispatcher:
    """Named Cloud Tasks creation for the three P0 queues.

    Task names are a transport deduplication aid: AlreadyExists means the
    logical work is already enqueued and is returned as `created=False`.
    Payloads carry references only — never source bodies, tokens, or prompts.
    """

    def __init__(
        self,
        client: Any,
        project_id: str,
        region: str,
        queue_names: Mapping[str, str],
        handler_urls: Mapping[str, str],
        service_account_email: str,
        oidc_audience: str,
        task_name_factory: TaskNameFactory,
    ) -> None:
        self._client = client if client is not None else tasks_v2.CloudTasksClient()
        self._project_id = project_id
        self._region = region
        self._queue_names = dict(queue_names)
        self._handler_urls = dict(handler_urls)
        self._service_account_email = service_account_email
        self._oidc_audience = oidc_audience
        self._task_name_factory = task_name_factory

    async def enqueue_source_sync(self, task: SourceSyncTaskV1) -> TaskDispatchResult:
        payload = asdict(task)
        payload["source"] = task.source.value
        return await self._create_task(
            "source_sync",
            self._task_name_factory.source_sync(task),
            payload,
            None,
        )

    async def enqueue_reconciliation(
        self,
        task: ReconcileObservationTaskV1,
    ) -> TaskDispatchResult:
        return await self._create_task(
            "reconciliation",
            self._task_name_factory.reconciliation(task),
            asdict(task),
            None,
        )

    async def enqueue_calendar_action(
        self,
        task: ExecuteCalendarActionTaskV1,
        schedule_at: datetime | None = None,
    ) -> TaskDispatchResult:
        return await self._create_task(
            "calendar_actions",
            self._task_name_factory.calendar_action(task),
            asdict(task),
            schedule_at,
        )

    async def _create_task(
        self,
        queue_key: str,
        task_name: str,
        payload: object,
        schedule_at: datetime | None,
    ) -> TaskDispatchResult:
        queue_path = self._client.queue_path(
            self._project_id, self._region, self._queue_names[queue_key]
        )
        task_body: dict[str, Any] = {
            "name": f"{queue_path}/tasks/{task_name}",
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": self._handler_urls[queue_key],
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode("utf-8"),
                "oidc_token": {
                    "service_account_email": self._service_account_email,
                    "audience": self._oidc_audience,
                },
            },
        }
        if schedule_at is not None:
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(schedule_at)
            task_body["schedule_time"] = timestamp

        def _blocking() -> bool:
            try:
                self._client.create_task(parent=queue_path, task=task_body)
                return True
            except google_exceptions.AlreadyExists:
                return False

        created = await asyncio.to_thread(_blocking)
        return TaskDispatchResult(task_name=task_name, created=created, scheduled_for=schedule_at)
